"""A hold a transfer left behind can be seen and ended on the source cluster.

The destination ends a hold when its move or copy finishes or is cancelled.
When its record is gone, nothing did, and a person had to remove the move
annotations and the transfer's Longhorn backups by hand with kubectl.
"""
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_move as MOVE
import homestead_move_holds as HOLDS
import homestead_move_source as SOURCE
import homestead_names as NAMES
import homestead_kube as K
from fakes.kube import Kube
from test_transfer_cleanup import ID, OTHER, artifact

VM = "/apis/kubevirt.io/v1/namespaces/lab/virtualmachines/"
UID = "win-dev-uid"
GROUPS = ("kubevirt.io", "apps", "longhorn.io")


def held(name, origin, owner=ID, uid=UID, backups=()):
    annotations = {NAMES.key(SOURCE.ORIGIN): json.dumps(origin)}
    if owner:
        annotations[NAMES.key(SOURCE.OWNER)] = owner
    if backups:
        annotations[NAMES.key(SOURCE.BACKUPS)] = json.dumps(list(backups))
    return {"apiVersion": "kubevirt.io/v1", "kind": "VirtualMachine",
            "metadata": {"name": name, "namespace": "lab", "uid": uid, "resourceVersion": "7",
                         "annotations": annotations},
            "spec": {"runStrategy": "RerunOnFailure"}}


def longhorn(kind, name, transfer=ID, source_uid=UID, **fields):
    obj = {"apiVersion": "longhorn.io/v1beta2", "kind": kind,
           **artifact(name, transfer=transfer, source_uid=source_uid, **fields)}
    obj["metadata"]["namespace"] = "longhorn-system"
    return obj


class HoldTests(unittest.TestCase):
    def setUp(self):
        self.kube = self.enterContext(Kube(groups=GROUPS))
        for name, value in (("kget", K.kget), ("ksend", K.ksend)):
            self.enterContext(mock.patch.object(SOURCE, name, value))

    def put(self, kind, name, **fields):
        return self.kube.add(longhorn(kind, name, **fields))

    def live_copy(self):
        self.kube.add(held("win-dev", {"runStrategy": "RerunOnFailure", "_copy_running": True, "_copy_live": True},
                           backups=[{"backup": "b1", "requested_at": 1794010732.6}]))

    def test_a_live_copy_s_hold_is_listed_as_kept_running(self):
        self.live_copy()
        self.kube.add({"apiVersion": "apps/v1", "kind": "Deployment",
                       "metadata": {"name": "grafana", "namespace": "lab"}, "spec": {}})

        rows = HOLDS.holds()["holds"]

        self.assertEqual(1, len(rows))
        self.assertEqual({"kind": "vm", "name": "win-dev", "transfer_id": ID, "uid": UID, "kept_running": True,
                          "copy": True, "backups": 1}, {k: rows[0][k] for k in
                                                        ("kind", "name", "transfer_id", "uid", "kept_running", "copy", "backups")})

    def test_releasing_a_live_copy_drops_the_hold_and_its_backups_and_leaves_the_vm_running(self):
        self.live_copy()
        backup = self.put("Backup", "b1", spec={"snapshotName": "s1"})
        snapshot = self.put("Snapshot", "s1", spec={"volume": "pvc-disk"})

        result = HOLDS.release("vm", "lab", "win-dev", ID, UID)

        self.assertTrue(result["complete"], result)
        vm = self.kube.object(VM + "win-dev")
        annotations = vm["metadata"]["annotations"]
        for key in (SOURCE.ORIGIN, SOURCE.OWNER, SOURCE.BACKUPS):
            self.assertNotIn(NAMES.key(key), annotations)
        # Fenced: a late step from the old transfer cannot hold it again.
        self.assertEqual(ID, annotations[NAMES.key(SOURCE.RELEASED)])
        self.assertEqual("RerunOnFailure", vm["spec"]["runStrategy"])
        self.assertIsNone(self.kube.object(backup))
        self.assertIsNone(self.kube.object(snapshot))
        self.assertEqual([], HOLDS.holds()["holds"])

    def test_a_vm_stopped_for_a_move_is_put_back_as_it_was(self):
        vm = held("db", {"runStrategy": "Always"}, uid="db-uid")
        vm["spec"] = {"runStrategy": "Halted"}
        self.kube.add(vm)

        row = HOLDS.holds()["holds"][0]
        self.assertFalse(row["kept_running"])
        self.assertFalse(row["copy"])
        HOLDS.release("vm", "lab", "db", ID, "db-uid")

        self.assertEqual("Always", self.kube.object(VM + "db")["spec"]["runStrategy"])

    def test_a_hold_that_changed_since_it_was_read_is_refused(self):
        self.live_copy()
        with self.assertRaisesRegex(ValueError, "different transfer"):
            HOLDS.release("vm", "lab", "win-dev", OTHER, UID)
        with self.assertRaisesRegex(ValueError, "replaced"):
            HOLDS.release("vm", "lab", "win-dev", ID, "someone-else")
        self.assertIn(NAMES.key(SOURCE.OWNER), self.kube.object(VM + "win-dev")["metadata"]["annotations"])

    def test_backups_still_deleting_show_as_leftovers_until_tidied(self):
        self.live_copy()
        # Longhorn's finalizer: deletion is asked for and finishes later.
        stored = longhorn("Backup", "b1", spec={"snapshotName": "s1"})
        stored["metadata"]["finalizers"] = ["longhorn.io"]
        backup = self.kube.add(stored)
        self.put("Snapshot", "s1", spec={"volume": "pvc-disk"})

        result = HOLDS.release("vm", "lab", "win-dev", ID, UID)

        self.assertFalse(result["complete"])
        self.assertIn("Leftovers", result["detail"])
        left = HOLDS.holds()["leftovers"]
        self.assertEqual([("win-dev", ID, 1, 1, 1)],
                         [(r["name"], r["transfer_id"], r["backups"], r["snapshots"], r["deleting"]) for r in left])
        self.kube.discard(backup)  # Longhorn finished with it.
        self.assertTrue(HOLDS.tidy(ID, UID)["complete"])
        self.assertEqual([], HOLDS.holds()["leftovers"])

    def test_a_transfer_still_holding_its_workload_has_no_leftovers_and_cannot_be_tidied(self):
        self.live_copy()
        self.put("Backup", "b1", spec={"snapshotName": "s1"})

        self.assertEqual([], HOLDS.holds()["leftovers"])
        with self.assertRaisesRegex(ValueError, "still held by that transfer"):
            HOLDS.tidy(ID, UID)

    def test_what_a_person_made_is_never_a_leftover(self):
        self.live_copy()
        mine = longhorn("Backup", "nightly")
        mine["metadata"]["annotations"] = {}
        self.kube.add(mine)

        self.assertEqual([], HOLDS.holds()["leftovers"])

    def test_leftovers_of_a_workload_no_longer_here_are_listed_without_a_name(self):
        self.put("Snapshot", "old", transfer=OTHER, source_uid="gone-uid", spec={"volume": "pvc-x"})

        left = HOLDS.holds()["leftovers"]

        self.assertEqual([("", OTHER, 0, 1)], [(r["name"], r["transfer_id"], r["backups"], r["snapshots"]) for r in left])


class SignInTests(unittest.TestCase):
    """A Homestead mid-restart may refuse a session it has only just given."""

    def test_a_401_straight_after_a_fresh_sign_in_is_waited_out(self):
        refused = urllib.error.HTTPError("", 401, "unauthorized", {}, None)
        with mock.patch.object(MOVE, "_cluster", return_value={"url": "http://192.0.2.235:8088", "user": "admin"}), \
                mock.patch.object(MOVE, "_token", return_value="t") as token, \
                mock.patch.object(MOVE, "_call", side_effect=refused), \
                mock.patch.object(MOVE, "_reason", return_value="not signed in"):
            with self.assertRaisesRegex(MOVE.Unreachable, "may be restarting"):
                MOVE.remote("test-cluster", "/api/move/source-status")
        self.assertEqual([False, True], [c.kwargs["force"] for c in token.call_args_list])

    def test_credentials_it_refuses_still_stop_the_transfer(self):
        refused = urllib.error.HTTPError("", 401, "unauthorized", {}, None)
        with mock.patch.object(MOVE, "_cluster", return_value={"url": "http://192.0.2.235:8088", "user": "admin"}), \
                mock.patch.object(MOVE, "_password", return_value="wrong"), \
                mock.patch.object(MOVE, "_call", side_effect=refused), \
                mock.patch.dict(MOVE._tokens, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "refused those Homestead credentials"):
                MOVE.remote("test-cluster", "/api/move/source-status")


if __name__ == "__main__":
    unittest.main()
