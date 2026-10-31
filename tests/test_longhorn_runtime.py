"""Runtime movement cannot authorize erasing replica data or another device."""
import base64
import copy
import errno
import hashlib
import json
import os
from pathlib import Path
import socket
import shlex
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import homestead_longhorn_runtime as R
import homestead_runtime_worker as W
import homestead_storage_conflicts as C
import homestead_capacity_review as REVIEW
import test_disk_allocation as FIXTURES
import test_disk_region as REGIONS


class RuntimeContentsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'; self.source.mkdir()
        (self.source / 'engine-binaries').mkdir()
        (self.source / 'engine-binaries/engine').write_bytes(b'synthetic engine')
        (self.source / 'replicas').mkdir()
        (self.source / 'longhorn-disk.cfg').write_text('{"diskUUID":"example-disk"}')

    def test_unknown_files_and_all_nonempty_data_directories_refuse_copy(self):
        for name in (*W.EMPTY, 'unrecognized'):
            with self.subTest(name=name):
                folder = self.source / name; folder.mkdir(exist_ok=True)
                file = folder / 'data'; file.write_text('must retain')
                with self.assertRaises(ValueError): W.manifest(self.source, source=True)
                self.assertEqual(file.read_text(), 'must retain')
                file.unlink()
                if name == 'unrecognized': folder.rmdir()

    @unittest.skipUnless(os.name == 'posix', 'Linux metadata semantics')
    def test_copy_preserves_content_permissions_xattrs_but_excludes_replica_identity(self):
        binary = self.source / 'engine-binaries/engine'
        binary.chmod(0o751); os.setxattr(binary, 'user.example', b'fixture')
        expected = W.manifest(self.source, source=True)
        W.copy_runtime(self.source, self.root / 'active', expected)
        W.copy_runtime(self.source, self.root / 'archive', expected)
        self.assertEqual(W.manifest(self.root / 'active'), expected)
        self.assertFalse((self.root / 'active/longhorn-disk.cfg').exists())
        self.assertFalse((self.root / 'active/replicas').exists())
        self.assertTrue((self.source / 'longhorn-disk.cfg').exists())

    @unittest.skipUnless(os.name == 'posix', 'Linux link semantics')
    def test_symlinks_and_external_hardlinks_are_not_followed(self):
        outside = self.root / 'outside'; outside.write_text('protected')
        link = self.source / 'engine-binaries/alias'
        link.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, 'symlink'): W.manifest(self.source, source=True)
        link.unlink(); os.link(outside, link)
        with self.assertRaisesRegex(ValueError, 'Hard-linked'): W.manifest(self.source, source=True)
        self.assertEqual(outside.read_text(), 'protected')

    @unittest.skipUnless(os.name == 'posix', 'Unix sockets')
    def test_only_known_runtime_sockets_are_ephemeral(self):
        folder = self.source / 'unix-domain-socket'; folder.mkdir()
        sock = socket.socket(socket.AF_UNIX); self.addCleanup(sock.close)
        sock.bind(str(folder / 'endpoint'))
        self.assertNotIn('unix-domain-socket/endpoint', W.manifest(self.source, source=True))
        other = socket.socket(socket.AF_UNIX); self.addCleanup(other.close)
        other.bind(str(self.source / 'engine-binaries/unexpected'))
        with self.assertRaisesRegex(ValueError, 'Unexpected runtime file'): W.manifest(self.source, source=True)

    def test_missing_archive_or_changed_retained_file_cannot_arm_retirement(self):
        folder = self.root / 'operation'; folder.mkdir()
        plan = {'retained':str(self.source)}
        expected = W.manifest(self.source, source=True)
        W.durable(folder / 'plan.json', json.dumps(plan))
        W.save(folder, {'phase':'relocated', 'manifest':expected})
        with patch.object(W, 'runtime_check'), patch.object(W, 'source_check', return_value=expected):
            with self.assertRaises(ValueError): W.arm_retirement(folder)
        self.assertEqual(W.read(folder / 'state.json')['phase'], 'relocated')

    def test_fstab_rewrite_preserves_other_mounts_and_requires_exact_uuid(self):
        plan = {'request_id':'a'*24, 'retained':'/mnt/retained', 'hardware':{'uuid':'test-fs','fstype':'ext4'},
                'fstab':'# keep\nUUID=system / ext4 defaults 0 1\nUUID=test-fs /var/lib/longhorn ext4 defaults,nofail 0 2\nUUID=other /data ext4 defaults 0 2\n'}
        changed = W.fstab_lines(plan)
        self.assertIn('UUID=system / ext4 defaults 0 1', changed)
        self.assertIn('UUID=other /data ext4 defaults 0 2', changed)
        self.assertIn('UUID=test-fs /mnt/retained ext4 defaults,ro,nofail 0 0', changed)
        self.assertIn('/active /var/lib/longhorn none bind 0 0', changed)
        for bad in (plan['fstab'].replace('UUID=test-fs','UUID=wrong'), plan['fstab']*2, plan['fstab'].replace('defaults,nofail','x-systemd.automount')):
            with self.assertRaises(ValueError): W.fstab_lines({**plan,'fstab':bad})

    @unittest.skipUnless(os.name == 'posix', 'Linux metadata')
    def test_atomic_fstab_rewrite_preserves_original_permissions_and_extended_attributes(self):
        file=self.root/'fstab';file.write_text('original');file.chmod(0o644)
        os.setxattr(file,'user.fixture',b'keep')
        before=W.metadata(file)
        W.durable(file,'replacement')
        self.assertEqual(W.metadata(file),before)
        self.assertEqual(file.read_text(),'replacement')

    @unittest.skipUnless(os.name == 'posix', 'Linux ownership')
    def test_non_root_managed_parent_is_rejected(self):
        parent=self.root/'writable';parent.mkdir();parent.chmod(0o777)
        with self.assertRaisesRegex(ValueError,'exclusively root-managed'):W.administrative(parent/'plan')


class RuntimeAttributePolicyTests(unittest.TestCase):
    """#973: extended attributes are kept by namespace, and only what differs
    is written. On an SELinux host every file carries security.selinux and an
    unprivileged or confined writer cannot set it, even to the same value.
    The kernel is simulated here, so these run on hosts without SELinux too."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.file = Path(self.temp.name) / 'f'; self.file.write_text('x')
        self.attrs = {}      # what the simulated file carries
        self.written = []
        self.refuse = set()  # names the simulated kernel refuses to set
        def getxattr(path, name):
            if name not in self.attrs: raise OSError(errno.ENODATA, 'No data available')
            return self.attrs[name]
        def setxattr(path, name, value):
            if name in self.refuse: raise OSError(errno.EOPNOTSUPP, 'Operation not supported')
            self.written.append(name); self.attrs[name] = value
        self.enterContext(mock.patch.object(W.os, 'getxattr', getxattr))
        self.enterContext(mock.patch.object(W.os, 'setxattr', setxattr))
        self.enterContext(mock.patch.object(W.os, 'listxattr', lambda path: sorted(self.attrs)))

    @staticmethod
    def wanted(**values):
        return {name.replace('__', '.'): base64.b64encode(value).decode() for name, value in values.items()}

    def test_an_equal_label_is_never_written(self):
        self.attrs['security.selinux'] = b'system_u:object_r:var_lib_t:s0'
        self.refuse.add('security.selinux')
        W.keep_attrs(self.file, self.wanted(security__selinux=b'system_u:object_r:var_lib_t:s0'))
        self.assertEqual([], self.written)

    def test_a_refused_different_label_stops_naming_the_file_and_both_labels(self):
        self.attrs['security.selinux'] = b'system_u:object_r:tmp_t:s0'
        self.refuse.add('security.selinux')
        with self.assertRaises(ValueError) as caught:
            W.keep_attrs(self.file, self.wanted(security__selinux=b'system_u:object_r:var_lib_t:s0'))
        message = str(caught.exception)
        for part in (str(self.file), 'tmp_t', 'var_lib_t', 'context='):
            self.assertIn(part, message)

    def test_a_different_label_the_kernel_allows_is_kept(self):
        self.attrs['security.selinux'] = b'system_u:object_r:tmp_t:s0'
        W.keep_attrs(self.file, self.wanted(security__selinux=b'system_u:object_r:var_lib_t:s0'))
        self.assertEqual(b'system_u:object_r:var_lib_t:s0', self.attrs['security.selinux'])

    def test_file_capabilities_are_kept_exactly(self):
        W.keep_attrs(self.file, self.wanted(security__capability=b'\x01\x00\x00\x02'))
        self.assertEqual(['security.capability'], self.written)

    def test_a_refused_user_attribute_still_stops(self):
        self.refuse.add('user.example')
        with self.assertRaises(OSError):
            W.keep_attrs(self.file, self.wanted(user__example=b'fixture'))

    def test_kernel_managed_integrity_attributes_are_neither_read_nor_written(self):
        self.attrs.update({'security.ima': b'hash', 'security.evm': b'mac', 'user.example': b'kept'})
        self.assertEqual(['user.example'], sorted(W.read_attrs(self.file)))
        W.keep_attrs(self.file, self.wanted(security__ima=b'other', security__evm=b'other'))
        self.assertEqual([], self.written)

    def test_owner_and_mode_are_set_before_the_attributes(self):
        """chown clears file capabilities: apply_metadata must set them after it."""
        self.enterContext(mock.patch.object(W.os, 'chown', lambda path, uid, gid: self.attrs.pop('security.capability', None)))
        self.enterContext(mock.patch.object(W.os, 'chmod', lambda path, mode: None))
        W.apply_metadata(self.file, {'uid': 0, 'gid': 0, 'mode': 0o755,
                                     'attrs': self.wanted(security__capability=b'cap')})
        self.assertEqual(b'cap', self.attrs.get('security.capability'))

    def test_the_manifest_uses_the_same_rules(self):
        root = Path(self.temp.name) / 'runtime'; (root / 'engine-binaries').mkdir(parents=True)
        self.attrs.update({'security.ima': b'hash', 'user.example': b'kept'})
        entry = W.manifest(root)['engine-binaries']
        self.assertEqual(['user.example'], sorted(entry['attrs']))


@unittest.skipUnless(os.name == 'posix', 'Linux process inventory')
class RuntimeProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.proc=self.root/'99999999';self.proc.mkdir()
        fields=['0']*20;fields[0]='S';fields[19]='12345'
        (self.proc/'stat').write_text('99999999 (fixture process) '+' '.join(fields))
        (self.proc/'comm').write_text('fixture-process')
        (self.proc/'maps').write_text('')
        (self.proc/'mountinfo').write_text('1 0 0:1 / / rw - ext4 test rw\n')
        (self.proc/'ns').mkdir();(self.proc/'ns/mnt').write_text('')
        (self.proc/'fd').mkdir();(self.proc/'cwd').symlink_to(self.root);(self.proc/'root').symlink_to(self.root)
        self.device=os.makedev(252,1);self.namespace=(self.proc/'ns/mnt').stat().st_ino
    def check(self):W.process_use(self.proc,self.device,self.namespace,'252:1',{'k3s'})
    def test_closed_or_exited_process_is_absent_but_unreadable_existing_process_is_not(self):
        self.check()
        original=Path.read_text
        def denied(path,*args,**kwargs):
            if path==self.proc/'maps':raise PermissionError('fixture denial')
            return original(path,*args,**kwargs)
        with patch.object(Path,'read_text',denied),self.assertRaises(PermissionError):self.check()
        W.process_use(self.root/'12345678',self.device,self.namespace,'252:1',set())
    def test_process_mapping_private_mount_and_runtime_process_block(self):
        (self.proc/'maps').write_text('1000-2000 r--p 00000000 fc:01 12 /retained/file\n')
        with self.assertRaisesRegex(ValueError,'maps a file'):self.check()
        (self.proc/'maps').write_text('');(self.proc/'mountinfo').write_text('1 0 252:1 / /private rw - ext4 test rw\n')
        with self.assertRaisesRegex(ValueError,'private mount'):W.process_use(self.proc,self.device,-1,'252:1',set())
        (self.proc/'comm').write_text('k3s')
        with self.assertRaisesRegex(ValueError,'still running'):self.check()
    def test_open_descriptor_cwd_and_root_each_block_retirement(self):
        (self.proc/'fd/7').symlink_to(self.root/'outside')
        original=Path.stat
        for suffix in ('cwd','root','fd/7'):
            def inspected(path,*args,**kwargs):
                if path==self.proc/suffix:return SimpleNamespace(st_dev=self.device,st_mode=0o100644,st_rdev=0)
                return original(path,*args,**kwargs)
            with self.subTest(suffix=suffix),patch.object(Path,'stat',inspected),self.assertRaises(ValueError):self.check()
    def test_reused_pid_does_not_pass_as_previous_process(self):
        original=Path.read_text;count=0
        def reused(path,*args,**kwargs):
            nonlocal count
            value=original(path,*args,**kwargs)
            if path==self.proc/'stat':
                count+=1
                if count>1:return value.replace('12345','12346')
            return value
        with patch.object(Path,'read_text',reused),self.assertRaisesRegex(ValueError,'identity changed'):self.check()


class RuntimeStagingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.base=self.root/'maintenance';self.systemd=self.root/'systemd';self.systemd.mkdir()
        self.fstab=self.root/'fstab';self.fstab.write_text('original');self.source='synthetic worker source'
        self.plan={'request_id':'b'*24,'root':{},'fstab':'original','fstab_metadata':W.metadata(self.fstab),'service':'k3s',
                   'retained':str(self.root/'retained'),'worker_sha256':hashlib.sha256(self.source.encode()).hexdigest()}
        for name,value in [('BASE',self.base),('SYSTEMD',self.systemd),('FSTAB',self.fstab)]:
            p=patch.object(W,name,value);p.start();self.addCleanup(p.stop)
    def command(self,*args):
        if args[0]=='lsattr':return '----i-------- '+self.plan['retained']
        if args[:2]==('systemctl','show'):return 'homestead-longhorn-runtime.service'
        return ''
    def test_each_staging_interruption_cannot_arm_without_complete_startup_guard(self):
        for boundary in ('plan.json','worker.py','state.json','homestead-longhorn-runtime.service','homestead-runtime.conf'):
            with self.subTest(boundary=boundary),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);(root/'systemd').mkdir();plan={**self.plan,'retained':str(root/'retained')}
                original=W.durable
                def interrupted(path,content):
                    original(path,content)
                    if path.name==boundary:raise OSError('power interrupted staging')
                with patch.multiple(W,BASE=root/'maintenance',SYSTEMD=root/'systemd'),patch.object(W,'inspect',return_value=plan),patch.object(W,'run',side_effect=self.command),patch.object(W,'durable',side_effect=interrupted):
                    with self.assertRaises(OSError):W.stage(plan,self.source)
                self.assertEqual(self.fstab.read_text(),'original')
                if boundary!='homestead-runtime.conf':
                    with patch.multiple(W,BASE=root/'maintenance',SYSTEMD=root/'systemd'),patch.object(W,'administrative',side_effect=W.plain),patch.object(W,'run',side_effect=self.command),patch.object(W,'source_check'):
                        with self.assertRaises((ValueError,FileNotFoundError)):W.verify_staging(plan)
                with patch.multiple(W,BASE=root/'maintenance',SYSTEMD=root/'systemd'),patch.object(W,'administrative',side_effect=W.plain), \
                        patch.object(W,'run',side_effect=self.command),patch.object(W,'source_check'),patch.object(W,'root_identity',return_value=plan['root']),patch.object(W,'mount',return_value='rw'):
                    self.assertEqual(W.restore_staging(plan,self.source)['phase'],'unarmed')
                    W.verify_staging(plan)
    def test_missing_effective_dependency_or_modified_worker_prevents_arming(self):
        with patch.object(W,'inspect',return_value=self.plan),patch.object(W,'run',side_effect=self.command):W.stage(self.plan,self.source)
        with patch.object(W,'administrative',side_effect=W.plain),patch.object(W,'run',side_effect=self.command),patch.object(W,'source_check'):
            W.verify_staging(self.plan)
            def missing_dependency(*args):
                return '' if args[:2]==('systemctl','show') else self.command(*args)
            with patch.object(W,'run',side_effect=missing_dependency),self.assertRaisesRegex(ValueError,'effective Kubernetes'):
                W.verify_staging(self.plan)
            (self.base/self.plan['request_id']/'worker.py').write_text('changed')
            with self.assertRaisesRegex(ValueError,'worker changed'):W.verify_staging(self.plan)

    def test_local_recovery_only_releases_exact_guard_before_copy_and_can_be_rearmed(self):
        with patch.object(W,'inspect',return_value=self.plan),patch.object(W,'run',side_effect=self.command):W.stage(self.plan,self.source)
        folder=self.base/self.plan['request_id'];guard=self.systemd/'k3s.service.d/homestead-runtime.conf'
        def stopped(*args):
            if args[:2]==('systemctl','show'):return 'inactive' if 'ActiveState' in args else ''
            return self.command(*args)
        with patch.object(W,'administrative',side_effect=W.plain),patch.object(W,'run',side_effect=stopped),patch.object(W,'source_check'), \
                patch.object(W,'root_identity',return_value=self.plan['root']),patch.object(W,'mount',return_value='rw'):
            for phase in ('copying','cutover','relocated','retire-staged','retiring','retired'):
                W.save(folder,{'phase':phase})
                with self.subTest(phase=phase),self.assertRaisesRegex(ValueError,'unarmed'):W.recover_staging(folder)
                self.assertTrue(guard.exists())
            W.save(folder,{'phase':'unarmed'})
            guard.write_text('changed')
            with self.assertRaisesRegex(ValueError,'dependency changed'):W.recover_staging(folder)
            guard.write_text(W.GUARD)
            self.assertEqual(W.recover_staging(folder)['phase'],'unarmed');self.assertFalse(guard.exists())
            self.assertEqual(self.fstab.read_text(),'original')
            with patch.object(W,'run',side_effect=self.command):W.restore_staging(self.plan,self.source)
            self.assertEqual(guard.read_text(),W.GUARD)


@unittest.skipUnless(os.name == 'posix', 'Executable host shell fixture')
class RuntimeAllocationTests(unittest.TestCase):
    def selection(self,points,root):
        script=R.ALLOCATION.INSPECT.split('fs_facts os /\n',1)[1].split('echo END',1)[0]
        prefix='set -eu\nfail(){ echo "$*" >&2;exit 1; }\nfs_facts(){ echo "$*"; }\nfindmnt(){ if [ "$*" = "-rn -o TARGET" ]; then printf "%s\\n" '+shlex.quote(points)+';else printf "%s\\n" '+shlex.quote(root)+';fi; }\n'
        return subprocess.run(['sh','-c',prefix+script],text=True,capture_output=True)
    def test_runtime_bind_does_not_break_os_growth_or_become_another_growable_lv(self):
        result=self.selection('/\n/var/lib/longhorn','/var/lib/homestead/longhorn-runtime/task/active')
        self.assertEqual(result.returncode,0,result.stderr);self.assertEqual(result.stdout,'')
        result=self.selection('/\n/mnt/longhorn-os\n/var/lib/longhorn','/')
        self.assertEqual(result.stdout,'longhorn /mnt/longhorn-os\n')


class RuntimeReviewTests(unittest.TestCase):
    def setUp(self):
        self.store = FIXTURES.Store()
        p = patch.object(R, 'ops', self.store); p.start(); self.addCleanup(p.stop)
        p = patch.object(REVIEW, '_key', lambda:b'runtime-fixture-key'); p.start(); self.addCleanup(p.stop)
        self.plan = {'node':'host-1','disk':'store-1','request_id':'a'*24,'node_uid':'host-uid','lh_uid':'lh-uid','boot_id':'boot-1',
            'hardware':{'device':'/dev/dm-1','by_id':'/dev/disk/by-id/dm-uuid-LVM-fixture','size_bytes':128*1024**3},
            'runtime_bytes':1024,'available_bytes':10*1024**3,'retained':'/mnt/homestead-retired-'+'a'*24,
            'original_disk':{'path':'/var/lib/longhorn','allowScheduling':True},'disk_uuid':'disk-uuid'}
        p=patch.object(R,'_plan',side_effect=lambda n,d,r:{**copy.deepcopy(self.plan),'request_id':r});p.start();self.addCleanup(p.stop)

    def test_expired_or_changed_review_dispatches_nothing(self):
        plan = R.review({'node':'host-1','disk':'store-1'})
        self.plan['hardware']['size_bytes'] += 1024
        with self.assertRaisesRegex(ValueError,'changed'):
            R.start({**plan,'confirm_capacity':True})
        self.assertFalse(self.store.items)

    def test_start_requires_review_confirmation_and_replays_same_job(self):
        plan = R.review({'node':'host-1','disk':'store-1'})
        with self.assertRaises(ValueError): R.start(plan)
        one = R.start({**plan,'confirm_capacity':True})
        two = R.start({**plan,'confirm_capacity':True})
        self.assertEqual(one['id'],two['id']); self.assertEqual(len(self.store.items),1)

    def test_lost_staging_is_never_automatically_repeated(self):
        item={'ref':{**self.plan,'phase':'staging'},'progress':0}
        with patch.object(R,'_call') as call:
            self.assertEqual(R.progress(item)[0],'failed')
            call.assert_not_called()

    def test_retirement_requires_typed_exact_device_and_fresh_manifest(self):
        plan=R.review({'node':'host-1','disk':'store-1'}); op=R.start({**plan,'confirm_capacity':True})
        with patch.object(R,'_recheck'), patch.object(R,'_call') as call:
            with self.assertRaises(ValueError): R.retire({'id':op['id'],'action':'retire-runtime','confirm_capacity':True,'confirm_device':'/dev/dm-0'})
            call.assert_not_called()

    def test_failed_runtime_keeps_fence_but_reviewed_reboot_is_allowed(self):
        item={'kind':R.KIND,'status':'failed','ref':{'node':'host-1','phase':'staged'}}
        for kind in ('disk-allocation','disk-v2-convert','host-os','disk-retire'):
            with self.subTest(kind=kind),self.assertRaises(ValueError): C.require_clear([item],kind,{'node':'host-1'})
        C.require_clear([item],'node-power',{'node':'host-1','action':'reboot'})
        with self.assertRaises(ValueError): C.require_clear([item],'node-power',{'node':'host-1','action':'shutdown'})
        item['ref']['phase']='retired'
        C.require_clear([item],'disk-v2-convert',{'node':'host-1'})

    def test_runtime_maintenance_is_serialized_across_cluster_members(self):
        self.store.items=[{'id':'other','kind':R.KIND,'status':'running','ref':{'node':'host-2','phase':'relocated'}}]
        with self.assertRaisesRegex(ValueError,'another host'):R.review({'node':'host-1','disk':'store-1'})

    def test_generated_python_runs_and_uses_host_change_lease(self):
        def execute(node,script,**kwargs):
            self.assertEqual(kwargs.get('change'),'Longhorn runtime maintenance')
            if os.name != 'posix': return 'RESULT true\n',''
            result=subprocess.run(['sh','-c',script],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            return result.stdout,result.stderr
        with patch.object(R.HOST,'run',side_effect=execute):
            self.assertTrue(R._call('host-1','bool(RUNTIME)',write=True))


class RuntimeInventoryTests(unittest.TestCase):
    def setUp(self):
        self.host={'metadata':{'uid':'host-uid','resourceVersion':'1'},'status':{'conditions':[{'type':'Ready','status':'True'}]}}
        self.lh={'metadata':{'uid':'lh-uid','resourceVersion':'2'},'spec':{'disks':{'store':{'path':R.SOURCE}}},
                 'status':{'conditions':[{'type':'Ready','status':'True'}],'diskStatus':{'store':{'diskUUID':'disk-uuid'}}}}
        self.rows={}; self.writes=[]
        def read(path):
            if path=='/api/v1/nodes/host-1':return copy.deepcopy(self.host)
            if path==R.V2.LH+'/nodes/host-1':return copy.deepcopy(self.lh)
            return self.rows.get(path,{'items':[]})
        for name,value in [('kget',read),('ksend',lambda *a,**k:self.writes.append(a)),('platform',lambda force:{'harvester':False}),('_upgrade_idle',lambda:None)]:
            p=patch.object(R.V2,name,value);p.start();self.addCleanup(p.stop)

    def test_runtime_store_accepts_trailing_slash_without_changing_registration(self):
        for path in (R.SOURCE, R.SOURCE + '/', R.SOURCE + '//'):
            self.lh['spec']['disks']['store']['path'] = path
            self.assertEqual(R._inventory('host-1', 'store')['spec']['path'], path)
            self.rows = {R.V2.LH + '/replicas': {'items': [{'spec': {'nodeID': 'host-1', 'diskID': 'disk-uuid'}}]}}
            with self.assertRaisesRegex(ValueError, 'every replica'):
                R._inventory('host-1', 'store')
            self.rows = {}
        self.assertFalse(self.writes)

    def test_runtime_store_rejects_subdirectories_other_mounts_and_parent_aliases(self):
        for path in ('/', '/mnt/data', R.SOURCE + '/replicas', R.SOURCE + '/.',
                     '/var/lib/other/../longhorn', None):
            self.lh['spec']['disks']['store']['path'] = path
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, 'dedicated V1 store'):
                R._inventory('host-1', 'store')
        self.assertFalse(self.writes)

    def test_empty_api_objects_are_not_enough_if_any_claim_or_paginated_inventory_remains(self):
        for suffix,obj in [('replicas',{'items':[{'spec':{'nodeID':'host-1','diskID':'disk-uuid'}}]}),
                           ('replicas',{'items':[],'metadata':{'continue':'next'}}),
                           ('backingimages',{'items':[{'metadata':{'name':'image'},'spec':{'diskFileSpecMap':{'disk-uuid':{}}}}]}),
                           ('orphans',{'items':[{'metadata':{'name':'orphan'},'spec':{'nodeID':'host-1','parameters':{}}}]}),
                           ('volumes',{'items':[{'spec':{'migrationNodeID':'host-2'}}]})]:
            self.rows={R.V2.LH+'/'+suffix:obj}
            with self.subTest(suffix=suffix),self.assertRaises(ValueError):R._inventory('host-1','store')
        self.assertFalse(self.writes)

    def test_default_disk_recreation_label_and_changed_node_identity_block(self):
        self.host['metadata']['labels']={'node.longhorn.io/create-default-disk':'true'}
        with self.assertRaisesRegex(ValueError,'disk-creation'): R._inventory('host-1','store')
        del self.host['metadata']['labels']
        with self.assertRaisesRegex(ValueError,'identity changed'):
            R._inventory('host-1','store',ref={'node_uid':'wrong','lh_uid':'lh-uid','disk_uuid':'disk-uuid'})

    def test_rebound_empty_placeholder_retains_uuid_while_not_ready(self):
        # Longhorn's disk monitor preserves the recorded UUID on config-write
        # failure; the node controller updates Ready, not the existing UUID.
        # https://github.com/longhorn/longhorn-manager/blob/v1.13.0/controller/monitor/disk_monitor.go
        self.lh['spec']['disks']['store']={'path':'/mnt/homestead-retired-fixture','allowScheduling':False}
        status=self.lh['status']['diskStatus']['store']
        status.update(conditions=[{'type':'Ready','status':'False','reason':'NoDiskInfo'}],scheduledReplica={})
        ref={'node_uid':'host-uid','lh_uid':'lh-uid','disk_uuid':'disk-uuid'}
        self.assertEqual(R._inventory('host-1','store',ref=ref)['status']['diskUUID'],'disk-uuid')
        status['diskUUID']=''
        with self.assertRaises(ValueError):R._inventory('host-1','store',ref=ref)

    def test_failed_registration_compare_or_late_inventory_change_never_arms_boot(self):
        ref={'node':'host-1','disk':'store','request_id':'a'*24,'original_disk':{'path':R.SOURCE},
             'retained':'/mnt/homestead-retired-fixture','hardware':{}}
        original={'spec':ref['original_disk']}
        retained={'spec':R._disabled(ref,True)}
        for fault in ('patch','inventory','changed-spec'):
            reads=[original,ValueError('Replica appeared')] if fault=='inventory' else [original,original if fault=='changed-spec' else retained]
            with self.subTest(fault=fault),patch.object(R,'_recheck',side_effect=reads), \
                    patch.object(R.V2,'_patch_disk',side_effect=ValueError('Resource version changed') if fault=='patch' else None), \
                    patch.object(R,'_host') as host,self.assertRaises(ValueError):R._arm(ref)
            host.assert_not_called()


class RuntimeHardwareTests(unittest.TestCase):
    def test_only_the_verified_installer_lv_is_accepted(self):
        with patch.object(R.HOST,'run',return_value=(REGIONS.region(),'')),patch.object(R.ALLOCATION,'inspect',return_value={
                'areas':[{'role':'longhorn','stable_device':'/dev/disk/by-id/dm-uuid-LVM-test-region'}]}):
            self.assertEqual(R._hardware('host-1')['kind'],'lvm')
        with patch.object(R.HOST,'run',return_value=(REGIONS.region('part'),'')),patch.object(R.ALLOCATION,'inspect') as inspect:
            with self.assertRaisesRegex(ValueError,'dedicated installer logical volume'):R._hardware('host-1')
            inspect.assert_not_called()
        with patch.object(R.HOST,'run',return_value=(REGIONS.region(),'')):
            with patch.object(R.ALLOCATION,'inspect',side_effect=ValueError('Multiple physical volumes need manual allocation')):
                with self.assertRaisesRegex(ValueError,'Multiple physical volumes'):R._hardware('host-1')
            with patch.object(R.ALLOCATION,'inspect',return_value={'areas':[{'role':'longhorn','stable_device':'/dev/disk/by-id/other'}]}):
                with self.assertRaisesRegex(ValueError,'single-PV'):R._hardware('host-1')

    def test_identity_changes_stop_before_any_mutating_command(self):
        plan={'root':{'uuid':'os-fixture','filesystem':'ext4'},'hardware':{'by_id':'/dev/disk/by-id/example','uuid':'data-fixture','size_bytes':128},'identity_script':'verify_fixture'}
        device=SimpleNamespace(st_mode=0o060000,st_rdev=os.makedev(252,1) if os.name=='posix' else 1)
        def command(*args):
            if args[0]=='blkid':return 'data-fixture'
            if args[0]=='blockdev':return '128'
            return ''
        with patch.object(W.os.path,'realpath',return_value='/dev/dm-1'),patch.object(W.os,'stat',return_value=device),patch.object(W,'root_identity',return_value=plan['root']):
            with patch.object(W,'run',side_effect=command):self.assertEqual(W.identity(plan),'/dev/dm-1')
            for fault in ('root','uuid','size','topology','pinned'):
                def bad_command(*args):
                    if fault=='uuid' and args[0]=='blkid':return 'other-filesystem'
                    if fault=='size' and args[0]=='blockdev':return '256'
                    if fault=='topology' and args[0]=='sh':raise ValueError('OS sibling changed')
                    return command(*args)
                with self.subTest(fault=fault),patch.object(W,'run',side_effect=bad_command) as commands, \
                        patch.object(W,'root_identity',return_value={} if fault=='root' else plan['root']), \
                        patch.object(W,'PINNED',8 if fault=='pinned' else None), \
                        patch.object(W.os,'fstat',return_value=SimpleNamespace(st_rdev=999)),self.assertRaises(ValueError):W.identity(plan)
                self.assertTrue(all(c.args[0] in ('blkid','blockdev','sh') for c in commands.call_args_list))


@unittest.skipUnless(os.name == 'posix' and getattr(os, 'geteuid', lambda: -1)() == 0, 'Requires disposable Linux loop mounts')
class RuntimeBootRehearsalTests(unittest.TestCase):
    def test_real_copy_cutover_retirement_and_interrupted_boot_recovery(self):
        try:
            probe=subprocess.run(['unshare','--mount','--propagation','private','true'],capture_output=True)
        except FileNotFoundError:
            self.skipTest('Private mount namespaces unavailable')
        if probe.returncode:self.skipTest('Private mount namespace permission unavailable')
        for point in ('','copying','cutover','relocated','retiring','unlink','changed-source','changed-archive','mid-copy',
                      'fstab-written','unmounted','retained-mounted','runtime-bound','copy-capacity'):
            with self.subTest(point=point):
                result=subprocess.run(['unshare','--mount','--propagation','private',sys.executable,
                    str(Path(__file__).with_name('runtime_move_rehearsal.py')),point],text=True,capture_output=True,timeout=120)
                self.assertEqual(result.returncode,0,result.stdout+'\n'+result.stderr)


if __name__ == '__main__': unittest.main()
