"""A VIP move survives kube-vip or servicelb writing a Service's status while
it moves: a 409 on a Service is retried once from a fresh read with the same
change. A move that still cannot finish moves the Services it already moved
back to the old address, so nothing is left half-moved and trying again works;
if that fails too, the message says which Service is on which address."""
import copy
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.environ.get("HOMESTEAD_TEST_SERVER_DIR", str(Path(__file__).resolve().parents[1] / "server")))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_kube as K
import homestead_networking as NET
import homestead_network_inventory as INV
import homestead_network_state as STATE
import homestead_platform as PLATFORM
import homestead_routes
import homestead_vip_registry as REG
from fakes.kube import Kube

OLD, NEW = "192.0.2.200", "192.0.2.210"
MAP = f"/api/v1/namespaces/lab/configmaps/{REG.VIP_MAP}"
NAMES = ("web", "frigate", "minio")
LB = "kube-vip.io/loadbalancerIPs"


def path(name):
    return f"/api/v1/namespaces/lab/services/{name}"


def svc(name):
    return {"apiVersion": "v1", "kind": "Service",
            "metadata": {"name": name, "namespace": "lab",
                         "annotations": {LB: OLD, "kube-vip.io/leaseName": f"lease-{name}",
                                         "homestead.io/vip-mode": "manual"}},
            "spec": {"type": "LoadBalancer", "loadBalancerIP": OLD, "ports": [{"port": 80, "protocol": "TCP"}]},
            "status": {"loadBalancer": {"ingress": [{"ip": OLD}]}}}


def row(name):
    return {"namespace": "lab", "name": name, "type": "LoadBalancer", "targets": [name],
            "ports": [{"port": 80, "protocol": "TCP"}], "external_ips": [OLD], "requested_ips": [OLD], "system": False}


def registry():
    return {"apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": REG.VIP_MAP, "namespace": "lab", "labels": {"homestead.io/managed": "true"}},
            "data": {"vips.json": json.dumps([{"ip": OLD, "label": "Apps", "default": True}])}}


class VipMoveConflictTests(unittest.TestCase):
    def setUp(self):
        saved = STATE.kget, STATE.ksend, STATE.DEFAULT_NAMESPACE, STATE.SHARED_VIP, set(STATE.SYSTEM_NAMESPACES)
        self.addCleanup(lambda: NET.bind(saved[0], saved[1], saved[4], saved[2], saved[3]))
        self.inventory = {"node_ips": ["192.0.2.21"], "services": [row(name) for name in NAMES]}
        for patch in (mock.patch.object(INV, "inventory", lambda: copy.deepcopy(self.inventory)),
                      mock.patch.object(INV, "check_address", lambda ip, state=None: None),
                      mock.patch.object(PLATFORM, "vip_annotations", lambda vip: {LB: vip})):
            patch.start()
            self.addCleanup(patch.stop)

    def move(self, kube, conflicts):
        """change_vip applied. conflicts(name, to) says how many of the PUTs that
        put Service name on address to have kube-vip write its status first (a
        newer resourceVersion). self.puts counts those PUTs per (name, to)."""
        self.puts = {}

        def send(method, target, body=None, **options):
            name = target.rsplit("/", 1)[-1]
            if method == "PUT" and name in NAMES:
                to = body["metadata"]["annotations"].get(LB)
                self.puts[name, to] = self.puts.get((name, to), 0) + 1
                if conflicts(name, to) >= self.puts[name, to]:
                    now = kube.object(target)
                    now["status"]["loadBalancer"]["ingress"].append({"ip": "192.0.2.99", "ipMode": "VIP"})
                    K.send("PUT", target, now)
            return K.send(method, target, body, **options)
        NET.bind(K.kget, send, {"kube-system"}, "lab", "")
        return REG.change_vip(OLD, NEW, apply=True)

    def where(self, kube):
        return {name: kube.object(path(name))["metadata"]["annotations"].get(LB) for name in NAMES}

    def registry_ips(self, kube):
        return [r["ip"] for r in json.loads(kube.object(MAP)["data"]["vips.json"])]

    def test_a_status_write_during_the_move_is_retried_and_every_service_moves(self):
        with Kube(registry(), *[svc(name) for name in NAMES]) as kube:
            self.move(kube, lambda name, to: 1 if name == "frigate" else 0)
            self.assertEqual({name: NEW for name in NAMES}, self.where(kube))
            self.assertEqual("manual", kube.object(path("frigate"))["metadata"]["annotations"]["homestead.io/vip-mode"])
            self.assertEqual([NEW], self.registry_ips(kube), "the registry follows the Services")

    def test_the_retry_keeps_what_kube_vip_wrote_meanwhile(self):
        with Kube(registry(), *[svc(name) for name in NAMES]) as kube:
            self.move(kube, lambda name, to: 1 if name == "frigate" else 0)
            ingress = kube.object(path("frigate"))["status"]["loadBalancer"]["ingress"]
            self.assertIn({"ip": "192.0.2.99", "ipMode": "VIP"}, ingress, "the retry wrote over a fresher Service")

    def test_a_conflict_is_retried_once_not_for_ever(self):
        with Kube(registry(), *[svc(name) for name in NAMES]) as kube:
            with self.assertRaises(ValueError):
                self.move(kube, lambda name, to: 99 if (name, to) == ("minio", NEW) else 0)
        self.assertEqual(2, self.puts["minio", NEW], "one try and one retry from a fresh read")

    def test_a_second_conflict_moves_the_others_back_and_nothing_is_moved(self):
        with Kube(registry(), *[svc(name) for name in NAMES]) as kube:
            with self.assertRaisesRegex(ValueError, r"lab/minio kept changing.*nothing was moved.*try the move again") as caught:
                self.move(kube, lambda name, to: 99 if (name, to) == ("minio", NEW) else 0)
            self.assertNotIn("409", str(caught.exception))
            self.assertEqual({name: OLD for name in NAMES}, self.where(kube), "Services left on the new address")
            for name in ("web", "frigate"):
                obj = kube.object(path(name))
                self.assertEqual(f"lease-{name}", obj["metadata"]["annotations"].get("kube-vip.io/leaseName"),
                                 "moved back as they were, not just to the old address")
                self.assertEqual(OLD, obj["spec"]["loadBalancerIP"])
            self.assertEqual([OLD], self.registry_ips(kube), "Your VIPs is not changed")

    def test_an_error_other_than_a_conflict_moves_the_others_back_too(self):
        with Kube(registry(), *[svc(name) for name in NAMES]) as kube:
            kube.fail(path("minio"), 500, times=1, method="PUT")
            with self.assertRaisesRegex(ValueError, r"lab/minio could not be moved.*nothing was moved.*try the move again"):
                self.move(kube, lambda name, to: 0)
            self.assertEqual({name: OLD for name in NAMES}, self.where(kube))
            self.assertEqual([OLD], self.registry_ips(kube))

    def test_a_write_that_landed_though_its_reply_was_lost_is_moved_back_too(self):
        with Kube(registry(), *[svc(name) for name in NAMES]) as kube:
            kube.fail(path("minio"), 500, times=1, method="PUT", applied=True)
            with self.assertRaisesRegex(ValueError, r"nothing was moved"):
                self.move(kube, lambda name, to: 0)
            self.assertEqual({name: OLD for name in NAMES}, self.where(kube))
            self.assertEqual("lease-minio", kube.object(path("minio"))["metadata"]["annotations"].get("kube-vip.io/leaseName"))

    def test_a_move_back_that_fails_too_says_which_service_is_where(self):
        with Kube(registry(), *[svc(name) for name in NAMES]) as kube:
            def conflicts(name, to):
                return 99 if (name, to) in (("minio", NEW), ("web", OLD)) else 0
            with self.assertRaises(ValueError) as caught:
                self.move(kube, conflicts)
            message = str(caught.exception)
            self.assertEqual({"web": NEW, "frigate": OLD, "minio": OLD}, self.where(kube))
            self.assertRegex(message, rf"lab/web is still on {NEW}")
            self.assertRegex(message, rf"lab/frigate and lab/minio are on {OLD}")
            self.assertIn("not try the move again", message)
            self.assertEqual(2, self.puts["web", OLD], "the move back is retried once too")
            self.assertEqual([OLD], self.registry_ips(kube))

    def test_a_single_service_that_cannot_be_moved_or_put_back_is_named(self):
        self.inventory["services"] = [row("web")]
        with Kube(registry(), svc("web")) as kube:
            kube.fail(path("web"), 500, times=2, method="PUT")      # the move, then the put-back
            with self.assertRaises(ValueError) as caught:
                self.move(kube, lambda name, to: 0)
            message = str(caught.exception)
            self.assertIn("lab/web may be on either address", message)
            self.assertIn("not try the move again", message)
            self.assertEqual([OLD], self.registry_ips(kube))

    def test_a_service_whose_own_move_and_put_back_failed_is_not_said_to_be_on_the_new_address(self):
        with Kube(registry(), *[svc(name) for name in NAMES]) as kube:
            kube.fail(path("minio"), 500, times=2, method="PUT")
            with self.assertRaises(ValueError) as caught:
                self.move(kube, lambda name, to: 0)
            message = str(caught.exception)
            self.assertIn("lab/minio may be on either address; check it in Edit", message)
            self.assertNotIn("still on", message, "nothing is known to have moved and stayed")
            self.assertRegex(message, rf"lab/web and lab/frigate are on {OLD}")
            self.assertEqual({name: OLD for name in NAMES}, self.where(kube))

    def test_a_refused_move_still_drops_the_cached_overview(self):
        class Request:
            body = {"old": OLD, "new": NEW, "apply": True}
        forgotten = []
        with mock.patch.object(REG, "change_vip", side_effect=ValueError("lab/web may be on either address")), \
                mock.patch.object(homestead_routes, "forget", lambda *keys: forgotten.append(keys)):
            with self.assertRaises(ValueError):
                NET._change_vip_route(Request())
        self.assertIn(("network", "ov"), forgotten, "the page would show the addresses from before the move")


if __name__ == "__main__":
    unittest.main()
