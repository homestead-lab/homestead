"""Only the reviewed, dedicated filesystem region may become a V2 disk."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from test_disk_v2 import Cluster, HOST_FACTS
import test_disk_v2 as FIXTURES
import homestead_disk_v2 as V
import homestead_disk_region as R
import homestead_kube as K


def region(kind='lvm'):
    device = '/dev/dm-1' if kind == 'lvm' else '/dev/nvme0n1p4'
    stable = '/dev/disk/by-id/dm-uuid-LVM-test-region' if kind == 'lvm' else '/dev/disk/by-partuuid/test-region'
    layout = {'blockdevices': [{'kname':'/dev/nvme0n1','type':'disk','size':2000*1024**3,'children':[
        {'kname':'/dev/nvme0n1p1','type':'part','size':1024**3,'start':2048,'mountpoint':'/boot'},
        {'kname':'/dev/nvme0n1p3','type':'part','size':1998*1024**3,'start':2099200,'children':[
            {'kname':'/dev/dm-0','type':'lvm','size':100*1024**3,'mountpoint':'/'},
            {'kname':device,'type':kind,'size':1000*1024**3,'mountpoint':'/var/lib/longhorn'}]}]}]}
    if kind == 'part':
        parent = layout['blockdevices'][0]
        selected = parent['children'][1]['children'].pop()
        parent['children'][1]['size'] = 998 * 1024**3
        selected['start'] = 1000 * 1024**3 // 512
        parent['children'].append(selected)
    return (HOST_FACTS.replace('DEVICE /dev/sdb', 'DEVICE ' + device)
            .replace('KIND disk', 'KIND ' + kind).replace('END', '') +
            '\nREGION ' + 'a' * 64 + '\nREGION_PATH ' + stable +
            '\nPARENTS /dev/nvme0n1p3 /dev/nvme0n1\nLAYOUT '+json.dumps(layout)+'\nEND')


class RegionConversionTests(unittest.TestCase):
    def setUp(self):
        for name in ('kget', 'ksend', 'platform', 'ops', 'NS', 'read_log'):
            saved = patch.object(V, name, getattr(V, name)); saved.start(); self.addCleanup(saved.stop)
        self.cluster = Cluster(self)
        self.host = patch.object(V.HOST, 'run', side_effect=self.cluster.host)
        self.host_mock = self.host.start(); self.addCleanup(self.host.stop)

    def test_partition_and_lv_reviews_identify_exact_region_and_protect_parent(self):
        for kind in ('part', 'lvm'):
            self.cluster.facts = region(kind)
            plan = self.cluster.review()
            self.assertEqual(plan['region_kind'], kind)
            self.assertIn('/dev/nvme0n1', plan['protected_parents'])
        self.assertEqual(self.cluster.writes, [])

    def test_separate_default_longhorn_mount_is_allowed_but_system_mount_is_not(self):
        self.cluster.facts = region()
        facts = V._host_facts('/var/lib/longhorn/', 'node-1')
        self.assertEqual(facts['mountpoint'], '/var/lib/longhorn')
        for path in ('/', '/var', '/boot', '/var/lib/rancher', '/var/lib/longhorn/other'):
            with self.assertRaises(ValueError): V._host_facts(path, 'node-1')

    def test_geometry_change_invalidates_evacuate_and_erase_reviews(self):
        self.cluster.facts = region()
        plan = self.cluster.review()
        self.cluster.facts = region().replace('a' * 64, 'b' * 64)
        with self.assertRaises(ValueError): self.cluster.start(plan)
        self.cluster.facts = region()
        self.cluster.awaiting()
        self.cluster.facts = region().replace('a' * 64, 'b' * 64)
        with self.assertRaises(ValueError): V.prepare_review(self.cluster.ops.items[0]['id'])

    def test_region_uses_aio_and_rechecks_identity_at_each_destructive_boundary(self):
        self.cluster.facts = region()
        self.cluster.awaiting(); self.cluster.prepare()
        ref = self.cluster.ops.items[0]['ref']
        script = V._host_script(ref)
        self.assertEqual(V._new_disk(ref)['diskDriver'], 'aio')
        self.assertEqual(script.count('Region identity or geometry changed'), 1)
        self.assertEqual(script.count('\nidentity_check\n'), 2)
        self.assertLess(script.rindex('Region identity or geometry changed'), script.index('wipefs -a'))
        for forbidden in ('lvremove ', 'vgremove ', 'pvremove ', 'parted ', 'sgdisk ', 'wipefs -a "$PD"'):
            self.assertNotIn(forbidden, script)
        V._verify_blank(ref)
        self.assertIn('Region identity or geometry changed', self.host_mock.mock_calls[-1].args[1])

    def test_parent_cannot_be_mutated_while_saved_region_conversion_exists(self):
        self.cluster.facts = region(); self.cluster.start()
        with self.assertRaises(ValueError): V.mutation_guard('node-1', device='/dev/nvme0n1')
        V.mutation_guard('node-1', device='/dev/sdc')

    def test_missing_or_inconsistent_region_identity_is_refused(self):
        base = {'KIND': 'lvm', 'REGION': 'a' * 64, 'REGION_PATH': '/dev/disk/by-id/dm-uuid-LVM-test', 'PARENTS': '/dev/nvme0n1'}
        for key, value in [('REGION', ''), ('REGION_PATH', '/dev/nvme0n1'), ('PARENTS', ''), ('PARENTS', '/dev/../root'), ('KIND', 'crypt')]:
            with self.subTest(key=key, value=value), self.assertRaises(ValueError): R.facts({**base, key: value})

    def test_partition_overlap_missing_offset_out_of_bounds_and_nested_partition_are_refused(self):
        lines = dict(line.split(' ', 1) for line in region().splitlines() if ' ' in line)
        layout = json.loads(lines['LAYOUT'])
        for change in ('overlap', 'missing', 'beyond', 'nested'):
            with self.subTest(change=change):
                copy_layout = json.loads(json.dumps(layout))
                parts = copy_layout['blockdevices'][0]['children']
                if change == 'overlap': parts[1]['start'] = 2048
                if change == 'missing': del parts[1]['start']
                if change == 'beyond': parts[1]['size'] = 3000 * 1024**3
                if change == 'nested': parts[1]['children'][0]['type'] = 'part'
                with self.assertRaisesRegex(ValueError, 'layout'):
                    R.facts({**lines, 'LAYOUT': json.dumps(copy_layout)})

    def test_partial_cluster_inventory_never_dispatches_an_erase(self):
        for collection in ('replicas', 'volumes', 'engines', 'backingimages', 'orphans'):
            for metadata in ({'continue':'next-page'}, {'remainingItemCount':1}):
                with self.subTest(collection=collection, metadata=metadata):
                    self.cluster = Cluster(self); self.cluster.facts = region()
                    self.host_mock.side_effect = self.cluster.host
                    self.cluster.awaiting(); self.cluster.prepare()
                    original = K.kget
                    def partial(path):
                        value = original(path)
                        if path.endswith('/' + collection): value['metadata'] = metadata
                        return value
                    with patch.object(V, 'kget', side_effect=partial): self.cluster.tick()
                    self.assertFalse(self.cluster.jobs)
                    self.assertFalse(any(write[0] == 'DELETE' for write in self.cluster.writes))

    def test_another_registration_on_the_filesystem_blocks_even_without_replicas(self):
        self.cluster.facts = region()
        self.cluster.store_devices['spare'] = '/dev/dm-1'
        with self.assertRaisesRegex(ValueError, 'Other V1 stores.*spare'): self.cluster.review()
        self.assertFalse(self.cluster.writes)

    def test_late_store_or_orphan_or_backing_image_stops_dispatch_after_approval(self):
        for kind in ('store', 'orphan', 'backing-image'):
            with self.subTest(kind=kind):
                self.cluster = Cluster(self); self.cluster.facts = region()
                self.host_mock.side_effect = self.cluster.host
                self.cluster.awaiting(); self.cluster.prepare()
                if kind == 'store': self.cluster.store_devices['spare'] = '/dev/dm-1'
                if kind == 'orphan': self.cluster.kube.add(FIXTURES.lh('Orphan', 'orphan-data', spec={'nodeID':'node-1','parameters': {'DiskUUID':'disk-source-uuid'}}))
                if kind == 'backing-image': self.cluster.kube.add(FIXTURES.lh('BackingImage', 'image-data', spec={'diskFileSpecMap': {'disk-source-uuid':{}}}))
                self.cluster.tick()
                self.assertFalse(self.cluster.jobs)

    def test_disappeared_volume_is_not_assumed_deliberately_deleted(self):
        self.cluster.facts = region(); self.cluster.awaiting()
        for volume in K.kget(V.LH + '/volumes')['items']: self.cluster.kube.discard(FIXTURES.VOL(volume['metadata']['name']))
        with self.assertRaisesRegex(ValueError, 'disappeared'): self.cluster.prepare()
        self.assertFalse(self.cluster.jobs)

    def test_offline_replacement_host_cannot_be_counted_from_stale_rw_status(self):
        self.cluster.facts = region(); self.cluster.awaiting(); self.cluster.prepare()
        self.cluster.tick()  # remove the old V1 registration
        self.cluster.put(FIXTURES.NODE('node-2'), ('status', 'conditions'), [])
        self.cluster.tick()
        self.assertFalse(self.cluster.jobs)


@unittest.skipUnless(sys.platform == 'linux', 'shell fixture uses Linux tools')
class RegionShellTests(unittest.TestCase):
    """Execute the actual checks with synthetic block topology, never devices."""
    fixture = r'''
set -eu
D=/dev/dm-1
fail() { echo "ERROR $*"; exit 1; }
lsblk() {
  case "$*" in
    '-dnro TYPE '*) echo "${KIND:-lvm}";;
    '-nrpo NAME '*) echo "$D";;
    '-snrpo KNAME,TYPE '*)
      if [ "${KIND:-lvm}" = part ]; then echo "$D part"; else echo "$D lvm"; echo '/dev/sda3 part'; fi
      echo '/dev/sda disk'; [ "${MULTI:-no}" = no ] || echo '/dev/sdb disk';;
    '-dnro TRAN '*) echo "${TRAN:-nvme}";;
    '-dnro SERIAL '*) echo fixture-parent;;
    '-dnro WWN '*) echo fixture-wwn;;
    '-dnro PARTUUID '*) echo fixture-part;;
    '-snrpb '*) echo "${GEOMETRY:-geometry-one}";;
    '-nrpb '*) echo "${SIBLINGS:-siblings-one}";;
    *) exit 2;;
  esac
}
ls() { [ "${READ_FAIL:-}" != holders ] || return 2; [ "${HOLDER:-no}" = no ] || echo child; }
readlink() {
  case "$2" in
    /dev/disk/*) [ "${TEST_STABLE:-yes}" = yes ] && echo "$D" || echo /dev/other;;
    /dev/mapper/*)
      [ "${READ_FAIL:-}" != mapper ] || return 2
      if [ "${ALIAS:-same}" = other ] || { [ "${ALIAS:-same}" = changed ] && [ -f "$TRACE/lvs" ]; }; then echo /dev/other; else echo "$D"; fi;;
    /sys/*) echo /sys/devices/pci0000/fixture;;
    *) echo "$2";;
  esac
}
lvs() {
  # Match LVM's real argument handling: /dev/dm-N is NOT an LV selector.
  [ "$*" = "--segments --noheadings --separator | -o lv_uuid,vg_uuid,segtype,lv_attr,devices /dev/mapper/fixture--vg-longhorn" ] || { echo 'Cannot process volume group dm-1' >&2; return 5; }
  [ "${READ_FAIL:-}" != lvs ] || return 5
  touch "$TRACE/lvs"
  echo "${LV_UUID:-fixture-lv}|fixture-vg|${SEGMENT:-linear}|${ATTR:--wi-ao----}|/dev/sda3(0)"
  [ "${MULTI_SEGMENT:-no}" = no ] || echo 'fixture-lv|fixture-vg|linear|-wi-ao----|/dev/sda3(1)'
}
cat() {
  case "$1" in
    */dm/name) [ "${READ_FAIL:-}" != name ] || return 2; echo "${DM_NAME:-fixture--vg-longhorn}";;
    */dm/uuid)
      [ "${READ_FAIL:-}" != uuid ] || return 2
      if [ "${IDENTITY:-same}" = changed ] && [ -f "$TRACE/lvs" ]; then echo LVM-other; else echo LVM-fixturevgfixturelv; fi;;
    *) return 2;;
  esac
}
findmnt() { [ "${SYSTEM:-no}" = no ] && echo /dev/os || echo "$D"; }
'''

    def run_check(self, **options):
        script = self.fixture + R.inspection() + '\nprintf "%s\\n" "$REGION"\n'
        with tempfile.TemporaryDirectory() as trace:
            return subprocess.run(['sh', '-c', script], env=dict(os.environ, TRACE=trace, **options), capture_output=True, text=True, timeout=5)

    def test_runtime_and_other_contents_remain_blocked_with_bounded_plain_text_names(self):
        for runtime in (False, True):
            with self.subTest(runtime=runtime), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                if runtime: (root / 'engine-binaries').mkdir()
                names = [str(root / ('user-<file>' + str(n) + 'x' * 100)) for n in range(9)]
                script = 'set -eu\nfail() { echo "$*"; exit 1; }\n' + V.foreign_contents_message() + '\necho ALLOWED\n'
                result = subprocess.run(['sh', '-c', script], env=dict(os.environ, P=directory, OTHER='\n'.join(names)), capture_output=True, text=True, timeout=5)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('Longhorn runtime storage' if runtime else 'Other files', result.stdout, result.stderr)
                self.assertIn('whole filesystem', result.stdout)
                self.assertNotIn('ALLOWED', result.stdout)
                self.assertNotIn('<', result.stdout)
                self.assertNotIn(directory, result.stdout)
                self.assertLess(len(result.stdout), 500)

    def test_mapper_selector_is_bound_to_device_and_lvm_uuid_before_and_after_report(self):
        for options, error in (
                ({'ALIAS':'other'}, 'different device'),
                ({'ALIAS':'changed'}, 'changed during inspection'),
                ({'IDENTITY':'changed'}, 'changed during inspection'),
                ({'LV_UUID':'another-lv'}, 'report does not match'),
                ({'DM_NAME':'../other'}, 'name is invalid'),
                ({'READ_FAIL':'name'}, 'Cannot read logical volume name'),
                ({'READ_FAIL':'uuid'}, 'Cannot read logical volume identity'),
                ({'READ_FAIL':'mapper'}, 'Cannot resolve logical volume name'),
                ({'READ_FAIL':'lvs'}, 'Cannot inspect logical volume'),
                ({'MULTI_SEGMENT':'yes'}, 'Multi-segment')):
            with self.subTest(options=options):
                result = self.run_check(**options)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(error, result.stdout)

    def test_partition_and_linear_lv_have_stable_distinct_identities(self):
        lv = self.run_check(); part = self.run_check(KIND='part')
        self.assertEqual(lv.returncode, 0, lv.stdout + lv.stderr)
        self.assertEqual(part.returncode, 0, part.stdout + part.stderr)
        self.assertNotEqual(lv.stdout, part.stdout)
        self.assertNotEqual(lv.stdout, self.run_check(GEOMETRY='changed-extents').stdout)

    def test_shared_system_thin_multi_parent_holder_and_network_disks_are_rejected(self):
        for options in ({'SYSTEM':'yes'}, {'SEGMENT':'thin'}, {'ATTR':'owi-ao----'}, {'MULTI':'yes'}, {'HOLDER':'yes'}, {'TEST_STABLE':'no'}, {'TRAN':'iscsi'}, {'READ_FAIL':'holders'}):
            with self.subTest(options=options):
                result = self.run_check(**options)
                self.assertNotEqual(result.returncode, 0, result.stdout)

    def test_full_generated_region_helpers_keep_os_sibling_and_refuse_late_identity_change(self):
        for kind in ('part', 'lvm'):
            for changed in (('', 'uuid') if kind == 'part' else ('', 'uuid', 'mapper', 'dm-identity')):
                with self.subTest(kind=kind, changed=changed):
                    fixture = FIXTURES.HostShellTests(); fixture.setUp()
                    try:
                        root = fixture.root
                        (root / 'proc/swaps').write_text('Filename Type Size Used Priority\n')
                        (root / 'sys/devices/pci0000').mkdir(parents=True)
                        (root / 'sys/class/block/sda').mkdir()
                        (root / 'sys/class/block/sda/device').symlink_to(root / 'sys/devices/pci0000')
                        (root / 'sys/class/block/sdb/dm').mkdir()
                        (root / 'sys/class/block/sdb/dm/uuid').write_text('LVM-fixturevgfixturelv')
                        (root / 'sys/class/block/sdb/dm/name').write_text('fixture--vg-longhorn')
                        (root / 'dev/mapper').mkdir()
                        (root / 'dev/mapper/fixture--vg-longhorn').symlink_to(root / 'dev/sdb')
                        umount = root / 'bin/umount'
                        umount.write_text(umount.read_text() + r'''
case "${TEST_AFTER_UNMOUNT:-}" in
 mapper) rm "$FIXTURE/dev/mapper/fixture--vg-longhorn"; ln -s "$FIXTURE/dev/os" "$FIXTURE/dev/mapper/fixture--vg-longhorn";;
 dm-identity) echo LVM-other > "$FIXTURE/sys/class/block/sdb/dm/uuid";;
esac
''')
                        stable = '/dev/disk/by-partuuid/fixture-part' if kind == 'part' else '/dev/disk/by-id/dm-uuid-LVM-fixturevgfixturelv'
                        link = root / stable.lstrip('/'); link.parent.mkdir(exist_ok=True); link.symlink_to(root / 'dev/sdb')
                        tool = root / 'bin/lsblk'
                        original = tool.read_text()
                        extra = r'''case "$*" in
*'-snrpo KNAME,TYPE'*) echo "$FIXTURE/dev/sdb $TEST_KIND"; [ "$TEST_KIND" = part ] || echo "$FIXTURE/dev/sda3 part"; echo "$FIXTURE/dev/sda disk"; exit 0;;
*'-dnro TYPE'*) echo "$TEST_KIND"; exit 0;;
*'-dnro TRAN'*) echo nvme; exit 0;;
*'-dnro PARTUUID'*) echo fixture-part; exit 0;;
*'-snrpb'*) echo geometry-one; exit 0;;
*'-nrpb'*) echo 'sda3 part 2000'; echo 'os-lv lvm 500'; echo 'data-lv lvm 1000'; exit 0;;
esac
'''
                        tool.write_text('#!/bin/sh\nset -eu\n' + extra + original.split('\n', 2)[2])
                        lvs = root / 'bin/lvs'
                        lvs.write_text('#!/bin/sh\n[ "$*" = "--segments --noheadings --separator | -o lv_uuid,vg_uuid,segtype,lv_attr,devices $FIXTURE/dev/mapper/fixture--vg-longhorn" ] || exit 5\necho "fixture-lv|fixture-vg|linear|-wi-ao----|/dev/sda3(0)"\n')
                        lvs.chmod(0o755)
                        fixture.env['TEST_KIND'] = kind
                        inspect = 'set -eu\nD=' + str(root / 'dev/sdb') + '\nfail() { echo "$*"; exit 1; }\n' + fixture.rebase(R.inspection()) + '\necho "$REGION"'
                        result = subprocess.run(['sh','-c',inspect],env=fixture.env,capture_output=True,text=True,timeout=5)
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                        facts = {**fixture.facts, 'kind':kind, 'by_id':stable, 'region':result.stdout.strip()}
                        fixture.script = fixture.rebase(V._host_script({'hardware':facts,'boot_id':'boot-1','prepare_request_id':'b'*24,'disk_uuid':'disk-source-uuid'}))
                        result = fixture.run_script(TEST_AFTER_UNMOUNT=changed)
                        if changed:
                            self.assertNotEqual(result.returncode, 0); self.assertNotIn('wiped', fixture.trace())
                        else:
                            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                            self.assertEqual(fixture.trace(), 'unmounted\nwiped\n')
                        self.assertIn('UUID=other / ext4', (root / 'etc/fstab').read_text())
                    finally: fixture.doCleanups()


if __name__ == '__main__': unittest.main()
