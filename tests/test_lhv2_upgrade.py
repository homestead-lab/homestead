"""Longhorn owns upgrades; reviews and completion require observed evidence."""
import copy
import json
import sys
import tempfile
from types import SimpleNamespace
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import homestead_components as C
import homestead_kube as K
import homestead_lhv2_upgrade as V
import homestead_capacity_review as R
import homestead_changes as CHANGES
import homestead_host_access as HOSTACCESS
import homestead_operations as OPS
from fakes.job_store import Store
from fakes.kube import Kube

LONGHORN = 'longhorn.io/v1beta2'
VOLUME = V.LH + '/volumes/test-disk'
MANAGER_DS = '/apis/apps/v1/namespaces/longhorn-system/daemonsets/longhorn-manager'
CONTROL = V.LH + '/instancemanagerupgradecontrols/' + V.CONTROL


def ready():
    return [{'type': 'Ready', 'status': 'True'}]


def lh(kind, name, **rest):
    return {'apiVersion': LONGHORN, 'kind': kind, 'metadata': {'name': name, 'namespace': 'longhorn-system'}, **rest}


class Cluster:
    """Longhorn's volumes, replicas, managers, settings and upgrade objects, and
    the hosts, on the shared fake API (tests/fakes/kube.py). A test changes what
    is there with edit(), as an operator or Longhorn would."""
    def __init__(self, case, installed='v1.12.2'):
        self.installed = installed
        self.platform = {'distribution': 'k3s'}
        # The shared fake runs the real write guards; the storage guard takes the
        # job store's lock, which lives under its DATA_DIR.
        case.enterContext(patch.object(OPS, 'DATA_DIR', case.enterContext(tempfile.TemporaryDirectory())))
        # No signed-in person on this thread, whatever an earlier test left behind: the write guards read who is asking.
        case.enterContext(patch.object(HOSTACCESS, 'role', lambda: None))
        case.enterContext(patch.object(CHANGES.REQUEST, 'user', None, create=True))
        image = 'longhornio/longhorn-instance-manager:v1.12.2'
        self.kube = case.enterContext(Kube(
            *[{'apiVersion': 'v1', 'kind': 'Node', 'metadata': {'name': f'node-{i}'}, 'status': {
                'nodeInfo': {'kubeletVersion': 'v1.34.2+k3s1'}, 'conditions': ready()}} for i in (1, 2, 3)],
            lh('Volume', 'test-disk', spec={'dataEngine': 'v2', 'frontend': 'blockdev'},
               status={'state': 'attached', 'robustness': 'healthy', 'currentNodeID': 'node-1'}),
            *[lh('Replica', f'replica-{i}', spec={'volumeName': 'test-disk', 'nodeID': f'node-{i}'},
                 status={'currentState': 'running'}) for i in (1, 2, 3)],
            lh('Engine', 'engine-1', spec={'volumeName': 'test-disk'}, status={
                'currentState': 'running', 'replicaModeMap': {f'replica-{i}': 'RW' for i in (1, 2, 3)}}),
            *[lh('InstanceManager', f'im-{i}', spec={'nodeID': f'node-{i}', 'dataEngine': 'v2', 'type': 'aio', 'image': image},
                 status={'currentState': 'running'}) for i in (1, 2, 3)],
            *[lh('Node', f'node-{i}', spec={'allowScheduling': True,
                                            'disks': {'disk': {'diskType': 'block', 'allowScheduling': True}}},
                 status={'conditions': ready(), 'diskStatus': {'disk': {'storageAvailable': 1000000000,
                         'conditions': ready() + [{'type': 'Schedulable', 'status': 'True'}]}}}) for i in (1, 2, 3)],
            *[lh('Setting', name, metadata={'name': name, 'namespace': 'longhorn-system', 'uid': name, 'resourceVersion': '1'},
                 value=json.dumps(values)) for name, values in [(V.AUTO, {'v1': 'false', 'v2': 'false'}), (V.TIMEOUT, {'v2': '60'})]],
            lh('Setting', 'default-instance-manager-image', value='longhornio/longhorn-instance-manager:v1.13.0'),
            *[{'apiVersion': 'v1', 'kind': 'Pod', 'metadata': {'name': f'im-{i}', 'namespace': 'longhorn-system'},
               'spec': {'containers': [{'image': image}]},
               'status': {'conditions': ready(), 'containerStatuses': [{'ready': True, 'image': image}]}} for i in (1, 2, 3)],
            {'apiVersion': 'apps/v1', 'kind': 'DaemonSet', 'metadata': {'name': 'longhorn-manager', 'namespace': 'longhorn-system', 'generation': 2},
             'spec': {'template': {'spec': {'containers': [
                 {'name': 'longhorn-manager', 'image': 'longhornio/longhorn-manager:v1.13.0'}]}}},
             'status': {'observedGeneration': 2, 'desiredNumberScheduled': 3, 'updatedNumberScheduled': 3,
                        'numberAvailable': 3, 'numberReady': 3}}))
        self.kube.react('GET', '/version', lambda *call: {'gitVersion': 'v1.34.2+k3s1'})

        def preconditions(method, path, body):
            # The setting patch names the uid and resourceVersion it read (two "test" operations).
            found = self.kube.object(path)
            assert body[0]['value'] == found['metadata']['uid']
            assert body[1]['value'] == found['metadata']['resourceVersion']
        for name in (V.AUTO, V.TIMEOUT):
            self.kube.react('PATCH', V.LH + '/settings/' + name, preconditions)
        R.bind(lambda: b'test-upgrade-review-key')
        V.bind(K.kget, K.ksend, lambda force=False: self.platform, lambda: self.installed, C.parse)

    @property
    def sent(self):
        return [(method, path, body, ctype) for method, path, body, ctype in self.kube.writes]

    def edit(self, path, change):
        found = self.kube.object(path)
        change(found)
        self.kube.add(found)

    def remove(self, path):
        """Gone from the cluster; not a write of Homestead's."""
        written = len(self.kube.writes)
        self.kube('DELETE', path)
        del self.kube.writes[written:]

    def value(self, setting):
        return self.kube.object(V.LH + '/settings/' + setting)['value']

    def set_value(self, setting, value):
        self.edit(V.LH + '/settings/' + setting, lambda found: found.update(value=value))

    @staticmethod
    def node(i): return f'/api/v1/nodes/node-{i}'
    @staticmethod
    def replica(i): return f'{V.LH}/replicas/replica-{i}'
    @staticmethod
    def manager(i): return f'{V.LH}/instancemanagers/im-{i}'
    @staticmethod
    def lh_node(i): return f'{V.LH}/nodes/node-{i}'
    @staticmethod
    def pod(i): return f'/api/v1/namespaces/longhorn-system/pods/im-{i}'
    @staticmethod
    def engine(): return f'{V.LH}/engines/engine-1'

    def set_control(self, control):
        self.kube.add(lh('InstanceManagerUpgradeControl', V.CONTROL, **control))

    def active(self, stage='waiting-for-healthy-volumes'):
        spec = {'targetImage': self.value('default-instance-manager-image')}
        self.set_control({'spec': spec, 'status': {'currentNode': 'node-1', 'nodes': {
            'node-1': {'state': 'in-progress', 'imuName': 'upgrade-1'}}}})
        self.kube.add(lh('InstanceManagerUpgrade', 'upgrade-1', spec=spec, status={'state': stage}))

    def body(self, enabled, timeout=60):
        review, _ = V.settings_review(enabled, timeout)
        return {'enabled': enabled, 'timeout': timeout, 'confirm_capacity': True, 'capacity_token': review['capacity_token']}


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        # Cluster() binds both modules to its fake; later tests in the same
        # process must find them as they were.
        bound = {name: getattr(V, name) for name in ('kget', 'ksend', 'platform', 'version', 'parse')}
        key = R._key
        self.addCleanup(lambda: [setattr(V, name, value) for name, value in bound.items()] and None)
        self.addCleanup(setattr, R, '_key', key)
        self.c = Cluster(self)

    def test_live_supported_offline_attached_refused(self):
        r = V.plan('v1.13.0')
        self.assertTrue(r['live_ready'])
        self.assertFalse(r['offline_ready'])
        with self.assertRaisesRegex(ValueError, 'detach'):
            V.require_upgrade('v1.13.0', 'offline')
        self.assertFalse(self.c.sent)

    def test_supported_source_is_not_just_the_manager_version(self):
        self.c.edit(self.c.manager(1), lambda m: m['spec'].update(image='longhornio/longhorn-instance-manager:v1.12.1'))
        self.assertFalse(V.plan('v1.13.0')['live_ready'])
        self.c.installed = 'v1.12.1'
        self.assertIn('1.12.2', ' '.join(V.plan('v1.13.0')['live_blockers']))

    def test_no_minor_skip_or_prerelease_assumption(self):
        self.assertFalse(V.plan('v1.12.2')['live_ready'])
        with self.assertRaises(ValueError): V.plan('v1.13.0-rc1')

    def test_kubernetes_minimum_checks_every_host_and_apiserver(self):
        self.c.edit(self.c.node(2), lambda n: n['status']['nodeInfo'].update(kubeletVersion='v1.33.5+k3s1'))
        self.assertIn('Kubernetes 1.34', ' '.join(V.plan('v1.13.0')['offline_blockers']))
        self.c.edit(self.c.node(2), lambda n: n['status']['nodeInfo'].update(kubeletVersion='unknown'))
        self.assertFalse(V.plan('v1.13.0')['live_ready'])
        read = K.kget
        with patch.object(V, 'kget', side_effect=lambda path: {'gitVersion': 'v1.33.5'} if path == '/version' else read(path)):
            self.assertFalse(V.plan('v1.13.0')['live_ready'])

    def test_actual_rw_replica_topology_is_required(self):
        for i in (1, 2, 3): self.c.edit(self.c.replica(i), lambda r: r['spec'].update(nodeID='node-1'))
        self.assertFalse(V.plan('v1.13.0')['live_ready'])
        for i in (1, 2, 3): self.c.edit(self.c.replica(i), lambda r, i=i: r['spec'].update(nodeID=f'node-{i}'))
        self.c.edit(self.c.engine(), lambda e: e['status'].update(replicaModeMap={'replica-1': 'RW', 'replica-2': 'WO'}))
        self.assertFalse(V.plan('v1.13.0')['live_ready'])

    def test_single_host_frontend_shards_health_and_busy_volume(self):
        cases = [('frontend', 'ublk'), ('frontend', 'iscsi'), ('dataLayout', {'type': 'sharded'}), ('migrationNodeID', 'node-2')]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                c = Cluster(self); c.edit(VOLUME, lambda v: v['spec'].update({field: value}))
                self.assertFalse(V.plan('v1.13.0')['live_ready'])
        c = Cluster(self); c.remove(c.node(2)); c.remove(c.node(3))
        self.assertFalse(V.plan('v1.13.0')['live_ready'])
        c = Cluster(self); c.edit(VOLUME, lambda v: v['status'].update(robustness='degraded'))
        self.assertFalse(V.plan('v1.13.0')['live_ready'])

    def test_target_hosts_need_disk_space_and_running_instance_manager(self):
        self.c.edit(self.c.lh_node(2), lambda n: n['status']['diskStatus']['disk'].update(storageAvailable=0))
        self.c.edit(self.c.manager(3), lambda m: m['status'].update(currentState='stopped'))
        self.assertFalse(V.plan('v1.13.0')['live_ready'])

    def test_offline_waits_for_actual_stopped_replicas(self):
        self.c.edit(VOLUME, lambda v: v.update(status={'state': 'detached'}))
        self.assertFalse(V.plan('v1.13.0')['offline_ready'])
        for i in (1, 2, 3):
            self.c.edit(self.c.replica(i), lambda r: (r['status'].update(currentState='stopped'), r['spec'].update(desiredState='stopped')))
        self.assertTrue(V.plan('v1.13.0')['offline_ready'])
        self.c.installed = 'v1.13.0'
        detail = V.progress({'ref': {'to': 'v1.13.0', 'from': 'v1.12.2', 'v2_mode': 'offline'}})[2]
        self.assertIn('offline', detail)
        self.assertNotIn('paused', detail)

    def test_inventory_failure_does_not_allow_empty_cluster(self):
        for code in (403, 404, 503):
            with patch.object(V, 'kget', side_effect=urllib.error.HTTPError('', code, '', {}, None)):
                with self.assertRaises(ValueError): V.require_upgrade('v1.13.0', 'live')
        with patch.object(V, 'kget', return_value={}):
            with self.assertRaises(ValueError): V.require_upgrade('v1.13.0', 'offline')

    def test_harvester_managed_upgrade_cannot_be_started(self):
        self.c.platform['harvester'] = True
        with self.assertRaisesRegex(ValueError, 'Harvester'): V.require_upgrade('v1.13.0', 'live')

    def test_settings_require_fresh_review_and_preserve_other_engine_keys(self):
        self.c.installed = 'v1.13.0'
        body = self.c.body(True, 90)
        with self.assertRaisesRegex(ValueError, 'Review'):
            V.configure({**body, 'timeout': 120})
        self.assertFalse(self.c.sent)
        V.configure(body)
        self.assertEqual({'v1': 'false', 'v2': 'true'}, json.loads(self.c.value(V.AUTO)))
        self.assertEqual('90', json.loads(self.c.value(V.TIMEOUT))['v2'])
        self.assertTrue(all(p[3] == 'application/json-patch+json' for p in self.c.sent))

    def test_changed_settings_or_version_invalidates_review(self):
        self.c.installed = 'v1.13.0'; body = self.c.body(False)
        self.c.set_value(V.TIMEOUT, '{"v2":"120"}')
        with self.assertRaises(ValueError): V.configure(body)
        self.assertFalse(self.c.sent)

    def test_pausing_degraded_active_upgrade_does_not_wait_for_health(self):
        self.c.installed = 'v1.13.0'; self.c.active(); self.c.edit(VOLUME, lambda v: v['status'].update(robustness='degraded'))
        self.c.set_value(V.AUTO, '{"v2":"true"}')
        V.configure(self.c.body(False))
        self.assertEqual('false', json.loads(self.c.value(V.AUTO))['v2'])
        self.assertTrue(V.observation()['active'])
        with self.assertRaisesRegex(ValueError, 'Wait for'): V.ensure_idle()

    def test_manager_rollout_is_not_engine_upgrade_completion(self):
        self.c.installed = 'v1.13.0'
        item = {'ref': {'to': 'v1.13.0', 'from': 'v1.12.2', 'v2_mode': 'live'}}
        self.assertEqual('running', V.progress(item)[0])
        self.assertTrue(item['ref']['v2_enabled'])
        self.c.active()
        self.assertIn('waiting-for-healthy-volumes', V.progress(item)[2])

    def test_completion_needs_target_pods_ready_and_healthy_volumes(self):
        self.c.installed = 'v1.13.0'
        item = {'ref': {'to': 'v1.13.0', 'from': 'v1.12.2', 'v2_mode': 'live', 'v2_enabled': True}}
        image = self.c.value('default-instance-manager-image')
        for i in (1, 2, 3):
            self.c.edit(self.c.manager(i), lambda m: m['spec'].update(image=image))
        self.assertEqual('running', V.progress(item)[0])
        for i in (1, 2, 3):
            self.c.edit(self.c.pod(i), lambda p: p.update(spec={'containers': [{'image': image}]}, status={
                'conditions': ready(), 'containerStatuses': [{'ready': True, 'image': image}]}))
        self.assertEqual('succeeded', V.progress(item)[0])
        self.c.edit(VOLUME, lambda v: v['status'].update(robustness='degraded'))
        self.assertEqual('running', V.progress(item)[0])

    def test_source_eligibility_checked_again_before_enabling(self):
        self.c.installed = 'v1.13.0'; self.c.edit(VOLUME, lambda v: v['status'].update(robustness='degraded'))
        item = {'ref': {'to': 'v1.13.0', 'from': 'v1.12.2', 'v2_mode': 'live'}}
        self.assertEqual('running', V.progress(item)[0])
        self.assertFalse(self.c.sent)
        self.assertNotIn('v2_enabled', item['ref'])

    def test_not_enabled_before_all_manager_pods_ready(self):
        self.c.installed = 'v1.13.0'; self.c.edit(MANAGER_DS, lambda ds: ds['status'].update(numberReady=2))
        item = {'ref': {'to': 'v1.13.0', 'from': 'v1.12.2', 'v2_mode': 'live'}}
        self.assertEqual(20, V.progress(item)[1]); self.assertFalse(self.c.sent)

    def test_timeout_and_state_validation(self):
        for enabled, timeout in [(None, 60), (True, True), (False, 0), (True, 1441), (False, '60')]:
            with self.subTest(enabled=enabled, timeout=timeout), self.assertRaises(ValueError):
                V.settings_review(enabled, timeout)

    def test_reported_desired_manager_image_is_not_proof_of_running_image(self):
        self.c.edit(self.c.pod(1), lambda p: p['status']['containerStatuses'][0].update(image='longhornio/longhorn-instance-manager:v1.12.1'))
        self.assertFalse(V.plan('v1.13.0')['live_ready'])

    def test_retries_exhausted_is_a_failure_not_a_finished_upgrade(self):
        self.c.installed = 'v1.13.0'; self.c.active('failed')
        self.c.edit(CONTROL, lambda c: c['status'].update(currentNode=''))
        self.c.edit(CONTROL, lambda c: c['status']['nodes']['node-1'].update(state='failed', retryCount=5, errorMsg='Disk full'))
        item = {'ref': {'to': 'v1.13.0', 'from': 'v1.12.2', 'v2_mode': 'live', 'v2_enabled': True}}
        status, _, detail = V.progress(item)
        self.assertEqual('failed', status); self.assertIn('Disk full', detail)

    def test_pausing_does_not_require_volume_inventory(self):
        self.c.installed = 'v1.13.0'; read = K.kget
        def missing_volumes(path):
            if path == V.LH + '/volumes': raise urllib.error.HTTPError(path, 403, '', {}, None)
            return read(path)
        with patch.object(V, 'kget', side_effect=missing_volumes):
            V.configure(self.c.body(False))

    def test_v1_resize_unaffected_but_v2_resize_blocked_during_paused_active_upgrade(self):
        self.c.installed = 'v1.13.0'; self.c.active()
        pvc = {'spec': {'volumeName': 'test-pv'}}
        self.c.kube.add({'apiVersion': 'v1', 'kind': 'PersistentVolume', 'metadata': {'name': 'test-pv'},
                         'spec': {'csi': {'driver': 'driver.longhorn.io', 'volumeHandle': 'test-disk'}}})
        read_v2 = K.kget
        with self.assertRaisesRegex(ValueError, 'Wait for'): V.ensure_resize_idle(pvc, read_v2)
        self.c.edit(VOLUME, lambda v: v['spec'].update(dataEngine='v1'))
        V.ensure_resize_idle(pvc, read_v2)

    def test_vm_with_v2_pvc_is_blocked_before_live_migration(self):
        self.c.installed = 'v1.13.0'; self.c.active()
        for obj in ({'apiVersion': 'kubevirt.io/v1', 'kind': 'VirtualMachineInstance',
                     'metadata': {'name': 'test-vm', 'namespace': 'apps'},
                     'spec': {'volumes': [{'dataVolume': {'name': 'test-claim'}}]}},
                    {'apiVersion': 'v1', 'kind': 'PersistentVolumeClaim',
                     'metadata': {'name': 'test-claim', 'namespace': 'apps'}, 'spec': {'volumeName': 'test-pv'}},
                    {'apiVersion': 'v1', 'kind': 'PersistentVolume', 'metadata': {'name': 'test-pv'},
                     'spec': {'csi': {'driver': 'driver.longhorn.io', 'volumeHandle': 'test-disk'}}}):
            self.c.kube.add(obj)
        with self.assertRaisesRegex(ValueError, 'Wait for'): V.ensure_vm_idle('apps', 'test-vm')
        self.c.edit(VOLUME, lambda v: v['spec'].update(dataEngine='v1'))
        V.ensure_vm_idle('apps', 'test-vm')

    def test_paused_pending_hosts_still_block_expansion_and_migration(self):
        self.c.installed = 'v1.13.0'
        self.c.set_control({'spec': {'targetImage': 'target'}, 'status': {'nodes': {'node-2': {'state': 'pending'}}}})
        with self.assertRaisesRegex(ValueError, 'Wait for'): V.ensure_idle()

    def test_pause_during_manager_rollout_cannot_be_undone_by_job_refresh(self):
        self.c.installed = 'v1.13.0'
        item = {'kind': 'platform-upgrade', 'status': 'running', 'ref': {
            'component': 'longhorn', 'to': 'v1.13.0', 'from': 'v1.12.2', 'v2_mode': 'live'}}
        unrelated = {'kind': 'platform-upgrade', 'status': 'running', 'ref': {'component': 'cdi'}}
        store = [copy.deepcopy(item), unrelated]
        def write(items):
            store[:] = copy.deepcopy(items)
        ops = Store(read=lambda: copy.deepcopy(store), write=write, require_write=lambda: None)
        body = self.c.body(False)
        V.configure(body, ops)
        self.assertTrue(store[0]['ref']['v2_enabled'])
        self.assertEqual(unrelated, store[1])
        V.progress(copy.deepcopy(store[0]))
        self.assertEqual('false', json.loads(self.c.value(V.AUTO))['v2'])
        V.configure(self.c.body(True), ops)
        self.assertEqual('true', json.loads(self.c.value(V.AUTO))['v2'])

    def test_invalid_pause_review_cannot_change_saved_job_intent(self):
        self.c.installed = 'v1.13.0'
        body = self.c.body(False)
        body['confirm_capacity'] = False
        ops = Store(require_write=lambda: None)
        with self.assertRaisesRegex(ValueError, 'Review'):
            V.configure(body, ops)
        self.assertFalse(self.c.sent)

    def test_component_cannot_mutate_helm_before_v2_checks_and_backup_ack(self):
        found = {'name': 'Longhorn', 'installed': 'v1.12.2', 'next': 'v1.13.0'}
        with patch.object(C, '_component', return_value=found), patch.object(C, 'helm_upgrade') as helm:
            with self.assertRaisesRegex(ValueError, 'detach'): C.upgrade('longhorn', 'v1.13.0')
            with self.assertRaisesRegex(ValueError, 'backed up'): C.upgrade('longhorn', 'v1.13.0', {'v2_mode': 'live'})
            helm.assert_not_called()
            result = C.upgrade('longhorn', 'v1.13.0', {'v2_mode': 'live', 'confirm_backup': True})
            self.assertEqual('live', result['v2_mode']); helm.assert_called_once()

    def test_an_unfinished_disk_conversion_refuses_the_upgrade_before_helm_is_touched(self):
        # The conflict rule must hold before the upgrade starts, not when its job is recorded after.
        found = {'name': 'Longhorn', 'installed': 'v1.12.2', 'next': 'v1.13.0'}
        converting = {'kind': 'disk-v2-convert', 'status': 'running', 'ref': {'node': 'node-1', 'phase': 'erasing'}}
        with patch.object(C, '_component', return_value=found), patch.object(C, 'helm_upgrade') as helm, \
                patch.object(C, 'OPS', SimpleNamespace(stored=lambda: [converting]), create=True):
            with self.assertRaisesRegex(ValueError, 'Finish disk preparation'):
                C.upgrade('longhorn', 'v1.13.0', {'v2_mode': 'live', 'confirm_backup': True})
            helm.assert_not_called()


if __name__ == '__main__': unittest.main()
