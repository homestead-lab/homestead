"""#931 follow-up: an outage webhook's Secret left behind by an app or VM
deleted outside Homestead is swept by the leader - and nothing else is.

#942 gave these Secrets no ownerReference: garbage collection would delete
one when a move or a rename deletes and recreates its app or VM. So the
leader sweeps them instead, and only once the owner has been seen missing in
two rounds GRACE apart, with no job on it, from lists it really read."""
import base64
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_outage as O
import homestead_outage_secret as S
import homestead_outage_sweep as SWEEP
from test_outage_secret import Base, Cluster, ITEM, KEY, REAL, SECRETS, VM  # noqa: F401  (Cluster wraps fakes.kube)
from test_outage_formats import SECRET as TOKEN
from fakes.kube import Kube

APP_SECRET = "homestead-outage-app-frigate"
VM_SECRET = "homestead-outage-vm-frigate"
T0 = 1_000_000


class SweepCase(Base):
    def setUp(self):
        super().setUp()
        SWEEP._seen.clear()
        SWEEP._next.clear()
        self.addCleanup(SWEEP._seen.clear)
        self.addCleanup(SWEEP._next.clear)
        emit = mock.patch.object(SWEEP.AUDIT, "emit")
        self.emit = emit.start()
        self.addCleanup(emit.stop)

    def keep(self, item=ITEM):
        """The Secret a save writes for item, as #942 writes it."""
        S.save(item["kind"], item["ns"], item["name"], REAL, self.cluster.kget, self.cluster.ksend)

    def gone(self, item=ITEM):
        self.cluster.discard(O._path_of(item["kind"], item["ns"], item["name"]))

    def there(self, name):
        return self.cluster.secret(name) is not None

    def sweep(self, now, busy=lambda ns, name: False):
        """The round at now, after the leader's rounds every EVERY since the last one."""
        last = getattr(self, "last", None)
        while last is not None and last + SWEEP.EVERY < now:
            last += SWEEP.EVERY
            SWEEP.sweep(busy, now=last)
        return self.jump(now, busy)

    def jump(self, now, busy=lambda ns, name: False):
        """Only the round at now: no rounds ran since the last (a gap, or another copy led)."""
        self.last = now
        return SWEEP.sweep(busy, now=now)

    def rounds(self, *times, busy=lambda ns, name: False):
        for at in times:
            self.sweep(at, busy)


class KeptTests(SweepCase):
    def test_a_secret_whose_app_is_there_is_kept(self):
        self.cluster.add(ITEM)
        self.keep()
        self.rounds(T0, T0 + SWEEP.GRACE, T0 + 3 * SWEEP.GRACE)
        self.assertTrue(self.there(APP_SECRET))
        self.emit.assert_not_called()

    def test_a_secret_whose_vm_is_there_is_kept(self):
        self.cluster.add(VM)
        self.keep(VM)
        self.rounds(T0, T0 + SWEEP.GRACE, T0 + 3 * SWEEP.GRACE)
        self.assertTrue(self.there(VM_SECRET))

    def test_an_owner_missing_briefly_during_a_move_is_kept(self):
        # Deleted and recreated: missing at one round, back at the next.
        self.cluster.add(ITEM)
        self.keep()
        self.gone()
        self.sweep(T0)
        self.cluster.add(ITEM)
        self.sweep(T0 + SWEEP.EVERY)
        self.gone()
        self.sweep(T0 + SWEEP.GRACE)       # missing again: the clock starts again here
        self.assertTrue(self.there(APP_SECRET))
        self.sweep(T0 + SWEEP.GRACE + SWEEP.EVERY)
        self.assertTrue(self.there(APP_SECRET), "missing for one interval only since it came back")

    def test_an_owner_missing_for_less_than_grace_is_kept(self):
        self.keep()
        self.rounds(T0, T0 + SWEEP.EVERY, T0 + SWEEP.GRACE - 1)
        self.assertTrue(self.there(APP_SECRET))

    def test_one_round_is_never_enough_however_old_the_secret(self):
        self.keep()
        self.sweep(T0)
        self.assertTrue(self.there(APP_SECRET))

    def test_an_owner_with_a_job_running_is_kept(self):
        # A move's job may take longer than GRACE: while it runs, nothing of it goes.
        self.keep()
        busy = lambda ns, name: (ns, name) == ("lab", "frigate")
        self.rounds(T0, T0 + SWEEP.GRACE, T0 + 2 * SWEEP.GRACE, busy=busy)
        self.assertTrue(self.there(APP_SECRET))

    def test_a_secret_a_present_app_names_is_kept(self):
        # A rename: the new app reads the old name's Secret until the leader moves it.
        self.keep({"kind": "app", "ns": "lab", "name": "old"})
        self.cluster.add(ITEM, {"webhook": "https://gotify.example.com/(hidden)", "format": "gotify",
                                "secret": "homestead-outage-app-old"})
        self.rounds(T0, T0 + SWEEP.GRACE)
        self.assertTrue(self.there("homestead-outage-app-old"))

    def test_two_rounds_far_apart_with_none_between_are_not_enough(self):
        # This copy lost the lead, or its round stopped, between them: the owner may have
        # come back and be missing again now for a move. Its clock starts again.
        self.keep()
        self.jump(T0)
        self.jump(T0 + 3 * SWEEP.GRACE)
        self.assertTrue(self.there(APP_SECRET))
        self.sweep(T0 + 4 * SWEEP.GRACE - SWEEP.EVERY)
        self.assertTrue(self.there(APP_SECRET))
        self.sweep(T0 + 4 * SWEEP.GRACE)
        self.assertFalse(self.there(APP_SECRET), "an hour of rounds in a row is enough")

    def test_a_secret_made_again_starts_its_clock_again(self):
        self.keep()
        self.sweep(T0)
        self.cluster.discard(f"{SECRETS}/{APP_SECRET}")
        self.keep()                         # a new object: a new uid
        self.sweep(T0 + SWEEP.GRACE)
        self.assertTrue(self.there(APP_SECRET))


class SweptTests(SweepCase):
    def test_a_long_absent_apps_secret_is_swept_logged_and_audited(self):
        self.cluster.add(ITEM)
        self.keep()
        self.gone()
        self.sweep(T0)
        with mock.patch("builtins.print") as said:
            result = self.sweep(T0 + SWEEP.GRACE)
        self.assertFalse(self.there(APP_SECRET))
        self.assertEqual([f"lab/{APP_SECRET}"], result["swept"])
        logged = " ".join(str(call.args[0]) for call in said.call_args_list)
        self.assertIn(f"lab/{APP_SECRET}", logged)
        self.assertIn("app/frigate", logged)
        self.assertNotIn("token", logged.replace("token=", ""), "never the address")
        self.emit.assert_called_once()
        entry = self.emit.call_args.args[0]
        self.assertEqual("outage-secret-swept", entry["event"])
        self.assertEqual(("leader", "homestead"), (entry["via"], entry["user"]))
        self.assertEqual(("DELETE", f"{SECRETS}/{APP_SECRET}"), (entry["method"], entry["path"]))
        self.assertNotIn("target", entry, "the Timeline links a target to the app of that name: the Secret is none")
        self.assertEqual("app/frigate", entry["for"])
        self.assertNotIn("gotify.example.com", json.dumps(entry))

    def test_no_address_or_token_reaches_the_log_or_the_audit(self):
        self.keep()
        with mock.patch("builtins.print") as said:
            self.rounds(T0, T0 + SWEEP.GRACE)
        self.assertFalse(self.there(APP_SECRET))
        written = json.dumps([call.args for call in said.call_args_list + self.emit.call_args_list])
        self.assertNotIn(TOKEN, written)
        self.assertNotIn("gotify.example.com", written)

    def test_a_secret_gone_before_the_delete_is_not_reported_as_swept(self):
        # Deleted meanwhile, by Homestead's own delete of the app say: nothing to log or audit.
        self.keep()
        self.sweep(T0)
        self.sweep(T0 + SWEEP.GRACE - SWEEP.EVERY)
        self.cluster.kube.fail(f"{SECRETS}/{APP_SECRET}", 404, method="DELETE")
        result = self.sweep(T0 + SWEEP.GRACE)
        self.assertEqual([], result["swept"])
        self.emit.assert_not_called()

    def test_an_owner_of_that_name_in_another_namespace_does_not_keep_it(self):
        self.cluster.add({"kind": "app", "ns": "other", "name": "frigate"})
        self.keep()
        self.rounds(T0, T0 + SWEEP.GRACE)
        self.assertFalse(self.there(APP_SECRET))

    def test_a_long_absent_vms_secret_is_swept(self):
        self.keep(VM)
        self.rounds(T0, T0 + SWEEP.GRACE)
        self.assertFalse(self.there(VM_SECRET))

    def test_the_delete_names_the_secret_as_read(self):
        self.keep()
        self.sweep(T0)
        self.sweep(T0 + SWEEP.GRACE)
        method, path, body = self.cluster.writes[-1]
        self.assertEqual(("DELETE", f"{SECRETS}/{APP_SECRET}"), (method, path))
        self.assertEqual({"uid", "resourceVersion"}, set(body["preconditions"]))

    def test_a_secret_changed_meanwhile_is_not_deleted(self):
        self.keep()
        self.sweep(T0)
        real_kget = self.cluster.kget

        def kget(path):
            found = real_kget(path)
            if "/secrets" in path and "labelSelector" in path:
                # Saved again between the list and the delete: a new resourceVersion.
                self.cluster.change(f"{SECRETS}/{APP_SECRET}", lambda obj: obj["metadata"].pop("resourceVersion"))
            return found
        O._bound["kget"] = kget
        self.sweep(T0 + SWEEP.GRACE)
        self.assertTrue(self.there(APP_SECRET))

    def test_at_most_max_deletes_a_round(self):
        for n in range(SWEEP.MAX_DELETES + 3):
            self.keep({"kind": "app", "ns": "lab", "name": f"gone-{n}"})
        self.sweep(T0)
        result = self.sweep(T0 + SWEEP.GRACE)
        self.assertEqual(SWEEP.MAX_DELETES, len(result["swept"]))
        self.sweep(T0 + SWEEP.GRACE + SWEEP.EVERY)
        self.assertEqual([], [n for n in range(SWEEP.MAX_DELETES + 3)
                              if self.there(f"homestead-outage-app-gone-{n}")])

    def test_rounds_closer_than_every_are_skipped(self):
        self.keep()
        self.sweep(T0)
        self.assertIsNone(self.sweep(T0 + 1))


class UnreadTests(SweepCase):
    def test_a_failed_app_list_sweeps_nothing(self):
        self.keep()
        self.sweep(T0)
        self.cluster.kube.fail("/apis/apps/v1/deployments", 500, times=1000)
        self.sweep(T0 + SWEEP.GRACE)
        self.assertTrue(self.there(APP_SECRET))

    def test_a_failed_vm_list_sweeps_nothing(self):
        self.keep()
        self.keep(VM)
        self.sweep(T0)
        self.cluster.kube.fail("/apis/kubevirt.io/v1/virtualmachines", 500, times=1000)
        self.sweep(T0 + SWEEP.GRACE)
        self.assertTrue(self.there(APP_SECRET))
        self.assertTrue(self.there(VM_SECRET))

    def test_a_failed_list_starts_every_clock_again(self):
        self.keep()
        self.sweep(T0)
        self.cluster.kube.fail("/apis/apps/v1/deployments", 500)
        self.sweep(T0 + SWEEP.EVERY)        # this round's list fails; the next ones are read
        self.sweep(T0 + SWEEP.GRACE)
        self.assertTrue(self.there(APP_SECRET), "absence is counted only over lists read in a row")

    def test_a_list_cut_short_sweeps_nothing(self):
        self.keep()
        self.sweep(T0)
        self.cluster.kube.react("GET", "/apis/apps/v1/deployments",
                                lambda *a: {"items": [], "metadata": {"continue": "more"}})
        self.sweep(T0 + SWEEP.GRACE)
        self.assertTrue(self.there(APP_SECRET))

    def test_a_failed_job_list_sweeps_nothing(self):
        self.keep()
        self.sweep(T0)

        def busy(ns, name):
            raise OSError("jobs not readable")
        self.sweep(T0 + SWEEP.GRACE, busy)
        self.assertTrue(self.there(APP_SECRET))

    def test_a_failed_secret_list_sweeps_nothing_and_never_raises(self):
        self.keep()
        self.sweep(T0)
        self.cluster.kube.fail("/api/v1/secrets", 500, times=1000)
        self.sweep(T0 + SWEEP.GRACE)
        self.assertTrue(self.there(APP_SECRET))


class NoKubeVirtTests(SweepCase):
    def setUp(self):
        super().setUp()
        self.cluster.kube = Kube(groups=("apps",), absent_groups=("kubevirt.io",))

    def test_without_kubevirt_an_apps_secret_is_still_swept(self):
        self.keep()
        self.rounds(T0, T0 + SWEEP.GRACE)
        self.assertFalse(self.there(APP_SECRET))


class ForeignTests(SweepCase):
    def add_secret(self, name, labels=None, annotations=None):
        self.cluster.kube.add({"apiVersion": "v1", "kind": "Secret",
                               "metadata": {"name": name, "namespace": "lab", "labels": labels or {},
                                            "annotations": annotations or {}},
                               "data": {"webhook": base64.b64encode(REAL.encode()).decode()}})

    def test_secrets_homestead_did_not_make_for_this_are_never_touched(self):
        ours = {S.LABEL: "app", "app.kubernetes.io/managed-by": "homestead"}
        cases = {
            "homestead-outage-app-a": ({}, {S.OWNER: "app/a"}),                                   # no label
            "homestead-outage-app-b": ({S.LABEL: "app"}, {S.OWNER: "app/b"}),                     # not managed by Homestead
            "homestead-outage-app-c": (ours, {}),                                                 # no owner
            "homestead-outage-app-d": (ours, {S.OWNER: "vm/d"}),                                  # owner of another kind
            "homestead-outage-app-e": (ours, {S.OWNER: "app/someone-else"}),                      # not that owner's name
            "homestead-outage-app-f": ({**ours, S.LABEL: "job"}, {S.OWNER: "job/f"}),             # a kind it does not know
            "homestead-env-frigate": ({"homestead.io/env-for": "frigate"}, {}),                   # an app's variables
            "unrelated": ({"app": "x"}, {}),
        }
        for name, (labels, annotations) in cases.items():
            self.add_secret(name, labels, annotations)
        self.rounds(T0, T0 + SWEEP.GRACE, T0 + 3 * SWEEP.GRACE)
        self.assertEqual([], [name for name in cases if not self.there(name)])
        self.assertEqual([], [w for w in self.cluster.writes if w[0] == "DELETE"])


class TickTests(Base):
    def tick(self, **sweep):
        server = self.server()
        with mock.patch.object(server.CACHE, "cached", side_effect=lambda key, ttl, fn: []), \
                mock.patch.object(server.OPS, "snapshot", return_value=[
                    {"status": "running", "resource": {"namespace": "lab", "name": "busy"}}]), \
                mock.patch.object(server.UPTIME, "report", return_value={}), \
                mock.patch.object(server.OVERVIEW_ROUTES.OUTAGE_CARRY, "carry_lan") as carry, \
                mock.patch.object(server.OVERVIEW_ROUTES.OUTAGE_SWEEP, "sweep", **sweep) as swept:
            server.OVERVIEW_ROUTES.outage_tick()
        return swept, carry

    def test_the_leaders_round_sweeps_with_its_running_jobs(self):
        swept, _ = self.tick(return_value=None)
        swept.assert_called_once()
        busy = swept.call_args.args[0]
        self.assertTrue(busy("lab", "busy"))
        self.assertFalse(busy("lab", "frigate"))

    def test_a_sweep_that_raises_never_stops_the_round(self):
        _, carry = self.tick(side_effect=RuntimeError("bug"))
        carry.assert_called_once()

    def busy_with(self, *jobs):
        server = self.server()
        with mock.patch.object(server.OPS, "snapshot", return_value=list(jobs)):
            return server.OVERVIEW_ROUTES._busy_owners()

    def test_a_job_still_cancelling_or_waiting_for_recovery_counts_as_busy(self):
        # A cancelled move puts the app back; a failed one may still be recovered.
        on = {"namespace": "lab", "name": "frigate"}
        for job in ({"status": "queued"}, {"status": "cancelling"},
                    {"status": "failed", "dismissible": False}, {"status": "cancelled", "dismissible": False}):
            with self.subTest(job=job):
                self.assertTrue(self.busy_with({**job, "resource": on})("lab", "frigate"))
        for job in ({"status": "succeeded", "dismissible": True}, {"status": "failed", "dismissible": True}):
            with self.subTest(job=job):
                self.assertFalse(self.busy_with({**job, "resource": on})("lab", "frigate"))

    def test_a_job_list_that_cannot_be_read_counts_every_owner_busy(self):
        server = self.server()
        with mock.patch.object(server.OPS, "snapshot", side_effect=OSError("unreadable")):
            busy = server.OVERVIEW_ROUTES._busy_owners()
        with self.assertRaises(OSError):
            busy("lab", "frigate")


if __name__ == "__main__":
    unittest.main()
