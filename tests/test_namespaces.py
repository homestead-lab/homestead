"""Which namespaces are yours, and making and removing them. Runs on the
shared fake API (tests/fakes/kube.py)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_kube as K
import homestead_namespaces as ns
from fakes.kube import Kube

# The kinds a namespace's contents are counted by, as the API serves them.
KINDS = {"persistentvolumeclaims": ("v1", "PersistentVolumeClaim"), "pods": ("v1", "Pod"),
         "cronjobs": ("batch/v1", "CronJob"), "daemonsets": ("apps/v1", "DaemonSet"),
         "datavolumes": ("cdi.kubevirt.io/v1beta1", "DataVolume"),
         "virtualmachines": ("kubevirt.io/v1", "VirtualMachine")}
PATHS = {"persistentvolumeclaims": "/api/v1/namespaces/{}/persistentvolumeclaims",
         "cronjobs": "/apis/batch/v1/namespaces/{}/cronjobs"}


def namespace(name, annotations=None):
    return {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": name, "annotations": annotations or {}},
            "status": {"phase": "Active"}}


def contents(kind, name, items):
    version, kind_name = KINDS[kind]
    rows = items if isinstance(items, list) else [{"metadata": {"name": f"{kind}-{i}"}} for i in range(items)]
    return [dict(row, apiVersion=version, kind=kind_name, metadata=dict(row["metadata"], namespace=name))
            for row in rows]


HARVESTER = ["default", "lab", "media", "kube-system", "cattle-system", "harvester-system", "longhorn-system",
             "fleet-local", "cluster-fleet-local-local-1a3d67d0a899", "p-7k2xm", "user-8fh3z", "u-b4qkz", "local",
             "cattle-impersonation-system", "harvester-public", "cdi"]


class NamespaceTests(unittest.TestCase):
    def use(self, names=HARVESTER, contents_of=None, failing=(), missing=()):
        objects = [namespace(n) for n in names]
        for (kind, name), items in (contents_of or {}).items():
            objects += contents(kind, name, items)
        absent = [KINDS[kind][0].split("/")[0] for kind in missing]
        self.kube = self.enterContext(Kube(*objects, absent_groups=absent))
        # A short API error on a kind, in every namespace.
        for kind in failing:
            self.kube.fail(PATHS[kind].replace("/namespaces/{}", ""), 500, times=99)
            for name in names:
                self.kube.fail(PATHS[kind].format(name), 500, times=99)
        saved = tuple(getattr(ns, name, None) for name in ("kget", "ksend", "DEFAULT_NS", "OWN_NS"))
        self.addCleanup(ns.bind, *saved)
        ns.bind(K.kget, K.ksend, "lab", "lab")

    def deletes(self):
        # The body too: a delete sent with a body (a dry run, say) is not the delete.
        return [write[:3] for write in self.kube.writes if write[0] == "DELETE"]

    def test_pickers_offer_only_places_for_apps(self):
        self.use()
        self.assertEqual(["default", "lab", "media"], ns.names())
        self.assertEqual(len(HARVESTER), len(ns.names(include_system=True)))

    def test_rancher_marks_its_own(self):
        self.assertTrue(ns.is_system(namespace("anything", {"management.cattle.io/system-namespace": "true"})))

    def test_a_namespace_is_made_and_labelled(self):
        self.use()
        ns.create("Photos")
        method, path, body, _ = self.kube.writes[-1]
        self.assertEqual(("POST", "/api/v1/namespaces", "photos"), (method, path, body["metadata"]["name"]))
        self.assertEqual("true", body["metadata"]["labels"]["homestead.io/managed"])
        self.assertIsNotNone(self.kube.object("/api/v1/namespaces/photos"))
        for bad in ("kube-thing", "cattle-x", "p-abcde", "no_underscores", ""):
            with self.subTest(bad):
                with self.assertRaises(ValueError):
                    ns.create(bad)

    def test_only_an_empty_namespace_of_yours_goes_and_only_when_named(self):
        self.use(contents_of={("persistentvolumeclaims", "media"): 2})
        for name, reason in (("lab", "default"), ("kube-system", "platform"), ("media", "2 volumes"),
                             ("default", "Kubernetes")):
            with self.subTest(name):
                with self.assertRaisesRegex(ValueError, reason):
                    ns.delete(name, name)
        self.use(names=["lab", "old"])
        with self.assertRaisesRegex(ValueError, "type old"):
            ns.delete("old", "")
        ns.delete("old", "old")
        self.assertEqual([("DELETE", "/api/v1/namespaces/old", None)], self.deletes())
        self.assertIsNone(self.kube.object("/api/v1/namespaces/old"))

    def test_a_failed_count_refuses_the_delete(self):
        # A short API error must not read as "no volumes here" (#599).
        self.use(names=["lab", "old"], failing=("persistentvolumeclaims",))
        with self.assertRaisesRegex(ValueError, "could not check volumes in old, so nothing was deleted"):
            ns.delete("old", "old")
        self.assertEqual([], self.deletes())

    def test_an_api_the_cluster_does_not_have_counts_as_none(self):
        self.use(names=["lab", "old"], missing=("virtualmachines", "datavolumes"))
        ns.delete("old", "old")
        self.assertEqual([("DELETE", "/api/v1/namespaces/old", None)], self.deletes())

    def test_a_cronjob_a_daemonset_a_disk_or_a_bare_pod_is_not_empty(self):
        bare = {"metadata": {"name": "debug"}, "status": {"phase": "Running"}}
        owned = {"metadata": {"name": "web-1", "ownerReferences": [{"kind": "ReplicaSet", "name": "web", "uid": "rs1"}]},
                 "status": {"phase": "Running"}}
        done = {"metadata": {"name": "once"}, "status": {"phase": "Succeeded"}}
        for kind, items, said in (("cronjobs", 1, "1 scheduled job"), ("daemonsets", 1, "1 daemon set"),
                                  ("datavolumes", 1, "1 VM disk"), ("pods", [bare, owned, done], "1 running pod")):
            with self.subTest(kind):
                self.use(names=["lab", "old"], contents_of={(kind, "old"): items})
                with self.assertRaisesRegex(ValueError, said):
                    ns.delete("old", "old")
        self.use(names=["lab", "old"], contents_of={("pods", "old"): [owned, done]})
        ns.delete("old", "old")
        self.assertEqual([("DELETE", "/api/v1/namespaces/old", None)], self.deletes())

    def test_the_inventory_says_what_it_could_not_read_and_offers_no_delete(self):
        self.use(names=["lab", "old"], failing=("cronjobs",))
        row = next(r for r in ns.inventory()["namespaces"] if r["name"] == "old")
        self.assertEqual(["cronjobs"], row["unread"])
        self.assertFalse(row["empty"])


class InventoryCostTests(unittest.TestCase):
    """The namespaces page lists each kind once for the cluster, not once per
    namespace (#985 §5)."""
    def cluster(self, count):
        objects = [{"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": f"team-{i}"}} for i in range(count)]
        for i in range(count):
            objects.append({"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "web", "namespace": f"team-{i}"}})
            if i % 2:
                objects.append({"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "by-hand", "namespace": f"team-{i}"},
                                "status": {"phase": "Running"}})
        return Kube(*objects)

    def gets(self, count):
        with self.cluster(count) as kube:
            ns.bind(K.kget, K.ksend, "lab", "lab")
            seen = []
            real = K.transport
            def counting(method, path, *args, **kwargs):
                seen.append(path)
                return real(method, path, *args, **kwargs)
            K.transport = counting
            try:
                found = ns.inventory()
            finally:
                K.transport = real
        return len(seen), found

    def test_the_call_count_does_not_grow_with_namespaces(self):
        small, _ = self.gets(2)
        large, found = self.gets(26)
        self.assertEqual(small, large)
        self.assertLessEqual(large, 9)
        rows = {r["name"]: r for r in found["namespaces"]}
        self.assertEqual(1, rows["team-3"]["deployments"])
        self.assertEqual(1, rows["team-3"]["pods"])
        self.assertEqual(0, rows["team-2"]["pods"])
        self.assertFalse(rows["team-2"]["empty"])

    def test_a_kind_that_cannot_be_read_marks_every_namespace(self):
        with self.cluster(3) as kube:
            ns.bind(K.kget, K.ksend, "lab", "lab")
            kube.fail("/api/v1/persistentvolumeclaims", 500, times=5)
            for path in [f"/api/v1/namespaces/team-{i}/persistentvolumeclaims" for i in range(3)]:
                kube.fail(path, 500, times=5)
            rows = ns.inventory()["namespaces"]
        self.assertTrue(all("volumes" in r["unread"] and not r["empty"] for r in rows))


if __name__ == "__main__":
    unittest.main()
