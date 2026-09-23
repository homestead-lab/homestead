"""Joining a host to the cluster, and removing a dead one."""
import base64
import sys
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_fatimg as fat
import homestead_onboard as onboard
import homestead_yaml as yaml


def node(name, ready=True, roles=(), machine=""):
    labels = {f"node-role.kubernetes.io/{r}": "true" for r in roles}
    annotations = {"cluster.x-k8s.io/machine": machine, "cluster.x-k8s.io/cluster-namespace": "fleet-local"} if machine else {}
    return {"metadata": {"name": name, "labels": labels, "annotations": annotations},
            "status": {"conditions": [{"type": "Ready", "status": "True" if ready else "Unknown",
                                       "lastTransitionTime": "2026-09-20T10:00:00Z"}],
                       "addresses": [{"type": "InternalIP", "address": "192.0.2.51"}]}}


class Cluster:
    def __init__(self):
        self.objects = {
            "/api/v1/nodes": {"items": [node("node1", roles=("control-plane", "etcd"), machine="custom-1"),
                                        node("node2", roles=("control-plane", "etcd"), machine="custom-2"),
                                        node("node3", roles=("control-plane", "etcd"), machine="custom-3")]},
            "/api/v1/namespaces/harvester-system/configmaps/vip": {"data": {"ip": "192.0.2.240"}},
            "/apis/harvesterhci.io/v1beta1/settings/server-version": {"value": "v1.4.1"},
        }
        self.machines = [{"metadata": {"name": f"custom-{i}", "namespace": "fleet-local"},
                          "status": {"nodeRef": {"name": f"node{i}"}, "phase": "Running"}} for i in (1, 2, 3)]
        self.replicas = []
        self.lh_nodes = ["node1", "node2", "node3"]
        self.sent = []
        self.secrets = {}

    def get(self, path):
        if path == "/apis/cluster.x-k8s.io/v1beta1/namespaces/fleet-local/machines":
            return {"items": self.machines}
        if path.startswith("/apis/cluster.x-k8s.io/v1beta1/namespaces/fleet-local/machines/"):
            found = [m for m in self.machines if m["metadata"]["name"] == path.rsplit("/", 1)[1]]
            if found:
                return found[0]
            raise urllib.error.HTTPError(path, 404, "missing", {}, None)
        if path.startswith("/api/v1/pods"):
            return {"items": getattr(self, "pods", [])}
        if path == "/apis/kubevirt.io/v1/virtualmachineinstances":
            return {"items": getattr(self, "vmis", [])}
        if path == "/apis/storage.k8s.io/v1/volumeattachments":
            return {"items": getattr(self, "attachments", [])}
        if path == "/apis/longhorn.io/v1beta2/namespaces/longhorn-system/replicas":
            return {"items": self.replicas}
        if path == "/apis/longhorn.io/v1beta2/namespaces/longhorn-system/nodes":
            return {"items": [{"metadata": {"name": n}} for n in self.lh_nodes]}
        if path.startswith("/apis/longhorn.io/v1beta2/namespaces/longhorn-system/nodes/"):
            name = path.rsplit("/", 1)[1]
            if name in self.lh_nodes:
                return {"metadata": {"name": name}}
            raise urllib.error.HTTPError(path, 404, "missing", {}, None)
        if path.startswith("/api/v1/nodes/"):
            name = path.rsplit("/", 1)[1]
            found = [n for n in self.objects["/api/v1/nodes"]["items"] if n["metadata"]["name"] == name]
            if found:
                return found[0]
            raise urllib.error.HTTPError(path, 404, "missing", {}, None)
        if "/secrets/" in path:
            name = path.rsplit("/", 1)[1]
            if name in self.secrets:
                return self.secrets[name]
            raise urllib.error.HTTPError(path, 404, "missing", {}, None)
        if path in self.objects:
            return self.objects[path]
        raise urllib.error.HTTPError(path, 404, "missing", {}, None)

    def send(self, method, path, body=None, ctype="application/json", **kwargs):
        self.sent.append((method, path))
        if method == "POST" and path.endswith("/secrets"):
            data = {k: base64.b64encode(v.encode()).decode() for k, v in body["stringData"].items()}
            self.secrets[body["metadata"]["name"]] = {"data": data}
        if method == "DELETE" and "/secrets/" in path:
            self.secrets.pop(path.rsplit("/", 1)[1], None)
        if method == "DELETE" and path.startswith("/api/v1/nodes/"):
            name = path.rsplit("/", 1)[1]
            self.objects["/api/v1/nodes"]["items"] = [n for n in self.objects["/api/v1/nodes"]["items"]
                                                      if n["metadata"]["name"] != name]
        if method == "DELETE" and "/machines/" in path:
            name = path.rsplit("/", 1)[1]
            for m in self.machines:
                if m["metadata"]["name"] == name and m["metadata"].get("finalizers"):
                    # A finalizer holds it: deletion starts and waits.
                    m["metadata"]["deletionTimestamp"] = "2026-09-23T10:00:00Z"
                    return body or {}
            self.machines = [m for m in self.machines if m["metadata"]["name"] != name]
        if method == "PATCH" and "/machines/" in path and body.get("metadata", {}).get("finalizers", 1) is None:
            name = path.rsplit("/", 1)[1]
            self.machines = [m for m in self.machines if m["metadata"]["name"] != name]
        if method == "DELETE" and "/replicas/" in path:
            self.replicas = [r for r in self.replicas if r["metadata"]["name"] != path.rsplit("/", 1)[1]]
        if method == "DELETE" and "/longhorn-system/nodes/" in path:
            self.lh_nodes.remove(path.rsplit("/", 1)[1])
        return body or {}


class Ops:
    def __init__(self):
        self.started = []

    def start(self, kind, title, resource, href, ref, message=""):
        self.started.append((kind, title))
        return {"id": "op1"}


PLAN = {"hostname": "node4", "role": "worker", "device": "/dev/nvme0n1", "data_disk": "/dev/sdb",
        "nic": "eno1", "mac": "52:54:00:AA:BB:CC", "method": "static", "address": "192.0.2.54/24",
        "gateway": "192.0.2.1", "dns": "1.1.1.1, 8.8.8.8", "ntp": "pool.ntp.org",
        "password": "correct horse", "server_url": "https://192.0.2.240:443",
        "token": "K10abc::server:secrettoken", "version": "1.4.1", "hours": 12,
        "homestead_url": "http://192.0.2.242:8080"}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cluster = Cluster()
        self.ops = Ops()
        onboard.bind(self.cluster.get, self.cluster.send, "lab", self.tmp.name, self.ops)

    def plan(self, **changes):
        return onboard.create_plan(dict(PLAN, **changes), "")

    def stored(self, plan_id):
        return onboard._find(plan_id)


class PlanTests(Base):
    def test_defaults_come_from_the_cluster(self):
        defaults = onboard.cluster_defaults()

        self.assertEqual(("1.4.1", "https://192.0.2.240:443"), (defaults["version"], defaults["server_url"]))

    def test_the_token_goes_to_a_secret_not_the_plan(self):
        plan = self.plan()

        self.assertNotIn("K10abc", str(plan))
        self.assertNotIn("K10abc", Path(self.tmp.name, "onboard.json").read_text())
        self.assertIn(f"homestead-onboard-{plan['id']}", self.cluster.secrets)
        self.assertEqual(("onboard", "Join node4"), self.ops.started[0])

    def test_the_page_never_sees_the_url_secret_as_a_field(self):
        plan = self.plan()

        self.assertNotIn("secret", plan)
        self.assertIn("/boot/", plan["urls"]["config"])

    def test_a_node_already_in_the_cluster_is_refused(self):
        with self.assertRaisesRegex(ValueError, "already in the cluster"):
            self.plan(hostname="node2")

    def test_bad_input_is_refused_plainly(self):
        for changes, words in (({"hostname": "Node_4"}, "hostname"), ({"device": "sda"}, "device path"),
                               ({"nic": "", "mac": ""}, "management network"), ({"mac": "zz"}, "MAC"),
                               ({"address": "192.0.2.54"}, "prefix"), ({"password": "short"}, "8 characters"),
                               ({"server_url": "192.0.2.240"}, "https"), ({"token": ""}, "token"),
                               ({"version": "latest"}, "version"), ({"homestead_url": "https://x"}, "plain HTTP")):
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(ValueError, words):
                    self.plan(**changes)

    def test_the_join_config_is_harvesters_format(self):
        plan = self.plan()

        doc = yaml.loads(onboard.config_document(self.stored(plan["id"])))

        self.assertEqual(("https://192.0.2.240:443", "K10abc::server:secrettoken"), (doc["server_url"], doc["token"]))
        self.assertEqual(("join", "worker", "/dev/nvme0n1", "/dev/sdb"),
                         (doc["install"]["mode"], doc["install"]["role"], doc["install"]["device"], doc["install"]["data_disk"]))
        management = doc["install"]["management_interface"]
        self.assertEqual([{"name": "eno1", "hwAddr": "52:54:00:aa:bb:cc"}], management["interfaces"])
        self.assertEqual(("static", "192.0.2.54", "255.255.255.0", "192.0.2.1"),
                         (management["method"], management["ip"], management["subnet_mask"], management["gateway"]))
        self.assertEqual(["1.1.1.1", "8.8.8.8"], doc["os"]["dns_nameservers"])
        self.assertEqual("node4", doc["os"]["hostname"])
        self.assertNotEqual("correct horse", doc["os"]["password"] if doc["os"]["password"].startswith("$6$") else "x")
        self.assertIn("harvester-v1.4.1-amd64.iso", doc["install"]["iso_url"])
        self.assertEqual({"STARTED", "SUCCEEDED", "FAILED"}, {w["event"] for w in doc["install"]["webhooks"]})

    def test_the_preview_hides_the_token(self):
        plan = self.plan()

        text = onboard.config_document(self.stored(plan["id"]), redact=True)

        self.assertNotIn("K10abc", text)
        self.assertIn("<cluster token>", text)

    def test_the_boot_script_asks_before_wiping(self):
        script = onboard.ipxe_script(self.stored(self.plan()["id"]))

        self.assertTrue(script.startswith("#!ipxe"))
        self.assertIn("ERASES /dev/nvme0n1 and /dev/sdb", script)
        self.assertIn("prompt --key i", script)
        self.assertIn("initrd=initrd", script, "UEFI needs the initrd named on the kernel line")
        self.assertIn("harvester.install.config_url=http://192.0.2.242:8080/boot/", script)

    def test_without_confirmation_it_goes_straight_to_install(self):
        script = onboard.ipxe_script(self.stored(self.plan(confirm_wipe=False)["id"]))

        self.assertNotIn("prompt --key i", script)

    def test_the_usb_image_holds_ipxe_and_the_script_but_no_token(self):
        plan = self.plan()
        onboard.ipxe_binary = lambda: b"MZ" + b"\0" * 200_000

        name, image = onboard.usb_image(plan["id"])
        files = fat.read(image)

        self.assertEqual("homestead-join-node4.img", name)
        self.assertEqual({"EFI/BOOT/BOOTX64.EFI", "EFI/BOOT/autoexec.ipxe", "autoexec.ipxe", "README.txt"}, set(files))
        self.assertIn(b"chain http://192.0.2.242:8080/boot/", files["autoexec.ipxe"])
        self.assertNotIn(b"K10abc", image)


class BootTests(Base):
    def test_a_secret_opens_only_its_own_plan_and_only_while_open(self):
        plan = self.stored(self.plan()["id"])

        self.assertEqual(plan["id"], onboard.by_secret(plan["secret"])["id"])
        self.assertIsNone(onboard.by_secret("x" * 32))
        self.assertIsNone(onboard.by_secret(""))

        plan["expires"] = time.time() - 1
        onboard._store(plan)
        self.assertIsNone(onboard.by_secret(plan["secret"]), "expired plans stop answering")

    def test_fetching_the_config_marks_it_installing(self):
        plan = self.stored(self.plan()["id"])

        onboard.note_fetch(plan["secret"], "config", "192.0.2.54")

        self.assertEqual("installing", self.stored(plan["id"])["status"])

    def test_webhooks_move_the_plan_along(self):
        plan = self.stored(self.plan()["id"])

        onboard.webhook(plan["secret"], "STARTED")
        self.assertEqual("Installing Harvester", self.stored(plan["id"])["message"])
        onboard.webhook(plan["secret"], "FAILED")
        self.assertEqual("failed", self.stored(plan["id"])["status"])
        self.assertIsNone(onboard.by_secret(plan["secret"]), "a failed plan stops serving")

    def test_a_joined_node_closes_its_plan_and_deletes_the_token(self):
        plan = self.plan()
        self.cluster.objects["/api/v1/nodes"]["items"].append(node("node4"))

        onboard.tick()

        self.assertEqual("joined", self.stored(plan["id"])["status"])
        self.assertNotIn(f"homestead-onboard-{plan['id']}", self.cluster.secrets)
        self.assertEqual(("succeeded", 100), onboard.op_state({"ref": {"plan": plan["id"]}})[:2])

    def test_an_expired_plan_deletes_its_token(self):
        plan = self.stored(self.plan()["id"])
        plan["expires"] = time.time() - 1
        onboard._store(plan)

        onboard.tick()

        self.assertEqual("expired", self.stored(plan["id"])["status"])
        self.assertEqual({}, self.cluster.secrets)

    def test_revoking_ends_it(self):
        plan = self.plan()

        onboard.revoke(plan["id"])

        self.assertEqual("cancelled", self.stored(plan["id"])["status"])
        self.assertEqual({}, self.cluster.secrets)


class PxeTests(Base):
    def test_the_pxe_pod_answers_only_the_new_hosts_mac(self):
        plan = self.stored(self.plan()["id"])

        pod = onboard.pxe_pod(plan, "node1", "mgmt-br", "192.0.2.0/24")
        args = pod["spec"]["containers"][0]["args"]

        self.assertTrue(pod["spec"]["hostNetwork"])
        self.assertIn("--dhcp-mac=set:joining,52:54:00:aa:bb:cc", args)
        self.assertIn("--dhcp-range=192.0.2.0,proxy,255.255.255.0", args)
        self.assertTrue(all("tag:bootipxe" in a or "tag:runscript" in a for a in args if a.startswith("--pxe-service")))
        self.assertLessEqual(pod["spec"]["activeDeadlineSeconds"], 6 * 3600, "it stops by itself")
        self.assertIn("@sha256:", pod["spec"]["containers"][0]["image"], "a pinned image")

    def test_pxe_needs_a_mac(self):
        plan = self.stored(self.plan(mac="")["id"])

        with self.assertRaisesRegex(ValueError, "MAC"):
            onboard.pxe_pod(plan, "node1", "mgmt-br", "192.0.2.0/24")

    def test_starting_pxe_guesses_the_subnet_from_the_node(self):
        plan = self.plan()

        onboard.pxe_start(plan["id"], "node1")

        self.assertEqual("192.0.2.0/24", self.stored(plan["id"])["pxe"]["subnet"])
        self.assertIn(("POST", "/api/v1/namespaces/lab/pods"), self.cluster.sent)


class RemovalTests(Base):
    def kill(self, name):
        for n in self.cluster.objects["/api/v1/nodes"]["items"]:
            if n["metadata"]["name"] == name:
                n["status"]["conditions"][0]["status"] = "Unknown"

    def test_a_ready_node_is_not_removed_from_here(self):
        report = onboard.removal_plan("node3")

        self.assertFalse(report["ok"])
        self.assertIn("rke2-uninstall.sh", report["blockers"][0])

    def test_removing_a_dead_control_plane_node_keeps_quorum_and_says_the_margin(self):
        self.kill("node3")

        report = onboard.removal_plan("node3")

        self.assertTrue(report["ok"], report["blockers"])
        self.assertTrue(any("one more failure" in w for w in report["warnings"]))

    def test_losing_quorum_is_refused(self):
        self.kill("node2")
        self.kill("node3")

        report = onboard.removal_plan("node3")

        self.assertFalse(report["ok"])
        self.assertIn("quorum", report["blockers"][0])

    def test_the_last_copy_of_a_volume_is_called_out(self):
        self.kill("node3")
        self.cluster.replicas = [
            {"spec": {"nodeID": "node3", "volumeName": "only-here"}, "status": {"currentState": "running"}},
            {"spec": {"nodeID": "node3", "volumeName": "mirrored"}, "status": {"currentState": "running"}},
            {"spec": {"nodeID": "node1", "volumeName": "mirrored"}, "status": {"currentState": "running"}},
        ]

        report = onboard.removal_plan("node3")
        self.assertEqual((["only-here"], ["mirrored"]), (report["lost_volumes"], report["rebuilt_volumes"]))
        with self.assertRaisesRegex(ValueError, "only copy"):
            onboard.remove_node("node3")

    def test_a_dead_node_is_removed_in_harvesters_order(self):
        self.kill("node3")

        result = onboard.remove_node("node3")

        order = [(m, p.split("/")[-1]) for m, p in self.cluster.sent]
        self.assertEqual([("PATCH", "node3"), ("DELETE", "node3"), ("DELETE", "custom-3"), ("DELETE", "node3")], order)
        self.assertEqual(4, len(result["log"]))
        self.assertNotIn("custom-3", [m["metadata"]["name"] for m in self.cluster.machines])

    def hold_things_on(self, name):
        """A host that died holding a pod, a VM, a volume attachment and a replica."""
        self.kill(name)
        self.cluster.pods = [{"metadata": {"name": "app-1", "namespace": "lab"}, "spec": {"nodeName": name}}]
        self.cluster.vmis = [{"metadata": {"name": "haos", "namespace": "lab"}, "status": {"nodeName": name}}]
        self.cluster.attachments = [{"metadata": {"name": "csi-abc"}, "spec": {"nodeName": name}}]
        self.cluster.replicas = [
            {"metadata": {"name": "vol-r-3"}, "spec": {"nodeID": name, "volumeName": "vol"}, "status": {"currentState": "running"}},
            {"metadata": {"name": "vol-r-1"}, "spec": {"nodeID": "node1", "volumeName": "vol"}, "status": {"currentState": "running"}}]
        for m in self.cluster.machines:
            if m["status"]["nodeRef"]["name"] == name:
                m["metadata"]["finalizers"] = ["machine.cluster.x-k8s.io"]

    def test_the_plan_says_what_a_dead_host_still_holds(self):
        self.hold_things_on("node3")

        report = onboard.removal_plan("node3")

        self.assertEqual({"pods": 1, "vms": ["haos"], "attachments": 1, "replicas": 1}, report["stuck"])
        self.assertTrue(any("haos" in step for step in report["gone_steps"]))

    def test_a_host_gone_for_good_lets_go_of_everything_it_held(self):
        self.hold_things_on("node3")

        result = onboard.remove_node("node3", gone=True)

        deleted = [p for m, p in self.cluster.sent if m == "DELETE"]
        self.assertIn("/apis/kubevirt.io/v1/namespaces/lab/virtualmachineinstances/haos", deleted)
        self.assertIn("/api/v1/namespaces/lab/pods/app-1", deleted)
        self.assertIn("/apis/storage.k8s.io/v1/volumeattachments/csi-abc", deleted)
        self.assertIn("/apis/longhorn.io/v1beta2/namespaces/longhorn-system/replicas/vol-r-3", deleted)
        self.assertNotIn("custom-3", [m["metadata"]["name"] for m in self.cluster.machines],
                         "the machine held by its finalizer is let go")
        self.assertNotIn("node3", self.cluster.lh_nodes, "no replicas left, so Longhorn's record goes too")
        self.assertTrue(any("finalizers" in line for line in result["log"]))

    def test_the_standard_removal_leaves_what_it_held_for_the_controllers(self):
        self.hold_things_on("node3")

        onboard.remove_node("node3")

        deleted = [p for m, p in self.cluster.sent if m == "DELETE"]
        self.assertNotIn("/api/v1/namespaces/lab/pods/app-1", deleted)
        self.assertIn("custom-3", [m["metadata"]["name"] for m in self.cluster.machines], "still deleting")

    def test_a_node_that_only_just_went_down_gets_a_warning(self):
        self.kill("node3")
        for n in self.cluster.objects["/api/v1/nodes"]["items"]:
            if n["metadata"]["name"] == "node3":
                n["status"]["conditions"][0]["lastTransitionTime"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        report = onboard.removal_plan("node3")

        self.assertTrue(any("rebooting" in w for w in report["warnings"]))

    def test_a_stuck_machine_or_longhorn_node_needs_force(self):
        self.cluster.machines.append({"metadata": {"name": "custom-9", "namespace": "fleet-local",
                                                   "deletionTimestamp": "2026-09-23T10:00:00Z", "finalizers": ["x"]},
                                      "status": {"nodeRef": {"name": "gone"}, "phase": "Deleting"}})
        self.cluster.lh_nodes.append("gone")
        self.cluster.replicas = [{"metadata": {"name": "r-gone"}, "spec": {"nodeID": "gone", "volumeName": "v"},
                                  "status": {"currentState": "error"}}]

        with self.assertRaisesRegex(ValueError, "force"):
            onboard.cleanup("machine", "custom-9")
        with self.assertRaisesRegex(ValueError, "force"):
            onboard.cleanup("longhorn", "gone")
        onboard.cleanup("machine", "custom-9", force=True)
        onboard.cleanup("longhorn", "gone", force=True)

        self.assertNotIn("custom-9", [m["metadata"]["name"] for m in self.cluster.machines])
        self.assertNotIn("gone", self.cluster.lh_nodes)

    def test_the_cleanup_report_finds_leftovers(self):
        self.kill("node2")
        self.cluster.machines.append({"metadata": {"name": "custom-9", "namespace": "fleet-local"},
                                      "status": {"nodeRef": {"name": "gone"}, "phase": "Running"}})
        self.cluster.lh_nodes.append("gone")

        report = onboard.cleanup_report()

        self.assertEqual(["node2"], [n["name"] for n in report["dead_nodes"]])
        self.assertEqual(["custom-9"], [m["name"] for m in report["stale_machines"]])
        self.assertEqual(["gone"], [n["name"] for n in report["stale_longhorn"]])

    def test_leftovers_are_cleaned_one_at_a_time_and_only_if_leftover(self):
        self.cluster.lh_nodes.append("gone")

        onboard.cleanup("longhorn", "gone")

        self.assertNotIn("gone", self.cluster.lh_nodes)
        self.assertIn(("PATCH", "/apis/longhorn.io/v1beta2/namespaces/longhorn-system/nodes/gone"), self.cluster.sent)
        with self.assertRaisesRegex(ValueError, "still has"):
            onboard.cleanup("longhorn", "node1")
        with self.assertRaisesRegex(ValueError, "not a leftover"):
            onboard.cleanup("machine", "custom-1")


class FatImageTests(unittest.TestCase):
    def test_files_come_back_as_written_with_long_names(self):
        files = {"EFI/BOOT/BOOTX64.EFI": bytes(range(256)) * 5000, "autoexec.ipxe": b"#!ipxe\n",
                 "Read Me Please.txt": b"hello"}

        back = fat.read(fat.build(files, 16))

        self.assertEqual(files, back)

    def test_a_partition_table_marks_one_bootable_fat_partition(self):
        image = fat.build({"a.txt": b"x"}, 16)

        self.assertEqual(16 * 1024 * 1024, len(image))
        self.assertEqual(b"\x55\xaa", image[510:512])
        self.assertEqual((0x80, 0x0E), (image[446], image[450]))

    def test_too_much_for_the_image_is_refused(self):
        with self.assertRaisesRegex(ValueError, "fit"):
            fat.build({"big.bin": b"\0" * (20 * 1024 * 1024)}, 16)


if __name__ == "__main__":
    unittest.main()
