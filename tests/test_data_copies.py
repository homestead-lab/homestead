"""#731 item 3: Homestead's own data volume on a one-host Longhorn cluster.

Stock Longhorn's `longhorn` class asks for three copies, and one host holds
one, so the volume reads degraded and every warning built on that says so.
homestead_data_copies lowers only Homestead's own data volume, only on a
cluster with exactly one Kubernetes node and one Longhorn node."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_kube as K
import homestead_node_parity as PARITY
import homestead_data_copies as COPIES
import homestead_lhcapacity as LHCAP
import homestead_changes as CHANGES
import homestead_host_access as HOSTACCESS
import homestead_operations as OPS
from fakes.kube import Kube

LH_NODES = PARITY.LH_NODES
GiB = 1024 ** 3
LONGHORN = {"apiVersion": "longhorn.io/v1beta2"}
VOLUME = f"{PARITY.LH_VOLUMES}/pvc-0001"
CLAIM = "/api/v1/namespaces/lab/persistentvolumeclaims/homestead-data"


def node(name, ready=True):
    return {"apiVersion": "v1", "kind": "Node",
            "metadata": {"name": name}, "status": {"conditions": [{"type": "Ready", "status": "True" if ready else "False"}]}}


def lh_node(name, ready=True):
    return {**LONGHORN, "kind": "Node", "metadata": {"name": name, "namespace": "longhorn-system"},
            "spec": {"allowScheduling": True, "disks": {"d": {"allowScheduling": True}}},
            "status": {"conditions": [{"type": "Ready", "status": "True" if ready else "False"}]}}


def volume(copies, robustness="degraded", size=2 * GiB, unschedulable=True, name="pvc-0001"):
    """unschedulable: Longhorn's own proof that a copy cannot be placed - its
    Scheduled condition False, reason ReplicaSchedulingFailure."""
    scheduled = ({"type": "Scheduled", "status": "False", "reason": "ReplicaSchedulingFailure",
                  "message": "replica scheduling failed"} if unschedulable else
                 {"type": "Scheduled", "status": "True", "reason": ""})
    return {**LONGHORN, "kind": "Volume", "metadata": {"name": name, "namespace": "longhorn-system"},
            "spec": {"numberOfReplicas": copies, "size": str(size)},
            "status": {"robustness": robustness, "conditions": [scheduled]}}


ENGINES = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system/engines?labelSelector=longhornvolume%3Dpvc-0001"


def engines(*modes):
    return {**LONGHORN, "kind": "Engine",
            "metadata": {"name": "pvc-0001-e-0", "namespace": "longhorn-system", "labels": {"longhornvolume": "pvc-0001"}},
            "spec": {"volumeName": "pvc-0001"},
            "status": {"replicaModeMap": {f"pvc-0001-r-{i}": mode for i, mode in enumerate(modes)}}}


class Cluster:
    """The nodes, Homestead's data volume and Longhorn's records of it, on the
    shared fake API (tests/fakes/kube.py). extra: more objects."""
    kget, ksend = staticmethod(K.kget), staticmethod(K.ksend)

    def __init__(self, case, nodes, lh_nodes, copies=3, robustness="degraded", storage_class="longhorn", extra=()):
        # The shared fake runs the real write guards; the storage guard takes the
        # job store's lock, which lives under its DATA_DIR.
        case.enterContext(mock.patch.object(OPS, "DATA_DIR", case.enterContext(tempfile.TemporaryDirectory())))
        # No signed-in person on this thread, whatever an earlier test left behind: the write guards read who is asking.
        case.enterContext(mock.patch.object(HOSTACCESS, "role", lambda: None))
        case.enterContext(mock.patch.object(CHANGES.REQUEST, "user", None, create=True))
        self.kube = case.enterContext(Kube(
            *nodes, *lh_nodes,
            {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "homestead", "namespace": "lab"},
             "spec": {"template": {"spec": {"volumes": [
                 {"name": "data", "persistentVolumeClaim": {"claimName": "homestead-data"}},
                 {"name": "tmp", "emptyDir": {}}]}}}},
            {"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": {"name": "homestead-data", "namespace": "lab"},
             "spec": {"volumeName": "pvc-0001", "storageClassName": storage_class,
                      "resources": {"requests": {"storage": "2Gi"}}}},
            volume(copies, robustness), volume(3, name="pvc-0002"), engines("RW"), *extra))

    @property
    def sent(self):
        return [(method, path, body) for method, path, body, _ in self.kube.writes]

    def edit(self, path, change):
        found = self.kube.object(path)
        change(found)
        self.kube.add(found)

    def remove(self, path):
        """Gone from the cluster; not a write of Homestead's."""
        written = len(self.kube.writes)
        self.kube("DELETE", path)
        del self.kube.writes[written:]

    def unreadable(self, path):
        """The read of path fails with an error that names it, as a dropped connection's does."""
        def refuse(method, where, body):
            raise OSError(f"cannot read {path}")
        self.kube.react("GET", path.partition("?")[0], refuse)


class FitSingleHostTests(unittest.TestCase):
    def setUp(self):
        self.data = tempfile.mkdtemp()

    def bind(self, cluster, distribution="kubeadm", longhorn=True, harvester=False):
        hostrun = type("H", (), {"run": staticmethod(lambda *a, **k: ("", ""))})
        PARITY.bind(cluster.kget, cluster.ksend, hostrun,
                    lambda force=False: {"distribution": distribution, "longhorn": longhorn, "harvester": harvester},
                    lambda: {}, self.data, ("lab", "homestead"))

    def test_one_host_stock_longhorn_lowers_own_volume_to_one(self):
        cluster = Cluster(self, [node("node-a")], [lh_node("node-a")])
        self.bind(cluster)
        line = COPIES.fit_single_host()
        self.assertEqual("Homestead's data volume homestead-data keeps 1 copy: this cluster has one host", line)
        self.assertEqual([("PATCH", f"{PARITY.LH_VOLUMES}/pvc-0001",
                           {"metadata": {"annotations": {"homestead.io/one-host-copies": "3"}},
                            "spec": {"numberOfReplicas": 1}})], cluster.sent)
        self.assertEqual(1, PARITY._load()["own_copies"])

    def test_other_volumes_and_settings_untouched(self):
        cluster = Cluster(self, [node("node-a")], [lh_node("node-a")])
        self.bind(cluster)
        COPIES.fit_single_host()
        paths = [path for _, path, _ in cluster.sent]
        self.assertNotIn(f"{PARITY.LH_VOLUMES}/pvc-0002", paths)
        self.assertFalse([p for p in paths if "settings" in p or "storageclasses" in p or "helmcharts" in p], paths)

    def test_any_distribution(self):
        for distribution in ("k3s", "rke2", "kubeadm", "talos", ""):
            with self.subTest(distribution=distribution):
                cluster = Cluster(self, [node("node-a")], [lh_node("node-a")])
                self.bind(cluster, distribution=distribution)
                self.assertTrue(COPIES.fit_single_host())

    def test_two_k8s_nodes_one_longhorn_node_lowers_nothing(self):
        # A tiebreaker, or an agent Longhorn does not run on: still two hosts.
        cluster = Cluster(self, [node("node-a"), node("node-b")], [lh_node("node-a")])
        self.bind(cluster)
        self.assertIsNone(COPIES.fit_single_host())
        self.assertEqual([], cluster.sent)

    def test_one_ready_of_three_lowers_nothing(self):
        # Review focus 1: hosts down or rebooting are still hosts.
        cluster = Cluster(self, [node("node-a"), node("node-b", ready=False), node("node-c", ready=False)],
                          [lh_node("node-a"), lh_node("node-b", ready=False), lh_node("node-c", ready=False)])
        self.bind(cluster)
        self.assertIsNone(COPIES.fit_single_host())
        self.assertEqual([], cluster.sent)

    def test_one_k8s_node_two_longhorn_records_lowers_nothing(self):
        cluster = Cluster(self, [node("node-a")], [lh_node("node-a"), lh_node("node-b", ready=False)])
        self.bind(cluster)
        self.assertIsNone(COPIES.fit_single_host())
        self.assertEqual([], cluster.sent)

    def test_harvester_and_no_longhorn_do_nothing(self):
        for kwargs in ({"harvester": True}, {"longhorn": False}):
            with self.subTest(**kwargs):
                cluster = Cluster(self, [node("node-a")], [lh_node("node-a")])
                self.bind(cluster, **kwargs)
                self.assertIsNone(COPIES.fit_single_host())
                self.assertEqual([], cluster.sent)

    def test_degraded_alone_is_not_proof(self):
        # Soft anti-affinity on one host with several disks: after a reboot the
        # volume reads degraded while its copies rebuild, and Longhorn can
        # place them. Only a copy Longhorn cannot schedule is proof.
        cluster = Cluster(self, [node("node-a")], [lh_node("node-a")])
        cluster.kube.add(volume(3, unschedulable=False))
        self.bind(cluster)
        self.assertIsNone(COPIES.fit_single_host())
        self.assertEqual([], cluster.sent)

    def test_a_rebuilding_copy_lowers_nothing(self):
        cluster = Cluster(self, [node("node-a")], [lh_node("node-a")])
        cluster.kube.add(engines("RW", "WO"))
        self.bind(cluster)
        self.assertIsNone(COPIES.fit_single_host())
        self.assertEqual([], cluster.sent)

    def test_unreadable_engines_lower_nothing(self):
        cluster = Cluster(self, [node("node-a")], [lh_node("node-a")])
        cluster.unreadable(ENGINES)
        self.bind(cluster)
        self.assertIsNone(COPIES.fit_single_host())
        self.assertEqual([], cluster.sent)

    def test_a_copy_longhorn_cannot_place_is_proof(self):
        # The older map form of Longhorn's conditions counts too.
        cluster = Cluster(self, [node("node-a")], [lh_node("node-a")])
        cluster.edit(VOLUME, lambda found: found["status"].update(conditions={
            "scheduled": {"type": "Scheduled", "status": "False", "reason": "ReplicaSchedulingFailure"}}))
        self.bind(cluster)
        self.assertTrue(COPIES.fit_single_host())

    def test_already_one_does_nothing(self):
        cluster = Cluster(self, [node("node-a")], [lh_node("node-a")], copies=1, robustness="healthy")
        self.bind(cluster)
        self.assertIsNone(COPIES.fit_single_host())
        self.assertEqual([], cluster.sent)

    def test_a_healthy_volume_keeps_its_copies(self):
        # Copies the one host does hold (soft anti-affinity, several disks)
        # are not thrown away: there is nothing to fix.
        cluster = Cluster(self, [node("node-a")], [lh_node("node-a")], copies=3, robustness="healthy")
        self.bind(cluster)
        self.assertIsNone(COPIES.fit_single_host())
        self.assertEqual([], cluster.sent)

    def test_longhorn_unreadable_does_nothing(self):
        for path in ("/api/v1/nodes", LH_NODES, f"{PARITY.LH_VOLUMES}/pvc-0001",
                     "/apis/apps/v1/namespaces/lab/deployments/homestead"):
            with self.subTest(path=path):
                cluster = Cluster(self, [node("node-a")], [lh_node("node-a")])
                cluster.unreadable(path)
                self.bind(cluster)
                self.assertIsNone(COPIES.fit_single_host())
                self.assertEqual([], cluster.sent)

    def test_own_data_volumes_describes_the_claim(self):
        cluster = Cluster(self, [node("node-a")], [lh_node("node-a")])
        self.bind(cluster)
        [found] = COPIES.own_data_volumes()
        self.assertEqual("homestead-data", found["claim"])
        self.assertEqual("pvc-0001", found["pv"])
        self.assertEqual("longhorn", found["storage_class"])
        self.assertEqual(2.0, found["size_gb"])
        self.assertEqual(3, found["volume"]["spec"]["numberOfReplicas"])

    def test_own_data_volumes_without_longhorn_volume(self):
        cluster = Cluster(self, [node("node-a")], [lh_node("node-a")], storage_class="local-path")
        cluster.remove(VOLUME)
        self.bind(cluster)
        [found] = COPIES.own_data_volumes()
        self.assertIsNone(found["volume"])
        self.assertEqual(2.0, found["size_gb"])


def lh_store(name, room_gib=20, ready=True):
    """A Longhorn node with one disk and room_gib of room left on it."""
    found = lh_node(name, ready)
    found["status"]["diskStatus"] = {"d": {"storageMaximum": 40 * GiB, "storageAvailable": 40 * GiB,
                                           "storageScheduled": (40 - room_gib) * GiB}}
    return found



def r2_class(replicas="2", migratable="false", provisioner="driver.longhorn.io", **extra):
    return {"apiVersion": "storage.k8s.io/v1", "kind": "StorageClass", "metadata": {"name": "longhorn-r2"}, "provisioner": provisioner,
            "parameters": {"numberOfReplicas": replicas, "migratable": migratable, **extra}}


class FactsTests(unittest.TestCase):
    def setUp(self):
        self.data = tempfile.mkdtemp()

    def cluster(self, names=("node-a", "node-b"), copies=1, rooms=None, distribution="kubeadm", harvester=False,
                longhorn=True, storage_class="longhorn", extra=None):
        rooms = rooms or [20] * len(names)
        cluster = Cluster(self, [node(n) for n in names], [lh_store(n, r) for n, r in zip(names, rooms)], copies=copies,
                          robustness="healthy", storage_class=storage_class, extra=extra or ())
        hostrun = type("H", (), {"run": staticmethod(lambda *a, **k: ("", ""))})
        PARITY.bind(cluster.kget, cluster.ksend, hostrun,
                    lambda force=False: {"distribution": distribution, "longhorn": longhorn, "harvester": harvester},
                    lambda: {}, self.data, ("lab", "homestead"))
        LHCAP.bind(cluster.kget, cluster.ksend, lambda: {})
        return cluster

    def test_facts_single_host(self):
        self.cluster(names=("node-a",))
        facts = COPIES.facts()
        self.assertEqual("single-host", facts["state"])
        self.assertEqual(1, facts["copies"])
        self.assertEqual(1, facts["storage_hosts"])

    def test_facts_can_keep_two(self):
        cluster = self.cluster()
        facts = COPIES.facts()
        self.assertEqual("can-keep-two", facts["state"])
        self.assertEqual({"claim": "homestead-data", "storage_class": "longhorn", "copies": 1, "storage_hosts": 2,
                          "room_hosts": 2, "size_gb": 2.0},
                         {k: facts[k] for k in ("claim", "storage_class", "copies", "storage_hosts", "room_hosts", "size_gb")})
        self.assertEqual({"exists": False, "replicas": 0, "migratable": False, "longhorn": False, "usable": True, "why": ""},
                         facts["r2"])
        self.assertEqual([], cluster.sent, "facts only read")

    def test_facts_rising_on_k3s(self):
        # Review focus 4: own_copies() raises it within a minute; nothing to offer.
        for distribution in ("k3s", "rke2"):
            with self.subTest(distribution=distribution):
                self.cluster(distribution=distribution)
                self.assertTrue(PARITY.own_copies_due(PARITY._storage_hosts(PARITY._ready_nodes())))
                self.assertEqual("rising", COPIES.facts()["state"])

    def test_own_copies_due_matches_own_copies(self):
        cluster = self.cluster(distribution="k3s")
        storing = PARITY._storage_hosts(PARITY._ready_nodes())
        self.assertTrue(PARITY.own_copies_due(storing))
        self.assertEqual([], cluster.sent, "a pure read")
        self.assertEqual([("homestead-data", 2)], PARITY.own_copies(storing))
        # The fake applied the raise itself.
        self.assertFalse(PARITY.own_copies_due(storing), "raised: nothing left to do")

    def test_own_copies_due_not_on_other_distributions_or_harvester(self):
        for kwargs in ({"distribution": "kubeadm"}, {"distribution": "k3s", "harvester": True}):
            with self.subTest(**kwargs):
                self.cluster(**kwargs)
                self.assertFalse(PARITY.own_copies_due(PARITY._storage_hosts(PARITY._ready_nodes())))

    def test_facts_no_room(self):
        self.cluster(rooms=[20, 1])
        facts = COPIES.facts()
        self.assertEqual("single-host", facts["state"])
        self.assertEqual(1, facts["room_hosts"])

    def test_a_host_longhorn_cannot_store_on_does_not_count(self):
        cluster = self.cluster(names=("node-a", "node-b"))
        cluster.edit(f"{LH_NODES}/node-b", lambda found: found["status"]["conditions"][0].update(status="False"))
        self.assertEqual("single-host", COPIES.facts()["state"])

    def test_facts_two_copies_ok(self):
        self.cluster(copies=2)
        self.assertEqual("ok", COPIES.facts()["state"])

    def test_facts_harvester_and_local_path_na(self):
        self.cluster(harvester=True)
        self.assertEqual("n/a", COPIES.facts()["state"])
        self.cluster(longhorn=False)
        self.assertEqual("n/a", COPIES.facts()["state"])
        cluster = self.cluster(storage_class="local-path")
        cluster.remove(VOLUME)
        self.assertEqual("n/a", COPIES.facts()["state"])

    def test_facts_unreadable_is_unknown_and_says_why(self):
        # #620: a read that failed is said, never shown as nothing to say.
        cluster = self.cluster()
        cluster.unreadable("/apis/apps/v1/namespaces/lab/deployments/homestead")
        facts = COPIES.facts()
        self.assertEqual("unknown", facts["state"])
        self.assertIn("deployments/homestead", facts["error"])

    def test_storage_hosts_unreadable_is_unknown(self):
        cluster = self.cluster()
        cluster.unreadable(LH_NODES)
        facts = COPIES.facts()
        self.assertEqual("unknown", facts["state"])
        self.assertIn("Longhorn's nodes could not be read", facts["error"])

    def test_unreadable_size_is_not_room(self):
        cluster = self.cluster()
        cluster.edit(VOLUME, lambda found: found["spec"].update(size="0"))
        cluster.edit(CLAIM, lambda found: found["spec"].update(resources={}))
        facts = COPIES.facts()
        self.assertEqual("unknown", facts["state"])
        self.assertIn("size", facts["error"])
        self.assertEqual(0, facts["room_hosts"])

    def test_on_a_two_copy_class_with_one_copy_the_offer_raises_in_place(self):
        self.cluster(storage_class="longhorn-r2", extra=[r2_class()])
        facts = COPIES.facts()
        self.assertEqual(("can-keep-two", "raise"), (facts["state"], facts["action"]))

    def test_on_longhorn_the_offer_is_a_move(self):
        self.cluster()
        self.assertEqual("move", COPIES.facts()["action"])

    def test_facts_r2_description(self):
        cases = ((r2_class(), True, ""),
                 (r2_class(migratable="true"), False, "migratable"),
                 (r2_class(replicas="1"), False, "1 copy"),
                 (r2_class(provisioner="rancher.io/local-path"), False, "not a Longhorn class"),
                 (r2_class(replicaSoftAntiAffinity="enabled"), False, "share a host"),
                 (r2_class(nodeSelector="fast"), False, "only on hosts tagged fast"))
        for found, usable, why in cases:
            with self.subTest(found=found):
                self.cluster(extra=[found])
                r2 = COPIES.facts()["r2"]
                self.assertTrue(r2["exists"])
                self.assertEqual(usable, r2["usable"])
                self.assertIn(why, r2["why"])


def uncached(test):
    """homestead_routes.cached is server.py's own cache once another test has
    imported server: these tests read through it, never from it."""
    from unittest import mock
    import homestead_routes
    test.enterContext(mock.patch.object(homestead_routes, "_cached", [lambda key, seconds, fn: fn()]))


class SelfHealthTests(unittest.TestCase):
    def setUp(self):
        uncached(self)

    def test_health_carries_data_copies(self):
        from unittest import mock
        import homestead_self_health as SELF_HEALTH
        fake = {"state": "can-keep-two", "copies": 1}
        with mock.patch.object(COPIES, "facts", lambda: fake):
            self.assertEqual(fake, SELF_HEALTH.data_copies())

    def test_health_says_na_when_facts_fail(self):
        from unittest import mock
        import homestead_self_health as SELF_HEALTH

        def broken():
            raise RuntimeError("Longhorn did not answer")
        with mock.patch.object(COPIES, "facts", broken):
            self.assertEqual({"state": "unknown", "error": "Longhorn did not answer"}, SELF_HEALTH.data_copies())


class KeepTwoCopiesTests(unittest.TestCase):
    """POST /api/self/data/two-copies: longhorn-r2 made if missing, refused
    when an existing one would not keep two shareable copies; never a move."""

    def setUp(self):
        from unittest import mock
        self.mock = mock
        uncached(self)
        self.created = []
        self.facts = {"state": "can-keep-two", "copies": 1,
                      "r2": {"exists": False, "replicas": 0, "migratable": False, "longhorn": False, "usable": True, "why": ""}}
        self.enterContext(mock.patch.object(COPIES, "facts", lambda: self.facts))
        self.enterContext(mock.patch.object(COPIES.STORAGE_CLASSES, "create_storage_class",
                                            lambda cfg: (self.created.append(cfg), {"ok": True, "name": cfg["name"]})[1]))
        self.sent = []
        self.enterContext(mock.patch.object(PARITY, "ksend", lambda *a, **k: self.sent.append(a)))

    def test_raises_in_place_on_a_two_copy_class(self):
        # Already on longhorn-r2, lowered to one copy: no move, no downtime.
        self.facts.update(action="raise", pv="pvc-0001")
        self.facts["r2"] = {"exists": True, "replicas": 2, "migratable": False, "longhorn": True, "usable": True, "why": ""}
        result = COPIES.ensure_two_copy_class()
        self.assertEqual((True, True, False), (result["ok"], result["raised"], result["created"]))
        self.assertEqual([("PATCH", f"{PARITY.LH_VOLUMES}/pvc-0001", {"spec": {"numberOfReplicas": 2}})], self.sent)
        self.assertEqual([], self.created)

    def test_creates_r2_when_missing(self):
        result = COPIES.ensure_two_copy_class()
        self.assertEqual([{"name": "longhorn-r2", "replicas": 2, "migratable": False, "reclaim_policy": "Retain",
                           "copies": "hosts", "hosts_only": True, "expandable": True}], self.created)
        self.assertTrue(result["ok"])
        self.assertTrue(result["created"])
        self.assertEqual("longhorn-r2", result["storage_class"])

    def test_reuses_usable_r2(self):
        self.facts["r2"] = {"exists": True, "replicas": 2, "migratable": False, "longhorn": True, "usable": True, "why": ""}
        result = COPIES.ensure_two_copy_class()
        self.assertEqual([], self.created)
        self.assertEqual((True, False), (result["ok"], result["created"]))

    def test_refuses_unusable_r2(self):
        # Review focus 3: never onto a class that would not keep two shareable copies.
        for why in ("longhorn-r2 is migratable: its volumes can be mounted on only one node at a time",
                    "longhorn-r2 keeps 1 copy, not two", "longhorn-r2 is not a Longhorn class (rancher.io/local-path)"):
            with self.subTest(why=why):
                self.facts["r2"] = {"exists": True, "replicas": 1, "migratable": True, "longhorn": False, "usable": False, "why": why}
                result = COPIES.ensure_two_copy_class()
                self.assertFalse(result["ok"])
                self.assertIn(why, result["message"])
                self.assertEqual([], self.created)
                self.assertEqual([], self.sent)

    def test_refuses_unless_can_keep_two(self):
        for state in ("single-host", "rising", "ok", "n/a"):
            with self.subTest(state=state):
                self.facts = {"state": state, "copies": 1, "r2": None}
                result = COPIES.ensure_two_copy_class()
                self.assertFalse(result["ok"])
                self.assertIn("nothing to do", result["message"].lower())
                self.assertEqual([], self.created)

    def test_never_starts_a_preparation(self):
        # Review focus 5: the class only; the move keeps its own review.
        calls = []
        self.enterContext(self.mock.patch.object(PARITY, "kget", lambda path, **k: calls.append(path) or {}))
        COPIES.ensure_two_copy_class()
        self.assertFalse([p for p in calls if "/self/data" in p or "persistentvolumeclaims" in p])
        self.assertEqual([], self.sent, "no PVC or volume written")

    def test_route_is_admin_and_listed(self):
        import homestead_routes
        self.assertIn("homestead_data_copies", homestead_routes.MODULES)
        role, handler = COPIES.ROUTES[("POST", "/api/self/data/two-copies")]
        self.assertEqual("admin", role)
        self.assertTrue(handler(None)["ok"])

    def test_replicas_carry_data_copies(self):
        # Decision 4: the Redundancy card reads it from its own call.
        import homestead_self_health as SELF_HEALTH
        with self.mock.patch.object(SELF_HEALTH, "replicas_now", lambda: {"desired": 1, "pods": []}):
            view = SELF_HEALTH.replicas_view()
        self.assertEqual("can-keep-two", view["data_copies"]["state"])
        self.assertEqual(1, view["desired"])


class HostsOnlyClassTests(unittest.TestCase):
    def test_hosts_only_keeps_copies_on_different_hosts_whatever_the_global_setting(self):
        from unittest import mock
        import homestead_storage_classes as SC
        sent = []
        with mock.patch.object(SC, "storage_classes", lambda: []), \
                mock.patch.object(SC, "_dns_name", lambda value, what: value), \
                mock.patch.object(SC, "storage_class_inventory", lambda: []), \
                mock.patch.object(SC, "ksend", lambda *a, **k: sent.append(a)):
            SC.create_storage_class({"name": "longhorn-r2", "replicas": 2, "copies": "hosts", "hosts_only": True})
            SC.create_storage_class({"name": "plain", "replicas": 2, "copies": "hosts"})
        self.assertEqual("disabled", sent[0][2]["parameters"]["replicaSoftAntiAffinity"])
        self.assertNotIn("replicaSoftAntiAffinity", sent[1][2]["parameters"], "other classes as before")


if __name__ == "__main__":
    unittest.main()
