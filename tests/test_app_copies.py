"""#841: app volumes on a one-host Longhorn cluster.

Stock Longhorn's `longhorn` class asks for three copies, and one host holds
one, so every app volume reads degraded. #809 lowered Homestead's own data
volume by itself; app volumes are people's own, so homestead_app_copies only
offers: it lists the volumes that cannot be placed (by #809's tests), and an
admin's reviewed click lowers them, recording the count they asked for. Once
a second host can hold copies, it offers to put them back.

On the shared fake API (tests/fakes/kube.py)."""
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_kube as K
import homestead_node_parity as PARITY
import homestead_operations as OPS
import homestead_app_copies as APP_COPIES
from fakes.kube import Kube

LH_NODES = PARITY.LH_NODES
VOLUMES = PARITY.LH_VOLUMES
KEY = "homestead.io/one-host-copies"
GiB = 1024 ** 3
LH_VERSION = "longhorn.io/v1beta2"


def node(name, ready=True):
    return {"apiVersion": "v1", "kind": "Node", "metadata": {"name": name},
            "status": {"conditions": [{"type": "Ready", "status": "True" if ready else "False"}]}}


def lh_node(name, ready=True):
    return {"apiVersion": LH_VERSION, "kind": "Node", "metadata": {"namespace": "longhorn-system", "name": name},
            "spec": {"allowScheduling": True, "disks": {"d": {"allowScheduling": True}}},
            "status": {"conditions": [{"type": "Ready", "status": "True" if ready else "False"}]}}


def volume(name, copies=3, robustness="degraded", unschedulable=True, claim="", ns="media", workload="",
           lowered_from=None):
    scheduled = ({"type": "Scheduled", "status": "False", "reason": "ReplicaSchedulingFailure",
                  "message": "replica scheduling failed"} if unschedulable else
                 {"type": "Scheduled", "status": "True", "reason": ""})
    meta = {"namespace": "longhorn-system", "name": name}
    if lowered_from is not None:
        meta["annotations"] = {KEY: str(lowered_from)}
    workloads = [{"workloadName": workload, "workloadType": "Deployment"}] if workload else []
    return {"apiVersion": LH_VERSION, "kind": "Volume", "metadata": meta,
            "spec": {"numberOfReplicas": copies, "size": str(2 * GiB)},
            "status": {"robustness": robustness, "conditions": [scheduled],
                       "kubernetesStatus": {"namespace": ns, "pvcName": claim or name + "-claim",
                                            "workloadsStatus": workloads}}}


ENGINES = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system/engines"


def engine(pv, *modes):
    return {"apiVersion": LH_VERSION, "kind": "Engine", "metadata": {"namespace": "longhorn-system", "name": f"{pv}-e"},
            "spec": {"volumeName": pv},
            "status": {"replicaModeMap": {f"{pv}-r-{i}": mode for i, mode in enumerate(modes)}}}


class Cluster:
    """Homestead's own data volume is pvc-own; the app volumes are the rest.
    Every engine is read in one list; modes[pv] sets a volume's copies. The
    fake API starts when a test binds the cluster, so modes and fail can be
    set first: a path in fail is refused with a 500, and the PATCH of
    failing_patch with a 409 that changes nothing."""
    def __init__(self, nodes, lh_nodes, volumes, failing_patch=None):
        self.nodes, self.lh_nodes, self.volumes = nodes, lh_nodes, volumes
        self.modes = {v["metadata"]["name"]: ("RW",) for v in volumes}
        self.read = []
        self.fail = set()
        self.failing_patch = failing_patch

    def start(self, test):
        deployment = {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"namespace": "lab", "name": "homestead"},
                      "spec": {"template": {"spec": {"volumes": [
                          {"name": "data", "persistentVolumeClaim": {"claimName": "homestead-data"}}]}}}}
        claim = {"apiVersion": "v1", "kind": "PersistentVolumeClaim",
                 "metadata": {"namespace": "lab", "name": "homestead-data"},
                 "spec": {"volumeName": "pvc-own", "storageClassName": "longhorn"}}
        self.kube = test.enterContext(Kube(*self.nodes, *self.lh_nodes, *self.volumes, deployment, claim,
                                           *(engine(pv, *modes) for pv, modes in self.modes.items())))
        for path in self.fail:
            self.kube.fail(path, 500, times=99, method="GET")
        if self.failing_patch:
            self.kube.fail(self.failing_patch, 409, times=99, method="PATCH")
        self.kube.react("GET", ENGINES, lambda method, path, body: self.read.append(path))   # counts; the fake answers
        return self

    @property
    def sent(self):
        """The writes that went through: a refused PATCH changed nothing."""
        return [(method, path, body) for method, path, body, _ in self.kube.writes if path != self.failing_patch]


ONE = ([node("node-1")], [lh_node("node-1")])
TWO = ([node("node-1"), node("node-2")], [lh_node("node-1"), lh_node("node-2")])
THREE = ([node("node-1"), node("node-2"), node("node-3")], [lh_node("node-1"), lh_node("node-2"), lh_node("node-3")])


class Base(unittest.TestCase):
    def setUp(self):
        self.data = tempfile.mkdtemp()

    def bind(self, cluster, distribution="k3s", longhorn=True, harvester=False):
        cluster.start(self)
        # The shared fake runs the real write guards; the storage guard takes the
        # job store's lock, which lives under its DATA_DIR.
        self.enterContext(mock.patch.object(OPS, "DATA_DIR", self.data))
        self.addCleanup(PARITY.bind, *(getattr(PARITY, name, None) for name in (
            "kget", "ksend", "hostrun", "platform", "probes", "DATA_DIR", "OWN")))
        hostrun = type("H", (), {"run": staticmethod(lambda *a, **k: ("", ""))})
        PARITY.bind(K.kget, K.ksend, hostrun,
                    lambda force=False: {"distribution": distribution, "longhorn": longhorn, "harvester": harvester},
                    lambda: {}, self.data, ("lab", "homestead"))
        return cluster


class OfferTests(Base):
    def test_one_host_offers_each_unplaceable_app_volume_with_its_app_and_counts(self):
        self.bind(Cluster(*ONE, [volume("pvc-a", claim="photos", workload="immich"),
                                 volume("pvc-b", copies=2, claim="notes", ns="docs")]))
        found = APP_COPIES.facts()
        self.assertEqual("lower", found["state"])
        self.assertEqual(1, found["hosts"])
        self.assertEqual([{"volume": "pvc-a", "claim": "photos", "namespace": "media", "app": "media/immich", "from": 3, "to": 1},
                          {"volume": "pvc-b", "claim": "notes", "namespace": "docs", "app": "docs/notes", "from": 2, "to": 1}],
                         found["volumes"])

    def test_reading_changes_nothing(self):
        cluster = self.bind(Cluster(*ONE, [volume("pvc-a")]))
        APP_COPIES.facts()
        self.assertEqual([], cluster.sent)

    def test_homestead_own_data_volume_is_never_offered(self):
        self.bind(Cluster(*ONE, [volume("pvc-own", claim="homestead-data", ns="lab"), volume("pvc-a")]))
        self.assertEqual(["pvc-a"], [v["volume"] for v in APP_COPIES.facts()["volumes"]])

    def test_only_volumes_longhorn_cannot_place(self):
        self.bind(Cluster(*ONE, [volume("pvc-healthy", robustness="healthy", unschedulable=False),
                                 volume("pvc-placeable", unschedulable=False),
                                 volume("pvc-one", copies=1),
                                 volume("pvc-a")]))
        self.assertEqual(["pvc-a"], [v["volume"] for v in APP_COPIES.facts()["volumes"]])

    def test_a_copy_rebuilding_is_never_offered(self):
        cluster = Cluster(*ONE, [volume("pvc-a"), volume("pvc-b")])
        cluster.modes["pvc-a"] = ("RW", "WO")
        self.bind(cluster)
        self.assertEqual(["pvc-b"], [v["volume"] for v in APP_COPIES.facts()["volumes"]])

    def test_the_engines_are_read_once_for_every_volume(self):
        cluster = self.bind(Cluster(*ONE, [volume(f"pvc-{i}") for i in range(5)]))
        self.assertEqual(5, len(APP_COPIES.facts()["volumes"]))
        self.assertEqual(1, sum(1 for path in cluster.read if "/engines" in path))

    def test_a_bigger_cluster_with_a_host_down_offers_nothing(self):
        for nodes, lh_nodes in (([node("node-1"), node("node-2", ready=False)], [lh_node("node-1"), lh_node("node-2", ready=False)]),
                                ([node("node-1")], [lh_node("node-1"), lh_node("node-2")]),
                                ([node("node-1"), node("node-2")], [lh_node("node-1")])):
            with self.subTest(nodes=len(nodes), lh_nodes=len(lh_nodes)):
                self.bind(Cluster(nodes, lh_nodes, [volume("pvc-a")]))
                found = APP_COPIES.facts()
                self.assertEqual([], found["volumes"])
                self.assertEqual("none", found["state"])

    def test_not_on_harvester_or_without_longhorn(self):
        for kwargs in ({"harvester": True}, {"longhorn": False}):
            with self.subTest(**kwargs):
                self.bind(Cluster(*ONE, [volume("pvc-a")]), **kwargs)
                self.assertEqual({"state": "n/a", "hosts": 0, "volumes": []}, APP_COPIES.facts())

    def test_failed_reads_offer_nothing_and_say_so(self):
        for path in ("/api/v1/nodes", LH_NODES, VOLUMES, ENGINES,
                     "/apis/apps/v1/namespaces/lab/deployments/homestead"):
            with self.subTest(path=path):
                cluster = Cluster(*ONE, [volume("pvc-a")])
                cluster.fail.add(path)
                self.bind(cluster)
                found = APP_COPIES.facts()
                self.assertEqual("unknown", found["state"])
                self.assertEqual([], found["volumes"])
                self.assertTrue(found["error"])


class LowerTests(Base):
    def reviewed(self, *rows):
        return {"action": "lower", "volumes": [{"volume": v, "from": f, "to": t} for v, f, t in rows]}

    def test_the_click_lowers_each_reviewed_volume_and_records_its_count(self):
        cluster = self.bind(Cluster(*ONE, [volume("pvc-a"), volume("pvc-b", copies=2)]))
        out = APP_COPIES.apply(self.reviewed(("pvc-a", 3, 1), ("pvc-b", 2, 1)))
        self.assertTrue(out["ok"], out)
        self.assertEqual([("PATCH", f"{VOLUMES}/pvc-a", {"metadata": {"annotations": {KEY: "3"}}, "spec": {"numberOfReplicas": 1}}),
                          ("PATCH", f"{VOLUMES}/pvc-b", {"metadata": {"annotations": {KEY: "2"}}, "spec": {"numberOfReplicas": 1}})],
                         cluster.sent)

    def test_no_longhorn_setting_class_or_other_volume_is_touched(self):
        cluster = self.bind(Cluster(*ONE, [volume("pvc-a"), volume("pvc-b")]))
        APP_COPIES.apply(self.reviewed(("pvc-a", 3, 1)))
        self.assertEqual([f"{VOLUMES}/pvc-a"], [path for _, path, _ in cluster.sent])

    def test_a_review_that_no_longer_matches_changes_nothing(self):
        for rows in ((("pvc-a", 2, 1),), (("pvc-gone", 3, 1),), (("pvc-own", 3, 1),), (("pvc-a", 3, 2),), ()):
            with self.subTest(rows=rows):
                cluster = self.bind(Cluster(*ONE, [volume("pvc-a"), volume("pvc-own", claim="homestead-data", ns="lab")]))
                out = APP_COPIES.apply(self.reviewed(*rows))
                self.assertFalse(out["ok"])
                self.assertEqual([], cluster.sent)

    def test_a_volume_that_started_rebuilding_since_the_review_is_refused(self):
        cluster = Cluster(*ONE, [volume("pvc-a")])
        cluster.modes["pvc-a"] = ("RW", "WO")
        self.bind(cluster)
        self.assertFalse(APP_COPIES.apply(self.reviewed(("pvc-a", 3, 1)))["ok"])
        self.assertEqual([], cluster.sent)

    def test_lowering_again_keeps_the_count_first_asked_for(self):
        # 3 -> 1 on one host, back to 2 when a host joined, and one host again.
        cluster = self.bind(Cluster(*ONE, [volume("pvc-a", copies=2, lowered_from=3)]))
        self.assertTrue(APP_COPIES.apply(self.reviewed(("pvc-a", 2, 1)))["ok"])
        self.assertEqual({KEY: "3"}, cluster.sent[0][2]["metadata"]["annotations"])

    def test_a_refused_change_says_what_did_change(self):
        cluster = self.bind(Cluster(*ONE, [volume("pvc-a"), volume("pvc-b"), volume("pvc-c")],
                                    failing_patch=f"{VOLUMES}/pvc-b"))
        out = APP_COPIES.apply(self.reviewed(("pvc-a", 3, 1), ("pvc-b", 3, 1), ("pvc-c", 3, 1)))
        self.assertFalse(out["ok"])
        self.assertEqual(["pvc-a", "pvc-c"], out["changed"])
        self.assertEqual([f"{VOLUMES}/pvc-a", f"{VOLUMES}/pvc-c"], [path for _, path, _ in cluster.sent])
        self.assertEqual(["pvc-b"], [f["volume"] for f in out["failed"]])
        self.assertIn("pvc-b", out["message"])
        self.assertIn("2 volumes", out["message"])

    def test_the_offer_is_read_afresh_after_any_change(self):
        import homestead_routes
        forgotten = []
        saved = homestead_routes.forget
        homestead_routes.forget = forgotten.append
        try:
            self.bind(Cluster(*ONE, [volume("pvc-a")], failing_patch=f"{VOLUMES}/pvc-a"))
            APP_COPIES.apply(self.reviewed(("pvc-a", 3, 1)))
        finally:
            homestead_routes.forget = saved
        self.assertEqual([APP_COPIES.CACHE], forgotten)

    def test_failed_reads_change_nothing(self):
        cluster = Cluster(*ONE, [volume("pvc-a")])
        cluster.fail.add(LH_NODES)
        self.bind(cluster)
        out = APP_COPIES.apply(self.reviewed(("pvc-a", 3, 1)))
        self.assertFalse(out["ok"])
        self.assertIn("could not", out["message"].lower())
        self.assertEqual([], cluster.sent)

    def test_an_unknown_action_is_refused(self):
        cluster = self.bind(Cluster(*ONE, [volume("pvc-a")]))
        with self.assertRaises(ValueError):
            APP_COPIES.apply({"action": "delete", "volumes": [{"volume": "pvc-a", "from": 3, "to": 1}]})
        self.assertEqual([], cluster.sent)


class RaiseTests(Base):
    def lowered(self, name, recorded=3, copies=1, **kw):
        return volume(name, copies=copies, robustness="healthy", unschedulable=False, lowered_from=recorded, **kw)

    def test_one_host_offers_no_raise(self):
        self.bind(Cluster(*ONE, [self.lowered("pvc-a")]))
        self.assertEqual("none", APP_COPIES.facts()["state"])

    def test_a_second_host_offers_the_recorded_count_as_far_as_hosts_allow(self):
        self.bind(Cluster(*TWO, [self.lowered("pvc-a", claim="photos", workload="immich"),
                                 self.lowered("pvc-b", recorded=2)]))
        found = APP_COPIES.facts()
        self.assertEqual("raise", found["state"])
        self.assertEqual(2, found["hosts"])
        self.assertEqual([("pvc-a", 1, 2), ("pvc-b", 1, 2)], [(v["volume"], v["from"], v["to"]) for v in found["volumes"]])
        self.assertEqual("media/immich", found["volumes"][0]["app"])

    def test_volumes_homestead_did_not_lower_are_not_offered(self):
        self.bind(Cluster(*TWO, [volume("pvc-mine", copies=1, robustness="healthy", unschedulable=False),
                                 self.lowered("pvc-own", claim="homestead-data", ns="lab"),
                                 self.lowered("pvc-raised", copies=3)]))
        self.assertEqual([], APP_COPIES.facts()["volumes"])

    def test_hosts_longhorn_cannot_store_on_do_not_count(self):
        down = lh_node("node-2")
        down["spec"]["allowScheduling"] = False
        self.bind(Cluster([node("node-1"), node("node-2")], [lh_node("node-1"), down], [self.lowered("pvc-a")]))
        self.assertEqual([], APP_COPIES.facts()["volumes"])

    def test_raising_part_way_keeps_the_record_and_all_the_way_drops_it(self):
        cluster = self.bind(Cluster(*TWO, [self.lowered("pvc-a"), self.lowered("pvc-b", recorded=2)]))
        out = APP_COPIES.apply({"action": "raise", "volumes": [{"volume": "pvc-a", "from": 1, "to": 2},
                                                               {"volume": "pvc-b", "from": 1, "to": 2}]})
        self.assertTrue(out["ok"], out)
        self.assertEqual([("PATCH", f"{VOLUMES}/pvc-a", {"spec": {"numberOfReplicas": 2}}),
                          ("PATCH", f"{VOLUMES}/pvc-b", {"metadata": {"annotations": {KEY: None}}, "spec": {"numberOfReplicas": 2}})],
                         cluster.sent)

    def test_a_third_host_offers_the_rest(self):
        self.bind(Cluster(*THREE, [self.lowered("pvc-a", copies=2)]))
        self.assertEqual([("pvc-a", 2, 3)], [(v["volume"], v["from"], v["to"]) for v in APP_COPIES.facts()["volumes"]])

    def test_an_unreadable_record_is_not_offered(self):
        self.bind(Cluster(*TWO, [self.lowered("pvc-a", recorded="three")]))
        self.assertEqual([], APP_COPIES.facts()["volumes"])


class RouteTests(unittest.TestCase):
    def test_reading_is_for_viewers_and_the_click_for_admins(self):
        routes = APP_COPIES.ROUTES
        self.assertEqual("viewer", routes[("GET", "/api/longhorn/one-host-copies")][0])
        self.assertEqual("admin", routes[("POST", "/api/longhorn/one-host-copies")][0])


if __name__ == "__main__":
    unittest.main()
