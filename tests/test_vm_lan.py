import base64, json, sys, unittest, urllib.error
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_imports as imports
import homestead_k3scluster as K3S

NAD = "/apis/k8s.cni.cncf.io/v1/namespaces/default/network-attachment-definitions/vlan1"


class StaticAddressTests(unittest.TestCase):
    """A VM on a VM network bridged to the LAN, with an address of its own."""

    def setUp(self):
        self.objects = {NAD: {"metadata": {"name": "vlan1"}}}
        self.sent = []

        def get(path):
            if path in self.objects:
                return self.objects[path]
            raise urllib.error.HTTPError(path, 404, "missing", {}, None)

        def send(method, path, body=None, **kwargs):
            self.sent.append((method, path, body))
            if path.endswith("/virtualmachines"):
                return dict(body, metadata=dict(body["metadata"], uid="uid-1"))
            return body
        imports.kget, imports.ksend, imports.NS, imports._cache = get, send, "lab", {}

    def create(self, **cfg):
        cfg = {"name": "k3s-demo-server-1", "password": "a-long-password", **cfg}
        return imports.create_vm(cfg, {"harvester": True, "cdi": True}, "longhorn-r2")

    def test_a_bridged_vm_gets_its_address_through_cloud_init(self):
        result = self.create(network="default/vlan1",
                             static_ip={"address": "192.0.2.60", "prefix": 24, "gateway": "192.0.2.1"})
        vm = next(b for m, p, b in self.sent if p.endswith("/virtualmachines"))
        spec = vm["spec"]["template"]["spec"]
        iface = spec["domain"]["devices"]["interfaces"][0]
        self.assertEqual({"name": "default", "multus": {"networkName": "default/vlan1"}}, spec["networks"][0])
        self.assertIn("bridge", iface)
        self.assertEqual(result["mac"], iface["macAddress"])
        secret = next(b for m, p, b in self.sent if p.endswith("/secrets"))
        network = base64.b64decode(secret["data"]["networkdata"]).decode()
        self.assertIn('macaddress: "%s"' % result["mac"], network)
        self.assertIn('addresses: ["192.0.2.60/24"]', network)
        self.assertIn("via: 192.0.2.1", network)
        self.assertEqual("192.0.2.60", result["address"])
        # The password lives in the Secret, not the VM's own definition.
        self.assertNotIn("a-long-password", json.dumps(vm))
        cloud = next(v for v in spec["volumes"] if v["name"] == "cloudinit")["cloudInitNoCloud"]
        self.assertEqual({"secretRef": {"name": "k3s-demo-server-1-cloudinit"},
                          "networkDataSecretRef": {"name": "k3s-demo-server-1-cloudinit"}}, cloud)
        owner = next(b for m, p, b in self.sent if m == "PATCH" and "/secrets/" in p)
        self.assertEqual("uid-1", owner["metadata"]["ownerReferences"][0]["uid"])

    def test_the_pod_network_cannot_have_an_address_of_its_own(self):
        with self.assertRaisesRegex(ValueError, r"LAN network \(bridged\)"):
            self.create(static_ip={"address": "192.0.2.60", "prefix": 24})

    def test_addresses_that_cannot_be_a_machines_are_refused(self):
        for address, gateway, pattern in (("192.0.2.0", "", "cannot be a machine's"),
                                          ("192.0.2.60", "10.0.0.1", "outside"),
                                          ("192.0.2.1", "192.0.2.1", "gateway's address")):
            with self.subTest(address=address), self.assertRaisesRegex(ValueError, pattern):
                self.create(network="default/vlan1", static_ip={"address": address, "prefix": 24, "gateway": gateway})

    def test_an_unknown_network_is_refused(self):
        with self.assertRaisesRegex(ValueError, "no LAN network default/nope"):
            self.create(network="default/nope")


class K3sClusterTests(unittest.TestCase):
    CFG = {"name": "k3s-demo", "servers": 1, "agents": 2, "network": "default/vlan1",
           "addresses": ["192.0.2.60", "192.0.2.61", "192.0.2.62"], "password": "a-long-password",
           "prefix": 24, "gateway": "192.0.2.1"}

    def setUp(self):
        self.made, self.taken = [], {}
        K3S.bind(lambda path: {}, lambda cfg: self.made.append(cfg), lambda ip: self.taken.get(ip, ""))

    def test_the_plan_gives_each_node_a_role_and_address(self):
        plan = K3S.plan(self.CFG)
        self.assertEqual([("k3s-demo-server-1", "server", "192.0.2.60"), ("k3s-demo-agent-1", "agent", "192.0.2.61"),
                          ("k3s-demo-agent-2", "agent", "192.0.2.62")],
                         [(n["name"], n["role"], n["address"]) for n in plan["nodes"]])
        self.assertEqual("http://192.0.2.60:8088", plan["url"])

    def test_the_plan_refuses_what_cannot_work(self):
        for change, pattern in (({"servers": 2}, "one server, or three"),
                                ({"addresses": ["192.0.2.60"]}, "need 3 addresses"),
                                ({"addresses": ["192.0.2.60"] * 3}, "an address of its own"),
                                ({"network": "pod"}, r"LAN network \(bridged\)")):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, pattern):
                K3S.plan(dict(self.CFG, **change))

    def test_nothing_is_made_when_an_address_is_taken(self):
        self.taken["192.0.2.62"] = "192.0.2.62 is taken: printer in IP addresses"
        with self.assertRaisesRegex(ValueError, "agent-2: 192.0.2.62 is taken"):
            K3S.start(self.CFG, None)
        self.assertEqual([], self.made)

    def test_servers_start_the_cluster_and_workers_join_it_with_one_token(self):
        class Ops:
            def start(self, *args, **kw):
                return {"id": "op"}
        K3S.start(self.CFG, Ops())
        server, agent = self.made[0]["cloud_init"], self.made[1]["cloud_init"]
        token = server.split("K3S_TOKEN=")[1].split(" ")[0]
        self.assertIn("bootstrap-k3s.sh | K3S_TOKEN=%s sh -s - server" % token, server)
        self.assertIn("sh -s - agent https://192.0.2.60:6443 %s" % token, agent)
        self.assertEqual({"address": "192.0.2.61", "prefix": 24, "gateway": "192.0.2.1", "dns": []},
                         self.made[1]["static_ip"])
        self.assertEqual({"homestead.io/k3s-cluster": "k3s-demo", "homestead.io/k3s-role": "agent"},
                         self.made[1]["labels"])
        # Their consoles are kept, so the job's Log shows each one installing.
        self.assertTrue(all(vm["log_console"] for vm in self.made))
        self.assertIn('password: "a-long-password"', server)

    def test_a_bare_cluster_uses_k3s_own_installer(self):
        node = {"name": "n", "role": "agent", "address": "192.0.2.61"}
        data = K3S.user_data(node, "192.0.2.60", "tok", "pw-long-enough", "k3s")
        self.assertIn("get.k3s.io | K3S_TOKEN=tok K3S_URL=https://192.0.2.60:6443 sh -s - agent", data)

    def test_the_job_follows_vms_then_k3s_then_homestead(self):
        item = {"ref": {"namespace": "lab", "nodes": [{"name": "k3s-demo-server-1"}], "first": "192.0.2.60",
                        "setup": "homestead", "started": __import__("time").time()}, "progress": 0}
        running = {"phase": "Pending"}
        K3S.kget = lambda path: {"status": running}
        answers = set()
        K3S._answers = lambda ip, port, timeout=2.0: port in answers
        self.assertIn("0 of 1 VMs running", K3S.status(item)[2])
        running["phase"] = "Running"
        self.assertIn("installing k3s", K3S.status(item)[2])
        answers.add(6443)
        self.assertIn("installing Longhorn and Homestead", K3S.status(item)[2])
        answers.add(8088)
        status, _, message = K3S.status(item)
        self.assertEqual("succeeded", status)
        self.assertIn("http://192.0.2.60:8088", message)


if __name__ == "__main__":
    unittest.main()
