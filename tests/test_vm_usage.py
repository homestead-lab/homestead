"""Each running VM says what it uses: CPU and memory from its launcher pod,
and disk traffic from KubeVirt's own counts, as a rate between readings."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_vms as VMS
import homestead_vmusage as U

METRICS = """# HELP kubevirt_vmi_storage_read_traffic_bytes_total Total number of bytes read from storage.
kubevirt_vmi_storage_read_traffic_bytes_total{drive="rootdisk",name="pihole",namespace="lab",node="n1"} {r1}
kubevirt_vmi_storage_read_traffic_bytes_total{drive="data",name="pihole",namespace="lab",node="n1"} {r2}
kubevirt_vmi_storage_write_traffic_bytes_total{drive="rootdisk",name="pihole",namespace="lab",node="n1"} {w}
kubevirt_vmi_memory_resident_bytes{name="pihole",namespace="lab",node="n1"} 1.2e+09
"""


def metrics(r1, r2, w):
    return METRICS.replace("{r1}", str(r1)).replace("{r2}", str(r2)).replace("{w}", str(w))


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.pod_metrics = {"items": [
            {"metadata": {"name": "virt-launcher-pihole-abcde", "namespace": "lab",
                          "labels": {"vm.kubevirt.io/name": "pihole"}},
             "containers": [{"usage": {"cpu": "500m", "memory": "512Mi"}},
                            {"usage": {"cpu": "250000000n", "memory": "0"}}]},
            {"metadata": {"name": "frigate-7d9", "namespace": "lab"}, "containers": [{"usage": {"cpu": "1", "memory": "1Gi"}}]}]}

        def get(path):
            if path.startswith("/apis/metrics.k8s.io"):
                return self.pod_metrics
            if "virt-handler" in path:
                return {"items": [{"spec": {"nodeName": "n1"}, "status": {"phase": "Running", "podIP": "10.0.0.5"}}]}
            raise AssertionError(path)
        U.bind(get)
        U._readings.clear(); U._usage.clear()

    def test_drives_are_summed_per_vm(self):
        self.assertEqual({("lab", "pihole"): [300.0, 50.0]}, U.parse_io(metrics(100, 200, 50)))

    def test_disk_traffic_is_a_rate_between_two_readings(self):
        U.sample(fetch=lambda ip: metrics(0, 0, 0), now=1000)
        self.assertNotIn("read_bps", U.usage(now=1000)[0][("lab", "pihole")])
        U.sample(fetch=lambda ip: metrics(3_000_000, 0, 600_000), now=1030)
        got = U.usage(now=1031)[0][("lab", "pihole")]
        self.assertEqual((100_000, 20_000), (got["read_bps"], got["write_bps"]))
        self.assertEqual((0.75, 512 * 2**20), (got["cpu"], got["mem"]))
        self.assertNotIn(("lab", "frigate"), U.usage(now=1031)[0])

    def test_a_vm_that_restarted_reads_as_zero_not_negative(self):
        U.sample(fetch=lambda ip: metrics(9_000, 0, 9_000), now=1000)
        U.sample(fetch=lambda ip: metrics(10, 0, 10), now=1030)
        got = U.usage(now=1031)[0][("lab", "pihole")]
        self.assertEqual((0, 0), (got["read_bps"], got["write_bps"]))

    def test_an_unreachable_virt_handler_leaves_disk_traffic_out_and_says_why(self):
        def refuse(ip):
            raise OSError("connection refused")
        U.sample(fetch=refuse, now=1000)
        U.sample(fetch=refuse, now=1030)
        got, note = U.usage(now=1031)
        self.assertNotIn("read_bps", got[("lab", "pihole")])
        self.assertIn("virt-handler on n1 did not answer", note)

    def test_the_vm_list_gives_a_running_vm_its_usage_as_shares_of_its_size(self):
        from test_vms import Cluster
        U._readings.clear(); U._usage.clear()
        U._usage[("default", "win11")] = {"cpu": 1.0, "mem": 4 * 2**30}
        U._readings.extend([{"at": 1000, "io": {("default", "win11"): [0, 0]}},
                            {"at": 1030, "io": {("default", "win11"): [30_000, 3_000]}}])
        for running, expect in ((True, True), (False, False)):
            with self.subTest(running=running):
                cluster = Cluster(running=running)
                VMS.bind(cluster.get, cluster.send, lambda *a, **k: [])
                with unittest.mock.patch.object(U.time, "time", lambda: 1031):
                    row = VMS.list_vms()[0]
                self.assertEqual(expect, "usage" in row)
                if expect:
                    u = row["usage"]
                    self.assertEqual((25.0, 1000, 100), (u["cpu_pct"], u["read_bps"], u["write_bps"]))
                    self.assertEqual(round(100 * 4 * 2**30 / VMS._bytes(row["memory"]), 1), u["mem_pct"])
                    self.assertEqual(["192.0.2.50"], row["ips"])


import unittest.mock  # noqa: E402

if __name__ == "__main__":
    unittest.main()
