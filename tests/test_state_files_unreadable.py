"""A state file on the data volume that is there but cannot be read is not
"nothing recorded" (sweep unknown-as-fine, row 10).

A missing file is a fresh start. A file that is corrupt or unreadable raises
SHARED.Unreadable (#658): an alert source that raises keeps its alerts
(_alert_sources gives None), a loop's round does nothing and says why, and
nothing is saved over the file.

Left as they are, on purpose: leader-state.json (a copy of what the leader
last did, rewritten every few seconds; unread shows "standby", not good news)
and passthrough-inventory.json (a display cache of inspections that must not
block a device handoff: test_passthrough's
test_invalid_display_inventory_is_ignored)."""
import contextlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_baseline as BASELINE
import homestead_forecast as FORECAST
import homestead_host_console as HOST_CONSOLE
import homestead_host_limits as HOST_LIMITS
import homestead_host_os as HOST_OS
import homestead_image_cleanup as CLEANUP
import homestead_manifests as MANIFESTS
import homestead_node_parity as NODE_PARITY
import homestead_os_rollout as OS_ROLLOUT
import homestead_outage as OUTAGE
import homestead_ports as PORTS
import homestead_recovery as RECOVERY
import homestead_root_guard as ROOT_GUARD
import homestead_schedules as SCHEDULES
import homestead_setup as SETUP
import homestead_shared as SHARED

CORRUPT = '{"items": {"app:lab/web": {"gave_up": tr'   # cut off mid-write

# module, its reader, and the file it reads (relative to the data dir).
STORES = [
    (OUTAGE, "_load", "outage-actions.json"),
    (ROOT_GUARD, "_load", "root-guard.json"),
    (HOST_OS, "_load", "host-os.json"),
    (SCHEDULES, "_load", "schedules.json"),
    (FORECAST, "_load", "storage-forecast.json"),
    (RECOVERY, "_load", "recovery.json"),
    (PORTS, "_load", "host-ports.json"),
    (OS_ROLLOUT, "_load", "os-rollout.json"),
    (BASELINE, "_load", "baseline.json"),
    (NODE_PARITY, "_load", "node-parity.json"),
    (MANIFESTS, "_load", "manifests-skipped.json"),
    (HOST_LIMITS, "_load", "host-limits.json"),
    (SETUP, "load", "setup.json"),
    (HOST_CONSOLE, "_read", "host-consoles.json"),
]


class Base(unittest.TestCase):
    def setUp(self):
        self.data = tempfile.mkdtemp()
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        for module, _, _ in STORES:
            self.stack.enter_context(mock.patch.object(module, "DATA_DIR", self.data))
        self.stack.enter_context(mock.patch.object(CLEANUP, "_path", lambda: os.path.join(self.data, CLEANUP.STORE)))
        self.stack.enter_context(mock.patch("sys.stdout", new_callable=lambda: open(os.devnull, "w")))

    def corrupt(self, name, text=CORRUPT):
        path = os.path.join(self.data, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def assertUntouched(self, path, text=CORRUPT):
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(text, handle.read(), f"{os.path.basename(path)} was saved over")

    def patch(self, module, **values):
        for name, value in values.items():
            self.stack.enter_context(mock.patch.object(module, name, value, create=True))


class EveryStoreTells_MissingFromUnreadable(Base):
    def test_a_missing_file_is_nothing_recorded(self):
        for module, reader, name in STORES + [(CLEANUP, "_load_since", CLEANUP.STORE)]:
            with self.subTest(store=name):
                found = getattr(module, reader)()
                self.assertIsInstance(found, dict)
                self.assertFalse(any(found.values()) if module is RECOVERY else found)

    def test_a_corrupt_or_wrongly_shaped_file_raises_unreadable(self):
        for module, reader, name in STORES + [(CLEANUP, "_load_since", CLEANUP.STORE)]:
            for text in (CORRUPT, "[1, 2]"):
                with self.subTest(store=name, text=text):
                    self.corrupt(name, text)
                    with self.assertRaises(SHARED.Unreadable):
                        getattr(module, reader)()

    def test_a_file_that_cannot_be_opened_raises_unreadable(self):
        for module, reader, name in STORES:
            with self.subTest(store=name):
                os.makedirs(os.path.join(self.data, name))     # EISDIR: there, but not readable as a file
                with self.assertRaises(SHARED.Unreadable):
                    getattr(module, reader)()


class AlertSourcesKeepTheirAlerts(Base):
    """A source that raises is None in _alert_sources, so ALERTS.observe keeps its alerts."""

    def test_root_guard(self):
        self.corrupt("root-guard.json")
        with self.assertRaises(SHARED.Unreadable):
            ROOT_GUARD.alert_facts()

    def test_host_os(self):
        self.corrupt("host-os.json")
        self.patch(HOST_OS, _boots=lambda: {})
        with self.assertRaises(SHARED.Unreadable):
            HOST_OS.alert_facts()

    def test_forecast(self):
        self.corrupt("storage-forecast.json")
        with self.assertRaises(SHARED.Unreadable):
            FORECAST.alert_facts(FORECAST.report())

    def test_recovery(self):
        self.corrupt("recovery.json")
        with self.assertRaises(SHARED.Unreadable):
            RECOVERY.alert_facts(mode="suggest", maintenance=None)


class StoppedActionsSaySo(Base):
    """Outage actions and schedules that cannot run while their file is
    unreadable keep their alerts and raise one that says they are not running."""

    def observe(self, source, file, facts_fn, held_key, new_key, words):
        import homestead_alerts as ALERTS
        before = ALERTS.DATA_DIR
        self.addCleanup(ALERTS.bind, before)
        ALERTS.bind(tempfile.mkdtemp())
        ALERTS.observe({source: []}, now=900)
        ALERTS.observe({source: [{"key": held_key, "title": "down", "severity": "critical", "category": "outage"}]}, now=1000)
        ALERTS.observe({source: [{"key": held_key, "title": "down", "severity": "critical", "category": "outage"}]}, now=1100)
        self.corrupt(file)
        facts = facts_fn()
        fresh = ALERTS.observe({source: facts}, now=1200) + ALERTS.observe({source: facts_fn()}, now=1300)
        self.assertEqual([("raised", new_key)], [(e["phase"], e["key"]) for e in fresh], "only the new alert; none cleared")
        row = ALERTS._load()["active"][new_key]
        self.assertIn(words, row["title"])
        self.assertIn(file, row["title"])
        self.assertIn(held_key, ALERTS._load()["active"])

    def test_outage_actions(self):
        self.observe("outage", "outage-actions.json", OUTAGE.alert_facts, "outage:app:lab/web",
                     "outage-actions:unreadable", "Outage actions are not running")

    def test_schedules(self):
        self.observe("schedules", "schedules.json", SCHEDULES.alert_facts, "schedule:app:lab/web",
                     "schedules:unreadable", "Scheduled stops and starts are not running")

    def test_the_outage_round_beats_its_own_error_and_still_carries_over(self):
        import server
        import homestead_self_health as SELF_HEALTH
        routes = server.OVERVIEW_ROUTES
        calls, beats = [], []

        def tick(*args):
            calls.append("tick")
            raise SHARED.Unreadable("Homestead could not read outage-actions.json on its data volume")
        with mock.patch.object(routes, "cached", side_effect=lambda key, ttl, fn: [{"ns": "lab", "name": "web"}] if key == "wl" else []), \
             mock.patch.object(routes.OUTAGE, "items", return_value=[{"kind": "app", "ns": "lab", "name": "web"}]), \
             mock.patch.object(routes.UPTIME, "report", return_value={}), \
             mock.patch.object(routes.OPS, "snapshot", return_value=[]), \
             mock.patch.object(routes.OUTAGE_CARRY, "carry_lan", side_effect=lambda: calls.append("carry")), \
             mock.patch.object(routes.OUTAGE, "tick", side_effect=tick), \
             mock.patch.object(routes, "beat", side_effect=lambda *a, **k: beats.append(a)):
            self.assertEqual([], routes.outage_tick())
        self.assertEqual(["tick", "carry"], calls)
        self.assertEqual("outage-actions", beats[-1][0])
        self.assertIn("outage-actions.json", str(beats[-1][2]))
        self.assertEqual("Outage actions", SELF_HEALTH.LOOP_WORDS["outage-actions"])


class LoopsDoNothingAndSaveNothingOver(Base):
    K3S = {"distribution": "k3s", "longhorn": True}
    NODES = {"items": [{"metadata": {"name": "node-a", "uid": "u1"},
                        "status": {"conditions": [{"type": "Ready", "status": "True"}], "nodeInfo": {"bootID": "b"}}}]}

    def setUp(self):
        super().setUp()
        self.ran = []
        runner = mock.Mock()
        runner.run = lambda *a, **k: (self.ran.append(a) or ("RAISED 1 2 3 4 done", ""))

        def kget(path):
            return {"items": []} if "longhorn" in path else self.NODES
        self.patch(ROOT_GUARD, platform=lambda *a: self.K3S, probes=lambda: {"node-a": {"mounts": [{"mountpoint": "/"}]}},
                   kget=kget, ksend=lambda *a, **k: self.ran.append(a))
        self.patch(HOST_LIMITS, platform=lambda *a: self.K3S, kget=kget, hostrun=runner)
        self.patch(MANIFESTS, platform=lambda *a: self.K3S, servers=lambda: [("node-a", True)], hostrun=runner)
        self.patch(NODE_PARITY, platform=lambda *a: self.K3S, _ready_nodes=lambda: ["node-a"], kget=kget, hostrun=runner,
                   ksend=lambda *a, **k: self.ran.append(a))
        self.patch(HOST_OS, platform=lambda *a: self.K3S, applies=lambda *a: True, kget=kget, hostrun=runner)

    def test_the_host_fixes_ticks_return_nothing_run_nothing_and_keep_the_file(self):
        """They share one loop: one unreadable store must not stop the others, so each returns."""
        for module, name, nothing in ((ROOT_GUARD, "root-guard.json", []), (HOST_LIMITS, "host-limits.json", {}),
                                      (MANIFESTS, "manifests-skipped.json", {}), (NODE_PARITY, "node-parity.json", []),
                                      (HOST_OS, "host-os.json", [])):
            with self.subTest(store=name):
                self.ran.clear()
                path = self.corrupt(name)
                self.assertEqual(nothing, module.tick())
                self.assertEqual([], self.ran, "nothing is done to a host from an unread record")
                self.assertUntouched(path)

    def test_outage_and_schedules_raise_and_keep_the_file(self):
        outage = self.corrupt("outage-actions.json")
        with self.assertRaises(SHARED.Unreadable):
            OUTAGE.tick([], {}, lambda item: None, lambda item: False)
        self.assertUntouched(outage)
        schedules = self.corrupt("schedules.json")
        with self.assertRaises(SHARED.Unreadable):
            SCHEDULES.tick([], lambda item, action: "")
        self.assertUntouched(schedules)

    def test_setup_guide_and_host_console_say_unreadable_rather_than_start_over(self):
        setup = self.corrupt("setup.json")
        with self.assertRaises(SHARED.Unreadable):
            SETUP.skips("ann")
        self.assertUntouched(setup)
        console = self.corrupt("host-consoles.json")
        with self.assertRaises(SHARED.Unreadable):
            HOST_CONSOLE.set_cluster(True)
        self.assertUntouched(console)

    def test_image_clean_up_keeps_its_first_seen_dates(self):
        path = self.corrupt(CLEANUP.STORE)
        with self.assertRaises(SHARED.Unreadable):
            CLEANUP.track({"images": [{"digest": "sha256:a", "nodes": ["node-a"]}]})
        self.assertUntouched(path)


if __name__ == "__main__":
    unittest.main()
