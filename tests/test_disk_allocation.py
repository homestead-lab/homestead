"""Allocation growth preserves siblings and never turns recovery into shrink."""
import copy
import json
import sys
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import homestead_capacity_review as REVIEW
import homestead_disk_allocation as A
import homestead_operations as OPS
import homestead_storage_conflicts as CONFLICTS
from fakes.job_store import JobStore

GiB = 1024**3
PE = 4 * 1024**2


def inventory(os_size=64, lh_size=128, os_fs=None, fs='ext4'):
    vg = {'vg_name':'system-vg','vg_uuid':'test-vg-uuid','vg_extent_size':str(PE), 'vg_extent_count':str(256*GiB//PE),
          'vg_free_count':str((256-os_size-lh_size)*GiB//PE)}
    pv = {'pv_name':'/dev/nvme0n1p3','pv_uuid':'test-pv-uuid','vg_uuid':vg['vg_uuid'],
          'pv_pe_count':vg['vg_extent_count'],'pv_pe_alloc_count':str((os_size+lh_size)*GiB//PE)}
    lvs = [{'lv_name':name,'lv_uuid':uid,'lv_path':'/dev/system-vg/'+name,'lv_size':str(size*GiB),'vg_uuid':vg['vg_uuid'],
            'lv_attr':'-wi-ao----','segtype':'linear','devices':'/dev/nvme0n1p3('+str(index*16384)+')',
            'lv_kernel_major':'253','lv_kernel_minor':str(index)}
           for index,(name,uid,size) in enumerate([('root','test-root-uuid',os_size),('longhorn','test-data-uuid',lh_size)])]
    chunks = []
    for name, field, rows in [('LVS','seg',lvs),('VGS','vg',[vg]),('PVS','pv',[pv])]:
        chunks += [name,json.dumps({'report':[{field:rows}]}),'END'+name]
    for index,(role,point,size) in enumerate([('os','/',os_size if os_fs is None else os_fs),('longhorn','/var/lib/longhorn',lh_size)]):
        dm = 'LVM-' + vg['vg_uuid'].replace('-','') + lvs[index]['lv_uuid'].replace('-','')
        chunks += [f'FS {role}|{point}|/dev/dm-{index}|fs-{role}-uuid|{fs}|4096|{size*GiB//4096}|253:{index}|/dev/disk/by-id/dm-uuid-{dm}|{dm}',
                   f'USAGE {role}|4096|{size*GiB//4096}|{size*GiB//8192}|{size*GiB//8192-100}',
                   'TOPO-'+role, f'KNAME="/dev/dm-{index}" TYPE="lvm" SIZE="{lvs[index]["lv_size"]}" SERIAL="" WWN="" PARTUUID="" START="" TRAN=""',
                   'KNAME="/dev/nvme0n1p3" TYPE="part" SIZE="274877906944" SERIAL="" WWN="" PARTUUID="test-part-uuid" START="2048" TRAN=""',
                   'KNAME="/dev/nvme0n1" TYPE="disk" SIZE="300000000000" SERIAL="TEST-LOCAL-001" WWN="" PARTUUID="" START="" TRAN="nvme"', 'ENDTOPO']
    return '\n'.join(chunks + ['END']) + '\n'


class Store(JobStore):
    TERMINAL = OPS.TERMINAL
    def __init__(self): self.items=[]; self._lock=threading.RLock()
    def _read(self): return copy.deepcopy(self.items)
    def _public(self, item): return {key:value for key,value in item.items() if key != 'ref'}
    def advance_soon(self): pass
    def start(self, kind, title, resource, href, ref, message):
        CONFLICTS.require_clear(self.items, kind, ref, resource)
        item=dict(id='job-'+str(len(self.items)),kind=kind,title=title,resource=resource,href=href,ref=copy.deepcopy(ref),status='queued',message=message,progress=0)
        self.items.append(item); return self._public(item)
    def checkpoint(self, item):
        index=next(n for n,i in enumerate(self.items) if i['id']==item['id']); self.items[index]=copy.deepcopy(item)


class AllocationTests(unittest.TestCase):
    def setUp(self):
        for name in ('kget','ksend','platform','ops','NS','read_log'):
            saved=patch.object(A,name,getattr(A,name));saved.start();self.addCleanup(saved.stop)
        self.store=Store();self.text=inventory();self.jobs={};self.writes=[]
        self.host={'metadata':{'uid':'host-test-uid'},'status':{'conditions':[{'type':'Ready','status':'True'}],'nodeInfo':{'bootID':'boot-test'}}}
        A.bind(self.get,self.send,lambda force:{'harvester':False},self.store,'lab',lambda path:'synthetic log')
        mock=patch.object(A.HOST,'run',side_effect=lambda *args,**kwargs:(self.text,''));mock.start();self.addCleanup(mock.stop)
        key=patch.object(REVIEW,'_key',lambda:b'test-only-allocation-review');key.start();self.addCleanup(key.stop)
    def get(self,path):
        if path.startswith('/api/v1/nodes/'):return copy.deepcopy(self.host)
        if '/jobs/' in path:
            if path in self.jobs:return copy.deepcopy(self.jobs[path])
            raise urllib.error.HTTPError(path,404,'missing',{},None)
        raise AssertionError(path)
    def send(self,method,path,body,**kwargs):
        self.writes.append((method,path,copy.deepcopy(body)))
        obj=copy.deepcopy(body);obj['metadata']['uid']='helper-test-uid';self.jobs[path+'/'+obj['metadata']['name']]=obj
        return copy.deepcopy(obj)
    def plan(self,**kwargs):return A.review({'node':'host-1','role':'os','size_gib':80,**kwargs})
    def begin(self,plan=None):
        plan=plan or self.plan()
        return A.start({**{key:plan[key] for key in ('node','role','size_gib','request_id','capacity_token')},'confirm_capacity':True})
    def tick(self):
        item=copy.deepcopy(self.store.items[0]);status,progress,message=A.progress(item)
        item.update(status=status,progress=progress,message=message);self.store.checkpoint(item)
        return item
    def test_exact_allocation_preview_uses_free_extents_and_live_usage(self):
        plan=self.plan()
        self.assertEqual(plan['target_bytes'],80*GiB);self.assertEqual(plan['remaining_bytes'],48*GiB)
        self.assertEqual(plan['areas'][0]['minimum_bytes'],64*GiB)
        self.assertGreater(plan['areas'][0]['used_bytes'],0)
        self.assertGreater(plan['areas'][0]['reserved_bytes'],0)
        self.assertFalse(self.writes)
    def test_real_segment_report_shape_is_required_for_complete_inventory(self):
        text=inventory()
        self.assertEqual(len(A.parse(text)['volumes']),2)
        with self.assertRaisesRegex(ValueError,'incomplete'):
            A.parse(text.replace('"seg":','"lv":'))
    def test_shrink_excess_space_fractional_boolean_and_unknown_role_are_refused(self):
        for cfg in ({'size_gib':32},{'size_gib':200},{'size_gib':64.5},{'size_gib':True},{'role':'boot'}):
            with self.subTest(cfg=cfg),self.assertRaises(ValueError):self.plan(**cfg)
        self.assertFalse(self.writes)
    def test_root_and_longhorn_growth_support_ext4_and_xfs_with_absolute_targets(self):
        for role in ('os','longhorn'):
            for fs in ('ext4','xfs'):
                self.text=inventory(fs=fs);plan=self.plan(role=role,size_gib=80 if role=='os' else 144)
                self.begin(plan);script=A._script(self.store.items[-1]['ref'])
                self.assertIn('lvextend --autobackup y --extents ',script)
                self.assertIn('resize2fs /proc/self/fd/8' if fs=='ext4' else 'xfs_growfs -D ',script)
                for forbidden in ('lvreduce','lvremove','mkfs','wipefs','parted','sgdisk','--resizefs'):
                    self.assertNotIn(forbidden,script)
                self.store.items.clear()
    def test_expired_review_changed_size_boot_and_uuid_dispatch_nothing(self):
        for change in ('size','boot','uuid','expired'):
            with self.subTest(change=change):
                self.text=inventory();self.host['status']['nodeInfo']['bootID']='boot-test';plan=self.plan()
                if change=='size':self.text=inventory(os_size=68)
                if change=='boot':self.host['status']['nodeInfo']['bootID']='new-boot'
                if change=='uuid':self.text=self.text.replace('test-root-uuid','other-root-uuid')
                if change=='expired':plan['capacity_token']='0.expired'
                with self.assertRaises(ValueError):self.begin(plan)
                self.assertFalse(self.store.items);self.assertFalse(self.writes)
    def test_race_after_approval_is_refused_before_helper_and_can_be_rechecked(self):
        item=self.begin();self.text=inventory(lh_size=132)
        self.assertEqual(self.tick()['status'],'failed');self.assertFalse(self.jobs)
        outcome=A.recheck(item['id']);self.assertEqual(outcome['operation']['phase'],'cancelled')
        self.assertEqual(outcome['operation']['status'],'cancelled')
    def test_duplicate_request_returns_original_job_and_helper_is_pinned(self):
        plan=self.plan();one=self.begin(plan);two=self.begin(plan);self.assertEqual(one['id'],two['id'])
        self.tick();spec=next(iter(self.jobs.values()))['spec']
        self.assertEqual(spec['backoffLimit'],0);self.assertEqual(spec['template']['spec']['nodeName'],'host-1')
        self.assertFalse(spec['template']['spec']['automountServiceAccountToken'])
        self.assertEqual(len(self.writes),1)
    def test_partial_growth_is_rechecked_then_only_filesystem_is_finished(self):
        item=self.begin();self.tick();job=next(iter(self.jobs.values()));job['status']={'failed':1}
        self.text=inventory(os_size=80,os_fs=64);self.assertEqual(self.tick()['status'],'failed')
        plan=A.recheck(item['id']);self.assertEqual(plan['area']['fs_bytes'],64*GiB)
        body={key:plan[key] for key in ('operation_id','request_id','action','capacity_token')};body['confirm_capacity']=True
        A.finish(body);ref=self.store.items[0]['ref'];self.assertTrue(ref['filesystem_only'])
        self.assertNotIn('lvextend ',A._script(ref));self.assertIn('resize2fs ',A._script(ref))
        self.tick();self.jobs['/apis/batch/v1/namespaces/lab/jobs/'+ref['job']]['status']={'succeeded':1}
        self.text=inventory(os_size=80);self.assertEqual(self.tick()['status'],'succeeded')
    def test_missing_or_tampered_helper_never_restarts_growth(self):
        item=self.begin();self.tick();self.jobs.clear()
        self.assertEqual(self.tick()['status'],'failed');self.assertEqual(len(self.writes),1)
        with self.assertRaisesRegex(ValueError,'original helper'):A.recheck(item['id'])
    def test_a_failed_growth_rechecked_after_its_helper_expired_still_recovers(self):
        """Kubernetes removes a finished helper after 7 days (its TTL): the end
        the job saw is kept with it, so a later recheck still knows it stopped."""
        item=self.begin();self.tick();job=next(iter(self.jobs.values()));job['status']={'failed':1}
        self.text=inventory(os_size=80,os_fs=64);self.assertEqual(self.tick()['status'],'failed')
        self.jobs.clear()
        plan=A.recheck(item['id']);self.assertEqual(plan['area']['fs_bytes'],64*GiB)
        body={key:plan[key] for key in ('operation_id','request_id','action','capacity_token')};body['confirm_capacity']=True
        self.assertEqual(A.finish(body)['phase'],'queued');self.assertTrue(self.store.items[0]['ref']['filesystem_only'])
        self.assertEqual(len(self.writes),1)
    def test_an_expired_helper_that_did_nothing_closes_the_job(self):
        item=self.begin();self.tick();next(iter(self.jobs.values()))['status']={'failed':1}
        self.assertEqual(self.tick()['status'],'failed');self.jobs.clear()
        self.assertEqual(A.recheck(item['id'])['operation']['phase'],'cancelled')
        self.assertIsNone(A._pending('host-1'))
    def test_a_saved_end_never_stands_in_for_another_helper(self):
        item=self.begin();self.tick();next(iter(self.jobs.values()))['status']={'failed':1}
        self.assertEqual(self.tick()['status'],'failed');self.jobs.clear()
        self.store.items[0]['ref']['job_uid']='another-helper-uid'
        with self.assertRaisesRegex(ValueError,'original helper'):A.recheck(item['id'])
    def test_success_requires_filesystem_and_unchanged_siblings(self):
        self.begin();self.tick();next(iter(self.jobs.values()))['status']={'succeeded':1}
        self.text=inventory(os_size=80,lh_size=132)
        self.assertEqual(self.tick()['status'],'failed')
    def test_unresolved_growth_fences_other_host_storage_and_maintenance(self):
        self.begin();item=self.store.items[0];item['status']='failed'
        self.assertTrue(CONFLICTS.unresolved(item))
        for kind,ref in [('disk-v2-convert',{'node':'host-1'}),('node-power',{'node':'host-1'}),('cluster-shutdown',{}),('disk-allocation',{'node':'host-1'})]:
            with self.subTest(kind=kind),self.assertRaises(ValueError):CONFLICTS.require_clear(self.store.items,kind,ref)
        self.assertEqual(self.plan()['operation']['id'],item['id'])
    def test_unknown_thin_snapshot_network_hidden_and_multi_pv_layouts_are_refused(self):
        source=inventory()
        for text in (source.replace('linear','thin'),source.replace('-wi-ao----','owi-ao----'),source.replace('TRAN="nvme"','TRAN="iscsi"'),
                     source.replace('test-pv-uuid',''),source.replace('"pv_pe_alloc_count": "49152"','"pv_pe_alloc_count": "49151"')):
            with self.subTest(text=text[:60]),self.assertRaises(ValueError):A.parse(text)


if __name__=='__main__':unittest.main()
