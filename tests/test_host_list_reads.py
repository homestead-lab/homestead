"""The host lists leave out what no list shows (#1211): /api/nodes (the Hosts
page, every 15 s) and the host rows in /api/overview (the Dashboard, every
15 s) carry no device list, mount table or iSCSI sessions from the node probe,
and no Kubernetes labels. A host's own read (/api/node) keeps them, and so
does everything in the server that reads the host rows."""
import json
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import homestead_auth as AUTH
import homestead_changes as CHANGES
import homestead_cache as CACHE
import homestead_names as NAMES
import homestead_node_probes as PROBES
from test_page_snapshot import Cluster, clean_cache, clean_snapshot, setUpModule  # noqa: F401 (restart record off)

HOSTS = {"node-a": "192.0.2.20", "node-b": "192.0.2.21"}


def devices(i):
    """A /dev as a probe on a small server reads it: about 800 paths, the
    probe's cap, and a dozen USB devices."""
    kinds = ["tty", "ttyS", "loop", "nvme0n1p", "sda", "sdb", "dm-", "hidraw", "input/event", "vcs", "vcsa",
             "bus/usb/001/", "dri/card", "dri/renderD12", "snd/pcmC0D", "disk/by-id/ata-EXAMPLE-DISK-part",
             "disk/by-path/pci-0000:00:17.0-ata-1-part", "mapper/longhorn-pvc-0000000000000000-"]
    paths = [f"/dev/{kinds[n % len(kinds)]}{n}" for n in range(800)]
    return {"dri": ["card0", "renderD128"], "apex": [], "video": [f"video{n}" for n in range(4)],
            "tty": [f"ttyUSB{n}" for n in range(2)], "paths": paths,
            "path_entries": [{"path": p, "type": "CharDevice" if "tty" in p else "BlockDevice"} for p in paths],
            "usb": [{"vid": f"{0x1d6b + n:04x}", "pid": f"{n:04x}", "name": f"Example USB device {n} on host {i}",
                     "path": f"/dev/bus/usb/00{n % 4 + 1}/{n:03d}"} for n in range(12)]}


def reading(node, i):
    """One host's probe answer, shaped as server/probe/probe.py sends it."""
    return {"node": node, "cpu_c": 41 + i, "max_c": 52 + i, "max_source": "NVMe nvme0", "cpu_model": "Example CPU",
            "thermal": [{"name": "x86_pkg_temp", "celsius": 41 + i}],
            "hwmon": [{"chip": "coretemp", "name": f"Core {n}", "celsius": 40 + n} for n in range(8)],
            "sensors": 9, "devices": devices(i),
            "disks": [{"name": "nvme0n1", "kind": "NVMe", "size_gb": 465.8, "read_mbps": 1.5, "write_mbps": 0.4}],
            "mounts": [{"disk": "nvme0n1", "device": f"nvme0n1p{n % 3 + 1}",
                        "mountpoint": f"/var/lib/longhorn/replicas/pvc-{n:08d}", "fstype": "ext4",
                        "source": f"/dev/nvme0n1p{n % 3 + 1}"} for n in range(24)],
            "uptime_s": 86400 * (i + 1), "kvm": True,
            "interfaces": [{"name": "eth0", "mac": "02:00:00:00:00:01", "speed": 1000}],
            "iscsi": [{"sid": n, "target": f"iqn.2019-10.io.longhorn:pvc-{n:08d}-0000-0000-0000-000000000000",
                       "state": "LOGGED_IN", "address": "198.51.100.5", "port": 3260, "disks": [f"sd{chr(99 + n)}"]}
                      for n in range(10)]}


def labels(node):
    """A host's labels with node feature discovery: a few hundred."""
    out = {"kubernetes.io/hostname": node, "kubernetes.io/os": "linux", "kubernetes.io/arch": "amd64",
           "node-role.kubernetes.io/control-plane": "true", "hardware/igpu": "true"}
    out.update({f"feature.node.kubernetes.io/cpu-cpuid.FEATURE{n:03d}": "true" for n in range(220)})
    out.update({f"feature.node.kubernetes.io/pci-{n:04x}.present": "true" for n in range(40)})
    return out


def probe_pod(node, ip):
    return {"apiVersion": "v1", "kind": "Pod",
            "metadata": {"namespace": "lab", "name": f"probe-{node}", "uid": f"uid-{node}",
                         "labels": {"app": NAMES.NODEPROBE}},
            "spec": {"nodeName": node}, "status": {"phase": "Running", "podIP": ip}}


def size(value):
    return len(json.dumps(value, separators=(",", ":")).encode())


class HostListReadsTests(unittest.TestCase):
    def setUp(self):
        # The account store and change-author key the tests fill go back when each ends (CacheRatchet).
        self.enterContext(mock.patch.dict(AUTH._store_cache, {"at": 0, "data": None}, clear=True))
        self.enterContext(mock.patch.dict(CHANGES._known, clear=True))
        clean_cache(self)
        PROBES.reset()
        self.addCleanup(PROBES.reset)
        self.cluster = Cluster(self)
        clean_snapshot(self)
        kube = self.cluster.kube
        for node, ip in HOSTS.items():
            obj = kube.object(f"/api/v1/nodes/{node}")
            obj["metadata"]["labels"] = labels(node)
            kube.add(obj)
            kube.add(probe_pod(node, ip))
        answers = {ip: reading(node, i) for i, (node, ip) in enumerate(HOSTS.items())}

        def read(ip, port, timeout, headers=None):
            if port != 9099:
                raise OSError("no SMART helper in these tests")
            return json.loads(json.dumps(answers[ip]))
        self.enterContext(mock.patch.object(PROBES, "_read", read))

    def table(self, path, **query):
        handler = self.cluster.server.MODULE_ROUTES.table()[("GET", path)].handler
        return handler(types.SimpleNamespace(query={k: [v] for k, v in query.items()}))

    def full(self):
        """The host rows as the server keeps them, for its own readers."""
        return CACHE.cached("nodes", 5, self.cluster.server.NODES.get_nodes)

    def assert_list_rows(self, rows):
        self.assertEqual(sorted(HOSTS), sorted(r["name"] for r in rows))
        for row in rows:
            self.assertNotIn("labels", row)
            for key in ("devices", "mounts", "iscsi"):
                self.assertNotIn(key, row["temps"], (row["name"], key))
            # What the Hosts page, the Dashboard and Health show from a row.
            for key in ("cpu_c", "max_c", "max_source", "cpu_model", "sensors", "disks", "hwmon", "thermal"):
                self.assertIn(key, row["temps"], (row["name"], key))
            self.assertEqual("NVMe", row["temps"]["disks"][0]["kind"])
            self.assertIn("health", row["temps"]["disks"][0])
            for key in ("roles", "hardware", "hardware_inventory", "uptime_s", "probe", "disk_issues", "status",
                        "schedulable", "cpu_pct", "mem_pct", "workloads", "duties"):
                self.assertIn(key, row, (row["name"], key))
            self.assertEqual(["control-plane"], row["roles"])
            self.assertTrue(row["hardware"]["igpu"], "a feature turned on by a label is still worked out")

    def test_the_hosts_list_leaves_out_devices_mounts_iscsi_and_labels(self):
        self.assert_list_rows(self.table("/api/nodes"))

    def test_the_dashboards_host_rows_leave_them_out_too(self):
        answer = self.table("/api/overview")
        self.assert_list_rows(answer["nodes"])
        self.assertIn("as_of", answer)

    def test_the_combined_list_of_linked_clusters_cuts_this_clusters_rows_too(self):
        # A linked cluster's rows come from its own /api/nodes, so they arrive
        # cut; this cluster's rows in the same list are cut alike.
        request = types.SimpleNamespace(query={}, user="admin", role="admin")
        handler = self.cluster.server.MODULE_ROUTES.table()[("GET", "/api/fleet/all/nodes")].handler
        answer = handler(request)
        rows = answer.body if hasattr(answer, "body") else answer
        self.assert_list_rows(rows)

    def test_the_lists_are_a_fraction_of_the_full_rows(self):
        full, hosts, overview = size(self.full()), size(self.table("/api/nodes")), self.table("/api/overview")
        rows = size(overview["nodes"])
        # About 163 KiB of full rows for two hosts, 6 KiB as list rows.
        self.assertGreater(full, 80 * 1024, f"a realistic inventory: {full} bytes")
        self.assertLess(hosts, full // 5, f"/api/nodes is {hosts} bytes of {full}")
        self.assertLess(rows, full // 5, f"/api/overview's host rows are {rows} bytes of {full}")

    def test_a_hosts_own_read_keeps_everything(self):
        self.table("/api/nodes")
        one = self.table("/api/node", name="node-a")
        self.assertEqual(800, len(one["temps"]["devices"]["path_entries"]))
        self.assertEqual(12, len(one["temps"]["devices"]["usb"]))
        self.assertEqual(24, len(one["temps"]["mounts"]))
        self.assertEqual(10, len(one["temps"]["iscsi"]))
        self.assertEqual(labels("node-a"), {k: v for k, v in one["labels"].items() if k in labels("node-a")})

    def test_the_servers_own_readers_still_get_the_full_rows(self):
        # The list reads leave the kept rows alone: placement, /api/v1, metrics
        # and the linked clusters' lists read these.
        self.table("/api/nodes")
        self.table("/api/overview")
        for row in self.full():
            self.assertIn("feature.node.kubernetes.io/cpu-cpuid.FEATURE000", row["labels"])
            self.assertEqual(800, len(row["temps"]["devices"]["paths"]))
            self.assertIn("mounts", row["temps"])
            self.assertIn("iscsi", row["temps"])
        self.assertIn("devices", self.cluster.server.OVERVIEW_ROUTES.get_overview()["nodes"][0]["temps"])


if __name__ == "__main__":
    unittest.main()
