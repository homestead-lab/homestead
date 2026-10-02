"""Longhorn owns upgrades; reviews and completion require observed evidence."""
import copy
import json
import sys
import threading
from types import SimpleNamespace
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import homestead_components as C
import homestead_lhv2_upgrade as V
import homestead_capacity_review as R


def ready():
    return [{'type': 'Ready', 'status': 'True'}]


class Cluster:
    def __init__(self, installed='v1.12.2'):
        self.installed, self.sent = installed, []
        self.platform = {'distribution': 'k3s'}
        self.nodes = [{'metadata': {'name': f'node-{i}'}, 'status': {
            'nodeInfo': {'kubeletVersion': 'v1.34.2+k3s1'}, 'conditions': ready()}} for i in (1, 2, 3)]
        self.volume = {'metadata': {'name': 'test-disk'}, 'spec': {'dataEngine': 'v2', 'frontend': 'blockdev'},
                       'status': {'state': 'attached', 'robustness': 'healthy', 'currentNodeID': 'node-1'}}
        self.replicas = [{'metadata': {'name': f'replica-{i}'}, 'spec': {'volumeName': 'test-disk', 'nodeID': f'node-{i}'},
                         'status': {'currentState': 'running'}} for i in (1, 2, 3)]
        self.engines = [{'spec': {'volumeName': 'test-disk'}, 'status': {'currentState': 'running',
                        'replicaModeMap': {f'replica-{i}': 'RW' for i in (1, 2, 3)}}}]
        self.managers = [{'metadata': {'name': f'im-{i}'}, 'spec': {'nodeID': f'node-{i}', 'dataEngine': 'v2', 'type': 'aio',
                          'image': 'longhornio/longhorn-instance-manager:v1.12.2'}, 'status': {'currentState': 'running'}} for i in (1, 2, 3)]
        self.lh_nodes = [{'metadata': {'name': f'node-{i}'}, 'spec': {'allowScheduling': True,
                         'disks': {'disk': {'diskType': 'block', 'allowScheduling': True}}},
                         'status': {'conditions': ready(), 'diskStatus': {'disk': {'storageAvailable': 1000000000,
                         'conditions': ready() + [{'type': 'Schedulable', 'status': 'True'}]}}}} for i in (1, 2, 3)]
        self.settings = {name: {'metadata': {'uid': name, 'resourceVersion': '1'}, 'value': json.dumps(values)}
                         for name, values in [(V.AUTO, {'v1': 'false', 'v2': 'false'}), (V.TIMEOUT, {'v2': '60'})]}
        self.settings['default-instance-manager-image'] = {'value': 'longhornio/longhorn-instance-manager:v1.13.0'}
        self.control = None
        self.upgrades = []
        self.pods = [{'metadata': m['metadata'], 'spec': {'containers': [{'image': m['spec']['image']}]},
                      'status': {'conditions': ready(), 'containerStatuses': [{'ready': True, 'image': m['spec']['image']}]}} for m in self.managers]
        self.ds = {'metadata': {'generation': 2}, 'spec': {'template': {'spec': {'containers': [
            {'name': 'longhorn-manager', 'image': 'longhornio/longhorn-manager:v1.13.0'}]}}},
            'status': {'observedGeneration': 2, 'desiredNumberScheduled': 3, 'updatedNumberScheduled': 3,
                       'numberAvailable': 3, 'numberReady': 3}}
        R.bind(lambda: b'test-upgrade-review-key')
        V.bind(self.get, self.send, lambda force=False: self.platform, lambda: self.installed, C.parse)

    def get(self, path):
        objects = {'/api/v1/nodes': {'items': self.nodes}, '/version': {'gitVersion': 'v1.34.2+k3s1'},
                   V.LH + '/volumes': {'items': [self.volume]}, V.LH + '/replicas': {'items': self.replicas},
                   V.LH + '/engines': {'items': self.engines}, V.LH + '/nodes': {'items': self.lh_nodes},
                   V.LH + '/instancemanagers': {'items': self.managers}, V.LH + '/instancemanagerupgrades': {'items': self.upgrades},
                   '/apis/apps/v1/namespaces/longhorn-system/daemonsets/longhorn-manager': self.ds,
                   '/api/v1/namespaces/longhorn-system/pods': {'items': self.pods}}
        if self.control is not None:
            objects[V.LH + '/instancemanagerupgradecontrols/' + V.CONTROL] = self.control
        for name, setting in self.settings.items():
            objects[V.LH + '/settings/' + name] = setting
        if path in objects:
            return copy.deepcopy(objects[path])
        raise urllib.error.HTTPError(path, 404, 'missing', {}, None)

    def send(self, method, path, body, **kwargs):
        self.sent.append((method, path, body, kwargs))
        setting = self.settings[path.rsplit('/', 1)[-1]]
        self.assert_tests(body, setting)
        setting['value'] = body[-1]['value']
        setting['metadata']['resourceVersion'] = str(int(setting['metadata']['resourceVersion']) + 1)

    @staticmethod
    def assert_tests(body, obj):
        assert body[0]['value'] == obj['metadata']['uid']
        assert body[1]['value'] == obj['metadata']['resourceVersion']

    def body(self, enabled, timeout=60):
        review, _ = V.settings_review(enabled, timeout)
        return {'enabled': enabled, 'timeout': timeout, 'confirm_capacity': True, 'capacity_token': review['capacity_token']}

    def active(self, stage='waiting-for-healthy-volumes'):
        self.control = {'spec': {'targetImage': self.settings['default-instance-manager-image']['value']},
                        'status': {'currentNode': 'node-1', 'nodes': {'node-1': {'state': 'in-progress', 'imuName': 'upgrade-1'}}}}
        self.upgrades = [{'metadata': {'name': 'upgrade-1'}, 'spec': self.control['spec'], 'status': {'state': stage}}]


class UpgradeTests(unittest.TestCase):
    def setUp(self): self.c = Cluster()

    def test_live_supported_offline_attached_refused(self):
        r = V.plan('v1.13.0')
        self.assertTrue(r['live_ready'])
        self.assertFalse(r['offline_ready'])
        with self.assertRaisesRegex(ValueError, 'detach'):
            V.require_upgrade('v1.13.0', 'offline')
        self.assertFalse(self.c.sent)

    def test_supported_source_is_not_just_the_manager_version(self):
        self.c.managers[0]['spec']['image'] = 'longhornio/longhorn-instance-manager:v1.12.1'
        self.assertFalse(V.plan('v1.13.0')['live_ready'])
        self.c.installed = 'v1.12.1'
        self.assertIn('1.12.2', ' '.join(V.plan('v1.13.0')['live_blockers']))

    def test_no_minor_skip_or_prerelease_assumption(self):
        self.assertFalse(V.plan('v1.12.2')['live_ready'])
        with self.assertRaises(ValueError): V.plan('v1.13.0-rc1')

    def test_kubernetes_minimum_checks_every_host_and_apiserver(self):
        self.c.nodes[1]['status']['nodeInfo']['kubeletVersion'] = 'v1.33.5+k3s1'
        self.assertIn('Kubernetes 1.34', ' '.join(V.plan('v1.13.0')['offline_blockers']))
        self.c.nodes[1]['status']['nodeInfo']['kubeletVersion'] = 'unknown'
        self.assertFalse(V.plan('v1.13.0')['live_ready'])
        read = self.c.get
        with patch.object(V, 'kget', side_effect=lambda path: {'gitVersion': 'v1.33.5'} if path == '/version' else read(path)):
            self.assertFalse(V.plan('v1.13.0')['live_ready'])

    def test_actual_rw_replica_topology_is_required(self):
        for r in self.c.replicas: r['spec']['nodeID'] = 'node-1'
        self.assertFalse(V.plan('v1.13.0')['live_ready'])
        for i, r in enumerate(self.c.replicas, 1): r['spec']['nodeID'] = f'node-{i}'
        self.c.engines[0]['status']['replicaModeMap'] = {'replica-1': 'RW', 'replica-2': 'WO'}
        self.assertFalse(V.plan('v1.13.0')['live_ready'])

    def test_single_host_frontend_shards_health_and_busy_volume(self):
        cases = [('frontend', 'ublk'), ('frontend', 'iscsi'), ('dataLayout', {'type': 'sharded'}), ('migrationNodeID', 'node-2')]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                c = Cluster(); c.volume['spec'][field] = value
                self.assertFalse(V.plan('v1.13.0')['live_ready'])
        c = Cluster(); c.nodes = c.nodes[:1]
        self.assertFalse(V.plan('v1.13.0')['live_ready'])
        c = Cluster(); c.volume['status']['robustness'] = 'degraded'
        self.assertFalse(V.plan('v1.13.0')['live_ready'])

    def test_target_hosts_need_disk_space_and_running_instance_manager(self):
        self.c.lh_nodes[1]['status']['diskStatus']['disk']['storageAvailable'] = 0
        self.c.managers[2]['status']['currentState'] = 'stopped'
        self.assertFalse(V.plan('v1.13.0')['live_ready'])

    def test_offline_waits_for_actual_stopped_replicas(self):
        self.c.volume['status'] = {'state': 'detached'}
        self.assertFalse(V.plan('v1.13.0')['offline_ready'])
        for r in self.c.replicas:
            r['status']['currentState'] = r['spec']['desiredState'] = 'stopped'
        self.assertTrue(V.plan('v1.13.0')['offline_ready'])

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
        self.assertEqual({'v1': 'false', 'v2': 'true'}, json.loads(self.c.settings[V.AUTO]['value']))
        self.assertEqual('90', json.loads(self.c.settings[V.TIMEOUT]['value'])['v2'])
        self.assertTrue(all(p[3]['ctype'] == 'application/json-patch+json' for p in self.c.sent))

    def test_changed_settings_or_version_invalidates_review(self):
        self.c.installed = 'v1.13.0'; body = self.c.body(False)
        self.c.settings[V.TIMEOUT]['value'] = '{"v2":"120"}'
        with self.assertRaises(ValueError): V.configure(body)
        self.assertFalse(self.c.sent)

    def test_pausing_degraded_active_upgrade_does_not_wait_for_health(self):
        self.c.installed = 'v1.13.0'; self.c.active(); self.c.volume['status']['robustness'] = 'degraded'
        self.c.settings[V.AUTO]['value'] = '{"v2":"true"}'
        V.configure(self.c.body(False))
        self.assertEqual('false', json.loads(self.c.settings[V.AUTO]['value'])['v2'])
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
        for m in self.c.managers:
            m['spec']['image'] = self.c.settings['default-instance-manager-image']['value']
        self.assertEqual('running', V.progress(item)[0])
        self.c.pods = [{'metadata': m['metadata'], 'spec': {'containers': [{'image': m['spec']['image']}]},
                        'status': {'conditions': ready(), 'containerStatuses': [{'ready': True, 'image': m['spec']['image']}]}} for m in self.c.managers]
        self.assertEqual('succeeded', V.progress(item)[0])
        self.c.volume['status']['robustness'] = 'degraded'
        self.assertEqual('running', V.progress(item)[0])

    def test_source_eligibility_checked_again_before_enabling(self):
        self.c.installed = 'v1.13.0'; self.c.volume['status']['robustness'] = 'degraded'
        item = {'ref': {'to': 'v1.13.0', 'from': 'v1.12.2', 'v2_mode': 'live'}}
        self.assertEqual('running', V.progress(item)[0])
        self.assertFalse(self.c.sent)
        self.assertNotIn('v2_enabled', item['ref'])

    def test_not_enabled_before_all_manager_pods_ready(self):
        self.c.installed = 'v1.13.0'; self.c.ds['status']['numberReady'] = 2
        item = {'ref': {'to': 'v1.13.0', 'from': 'v1.12.2', 'v2_mode': 'live'}}
        self.assertEqual(20, V.progress(item)[1]); self.assertFalse(self.c.sent)

    def test_timeout_and_state_validation(self):
        for enabled, timeout in [(None, 60), (True, True), (False, 0), (True, 1441), (False, '60')]:
            with self.subTest(enabled=enabled, timeout=timeout), self.assertRaises(ValueError):
                V.settings_review(enabled, timeout)

    def test_reported_desired_manager_image_is_not_proof_of_running_image(self):
        self.c.pods[0]['status']['containerStatuses'][0]['image'] = 'longhornio/longhorn-instance-manager:v1.12.1'
        self.assertFalse(V.plan('v1.13.0')['live_ready'])

    def test_retries_exhausted_is_a_failure_not_a_finished_upgrade(self):
        self.c.installed = 'v1.13.0'; self.c.active('failed')
        self.c.control['status']['currentNode'] = ''
        self.c.control['status']['nodes']['node-1'].update(state='failed', retryCount=5, errorMsg='Disk full')
        item = {'ref': {'to': 'v1.13.0', 'from': 'v1.12.2', 'v2_mode': 'live', 'v2_enabled': True}}
        status, _, detail = V.progress(item)
        self.assertEqual('failed', status); self.assertIn('Disk full', detail)

    def test_pausing_does_not_require_volume_inventory(self):
        self.c.installed = 'v1.13.0'; read = self.c.get
        def missing_volumes(path):
            if path == V.LH + '/volumes': raise urllib.error.HTTPError(path, 403, '', {}, None)
            return read(path)
        with patch.object(V, 'kget', side_effect=missing_volumes):
            V.configure(self.c.body(False))

    def test_v1_resize_unaffected_but_v2_resize_blocked_during_paused_active_upgrade(self):
        self.c.installed = 'v1.13.0'; self.c.active()
        pvc = {'spec': {'volumeName': 'test-pv'}}
        def read_v2(path):
            return {'spec': {'csi': {'driver': 'driver.longhorn.io', 'volumeHandle': 'test-disk'}}} if path.endswith('/test-pv') else self.c.volume
        with self.assertRaisesRegex(ValueError, 'Wait for'): V.ensure_resize_idle(pvc, read_v2)
        self.c.volume['spec']['dataEngine'] = 'v1'
        V.ensure_resize_idle(pvc, read_v2)

    def test_vm_with_v2_pvc_is_blocked_before_live_migration(self):
        self.c.installed = 'v1.13.0'; self.c.active(); read = self.c.get
        objects = {'/apis/kubevirt.io/v1/namespaces/apps/virtualmachineinstances/test-vm': {'spec': {'volumes': [{'dataVolume': {'name': 'test-claim'}}]}},
                   '/api/v1/namespaces/apps/persistentvolumeclaims/test-claim': {'spec': {'volumeName': 'test-pv'}},
                   '/api/v1/persistentvolumes/test-pv': {'spec': {'csi': {'driver': 'driver.longhorn.io', 'volumeHandle': 'test-disk'}}},
                   V.LH + '/volumes/test-disk': self.c.volume}
        with patch.object(V, 'kget', side_effect=lambda path: objects[path] if path in objects else read(path)):
            with self.assertRaisesRegex(ValueError, 'Wait for'): V.ensure_vm_idle('apps', 'test-vm')
            self.c.volume['spec']['dataEngine'] = 'v1'
            V.ensure_vm_idle('apps', 'test-vm')

    def test_paused_pending_hosts_still_block_expansion_and_migration(self):
        self.c.installed = 'v1.13.0'
        self.c.control = {'spec': {'targetImage': 'target'}, 'status': {'nodes': {'node-2': {'state': 'pending'}}}}
        with self.assertRaisesRegex(ValueError, 'Wait for'): V.ensure_idle()

    def test_pause_during_manager_rollout_cannot_be_undone_by_job_refresh(self):
        self.c.installed = 'v1.13.0'
        item = {'kind': 'platform-upgrade', 'status': 'running', 'ref': {
            'component': 'longhorn', 'to': 'v1.13.0', 'from': 'v1.12.2', 'v2_mode': 'live'}}
        unrelated = {'kind': 'platform-upgrade', 'status': 'running', 'ref': {'component': 'cdi'}}
        store = [copy.deepcopy(item), unrelated]
        def write(items):
            store[:] = copy.deepcopy(items)
        ops = SimpleNamespace(_lock=threading.RLock(), require_write=lambda: None,
                              _read=lambda: copy.deepcopy(store), _write=write)
        body = self.c.body(False)
        V.configure(body, ops)
        self.assertTrue(store[0]['ref']['v2_enabled'])
        self.assertEqual(unrelated, store[1])
        V.progress(copy.deepcopy(store[0]))
        self.assertEqual('false', json.loads(self.c.settings[V.AUTO]['value'])['v2'])
        V.configure(self.c.body(True), ops)
        self.assertEqual('true', json.loads(self.c.settings[V.AUTO]['value'])['v2'])

    def test_invalid_pause_review_cannot_change_saved_job_intent(self):
        self.c.installed = 'v1.13.0'
        body = self.c.body(False)
        body['confirm_capacity'] = False
        ops = SimpleNamespace(_lock=threading.RLock(), require_write=lambda: None)
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


if __name__ == '__main__': unittest.main()
