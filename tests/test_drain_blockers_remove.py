"""The restart review removes Longhorn's leftover engine records (#TBD-drain-blockers).

An engine-instance orphan record whose engine already runs on another host
keeps a leftover engine process on this host's instance-manager, so the drain
can't evict it. Removing the record lets Longhorn stop that process. Only those
records go: each is read again, must still be what the review showed (same
uid and resourceVersion, an engine-instance record on this host), and its
engine must still run on another host. The delete carries those preconditions.
A replica-data record ("replica" in Longhorn v1.13.0) is never removed here:
that deletes an on-disk copy of the volume's data."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_drain_blockers as BLOCKERS
from fakes.kube import Kube
from test_drain_blockers import LH, engine, engine_orphan, fixture

ROOT = Path(__file__).resolve().parents[1]


def record(orphan):
    meta = orphan["metadata"]
    return {"name": meta["name"], "uid": meta["uid"], "resourceVersion": meta["resourceVersion"]}


class RemoveTests(unittest.TestCase):
    def setUp(self):
        self.orphans = [engine_orphan("vol-a", 1), engine_orphan("vol-b", 2)]
        for orphan in self.orphans:
            orphan["spec"]["nodeID"] = "node-a"
        self.engines = [engine("vol-a", "node-b"), engine("vol-b", "node-b")]

    def deletes(self, kube):
        return [(path, body) for method, path, body, _ in kube.writes if method == "DELETE"]

    def test_engine_orphans_whose_engine_runs_elsewhere_are_removed_with_preconditions(self):
        with Kube(*self.orphans, *self.engines) as kube:
            stored = [kube.object(f"{LH}/orphans/{o['metadata']['name']}") for o in self.orphans]
            result = BLOCKERS.remove_engine_orphans("node-a", [record(o) for o in stored])
            deletes = self.deletes(kube)
        self.assertEqual(sorted(o["metadata"]["name"] for o in stored), sorted(result["removed"]))
        self.assertEqual([], result["refused"])
        self.assertEqual(2, len(deletes))
        for (path, body), orphan in zip(sorted(deletes), sorted(stored, key=lambda o: o["metadata"]["name"])):
            self.assertTrue(path.endswith("/orphans/" + orphan["metadata"]["name"]))
            self.assertEqual({"uid": orphan["metadata"]["uid"], "resourceVersion": orphan["metadata"]["resourceVersion"]},
                             body["preconditions"])

    def test_removal_refuses_an_orphan_whose_engine_is_now_here(self):
        with Kube(*self.orphans, *self.engines) as kube:
            stored = kube.object(f"{LH}/orphans/{self.orphans[0]['metadata']['name']}")
            moved = kube.object(f"{LH}/engines/vol-a-e-0")
            moved["spec"]["nodeID"] = "node-a"
            kube.add(moved)
            result = BLOCKERS.remove_engine_orphans("node-a", [record(stored)])
            deletes = self.deletes(kube)
        self.assertEqual([], result["removed"])
        self.assertEqual([stored["metadata"]["name"]], [r["name"] for r in result["refused"]])
        self.assertIn("node-a", result["refused"][0]["why"])
        self.assertEqual([], deletes)

    def test_removal_refuses_a_changed_or_other_type_record(self):
        data = fixture("orphan_replica_data")
        data["spec"]["nodeID"] = "node-a"
        with Kube(*self.orphans, *self.engines, data) as kube:
            stored = kube.object(f"{LH}/orphans/{self.orphans[0]['metadata']['name']}")
            changed = dict(record(stored), resourceVersion="1")
            stored_data = kube.object(f"{LH}/orphans/{data['metadata']['name']}")
            result = BLOCKERS.remove_engine_orphans("node-a", [changed, record(stored_data)])
            deletes = self.deletes(kube)
        self.assertEqual([], result["removed"])
        self.assertEqual(2, len(result["refused"]))
        self.assertEqual([], deletes, "a changed record and a replica-data record are never deleted")

    def test_a_record_already_gone_is_reported_not_an_error(self):
        with Kube(*self.engines) as kube:
            result = BLOCKERS.remove_engine_orphans("node-a", [record(self.orphans[0])])
            deletes = self.deletes(kube)
        self.assertEqual([], deletes)
        self.assertEqual([self.orphans[0]["metadata"]["name"]], [r["name"] for r in result["refused"]])

    def test_an_engine_that_cannot_be_read_refuses(self):
        with Kube(*self.orphans, *self.engines) as kube:
            stored = kube.object(f"{LH}/orphans/{self.orphans[0]['metadata']['name']}")
            kube.fail(f"{LH}/engines/vol-a-e-0", 500, times=9)
            result = BLOCKERS.remove_engine_orphans("node-a", [record(stored)])
            deletes = self.deletes(kube)
        self.assertEqual([], deletes)
        self.assertEqual(1, len(result["refused"]))

    def test_the_body_is_checked(self):
        for body in ({}, {"node": "node-a"}, {"node": "node-a", "records": "x"},
                     {"node": "Node A!", "records": []}, {"node": "node-a", "records": [{"name": 3}]}):
            with self.subTest(body=body), self.assertRaises(ValueError):
                BLOCKERS.remove_request(body)

    def test_removal_needs_admin(self):
        self.assertEqual("admin", BLOCKERS.ROUTES[("POST", "/api/node/orphans/remove")][0])

    def test_the_route_is_registered(self):
        import homestead_routes
        self.assertIn("homestead_drain_blockers", homestead_routes.MODULES)

    def test_rbac_names_orphan_delete(self):
        text = (ROOT / "deploy" / "deploy.yaml").read_text(encoding="utf-8")
        self.assertRegex(text, r'apiGroups: \["longhorn\.io"\]\n(?:\s+#.*\n)*\s+resources: \[orphans\]\n\s+verbs: \[delete\]')
        rendered = (ROOT / "deploy" / "rbac.yaml").read_text(encoding="utf-8")
        self.assertRegex(rendered, r"resources: \[orphans\]\n\s+verbs: \[delete\]")


if __name__ == "__main__":
    unittest.main()
