"""Your VIPs, the registry of reserved addresses, is changed on what is stored
when it is saved, so two changes at once both stay (#621)."""
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.environ.get("HOMESTEAD_TEST_SERVER_DIR", str(Path(__file__).resolve().parents[1] / "server")))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_kube as K
import homestead_networking as NET
import homestead_network_state as STATE
import homestead_vip_registry as REG
from fakes.kube import Kube
from rebind import keep_bound

MAP = f"/api/v1/namespaces/lab/configmaps/{REG.VIP_MAP}"


def registry(rows=None, text=None):
    return {"apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": REG.VIP_MAP, "namespace": "lab", "labels": {"homestead.io/managed": "true"}},
            "data": {"vips.json": text if text is not None else json.dumps(rows)}}


class VipRegistryWriteTests(unittest.TestCase):
    def setUp(self):
        keep_bound(self, NET)   # first, so it is put back last: after the partial NET.bind() below
        saved = STATE.kget, STATE.ksend, STATE.DEFAULT_NAMESPACE, STATE.SHARED_VIP, set(STATE.SYSTEM_NAMESPACES)
        self.addCleanup(lambda: NET.bind(saved[0], saved[1], saved[4], saved[2], saved[3]))

    def bind(self, send=None):
        keep_bound(self, NET)   # put back what NET.bind() sets
        NET.bind(K.kget, send or K.send, {"kube-system"}, "lab", "")

    def stored(self, kube):
        return json.loads(kube.object(MAP)["data"]["vips.json"])

    def test_a_change_made_meanwhile_is_kept(self):
        with Kube(registry([{"ip": "192.0.2.10", "label": ""}])) as kube:
            raced = []

            def send(method, path, body=None, **options):
                # Another copy adds an address just before this one saves.
                if method == "PUT" and path == MAP and not raced:
                    raced.append(1)
                    other = kube.object(MAP)
                    rows = json.loads(other["data"]["vips.json"]) + [{"ip": "192.0.2.11", "label": "added meanwhile"}]
                    K.send("PUT", MAP, dict(other, data={"vips.json": json.dumps(rows)}))
                return K.send(method, path, body, **options)
            self.bind(send)
            REG.set_vip_label("192.0.2.10", "Media")
            rows = {row["ip"]: row for row in self.stored(kube)}
        self.assertEqual("Media", rows["192.0.2.10"]["label"])
        self.assertEqual("added meanwhile", rows["192.0.2.11"]["label"], "the other change was lost")

    def test_an_unreadable_registry_is_not_saved_over(self):
        with Kube(registry(text="[{not json")) as kube:
            self.bind()
            with self.assertRaises(ValueError):
                REG.set_vip_label("192.0.2.10", "Media")
            self.assertEqual("[{not json", kube.object(MAP)["data"]["vips.json"])

    def test_a_registry_that_is_not_a_list_is_not_saved_over(self):
        with Kube(registry(text='{"192.0.2.10": "Media"}')) as kube:
            self.bind()
            with self.assertRaisesRegex(ValueError, "not a list"):
                REG.set_vip_label("192.0.2.10", "Media")
            self.assertEqual('{"192.0.2.10": "Media"}', kube.object(MAP)["data"]["vips.json"])

    def test_a_new_registry_is_created_sorted_and_readable(self):
        with Kube() as kube:
            self.bind()
            REG._update_registered(lambda rows: rows + [{"ip": "192.0.2.20"}, {"ip": "192.0.2.3"}])
            self.assertEqual(["192.0.2.3", "192.0.2.20"], [row["ip"] for row in self.stored(kube)])
            self.assertEqual(["192.0.2.3", "192.0.2.20"], [row["ip"] for row in REG.registered()])


if __name__ == "__main__":
    unittest.main()
