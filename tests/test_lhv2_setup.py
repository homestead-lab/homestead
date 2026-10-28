"""Host setup is reviewed, durable, and never turns zero capacity into readiness."""
import os
import re
import shutil
import subprocess
import tempfile
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import homestead_kube as K
import homestead_lhv2_setup as V
import homestead_changes as CHANGES
import homestead_host_access as HOSTACCESS
import homestead_operations as OPS
from fakes.job_store import JobStore
from fakes.kube import Kube

BOOT = '01234567-89ab-cdef-0123-456789abcdef'
JOBS = f'/apis/batch/v1/namespaces/{V.NS}/jobs'


class Operations(JobStore):
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
    """The host, Longhorn's settings and managers and the setup Jobs, on the
    shared fake API (tests/fakes/kube.py)."""
    def __init__(self, case):
        self.ops = Operations()
        # The shared fake runs the real write guards; the storage guard takes the
        # job store's lock, which lives under its DATA_DIR.
        case.enterContext(patch.object(OPS, 'DATA_DIR', case.enterContext(tempfile.TemporaryDirectory())))
        # No signed-in person on this thread, whatever an earlier test left behind: the write guards read who is asking.
        case.enterContext(patch.object(HOSTACCESS, 'role', lambda: None))
        case.enterContext(patch.object(CHANGES.REQUEST, 'user', None, create=True))
        self.kube = case.enterContext(Kube(
            {'apiVersion': 'v1', 'kind': 'Node', 'metadata': {'name': 'k3s-test', 'uid': 'node-uid'}, 'status': {
                'nodeInfo': {'bootID': BOOT, 'operatingSystem': 'linux'},
                'conditions': [{'type': 'Ready', 'status': 'True'}], 'capacity': {}, 'allocatable': {}}},
            {'apiVersion': 'longhorn.io/v1beta2', 'kind': 'Node',
             'metadata': {'name': 'k3s-test', 'namespace': 'longhorn-system'}}))
        self.setting('v2-data-engine', 'false')
        self.base = {'distribution': 'k3s', 'longhorn_ok': True, 'nodes': [{'name': 'k3s-test', 'checks': {'cpu': True, 'modules': False}}]}
        for name in ('kget', 'ksend', 'status', 'ops', 'NS'):
            case.addCleanup(setattr, V, name, getattr(V, name))
        V.bind(K.kget, K.ksend, lambda: self.base, self.ops, 'lab')

        def approved_first(method, path, body):
            assert self.ops.items, 'Persist approval before Kubernetes mutation'
        self.kube.react('POST', JOBS, approved_first)

    # What the tests change on the cluster, as the API server would keep it.
    @property
    def sent(self):
        return [(method, path, body) for method, path, body, _ in self.kube.writes]
    def change(self, path, change):
        found = self.kube.object(path)
        change(found)
        self.kube.add(found)
    def change_node(self, change): self.change('/api/v1/nodes/k3s-test', change)
    def setting(self, name, value):
        self.kube.add({'apiVersion': 'longhorn.io/v1beta2', 'kind': 'Setting',
                       'metadata': {'name': name, 'namespace': 'longhorn-system'}, 'value': value})
    def pods(self, *pods):
        """The only pods there are."""
        for number, pod in enumerate(pods):
            self.kube.add({'apiVersion': 'v1', 'kind': 'Pod', 'metadata': {'name': f'pod-{number}', 'namespace': 'lab'}, **pod})
    def managers(self, *managers):
        for number, manager in enumerate(managers):
            self.kube.add({'apiVersion': 'longhorn.io/v1beta2', 'kind': 'InstanceManager',
                           'metadata': {'name': f'manager-{number}', 'namespace': 'longhorn-system'}, **manager})
    def harvester(self, value):
        self.kube.add({'apiVersion': 'harvesterhci.io/v1beta1', 'kind': 'Setting',
                       'metadata': {'name': 'longhorn-v2-data-engine-enabled'}, 'value': value})
    def jobs(self): return K.kget(JOBS)['items']
    def job(self): return K.kget(JOBS)['items'][0]
    def change_job(self, change):
        job = self.job()
        self.change(f"{JOBS}/{job['metadata']['name']}", change)
    def drop_jobs(self):
        """Every Job gone (a TTL clean-up, say); not a write of Homestead's."""
        written = len(self.kube.writes)
        self.kube('DELETE', JOBS)
        del self.kube.writes[written:]
    def request(self):
        row = V.plan()['nodes'][0]
        return {'node': row['node'], 'review_token': row['review_token'], 'confirm': True, 'request_id': 'a' * 24}
    def configure(self):
        V.prepare(self.request())
        self.change_job(lambda job: job.update(status={'succeeded': 1}))
        self.ops.items[0]['status'] = 'succeeded'
    def capacity(self, quantity='2Gi'):
        def set_quantity(node):
            for key in ('capacity', 'allocatable'): node['status'][key]['hugepages-2Mi'] = quantity
        self.change_node(set_quantity)


class SetupTests(unittest.TestCase):
    def setUp(self): self.c = Cluster(self)
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
        pod = self.c.job()['spec']['template']['spec']
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
    def cpus(self, n, mask='{"v2":"0x3"}', isolation='{"v2":"true"}'):
        self.c.change_node(lambda node: node['status']['capacity'].update(cpu=str(n)))
        self.c.setting('data-engine-cpu-mask', mask)
        self.c.setting('data-engine-cpu-isolation-enabled', isolation)

    def test_two_cpu_hosts_get_one_polling_core_when_v2_is_enabled(self):
        # Longhorn 1.13 polls with two cores and keeps them: on a 2-CPU host
        # SPDK refuses to start at all.
        self.c.configure(); self.c.capacity(); self.cpus(2)
        plan = V.plan()
        self.assertTrue(plan['can_enable'])
        self.assertEqual({'from': '{"v2":"0x3"}', 'to': '{"v2": "0x1"}', 'cores': 1, 'was': 2, 'host_cpus': 2}, plan['cpu_mask_fix'])
        with patch.object(V.CAP, 'save') as save:
            V.enable({'confirm': True, 'review_token': plan['review_token']})
        self.assertIn(('PUT', V.LH + '/settings/data-engine-cpu-mask', '{"v2": "0x1"}'),
                      [(m, p, b['value']) for m, p, b in self.c.sent if m == 'PUT'])
        self.assertEqual('{"v2": "0x1"}', self.c.kube.object(V.LH + '/settings/data-engine-cpu-mask')['value'])
        save.assert_called_once_with({'v2': True}, allow_v2_enable=True)

    def test_hosts_with_cpus_to_spare_keep_longhorns_mask(self):
        self.c.configure(); self.c.capacity(); self.cpus(4)
        self.assertIsNone(V.plan()['cpu_mask_fix'])
        self.cpus(2, isolation='{"v2":"false"}')
        self.assertIsNone(V.plan()['cpu_mask_fix'], "without isolation SPDK shares the cores")

    def test_a_one_cpu_host_cannot_run_v2(self):
        self.c.configure(); self.c.capacity(); self.cpus(1)
        plan = V.plan()
        self.assertFalse(plan['can_enable'])
        self.assertIn('needs at least 2 CPUs', ' '.join(plan['blockers']))

    def test_a_host_without_cpu_room_for_v2s_instance_manager_says_so(self):
        # An RKE2 server on 2 CPUs: the control plane and V1 reserve 1875m,
        # V2's instance manager wants 12% (240m) and is never scheduled.
        self.c.configure(); self.c.capacity()
        self.c.change_node(lambda node: node['status']['allocatable'].update(cpu='2'))
        self.c.pods({'spec': {'nodeName': 'k3s-test', 'containers': [{'resources': {'requests': {'cpu': '1875m'}}}]},
                        'status': {'phase': 'Running'}})
        plan = V.plan()
        self.assertFalse(plan['can_enable'])
        self.assertIn("reserves 240m of CPU and Kubernetes has 125m left", ' '.join(plan['blockers']))
        self.c.change('/api/v1/namespaces/lab/pods/pod-0', lambda pod: pod['spec']['containers'][0]['resources']['requests'].update(cpu='1500m'))
        self.assertTrue(V.plan()['can_enable'], "room for it: enable")

    def test_reboot_requires_fresh_module_observations(self):
        self.c.configure(); self.c.capacity()
        self.c.change_node(lambda node: node['status']['nodeInfo'].update(bootID='a' * 36))
        self.assertFalse(V.plan()['can_enable'])
        self.c.base['nodes'][0]['checks']['modules'] = True
        self.assertTrue(V.plan()['can_enable'])
    def test_preparation_requires_confirmation_and_current_host_identity(self):
        body = self.c.request()
        for key, value in [('confirm', False), ('review_token', 'old'), ('node', 'other'), ('request_id', ';reboot')]:
            with self.subTest(key=key), self.assertRaises(ValueError): V.prepare({**body, key: value})
        self.c.change_node(lambda node: node['metadata'].update(uid='replacement'))
        with self.assertRaisesRegex(ValueError, 'changed'): V.prepare(body)
        self.assertEqual([], self.c.sent)
    def test_duplicate_request_returns_saved_operation_and_new_request_is_blocked(self):
        body = self.c.request()
        first = V.prepare(body)
        self.assertEqual(first, V.prepare(body))
        with self.assertRaisesRegex(ValueError, 'already active'): V.prepare({**body, 'request_id': 'b' * 24})
        self.assertEqual(1, len(self.c.jobs()))
    def test_maintenance_excludes_host_preparation(self):
        for kind in ('node-power', 'host-os'):
            self.c.ops.items = [{'kind': kind, 'status': 'running', 'ref': {'node': 'k3s-test'}}]
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, 'already active'): V.prepare(self.c.request())
        self.assertEqual([], self.c.sent)
    def test_missing_job_is_never_replayed_by_observer(self):
        V.prepare(self.c.request()); self.c.drop_jobs()
        self.assertEqual('failed', V.progress(self.c.ops.items[0])[0])
        self.assertEqual(1, len(self.c.sent))
    def test_altered_or_failed_job_never_proves_configuration(self):
        self.c.configure(); self.c.capacity()
        self.c.change_job(lambda job: job['spec']['template']['spec']['containers'][0].update(command=['true']))
        self.assertFalse(V.plan()['nodes'][0]['configured'])
        self.assertEqual('failed', V.progress(self.c.ops.items[0])[0])
        self.c.change_job(lambda job: job.update(V._body(self.c.ops.items[0]['ref'])))
        self.c.change_job(lambda job: job.update(status={'conditions': [{'type': 'Failed', 'status': 'True'}]}))
        self.assertEqual('failed', V.progress(self.c.ops.items[0])[0])
        self.assertFalse(V.plan()['can_enable'])
    def test_external_job_without_saved_approval_is_not_proof(self):
        self.c.configure(); self.c.capacity(); self.c.ops.items.clear()
        self.assertFalse(V.plan()['can_enable'])
    def test_changed_memory_requirement_invalidates_enable_review(self):
        self.c.configure(); self.c.capacity(); plan = V.plan()
        self.c.setting('data-engine-memory-size', '{"v2":"4096"}')
        with patch.object(V.CAP, 'save') as save, self.assertRaisesRegex(ValueError, 'changed'):
            V.enable({'confirm': True, 'review_token': plan['review_token']})
        save.assert_not_called()
        self.assertEqual(4096, V.plan()['required_mib'])
    def test_old_setting_and_disabled_hugepages_are_respected(self):
        self.c.setting('v2-data-engine-hugepage-limit', '4096')
        self.assertEqual(2048, V.plan()['nodes'][0]['target_pages'])
        self.c.setting('data-engine-hugepage-enabled', '{"v2":"false"}')
        self.c.configure()
        self.assertEqual(0, V.plan()['required_mib'])
        self.assertTrue(V.plan()['can_enable'])
    def test_other_pod_hugepage_requests_are_preserved_and_subtracted(self):
        self.c.pods({'spec': {'nodeName': 'k3s-test', 'containers': [{'resources': {'requests': {'hugepages-2Mi': '1Gi'}}}]}})
        self.assertEqual(1536, V.plan()['nodes'][0]['target_pages'])
        self.c.configure(); self.c.capacity()
        self.assertFalse(V.plan()['can_enable'])
        self.c.capacity('3Gi')
        self.assertTrue(V.plan()['can_enable'])
    def test_unknown_inventory_and_missing_harvester_setting_fail_closed(self):
        self.c.base['distribution'] = 'harvester'
        with self.assertRaisesRegex(ValueError, 'Harvester'): V.plan()
        self.c.base['distribution'] = 'k3s'
        original = K.kget
        def read(path):
            if path == '/api/v1/nodes': return {'items': [], 'metadata': {'continue': 'next'}}
            return original(path)
        with patch.object(V, 'kget', read), self.assertRaisesRegex(ValueError, 'incomplete'): V.plan()
    def test_harvester_owns_setup_and_cannot_run_generic_host_jobs(self):
        self.c.base['distribution'] = 'harvester'; self.c.harvester('false')
        plan = V.plan()
        self.assertTrue(plan['can_enable'])
        self.assertFalse(plan['nodes'][0]['can_prepare'])
        with self.assertRaises(ValueError): V.prepare(self.c.request())
        self.c.harvester('true')
        self.assertFalse(V.plan()['can_enable'])
    def test_engine_is_ready_only_after_running_manager_is_observed(self):
        self.c.setting('v2-data-engine', 'true')
        self.assertFalse(V.plan()['engine_ready'])
        self.c.managers({'spec': {'nodeID': 'k3s-test', 'dataEngine': 'v2'}, 'status': {'currentState': 'running'}})
        self.assertTrue(V.plan()['engine_ready'])
    def test_host_tasks_cannot_be_cancelled_through_direct_api(self):
        import homestead_operations as operations
        self.assertFalse(operations._plan_for({'kind': V.KIND, 'status': 'running'})['can'])

    def test_admin_routes_are_explicit(self):
        from homestead_route_policy import role
        for method, suffix in [('GET', 'plan'), ('POST', 'prepare'), ('POST', 'enable')]:
            self.assertEqual('admin', role('/api/longhorn/v2/' + suffix, method))

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
