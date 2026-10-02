"""Host setup is reviewed, durable, and never turns zero capacity into readiness."""
import copy
import os
import re
import shutil
import subprocess
import tempfile
import sys
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import homestead_lhv2_setup as V

BOOT = '01234567-89ab-cdef-0123-456789abcdef'


class Operations:
    TERMINAL = {'succeeded', 'failed', 'cancelled'}
    def __init__(self):
        self._lock = threading.RLock()
        self.items = []
    def _read(self): return self.items
    def _public(self, item): return {k: v for k, v in item.items() if k != 'ref'}
    def start(self, kind, title, resource, href, ref, message):
        item = dict(id=str(len(self.items)), kind=kind, ref=ref, status='queued')
        self.items.append(item)
        return self._public(item)


class Cluster:
    def __init__(self):
        self.ops = Operations()
        self.node = {'metadata': {'name': 'k3s-test', 'uid': 'node-uid'}, 'status': {
            'nodeInfo': {'bootID': BOOT, 'operatingSystem': 'linux'},
            'conditions': [{'type': 'Ready', 'status': 'True'}], 'capacity': {}, 'allocatable': {}}}
        self.settings = {'v2-data-engine': {'value': 'false'}}
        self.jobs, self.pods, self.managers, self.sent = [], [], [], []
        self.base = {'distribution': 'k3s', 'longhorn_ok': True, 'nodes': [{'name': 'k3s-test', 'checks': {'cpu': True, 'modules': False}}]}
        self.harvester = None
        V.bind(self.get, self.send, lambda: self.base, self.ops, 'lab')
    def get(self, path):
        if path == '/api/v1/nodes': return {'items': [self.node]}
        if path == '/api/v1/nodes/k3s-test': return self.node
        if path == '/api/v1/pods': return {'items': self.pods}
        if path == V.LH + '/nodes': return {'items': [{'metadata': {'name': 'k3s-test'}}]}
        if path == V.LH + '/instancemanagers': return {'items': self.managers}
        if path.startswith(V.LH + '/settings/'):
            if path.rsplit('/', 1)[-1] in self.settings: return self.settings[path.rsplit('/', 1)[-1]]
        if path == V.CAP.HARVESTER_V2 and self.harvester is not None: return self.harvester
        if '/jobs?' in path: return {'items': self.jobs}
        if '/jobs/' in path:
            for job in self.jobs:
                if job['metadata']['name'] == path.rsplit('/', 1)[-1]: return job
        raise urllib.error.HTTPError(path, 404, 'missing', None, None)
    def send(self, method, path, body):
        self.sent.append((method, path, body))
        assert self.ops.items, 'Persist approval before Kubernetes mutation'
        self.jobs.append(copy.deepcopy(body))
        return self.jobs[-1]
    def request(self):
        row = V.plan()['nodes'][0]
        return {'node': row['node'], 'review_token': row['review_token'], 'confirm': True, 'request_id': 'a' * 24}
    def configure(self):
        V.prepare(self.request())
        self.jobs[0]['status'] = {'succeeded': 1}
        self.ops.items[0]['status'] = 'succeeded'
    def capacity(self, quantity='2Gi'):
        for key in ('capacity', 'allocatable'): self.node['status'][key]['hugepages-2Mi'] = quantity


class SetupTests(unittest.TestCase):
    def setUp(self): self.c = Cluster()
    def test_zero_capacity_is_actionable_but_cannot_enable(self):
        plan = V.plan()
        self.assertFalse(plan['can_enable'])
        self.assertTrue(plan['nodes'][0]['can_prepare'])
        self.assertIn('0 MiB capacity', ' '.join(plan['blockers']))
        with patch.object(V.CAP, 'save') as save, self.assertRaises(ValueError):
            V.enable({'confirm': True, 'review_token': plan['review_token']})
        save.assert_not_called()
    def test_host_job_is_durable_pinned_and_does_not_restart_or_touch_disks(self):
        self.c.configure()
        pod = self.c.jobs[0]['spec']['template']['spec']
        self.assertEqual('k3s-test', pod['nodeName'])
        self.assertFalse(pod['automountServiceAccountToken'])
        self.assertTrue(pod['securityContext']['privileged'] if 'securityContext' in pod else pod['containers'][0]['securityContext']['privileged'])
        script = pod['containers'][0]['command'][-1]
        self.assertIn(BOOT, script)
        self.assertIn('required=1024', script)
        self.assertNotIn('systemctl restart', script)
        self.assertNotIn('mkfs', script)
        self.assertNotIn('shutdown ', script)
        self.assertEqual('succeeded', V.progress(self.c.ops.items[0])[0])
        row = V.plan()['nodes'][0]
        self.assertTrue(row['needs_reboot'])
        self.assertFalse(V.plan()['can_enable'])
    def test_capacity_and_current_host_proof_enable_without_requiring_a_disk(self):
        self.c.configure(); self.c.capacity()
        plan = V.plan()
        self.assertTrue(plan['can_enable'])
        self.assertFalse(plan['engine_ready'])
        with patch.object(V.CAP, 'save') as save:
            V.enable({'confirm': True, 'review_token': plan['review_token']})
        save.assert_called_once_with({'v2': True}, allow_v2_enable=True)
    def test_reboot_requires_fresh_module_observations(self):
        self.c.configure(); self.c.capacity()
        self.c.node['status']['nodeInfo']['bootID'] = 'a' * 36
        self.assertFalse(V.plan()['can_enable'])
        self.c.base['nodes'][0]['checks']['modules'] = True
        self.assertTrue(V.plan()['can_enable'])
    def test_preparation_requires_confirmation_and_current_host_identity(self):
        body = self.c.request()
        for key, value in [('confirm', False), ('review_token', 'old'), ('node', 'other'), ('request_id', ';reboot')]:
            with self.subTest(key=key), self.assertRaises(ValueError): V.prepare({**body, key: value})
        self.c.node['metadata']['uid'] = 'replacement'
        with self.assertRaisesRegex(ValueError, 'changed'): V.prepare(body)
        self.assertEqual([], self.c.sent)
    def test_duplicate_request_returns_saved_operation_and_new_request_is_blocked(self):
        body = self.c.request()
        first = V.prepare(body)
        self.assertEqual(first, V.prepare(body))
        with self.assertRaisesRegex(ValueError, 'already active'): V.prepare({**body, 'request_id': 'b' * 24})
        self.assertEqual(1, len(self.c.jobs))
    def test_maintenance_excludes_host_preparation(self):
        for kind in ('node-power', 'host-os'):
            self.c.ops.items = [{'kind': kind, 'status': 'running', 'ref': {'node': 'k3s-test'}}]
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, 'already active'): V.prepare(self.c.request())
        self.assertEqual([], self.c.sent)
    def test_missing_job_is_never_replayed_by_observer(self):
        V.prepare(self.c.request()); self.c.jobs.clear()
        self.assertEqual('failed', V.progress(self.c.ops.items[0])[0])
        self.assertEqual(1, len(self.c.sent))
    def test_altered_or_failed_job_never_proves_configuration(self):
        self.c.configure(); self.c.capacity()
        self.c.jobs[0]['spec']['template']['spec']['containers'][0]['command'] = ['true']
        self.assertFalse(V.plan()['nodes'][0]['configured'])
        self.assertEqual('failed', V.progress(self.c.ops.items[0])[0])
        self.c.jobs[0] = V._body(self.c.ops.items[0]['ref'])
        self.c.jobs[0]['status'] = {'conditions': [{'type': 'Failed', 'status': 'True'}]}
        self.assertEqual('failed', V.progress(self.c.ops.items[0])[0])
        self.assertFalse(V.plan()['can_enable'])
    def test_external_job_without_saved_approval_is_not_proof(self):
        self.c.configure(); self.c.capacity(); self.c.ops.items.clear()
        self.assertFalse(V.plan()['can_enable'])
    def test_changed_memory_requirement_invalidates_enable_review(self):
        self.c.configure(); self.c.capacity(); plan = V.plan()
        self.c.settings['data-engine-memory-size'] = {'value': '{"v2":"4096"}'}
        with patch.object(V.CAP, 'save') as save, self.assertRaisesRegex(ValueError, 'changed'):
            V.enable({'confirm': True, 'review_token': plan['review_token']})
        save.assert_not_called()
        self.assertEqual(4096, V.plan()['required_mib'])
    def test_old_setting_and_disabled_hugepages_are_respected(self):
        self.c.settings['v2-data-engine-hugepage-limit'] = {'value': '4096'}
        self.assertEqual(2048, V.plan()['nodes'][0]['target_pages'])
        self.c.settings['data-engine-hugepage-enabled'] = {'value': '{"v2":"false"}'}
        self.c.configure()
        self.assertEqual(0, V.plan()['required_mib'])
        self.assertTrue(V.plan()['can_enable'])
    def test_other_pod_hugepage_requests_are_preserved_and_subtracted(self):
        self.c.pods = [{'spec': {'nodeName': 'k3s-test', 'containers': [{'resources': {'requests': {'hugepages-2Mi': '1Gi'}}}]}}]
        self.assertEqual(1536, V.plan()['nodes'][0]['target_pages'])
        self.c.configure(); self.c.capacity()
        self.assertFalse(V.plan()['can_enable'])
        self.c.capacity('3Gi')
        self.assertTrue(V.plan()['can_enable'])
    def test_unknown_inventory_and_missing_harvester_setting_fail_closed(self):
        self.c.base['distribution'] = 'harvester'
        with self.assertRaisesRegex(ValueError, 'Harvester'): V.plan()
        self.c.base['distribution'] = 'k3s'
        original = self.c.get
        def read(path):
            if path == '/api/v1/nodes': return {'items': [], 'metadata': {'continue': 'next'}}
            return original(path)
        with patch.object(V, 'kget', read), self.assertRaisesRegex(ValueError, 'incomplete'): V.plan()
    def test_harvester_owns_setup_and_cannot_run_generic_host_jobs(self):
        self.c.base['distribution'] = 'harvester'; self.c.harvester = {'value': 'false'}
        plan = V.plan()
        self.assertTrue(plan['can_enable'])
        self.assertFalse(plan['nodes'][0]['can_prepare'])
        with self.assertRaises(ValueError): V.prepare(self.c.request())
        self.c.harvester['value'] = 'true'
        self.assertFalse(V.plan()['can_enable'])
    def test_engine_is_ready_only_after_running_manager_is_observed(self):
        self.c.settings['v2-data-engine']['value'] = 'true'
        self.assertFalse(V.plan()['engine_ready'])
        self.c.managers = [{'spec': {'nodeID': 'k3s-test', 'dataEngine': 'v2'}, 'status': {'currentState': 'running'}}]
        self.assertTrue(V.plan()['engine_ready'])
    def test_host_tasks_cannot_be_cancelled_through_direct_api(self):
        import homestead_operations as operations
        self.assertFalse(operations._plan_for({'kind': V.KIND, 'status': 'running'})['can'])

    def test_admin_routes_are_explicit(self):
        from homestead_route_policy import POLICY
        for method, suffix in [('GET', 'plan'), ('POST', 'prepare'), ('POST', 'enable')]:
            self.assertEqual('admin', POLICY[(method, '/api/longhorn/v2/' + suffix)])

class HostScriptTests(unittest.TestCase):
    def setUp(self):
        self.bash = str(Path(os.environ.get('ProgramFiles', 'C:/Program Files')) / 'Git/bin/bash.exe') if os.name == 'nt' else shutil.which('bash')
        if not self.bash or not Path(self.bash).exists(): self.skipTest('bash unavailable')
        self.temp = tempfile.TemporaryDirectory(prefix='v2-host-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for directory in ('proc/sys/kernel/random', 'etc', 'run/systemd/system', 'bin', 'sys/kernel/mm/hugepages/hugepages-2048kB'):
            (self.root / directory).mkdir(parents=True, exist_ok=True)
        self.pool = self.root / 'sys/kernel/mm/hugepages/hugepages-2048kB'
        (self.root / 'proc/sys/kernel/random/boot_id').write_text(BOOT)
        (self.root / 'proc/cpuinfo').write_text('flags : sse4_2\n')
        (self.root / 'proc/meminfo').write_text('Hugepagesize: 2048 kB\nMemAvailable: 8388608 kB\n')
        (self.root / 'etc/os-release').write_text('ID=ubuntu\n')
        for name in ('nr_hugepages', 'free_hugepages', 'resv_hugepages'): (self.pool / name).write_text('0')
        for name, body in {'uname': 'echo x86_64', 'nvme': 'exit 0', 'modprobe': 'exit 0',
                           'sysctl': 'value=${2#*=}; [ -z "$TEST_PARTIAL" ] || value=512; printf "%s" "$value" > "$TEST_ROOT/sys/kernel/mm/hugepages/hugepages-2048kB/nr_hugepages"'}.items():
            shim = self.root / 'bin' / name
            shim.write_text('#!/bin/sh\n' + body + '\n', encoding='utf-8')
            shim.chmod(0o755)
    def run_host(self, partial=False):
        root = self.root.as_posix()
        script = V.SCRIPT.replace('__BOOT__', BOOT).replace('__PAGES__', '1024').replace('__V2PAGES__', '1024')
        script = re.sub(r'(?<![a-zA-Z0-9_/])/(proc|etc|sys|run)/', lambda m: root + m.group(0), script)
        path = self.root / 'run.sh'; path.write_text(script, encoding='utf-8')
        env = {**os.environ, 'PATH': root + '/bin:/usr/bin:/bin', 'TEST_ROOT': root, 'TEST_PARTIAL': '1' if partial else ''}
        return subprocess.run([self.bash, path.as_posix()], env=env, capture_output=True, text=True, timeout=10)
    def test_existing_larger_pool_and_persisted_target_are_never_reduced(self):
        (self.pool / 'nr_hugepages').write_text('2048'); (self.pool / 'free_hugepages').write_text('2048')
        config = self.root / 'etc/sysctl.d/99-homestead-longhorn-v2.conf'
        config.parent.mkdir(); config.write_text('vm.nr_hugepages=3072\n')
        result = self.run_host()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual('vm.nr_hugepages=3072\n', config.read_text())
    def test_low_memory_refuses_reservation_before_writing_sysctl(self):
        (self.root / 'proc/meminfo').write_text('Hugepagesize: 2048 kB\nMemAvailable: 1048576 kB\n')
        result = self.run_host()
        self.assertNotEqual(0, result.returncode)
        self.assertIn('Insufficient available RAM', result.stderr)
        self.assertFalse((self.root / 'etc/sysctl.d/99-homestead-longhorn-v2.conf').exists())
        self.assertEqual('0', (self.pool / 'nr_hugepages').read_text())
    def test_partial_allocation_saves_configuration_and_explicitly_requires_reboot(self):
        result = self.run_host(partial=True)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn('reviewed host reboot is required', result.stdout)
        self.assertEqual('vm.nr_hugepages=1024\n', (self.root / 'etc/sysctl.d/99-homestead-longhorn-v2.conf').read_text())
    def test_a_one_gib_default_pool_is_not_modified(self):
        (self.root / 'proc/meminfo').write_text('Hugepagesize: 1048576 kB\nMemAvailable: 8388608 kB\n')
        result = self.run_host()
        self.assertNotEqual(0, result.returncode)
        self.assertIn('not 2 MiB', result.stderr)
        self.assertEqual('0', (self.pool / 'nr_hugepages').read_text())

if __name__ == '__main__': unittest.main()
