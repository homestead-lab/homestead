"""Disk conversion never treats missing evidence as permission to erase."""
import copy
import json
import os
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import homestead_capacity_review as REVIEW
import homestead_disk_v2 as V
import homestead_disks as DISKS
import homestead_kube as K
import homestead_operations as OPS
import homestead_storage_conflicts as CONFLICTS
from fakes.job_store import JobStore
from fakes.kube import Kube

GB = 1024 ** 3
READY = [{'type': 'Ready', 'status': 'True'}, {'type': 'Schedulable', 'status': 'True'}]
HOST_FACTS = '\n'.join(['DEVICE /dev/sdb', 'SIZE ' + str(1000 * GB), 'UUID fs-data', 'FSTYPE ext4',
    'SERIAL TEST-DISK-01', 'WWN test-wwn', 'KIND disk', 'BYID /dev/disk/by-id/ata-TEST-DISK-01',
    'CONFIG {"diskUUID":"disk-source-uuid"}', 'END'])



def setUpModule():
    # These tests check the conversion's steps, ticking it as fast as they like;
    # how often a waiting job looks at its host is tested in test_job_cadence.
    import homestead_job_cadence as CADENCE
    patch = mock.patch.dict(CADENCE.INTERVALS, {"disk-v2-convert": 0})
    patch.start()
    unittest.addModuleCleanup(patch.stop)

class Operations(JobStore):
    TERMINAL = OPS.TERMINAL
    def __init__(self): self.items = []; self._lock = threading.RLock(); self.woken = 0
    def advance_soon(self): self.woken += 1
    def _read(self): return copy.deepcopy(self.items)
    def _public(self, item): return OPS._public(item)
    def start(self, kind, title, resource, href, ref, message):
        CONFLICTS.require_clear(self.items, kind, ref, resource)
        item = dict(id='task-' + str(len(self.items)), kind=kind, title=title, resource=resource,
                    href=href, ref=copy.deepcopy(ref), status='queued', progress=0, message=message)
        self.items.append(item)
        return self._public(item)
    def checkpoint(self, item):
        index = next(n for n, i in enumerate(self.items) if i['id'] == item['id'])
        self.items[index] = copy.deepcopy(item)
    def list_operations(self):
        for item in self._read():
            if item['status'] not in self.TERMINAL:
                status, progress, message = V.progress(item)
                item.update(status=status, progress=progress, message=message)
                self.checkpoint(item)
        return [self._public(i) for i in self.items]


GONE = object()
NODES = ('node-1', 'node-2', 'node-3')
JOBS = '/apis/batch/v1/namespaces/lab/jobs'


def NODE(name): return '/api/v1/nodes/' + name
def LHN(name): return V.LH + '/nodes/' + name
def VOL(name): return V.LH + '/volumes/' + name
def ENG(volume): return V.LH + '/engines/' + volume + '-engine'
def SET(name): return V.LH + '/settings/' + name


def lh(kind, name, **body):
    return dict(apiVersion='longhorn.io/v1beta2', kind=kind, metadata={'namespace': 'longhorn-system', 'name': name, 'uid': name + '-uid'}, **body)


class Cluster:
    """Longhorn, its nodes and one volume of three replicas on the shared fake
    API (tests/fakes/kube.py); the host's own answers (facts, the blank check)
    are set here. put() changes an object as it stands, as something outside
    Homestead would."""
    def __init__(self, test):
        self.ops = Operations(); self.harvester = False
        settings = {'v2-data-engine': 'true', 'storage-minimal-available-percentage': '25',
            'storage-over-provisioning-percentage': '100', 'replica-soft-anti-affinity': 'false',
            'replica-disk-soft-anti-affinity': 'false', 'replica-zone-soft-anti-affinity': 'true',
            'allow-empty-node-selector-volume': 'true', 'allow-empty-disk-selector-volume': 'true'}
        self.kube = test.enterContext(Kube(
            *[lh('Setting', name, value=value) for name, value in settings.items()],
            *[{'apiVersion': 'v1', 'kind': 'Node', 'metadata': {'name': n, 'uid': n + '-uid'}, 'spec': {}, 'status': {
                'nodeInfo': {'bootID': 'boot-1'}, 'conditions': copy.deepcopy(READY)}} for n in NODES],
            *[lh('Node', n, spec={'allowScheduling': True, 'tags': [], 'disks': {}},
                 status={'conditions': copy.deepcopy(READY), 'diskStatus': {}}) | {'metadata': {
                     'namespace': 'longhorn-system', 'name': n, 'uid': n + '-lh-uid', 'resourceVersion': '1'}} for n in NODES],
            lh('InstanceManager', 'im-v2', spec={'nodeID': 'node-1', 'dataEngine': 'v2', 'type': 'aio'},
               status={'currentState': 'running'})))
        # The shared fake runs the real write guards; the storage guard takes the job store's lock under DATA_DIR.
        test.enterContext(patch.object(OPS, 'DATA_DIR', test.enterContext(tempfile.TemporaryDirectory())))
        for n in NODES: self.disk(n, 'data', 'filesystem', 'disk-source-uuid' if n == 'node-1' else n + '-disk')
        self.disk('node-1', 'spare', 'filesystem', 'disk-spare-uuid')
        self.store_devices = {}
        self.volume('volume-data', 100)
        self.facts = HOST_FACTS; self.blank = 'VERIFIED\n'
        REVIEW.bind(lambda: b'test-only-review-key')
        V.bind(K.kget, self.send, lambda force=False: {'harvester': self.harvester}, self.ops, 'lab', lambda path: 'fixture helper output')
    def put(self, path, keys, value):
        """Set (or, with GONE, take out) the value at keys in the object at path."""
        obj = self.kube.object(path); parent = obj
        for key in keys[:-1]: parent = parent[key]
        if value is GONE: del parent[keys[-1]]
        else: parent[keys[-1]] = copy.deepcopy(value)
        self.kube.add(obj)
    def lh_node(self, node): return self.kube.object(LHN(node))
    def disk(self, node, name, kind, uid):
        self.put(LHN(node), ('spec', 'disks', name), {'path': '/mnt/' + name, 'diskType': kind,
            'allowScheduling': True, 'evictionRequested': False, 'storageReserved': 0, 'tags': []})
        self.put(LHN(node), ('status', 'diskStatus', name), {'diskUUID': uid, 'conditions': copy.deepcopy(READY),
            'storageMaximum': 1000 * GB, 'storageAvailable': 900 * GB, 'storageScheduled': 100 * GB, 'scheduledReplica': {}})
    def volume(self, name, size):
        self.kube.add(lh('Volume', name, spec={'size': str(size * GB), 'numberOfReplicas': 3, 'dataEngine': 'v1'},
            status={'state': 'attached', 'robustness': 'healthy', 'currentNodeID': 'node-1'}))
        modes = {}
        for node in NODES:
            uid = self.lh_node(node)['status']['diskStatus']['data']['diskUUID']
            rname = name + '-' + node
            self.kube.add(lh('Replica', rname, spec={'volumeName': name, 'nodeID': node,
                'diskID': uid, 'diskPath': '/mnt/data', 'volumeSize': str(size * GB)}, status={'currentState': 'running'}))
            self.put(LHN(node), ('status', 'diskStatus', 'data', 'scheduledReplica', rname), size * GB)
            modes[rname] = 'RW'
        self.kube.add(lh('Engine', name + '-engine', spec={'volumeName': name},
            status={'currentState': 'running', 'replicaModeMap': modes}))
    def keep_first_replica(self):
        for row in K.kget(V.LH + '/replicas')['items'][1:]: self.kube.discard(V.LH + '/replicas/' + row['metadata']['name'])
    def remove_managers(self):
        for row in K.kget(V.LH + '/instancemanagers')['items']: self.kube.discard(V.LH + '/instancemanagers/' + row['metadata']['name'])
    def add_control(self, status):
        self.kube.add(lh('InstanceManagerUpgradeControl', 'longhorn-instance-manager-upgrade-control', status=status))
    @property
    def writes(self): return [(method, path, body) for method, path, body, _ in self.kube.writes]
    @property
    def jobs(self): return {job['metadata']['name']: job for job in K.kget(JOBS)['items']}
    def job_path(self): return JOBS + '/' + next(iter(self.jobs))
    def clear_jobs(self):
        for name in self.jobs: self.kube.discard(JOBS + '/' + name)
    def send(self, method, path, body, **options):
        assert self.ops.stored(), 'The approval must be durable before any mutation'
        assert (method == 'PATCH' and '/nodes/' in path) or (method == 'POST' and path.endswith('/jobs')) \
            or (method == 'DELETE' and '/jobs/' in path), (method, path)
        if method == 'DELETE': assert body['preconditions']['uid'] == self.kube.object(path)['metadata']['uid']
        result = K.ksend(method, path, body, **options)
        if method == 'POST':
            # The Job's pod, which the preparation looks up by the Job's name.
            name = body['metadata']['name']
            self.kube.add({'apiVersion': 'v1', 'kind': 'Pod', 'metadata': {'name': 'helper', 'namespace': 'lab',
                'labels': {'job-name': name}, 'ownerReferences': [{'apiVersion': 'batch/v1', 'kind': 'Job', 'name': name,
                                                                   'uid': result['metadata']['uid']}]}})
        return result
    def host(self, node, script, **options):
        if 'echo STORECHECK' in script:
            device = next(line.split()[1] for line in self.facts.splitlines() if line.startswith('DEVICE '))
            stores = self.lh_node(node)['spec']['disks']
            return '\n'.join(['STORECHECK'] + ['STORE ' + name + ' ' + self.store_devices.get(name, device if name == 'data' else '/dev/sdc') for name, spec in stores.items() if spec.get('diskType', 'filesystem') == 'filesystem'] + ['STORESEND']), ''
        return (self.blank if 'echo VERIFIED' in script else self.facts), ''
    def review(self): return V.review({'node': 'node-1', 'disk': 'data'})
    def start(self, plan=None):
        plan = plan or self.review()
        return V.start({k: plan[k] for k in ('node', 'disk', 'request_id', 'capacity_token')} | {'confirm_capacity': True})
    def tick(self):
        self.ops.list_operations()  # what the leader's jobs loop does (#622)
        return V.status(self.ops.stored()[0]['id'])
    def evacuate(self):
        for replica in K.kget(V.LH + '/replicas')['items']:
            if replica['spec']['nodeID'] == 'node-1':
                replica['spec'].update(diskID='disk-spare-uuid', diskPath='/mnt/spare')
                self.kube.add(replica)
        self.put(LHN('node-1'), ('status', 'diskStatus', 'data', 'scheduledReplica'), {})
    def awaiting(self): self.start(); self.tick(); self.evacuate(); return self.tick()
    def prepare(self):
        review = V.prepare_review(self.ops.stored()[0]['id'])
        body = {k: review[k] for k in ('operation_id', 'request_id', 'capacity_token')}
        body.update(confirm_capacity=True, confirm_device=review['device'])
        return V.prepare(body), body


class DiskV2Tests(unittest.TestCase):
    def setUp(self):
        for module, names in ((V, ('kget', 'ksend', 'platform', 'ops', 'NS', 'read_log')), (REVIEW, ('_key',))):
            for name in names:
                saved = patch.object(module, name, getattr(module, name)); saved.start(); self.addCleanup(saved.stop)
        self.c = Cluster(self)
        p = patch.object(V.HOST, 'run', self.c.host); p.start(); self.addCleanup(p.stop)
    def test_a_look_at_the_job_asks_the_loop_to_run_soon(self):
        self.c.start(); self.c.ops.woken = 0
        V.status(self.c.ops.stored()[0]['id'])
        self.assertEqual(1, self.c.ops.woken, 'the page polling here would wait for the idle tick')
    def test_review_is_read_only_and_v2_capacity_does_not_count(self):
        plan = self.c.review(); self.assertEqual(plan['blockers'], []); self.assertFalse(self.c.writes)
        self.c.put(LHN('node-1'), ('spec', 'disks', 'spare', 'diskType'), 'block')
        self.assertTrue(self.c.review()['blockers'])
    def test_only_copy_degraded_detached_and_unconfirmed_replica_block(self):
        for change in ('only-copy', 'degraded', 'detached', 'WO'):
            with self.subTest(change=change):
                c = Cluster(self)
                if change == 'only-copy': c.keep_first_replica()
                if change == 'degraded': c.put(VOL('volume-data'), ('status', 'robustness'), 'degraded')
                if change == 'detached': c.put(VOL('volume-data'), ('status', 'state'), 'detached')
                if change == 'WO': c.put(ENG('volume-data'), ('status', 'replicaModeMap', 'volume-data-node-2'), 'WO')
                self.assertTrue(c.review()['blockers']); self.assertFalse(c.writes)
    def test_capacity_budget_covers_all_volumes_and_full_replica_sizes(self):
        self.c.volume('volume-extra', 100)
        self.c.put(LHN('node-1'), ('status', 'diskStatus', 'spare', 'storageAvailable'), 400 * GB)
        self.assertTrue(self.c.review()['blockers'])
    def test_tags_empty_selector_policy_and_zone_affinity_are_respected(self):
        self.c.put(LHN('node-1'), ('spec', 'disks', 'spare', 'tags'), ['ssd'])
        self.c.put(SET('allow-empty-disk-selector-volume'), ('value',), 'false')
        self.assertTrue(self.c.review()['blockers'])
        self.c.put(VOL('volume-data'), ('spec', 'diskSelector'), ['ssd'])
        self.assertEqual(self.c.review()['blockers'], [])
        self.c.put(SET('replica-zone-soft-anti-affinity'), ('value',), 'false')
        self.assertTrue(self.c.review()['blockers'])  # unlabelled nodes share a zone
    def test_unknown_inventory_is_not_empty_and_backing_images_block(self):
        get = K.kget
        with patch.object(V, 'kget', lambda path: {} if path.endswith('/replicas') else get(path)):
            with self.assertRaisesRegex(ValueError, 'incomplete'): self.c.review()
        self.c.put(LHN('node-1'), ('status', 'diskStatus', 'data', 'scheduledBackingImage'), {'image': 1})
        with self.assertRaisesRegex(ValueError, 'backing-image'): self.c.review()
        self.assertFalse(self.c.writes)
    def test_harvester_missing_v2_and_active_upgrade_refuse_review(self):
        self.c.harvester = True
        with self.assertRaisesRegex(ValueError, 'Harvester'): self.c.review()
        self.c.harvester = False; self.c.remove_managers()
        with self.assertRaisesRegex(ValueError, 'Set up V2'): self.c.review()
        self.c.add_control({'currentNode': 'node-2'})
        with self.assertRaisesRegex(ValueError, 'upgrade'): self.c.review()
    def test_host_inspection_rejects_partition_system_missing_identity_and_partial_output(self):
        for text in (HOST_FACTS.replace('KIND disk', 'KIND part'), HOST_FACTS.replace('SERIAL TEST-DISK-01', 'SERIAL').replace('WWN test-wwn', 'WWN'), HOST_FACTS.replace('END', ''), 'ERR Filesystem has other mounts'):
            with self.subTest(text=text[:40]):
                self.c.facts = text
                with self.assertRaises(ValueError): self.c.review()
        self.c.facts = HOST_FACTS
        with self.assertRaisesRegex(ValueError, 'System storage'): V._host_facts('/var/lib/longhorn', 'node-1')
        self.assertFalse(self.c.writes)
    def test_stale_review_changed_hardware_and_expired_token_do_not_start(self):
        plan = self.c.review()
        self.c.facts = HOST_FACTS.replace('TEST-DISK-01', 'TEST-DISK-02')
        with self.assertRaisesRegex(ValueError, 'changed'): self.c.start(plan)
        self.c.facts = HOST_FACTS
        with patch.object(REVIEW.time, 'time', return_value=10**12):
            with self.assertRaisesRegex(ValueError, 'changed'): self.c.start(plan)
        self.assertFalse(self.c.ops.items); self.assertFalse(self.c.writes)
    def test_acknowledgement_is_required_and_duplicate_start_has_one_receipt(self):
        plan = self.c.review()
        with self.assertRaises(ValueError): V.start(plan)
        first = self.c.start(plan); second = self.c.start(plan)
        self.assertEqual(first['id'], second['id']); self.assertEqual(len(self.c.ops.items), 1)
        self.assertFalse(self.c.writes)
    def test_evacuation_waits_for_both_inventory_and_rw_health_without_erasing(self):
        self.c.start(); self.c.tick(); self.assertFalse(self.c.jobs)
        self.c.put(LHN('node-1'), ('status', 'diskStatus', 'data', 'scheduledReplica'), {})
        self.assertEqual(self.c.tick()['phase'], 'evacuating')  # actual CRs remain
        self.c.evacuate(); self.c.put(ENG('volume-data'), ('status', 'replicaModeMap', 'volume-data-node-1'), 'WO')
        self.assertEqual(self.c.tick()['phase'], 'evacuating')
        self.c.put(ENG('volume-data'), ('status', 'replicaModeMap', 'volume-data-node-1'), 'RW')
        for _ in range(3): self.assertEqual(self.c.tick()['phase'], 'awaiting-erase')
        self.assertFalse(self.c.jobs); self.assertEqual(len(self.c.writes), 1)
    def test_new_boot_needs_fresh_erase_review_and_device_confirmation(self):
        self.c.awaiting(); review = V.prepare_review('task-0')
        body = {k: review[k] for k in ('operation_id', 'request_id', 'capacity_token')}
        body.update(confirm_capacity=True, confirm_device='/dev/sdc')
        with self.assertRaisesRegex(ValueError, 'exact device'): V.prepare(body)
        body['confirm_device'] = '/dev/sdb'; self.c.put(NODE('node-1'), ('status', 'nodeInfo', 'bootID'), 'boot-2')
        with self.assertRaisesRegex(ValueError, 'fresh preparation'): V.prepare(body)
        self.c.prepare(); self.assertEqual(self.c.ops.items[0]['ref']['boot_id'], 'boot-2')
        self.assertFalse(self.c.jobs)
    def test_device_erase_is_only_dispatched_after_durable_second_approval(self):
        self.c.awaiting(); item, body = self.c.prepare()
        self.assertEqual(item['phase'], 'removing'); self.assertFalse(self.c.jobs)
        self.assertEqual(V.prepare(body)['id'], item['id'])
        self.c.tick(); self.assertNotIn('data', self.c.lh_node('node-1')['spec']['disks'])
        self.assertFalse(self.c.jobs)
        self.c.tick(); self.assertEqual(len(self.c.jobs), 1)
        self.c.tick(); self.assertEqual(len(self.c.jobs), 1)
        job = next(iter(self.c.jobs.values())); spec = job['spec']['template']['spec']
        self.assertEqual(job['spec']['backoffLimit'], 0); self.assertEqual(spec['restartPolicy'], 'Never')
        self.assertFalse(spec['automountServiceAccountToken']); self.assertEqual(spec['nodeName'], 'node-1')
        script = spec['containers'][0]['command'][-1]
        self.assertLess(script.index("printf 'started"), script.index('wipefs -a'))
        self.assertNotIn('umount -f', script); self.assertNotIn('umount -l', script)
    def test_changed_replacement_health_after_removal_prevents_dispatch(self):
        self.c.awaiting(); self.c.prepare(); self.c.tick()
        self.c.put(VOL('volume-data'), ('status', 'robustness'), 'degraded')
        item = self.c.tick(); self.assertEqual(item['status'], 'failed'); self.assertFalse(self.c.jobs)
        self.assertFalse(item['dismissible'])
    def test_expansion_migration_and_replaced_host_invalidate_pending_erase(self):
        self.c.awaiting()
        for change in ({'size': str(101 * GB)}, {'migrationNodeID': 'node-2'}):
            original = self.c.kube.object(VOL('volume-data'))['spec']
            self.c.put(VOL('volume-data'), ('spec',), dict(original, **change))
            with self.assertRaises(ValueError): V.prepare_review('task-0')
            self.c.put(VOL('volume-data'), ('spec',), original)
        self.c.prepare(); self.c.tick()
        self.c.put(NODE('node-1'), ('metadata', 'uid'), 'replacement-host')
        self.assertEqual(self.c.tick()['status'], 'failed'); self.assertFalse(self.c.jobs)
    def test_stale_cancel_retry_does_not_patch_disk_twice(self):
        self.c.awaiting(); V.cancel_run(self.c.ops.items[0], {})
        count = len(self.c.writes)
        V.cancel_run(self.c.ops.items[0], {})
        self.assertEqual(len(self.c.writes), count)
    def test_lost_or_foreign_job_is_never_recreated_or_deleted(self):
        self.c.awaiting(); self.c.prepare(); self.c.tick(); self.c.tick()
        self.c.clear_jobs(); writes = len(self.c.writes)
        self.assertEqual(self.c.tick()['status'], 'failed'); self.assertEqual(len(self.c.writes), writes)
        self.assertFalse(self.c.tick()['dismissible'])
    def test_job_admission_injection_and_uid_replacement_are_rejected(self):
        self.c.awaiting(); self.c.prepare(); self.c.tick(); self.c.tick()
        envFrom = ('spec', 'template', 'spec', 'containers', 0, 'envFrom')
        self.c.put(self.c.job_path(), envFrom, [{'secretRef': {'name': 'other'}}])
        self.assertEqual(self.c.tick()['status'], 'failed')
        ref = self.c.ops.items[0]['ref']; self.c.put(self.c.job_path(), envFrom, GONE); self.c.put(self.c.job_path(), ('metadata', 'uid'), 'foreign')
        with self.assertRaisesRegex(ValueError, 'identity'): V._job(ref)
        V._cleanup(ref); self.assertEqual(len(self.c.jobs), 1)
    def test_lost_dispatch_response_adopts_owned_helper_despite_later_health_change(self):
        self.c.awaiting(); self.c.prepare(); self.c.tick(); self.c.tick()
        saved = self.c.ops.items[0]; saved['ref']['phase'] = 'dispatching'; saved['ref'].pop('job_uid')
        self.c.put(VOL('volume-data'), ('status', 'robustness'), 'degraded')
        writes = len(self.c.writes)
        self.assertEqual(self.c.tick()['phase'], 'preparing')
        self.assertEqual(len(self.c.writes), writes)
    def test_registration_requires_blank_receipt_then_longhorn_ready_and_cleans_only_owned_helper(self):
        self.c.awaiting(); self.c.prepare(); self.c.tick(); self.c.tick()
        self.c.put(self.c.job_path(), ('status',), {'succeeded': 1})
        self.c.blank = ''
        self.assertEqual(self.c.tick()['phase'], 'registering')
        self.assertFalse(any(d.get('diskType') == 'block' for d in self.c.lh_node('node-1')['spec']['disks'].values()))
        self.c.blank = 'VERIFIED\n'
        item = self.c.tick(); self.assertEqual(item['phase'], 'verifying'); self.assertEqual(item['status'], 'running')
        ref = self.c.ops.items[0]['ref']; name = ref['v2_disk']
        self.c.put(LHN('node-1'), ('status', 'diskStatus', name), {'diskUUID': 'new-block-uuid', 'conditions': copy.deepcopy(READY)})
        self.assertEqual(self.c.tick()['status'], 'succeeded'); self.assertFalse(self.c.jobs)
        self.assertEqual(self.c.lh_node('node-1')['spec']['disks'][name]['path'], '/dev/disk/by-id/ata-TEST-DISK-01')
        self.assertEqual(self.c.kube.object(VOL('volume-data'))['spec']['dataEngine'], 'v1')
        self.assertTrue(self.c.ops.items[0]['ref']['preparation_log'])
    def test_cancel_can_stop_evacuation_with_degraded_host_and_keeps_disk_disabled(self):
        self.c.start(); self.c.tick(); self.c.remove_managers(); self.c.put(NODE('node-1'), ('status', 'conditions'), [])
        item = self.c.ops.items[0]
        V.cancel_run(item, {})
        disk = self.c.lh_node('node-1')['spec']['disks']['data']
        self.assertFalse(disk['allowScheduling']); self.assertFalse(disk['evictionRequested'])
        self.assertEqual(self.c.ops.items[0]['ref']['phase'], 'cancelled'); self.assertFalse(self.c.jobs)
    def test_cancellation_after_erase_approval_is_refused(self):
        self.c.awaiting(); self.c.prepare()
        with self.assertRaisesRegex(ValueError, 'approved'): V.cancel_run(self.c.ops.items[0], {})
    def test_partial_preparation_retains_log_disk_guard_and_maintenance_exclusions(self):
        self.c.awaiting(); self.c.prepare(); self.c.tick(); self.c.tick()
        self.c.put(self.c.job_path(), ('status',), {'failed': 1})
        item = self.c.tick(); self.assertEqual(item['status'], 'failed'); self.assertFalse(item['dismissible'])
        with self.assertRaises(ValueError): V.mutation_guard('node-1', 'data')
        V.mutation_guard('node-2', 'data')
        for kind, ref in [('node-power', {'node': 'node-1'}), ('platform-upgrade', {'component': 'longhorn'}), ('snapshot-delete', {'volume': 'volume-data'})]:
            with self.subTest(kind=kind), self.assertRaises(ValueError): CONFLICTS.require_clear(self.c.ops.items, kind, ref)
        self.assertEqual(V.logs(self.c.ops.items[0])[0]['text'], 'fixture helper output')
    def test_other_disk_actions_share_the_lock_and_cannot_change_reviewed_device(self):
        self.c.awaiting(); writes = []
        def change(*args, **kwargs):
            self.assertTrue(self.c.ops._lock._is_owned()); writes.append(args)
        names = ('set_disk_tags', 'set_scheduling', 'evict', 'remove', 'set_node_tags', 'add', 'set_up', 'use_os_space', 'retire_start')
        disks = types.SimpleNamespace(**{name: change for name in names})
        V.protect_mutations(disks)
        for apply in (lambda: disks.remove('node-1', 'data'), lambda: disks.set_up({'node': 'node-1', 'device': '/dev/sdb'}),
                      lambda: disks.add({'node': 'node-1', 'path': '/mnt/data'}), lambda: disks.set_node_tags('node-1', ['changed'])):
            with self.assertRaisesRegex(ValueError, 'saved V2'): apply()
        self.assertFalse(writes)
        disks.set_up({'node': 'node-1', 'device': '/dev/sdc'})
        disks.remove('node-2', 'data'); self.assertEqual(len(writes), 2)
    def test_standalone_reader_rebinding_clears_inherited_store_and_server_reinstalls_guards(self):
        names = ('kget', 'ksend', 'temps', 'mutation_scope', 'v2_tasks', 'set_disk_tags', 'set_scheduling',
                 'evict', 'remove', 'set_node_tags', 'add', 'set_up', 'use_os_space', 'retire_start')
        for name in names:
            saved = patch.object(DISKS, name, getattr(DISKS, name)); saved.start(); self.addCleanup(saved.stop)
        self.c.awaiting(); V.protect_mutations(DISKS)
        self.assertTrue(DISKS.v2_tasks())
        writes = []
        DISKS.bind(K.kget, lambda *args, **kw: writes.append(args), lambda: {})
        self.assertEqual(DISKS.v2_tasks(), [])
        with patch.object(V, 'ops', None):
            DISKS.evict('node-1', 'data', False)  # no inherited production /data store
        self.assertEqual(len(writes), 1)
        V.protect_mutations(DISKS)
        with self.assertRaisesRegex(ValueError, 'saved V2'): DISKS.evict('node-1', 'data', False)
        self.assertEqual(len(writes), 1)
    def test_json_patch_protects_unreviewed_disk_changes(self):
        self.c.start(); self.c.put(LHN('node-1'), ('spec', 'disks', 'data', 'tags'), ['changed'])
        self.assertIn('configuration changed', self.c.tick()['message']); self.assertFalse(self.c.writes)
    def test_persisted_phases_survive_process_restart_without_repeated_erase(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        with patch.object(OPS, 'DATA_DIR', directory.name), patch.object(OPS, 'WRITE_GUARD', None), patch.dict(OPS.RESOLVERS, {V.KIND: V.progress}):
            V.bind(K.kget, self.c.send, lambda force=False: {}, OPS, 'lab', lambda path: 'helper log')
            self.c.ops = OPS
            item = self.c.start(); operation_id = item['id']
            advanced = lambda: (OPS.list_operations(), V.status(operation_id))[1]  # the jobs loop, then a look
            advanced(); self.c.evacuate(); advanced()
            review = V.prepare_review(operation_id)
            body = {k: review[k] for k in ('operation_id', 'request_id', 'capacity_token')}
            V.prepare(body | {'confirm_capacity': True, 'confirm_device': '/dev/sdb'})
            for phase in ('dispatching', 'preparing', 'preparing'):
                # Every read is a deserialized record; no in-memory coordinator survives.
                self.assertEqual(advanced()['phase'], phase)
            self.assertEqual(sum(method == 'POST' for method, _, _ in self.c.writes), 1)
            saved = OPS.stored()[0]; saved['ref']['phase'] = 'complete'; OPS.checkpoint(saved)
            self.assertEqual(advanced()['status'], 'succeeded')



@unittest.skipUnless(sys.platform == 'linux', 'isolated host shell fixture needs Linux tools')
class HostShellTests(unittest.TestCase):
    """Run the real script with all host paths rebased and device tools faked.

    Only a regular fixture file is touched. No physical device, host mount,
    system fstab or real wipefs is available to this fixture.
    """
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(); self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        for folder in ('dev/disk/by-id', 'mnt/data', 'etc', 'proc/sys/kernel/random', 'proc/1', 'proc/2', 'sys/class/block/sdb/holders', 'bin'):
            (self.root / folder).mkdir(parents=True)
        (self.root / 'dev/sdb').write_text('fixture filesystem signature')
        (self.root / 'dev/disk/by-id/ata-TEST-DISK-01').symlink_to(self.root / 'dev/sdb')
        (self.root / 'proc/sys/kernel/random/boot_id').write_text('boot-1')
        for pid in ('1', '2'): (self.root / ('proc/' + pid + '/mountinfo')).write_text('1 0 8:1 / / rw - ext4 /dev/os rw\n')
        (self.root / 'mnt/data/longhorn-disk.cfg').write_text('{"diskUUID":"disk-source-uuid"}')
        (self.root / 'etc/fstab').write_text('UUID=other / ext4 defaults 0 1\nUUID=fs-data ' + str(self.root / 'mnt/data') + ' ext4 defaults 0 2\n')
        self.initial = (self.root / 'etc/fstab').read_text()
        commands = {
            'lsblk': r'''[ "${TEST_READ_FAIL:-}" != lsblk ] || exit 2
case "$*" in *SERIAL*) echo "${TEST_SERIAL:-TEST-DISK-01}";; *WWN*) echo test-wwn;; *TYPE*) echo disk;; *NAME*) echo "$FIXTURE/dev/sdb";; *MAJ:MIN*) echo 8:16;; *MOUNTPOINT*) [ "${TEST_LATE_MOUNT:-no}" != yes ] || echo /pod/mount;; *) exit 2;; esac''',
            'blockdev': 'echo ' + str(1000 * GB),
            'blkid': r'[ ! -e "$FIXTURE/changed-uuid" ] || { echo other-filesystem; exit 0; }; case "$*" in *" -p "*|-p*) exit 2;; *) echo "${TEST_UUID:-fs-data}";; esac',
            'findmnt': r'''[ "${TEST_READ_FAIL:-}" != findmnt ] || exit 2
case "$*" in *OPTIONS*) echo ro;; *FSROOT*) echo /;; *"--mountpoint"*SOURCE*) echo "$FIXTURE/dev/sdb";; *"--target"*) echo /dev/fixture-other;; *TARGET*) echo "$FIXTURE/mnt/data";; *) exit 2;; esac''',
            'umount': r'''[ "${TEST_BUSY:-no}" != yes ] || exit 1
echo unmounted >> "$FIXTURE/trace"
case "${TEST_AFTER_UNMOUNT:-}" in
 uuid) touch "$FIXTURE/changed-uuid";;
 alias) rm "$FIXTURE/dev/disk/by-id/ata-TEST-DISK-01"; ln -s /dev/fixture-other "$FIXTURE/dev/disk/by-id/ata-TEST-DISK-01";;
 holders) rmdir "$FIXTURE/sys/class/block/sdb/holders";;
 private) echo '2 0 8:16 / /pod/data rw - ext4 /dev/data rw' > "$FIXTURE/proc/2/mountinfo";;
 unreadable) rm "$FIXTURE/proc/2/mountinfo"; mkdir "$FIXTURE/proc/2/mountinfo";;
esac''',
            'mount': r'[ "${TEST_READONLY_FAIL:-no}" != yes ] || exit 1; if [ "${TEST_LATE_DATA:-no}" = yes ]; then mkdir -p "$FIXTURE/mnt/data/replicas"; echo data > "$FIXTURE/mnt/data/replicas/late"; fi',
            'wipefs': r'''[ "$1" != --version ] || { echo 'wipefs from util-linux'; exit 0; }
[ "$1" != --no-act ] || { [ "${TEST_SIGNATURE_FAIL:-no}" != yes ] || exit 2; [ "${TEST_SIGNATURE:-no}" != yes ] || echo ext4; exit 0; }
[ "$*" = '-a /proc/self/fd/8' ] || exit 98
[ "${TEST_EXCLUSIVE_BUSY:-no}" != yes ] || exit 1
[ "$(cat "$FIXTURE/var/lib/homestead/disk-v2/''' + 'b' * 24 + r'''")" = started ] || exit 99; echo wiped >> "$FIXTURE/trace"''',
            'systemctl': 'exit 0',
        }
        for name, body in commands.items():
            target = self.root / 'bin' / name; target.write_text('#!/bin/sh\nset -eu\n' + body + '\n'); target.chmod(0o755)
        facts = {'by_id': '/dev/disk/by-id/ata-TEST-DISK-01',
            'uuid': 'fs-data', 'serial': 'TEST-DISK-01', 'wwn': 'test-wwn', 'size_bytes': 1000 * GB, 'mountpoint': '/mnt/data'}
        self.facts = facts
        self.script = self.rebase(V._host_script({'hardware': facts, 'boot_id': 'boot-1', 'prepare_request_id': 'b' * 24, 'disk_uuid': 'disk-source-uuid'}))
        self.env = dict(os.environ, FIXTURE=str(self.root), PATH=str(self.root / 'bin') + ':' + os.environ['PATH'])

    def rebase(self, script):
        for source in ('/dev/disk/', '/dev/mapper/', '/mnt/data', '/proc/sys/kernel/random/boot_id', '/proc/[0-9]', '/proc/swaps', '/sys/class/block/', '/var/lib/homestead', '/etc/'):
            script = script.replace(source, str(self.root) + source)
        # A regular fixture file stands in for a physical device; all actual
        # device inspection and mutation commands above are fixture executables.
        return script.replace('[ -b "$D" ]', '[ -f "$D" ]')
    def run_script(self, **env):
        return subprocess.run(['sh', '-c', self.script], env=dict(self.env, **env), text=True, capture_output=True, timeout=15)
    def trace(self):
        path = self.root / 'trace'; return path.read_text() if path.exists() else ''
    def test_one_shot_receipt_precedes_erase_and_repeat_preserves_unrelated_fstab(self):
        result = self.run_script(); self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.trace(), 'unmounted\nwiped\n')
        self.assertEqual((self.root / 'etc/fstab').read_text(), 'UUID=other / ext4 defaults 0 1\n')
        self.assertEqual((self.root / ('var/lib/homestead/disk-v2/' + 'b' * 24)).read_text(), 'complete\n')
        second = self.run_script(); self.assertNotEqual(second.returncode, 0)
        self.assertIn('earlier erase attempt', second.stdout); self.assertEqual(self.trace(), 'unmounted\nwiped\n')
    def test_busy_mount_changed_serial_and_foreign_files_stop_before_erasing(self):
        for environment in ({'TEST_BUSY': 'yes'}, {'TEST_SERIAL': 'OTHER'}, {'TEST_UUID': 'OTHER'}):
            result = self.run_script(**environment); self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertEqual(self.trace(), ''); self.assertEqual((self.root / 'etc/fstab').read_text(), self.initial)
        (self.root / 'mnt/data/user-file').write_text('keep')
        result = self.run_script(); self.assertNotEqual(result.returncode, 0)
        self.assertIn('Other files appeared', result.stdout); self.assertEqual(self.trace(), '')

    def test_late_replica_data_and_readonly_failure_never_reach_erase(self):
        for options in ({'TEST_READONLY_FAIL':'yes'}, {'TEST_LATE_DATA':'yes'}):
            with self.subTest(options=options):
                result = self.run_script(**options)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.trace(), '')
                self.assertEqual((self.root / 'etc/fstab').read_text(), self.initial)

    def test_failed_reads_late_alias_mount_holder_private_mount_and_busy_open_block_erase(self):
        for options in ({'TEST_READ_FAIL':'lsblk'}, {'TEST_READ_FAIL':'findmnt'}, {'TEST_LATE_MOUNT':'yes'},
                        *({'TEST_AFTER_UNMOUNT':case} for case in ('alias', 'holders', 'private', 'unreadable')),
                        {'TEST_EXCLUSIVE_BUSY':'yes'}):
            with self.subTest(options=options):
                # Every attempt has a fresh isolated device and one-shot receipt.
                fixture = HostShellTests(); fixture.setUp()
                try:
                    result = fixture.run_script(**options)
                    self.assertNotEqual(result.returncode, 0, result.stdout)
                    self.assertNotIn('wiped', fixture.trace())
                finally: fixture.doCleanups()

    def test_registration_never_treats_failed_inspection_as_blank(self):
        captured = []
        with patch.object(V.HOST, 'run', side_effect=lambda node, script, **kwargs: (captured.append(script) or 'VERIFIED', '')):
            V._verify_blank({'hardware':self.facts, 'node':'node-1', 'prepare_request_id':'b'*24})
        folder = self.root / 'var/lib/homestead/disk-v2'; folder.mkdir(parents=True)
        (folder / ('b'*24)).write_text('complete')
        self.script = self.rebase(captured[0])
        good = self.run_script(); self.assertEqual(good.returncode, 0, good.stdout + good.stderr)
        for options in ({'TEST_READ_FAIL':'lsblk'}, {'TEST_SIGNATURE_FAIL':'yes'}, {'TEST_SIGNATURE':'yes'}, {'TEST_LATE_MOUNT':'yes'}):
            with self.subTest(options=options):
                result = self.run_script(**options)
                self.assertNotEqual(result.returncode, 0); self.assertNotIn('VERIFIED', result.stdout)


if __name__ == '__main__': unittest.main()
