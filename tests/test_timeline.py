"""The "since you were last here" timeline (#748)."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_shared as SHARED
import homestead_timeline as T

T0 = 1_790_000_000


class DataDir(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(SHARED, "DIR", self.tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)

    def seen_file(self):
        return os.path.join(self.tmp.name, T.SEEN_FILE)

    def seen(self):
        with open(self.seen_file(), encoding="utf-8") as handle:
            return json.load(handle)


class LastHereTests(DataDir):
    def test_first_visit_looks_back_one_day(self):
        self.assertEqual(T0 - T.FIRST_LOOK, T.last_here("alice", T0))
        self.assertEqual(T0 - T.FIRST_LOOK, T.mark_seen("alice", T0))
        self.assertEqual(T0 - T.FIRST_LOOK, T.last_here("alice", T0 + 60))

    def test_refresh_within_a_visit_keeps_since(self):
        T.mark_seen("alice", T0)
        T.mark_seen("alice", T0 + 600)
        # Still the same visit: the card does not empty on a refresh.
        self.assertEqual(T0 - T.FIRST_LOOK, T.last_here("alice", T0 + 900))
        self.assertEqual(T0 - T.FIRST_LOOK, T.mark_seen("alice", T0 + 1200))

    def test_new_visit_starts_where_the_last_ended(self):
        T.mark_seen("alice", T0)
        T.mark_seen("alice", T0 + 600)
        back = T0 + 600 + T.VISIT_GAP + 1
        self.assertEqual(T0 + 600, T.last_here("alice", back))
        self.assertEqual(T0 + 600, T.mark_seen("alice", back))
        self.assertEqual(T0 + 600, T.last_here("alice", back + 60))

    def test_rows_are_bounded_and_expire(self):
        T.mark_seen("old", T0)
        for i in range(T.SEEN_USERS + 5):
            T.mark_seen(f"user{i}", T0 + T.SEEN_KEEP + 10 + i)
        rows = self.seen()["users"]
        self.assertNotIn("old", rows)
        self.assertEqual(T.SEEN_USERS, len(rows))
        self.assertNotIn("user0", rows)          # the least recently here goes first
        self.assertIn(f"user{T.SEEN_USERS + 4}", rows)

    def test_each_person_has_their_own_marker(self):
        T.mark_seen("alice", T0)
        T.mark_seen("alice", T0 + T.VISIT_GAP + 100)
        self.assertEqual(T0, T.last_here("alice", T0 + T.VISIT_GAP + 200))
        self.assertEqual(T0 + T.VISIT_GAP + 200 - T.FIRST_LOOK, T.last_here("bob", T0 + T.VISIT_GAP + 200))

    def test_no_user_has_no_marker(self):
        self.assertEqual(T0 - T.FIRST_LOOK, T.last_here("", T0))
        self.assertIsNone(T.mark_seen("", T0))
        self.assertFalse(os.path.exists(self.seen_file()))

    def test_unreadable_marker_file_raises_and_is_not_overwritten(self):
        with open(self.seen_file(), "w", encoding="utf-8") as handle:
            handle.write("{half")
        with self.assertRaises(SHARED.Unreadable):
            T.last_here("alice", T0)
        with self.assertRaises(SHARED.Unreadable):
            T.mark_seen("alice", T0)
        with open(self.seen_file(), encoding="utf-8") as handle:
            self.assertEqual("{half", handle.read())


def iso(t):
    import time as _t
    return _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime(t))


class Sources:
    """Fakes for every source, patched onto the module the timeline reads."""

    def __init__(self, case, now=T0):
        self.now = now
        self.changes = [{"at": now - 3600, "namespace": "apps", "name": "game", "source": "homestead", "by": "alice",
                         "changes": [{"field": "Image", "before": "example/game:1", "after": "example/game:2"},
                                     {"field": "Environment", "before": "PASSWORD=hunter2", "after": "PASSWORD=hunter3"}]},
                        {"at": now - 7200, "namespace": "apps", "name": "wiki", "source": "outside", "by": "",
                         "changes": [{"field": "Copies", "before": "1", "after": "2"}]},
                        {"at": now - 5 * 86400, "namespace": "apps", "name": "old", "source": "outside", "by": "",
                         "changes": [{"field": "Copies", "before": "1", "after": "2"}]}]
        self.restarts = [{"ns": "apps", "name": "game", "kind": "restart", "at": iso(now - 1800), "pod": "game-x",
                          "container": "server", "reason": "Ran out of memory (limit 512 MiB)",
                          "detail": "secret-detail-text", "note": ""},
                         {"ns": "apps", "name": "wiki", "kind": "rollout", "at": iso(now - 7100), "pod": "wiki-y",
                          "reason": "Image update", "detail": ""},
                         {"ns": "apps", "name": "db", "kind": "replaced", "at": iso(now - 600), "reason": "Evicted",
                          "detail": "The node was low on resource: memory."}]
        self.cron = [{"ns": "apps", "name": "backup", "result": "failed", "result_at": iso(now - 900),
                      "result_reason": "BackoffLimitExceeded: token=abc123", "missed": False, "last": iso(now - 1000)},
                     {"ns": "apps", "name": "nightly", "result": "ok", "result_at": iso(now - 300), "result_reason": "",
                      "missed": False, "last": iso(now - 400)},
                     {"ns": "apps", "name": "weekly", "result": "ok", "result_at": iso(now - 9 * 86400),
                      "result_reason": "", "missed": True, "last": iso(now - 9 * 86400)},
                     {"ns": "apps", "name": "lost", "result": "unknown", "result_at": "", "result_reason": "",
                      "missed": None, "last": ""}]
        self.alerts = [{"id": 1, "at": now - 950, "phase": "raised", "key": "cronjob:apps/backup:failed",
                        "category": "degraded", "severity": "degraded", "title": "Scheduled job backup failed",
                        "href": "/schedules?find=apps/backup"},
                       {"id": 2, "at": now - 100, "phase": "resolved", "key": "cronjob:apps/backup:failed",
                        "category": "degraded", "severity": "degraded", "title": "Scheduled job backup ran successfully",
                        "href": "/schedules?find=apps/backup"},
                       {"id": 3, "at": now - 50, "phase": "raised", "key": "test", "category": "test",
                        "severity": "info", "title": "Test notification", "href": "/"}]
        self.hosts = [{"node": "node-2", "start": now - 4000, "end": now - 3600, "reason": "OS update",
                       "job": "j1", "outcome": "returned"}]
        self.failing = set()
        self.case = case
        def guard(name, value):
            def read(*args, **kwargs):
                if name in self.failing:
                    raise SHARED.Unreadable(f"Homestead could not read {name}.json on its data volume (Permission denied)")
                return value() if callable(value) else value
            return read
        for target, name, value in ((T.CHANGES, "history", lambda: self.changes),
                                    (T.RESTARTS, "recent", None), (T, "_cron_rows", lambda: self.cron),
                                    (T.ALERTS, "log", lambda: {"latest": 3, "alerts": self.alerts}),
                                    (T.HISTORY, "maintenance_since", None)):
            if name == "recent":
                fn = lambda since, _g=guard("restarts", lambda: self.restarts): [r for r in _g() if r["at"] > iso(since)]
            elif name == "maintenance_since":
                fn = lambda since, _g=guard("hosts", lambda: self.hosts): _g()
            else:
                key = {"history": "changes", "_cron_rows": "schedules", "log": "alerts", "_audit_lines": "audit"}[name]
                fn = guard(key, value)
            patcher = mock.patch.object(target, name, fn)
            patcher.start()
            case.addCleanup(patcher.stop)


class FeedTests(DataDir):
    def setUp(self):
        super().setUp()
        self.src = Sources(self)

    def feed(self, **kw):
        kw.setdefault("since", T0 - 86400)
        return T.timeline("alice", kw.pop("role", "viewer"), now=T0, **kw)

    def test_each_source_becomes_sentences_with_links(self):
        out = self.feed()
        texts = {e["kind"]: [] for e in out["entries"]}
        for e in out["entries"]:
            texts[e["kind"]].append(e)
        self.assertEqual({"change", "restart", "schedule", "alert", "host"}, set(texts))
        game = next(e for e in texts["change"] if e["app"] == "apps/game")
        self.assertEqual("game changed: Image, Environment, by alice", game["text"])
        self.assertEqual("/containers?q=game", game["href"])
        wiki = next(e for e in texts["change"] if e["app"] == "apps/wiki")
        self.assertEqual("wiki changed: Copies, outside Homestead", wiki["text"])
        self.assertNotIn("apps/old", [e["app"] for e in texts["change"]])
        oom = next(e for e in texts["restart"] if e["app"] == "apps/game")
        self.assertEqual(("game restarted: Ran out of memory (limit 512 MiB)", "warn"), (oom["text"], oom["tone"]))
        self.assertEqual("wiki rolled out: Image update", next(e for e in texts["restart"] if e["app"] == "apps/wiki")["text"])
        self.assertEqual("db's pod was replaced: Evicted", next(e for e in texts["restart"] if e["app"] == "apps/db")["text"])
        failed = next(e for e in texts["schedule"] if e["app"] == "apps/backup")
        self.assertEqual(("backup failed", "bad", "/schedules?find=apps/backup"), (failed["text"], failed["tone"], failed["href"]))
        self.assertEqual(("nightly ran", "ok"), next((e["text"], e["tone"]) for e in texts["schedule"] if e["app"] == "apps/nightly"))
        missed = next(e for e in texts["schedule"] if e["app"] == "apps/weekly")
        self.assertEqual(("weekly did not run when it was due", "warn", T0), (missed["text"], missed["tone"], missed["at"]))
        self.assertEqual(["Scheduled job backup failed", "Scheduled job backup ran successfully"],
                         sorted(e["text"] for e in texts["alert"]))
        self.assertEqual("ok", next(e["tone"] for e in texts["alert"] if "successfully" in e["text"]))
        host = texts["host"][0]
        self.assertEqual(("node-2", "node-2 went into maintenance (OS update), back after 7 min", "/nodes?node=node-2"),
                         (host["host"], host["text"], host["href"]))
        self.assertEqual({"change": 2, "restart": 3, "schedule": 3, "alert": 2, "host": 1}, out["counts"])
        self.assertEqual(sorted((e["at"] for e in out["entries"]), reverse=True), [e["at"] for e in out["entries"]])

    def test_unknown_cronjob_runs_are_said_not_skipped(self):
        out = self.feed()
        self.assertIn({"source": "schedules", "label": "Scheduled jobs",
                       "reason": "how 1 scheduled job last ran could not be read"}, out["unread"])

    def test_a_source_that_fails_is_named_not_skipped(self):
        for name, label in (("changes", "App changes"), ("restarts", "App restarts"), ("schedules", "Scheduled jobs"),
                            ("alerts", "Alerts"), ("hosts", "Host maintenance")):
            with self.subTest(name):
                self.src.failing = {name}
                out = self.feed()
                gone = [u for u in out["unread"] if u["source"] == name]
                self.assertEqual(1, len(gone), out["unread"])
                self.assertEqual(label, gone[0]["label"])
                self.assertIn("could not read", gone[0]["reason"])
                self.assertTrue(out["entries"], "the other sources still answer")

    def test_window_clamped_to_thirty_days(self):
        out = self.feed(since=T0 - 90 * 86400)
        self.assertEqual((T0 - T.MAX_WINDOW, True), (out["since"], out["clamped"]))
        self.assertIn("apps/old", [e["app"] for e in out["entries"]])
        out = self.feed(since=T0 + 500)
        self.assertEqual(T0, out["since"])

    def test_since_defaults_to_last_here(self):
        T.mark_seen("alice", T0 - 10 * 86400)
        T.mark_seen("alice", T0 - 3 * 86400)     # a new visit: since = 10 days ago
        out = T.timeline("alice", "viewer", now=T0 - 3 * 86400 + 60)
        self.assertEqual(T0 - 10 * 86400, out["since"])
        self.assertEqual(T0 - 10 * 86400, out["last_here"])

    def test_unreadable_marker_is_named_and_a_day_is_shown(self):
        with open(self.seen_file(), "w", encoding="utf-8") as handle:
            handle.write("[")
        out = T.timeline("alice", "viewer", now=T0)
        self.assertEqual(T0 - T.FIRST_LOOK, out["since"])
        self.assertEqual(["last-here"], [u["source"] for u in out["unread"] if u["source"] == "last-here"])

    def test_entries_capped_newest_first_and_counted(self):
        self.src.changes = [{"at": T0 - i, "namespace": "apps", "name": f"a{i}", "source": "outside", "by": "",
                             "changes": [{"field": "Copies", "before": "1", "after": "2"}]} for i in range(1, 400)]
        out = self.feed()
        self.assertEqual(T.MAX_ENTRIES, len(out["entries"]))
        self.assertEqual(399 + 3 + 3 + 2 + 1 - T.MAX_ENTRIES, out["more"])
        self.assertEqual(T0, out["entries"][0]["at"])      # weekly's missed run is "now"
        self.assertEqual(399, out["counts"]["change"])       # counts are of the whole window

    def test_no_values_from_change_rows_or_messages(self):
        text = json.dumps(self.feed())
        for secret in ("hunter", "example/game:2", "token=abc123", "BackoffLimitExceeded", "secret-detail-text",
                       "low on resource"):
            self.assertNotIn(secret, text)

    def test_maintenance_begun_before_the_window_shows_when_it_ended(self):
        self.src.hosts = [{"node": "node-2", "start": T0 - 2 * 86400, "end": T0 - 3600, "reason": "OS update",
                           "job": "j1", "outcome": "returned"}]
        host = [e for e in self.feed()["entries"] if e["kind"] == "host"]
        self.assertEqual([T0 - 3600], [e["at"] for e in host])


class AuditTests(DataDir):
    def setUp(self):
        super().setUp()
        self.src = Sources(self)
        self.src.changes = self.src.restarts = self.src.cron = self.src.alerts = self.src.hosts = []

    def line(self, at, **kw):
        entry = {"v": 1, "at": iso(at).replace("Z", ".000Z"), "replica": "homestead-abc", "via": "browser",
                 "user": "alice", "role": "admin", "addr": "192.0.2.15", "access": "alice@example.com",
                 "method": "POST", "path": "/api/restart", "target": {"ns": "apps", "name": "game"}, "status": 200, "ms": 12}
        entry.update(kw)
        return json.dumps(entry)

    def write(self, lines, name=None):
        import homestead_audit as AUDIT
        with open(os.path.join(self.tmp.name, name or AUDIT.FILE), "a", encoding="utf-8") as handle:
            handle.write("".join(l + "\n" for l in lines))

    def audit(self, role="admin", since=T0 - 86400):
        out = T.timeline("alice", role, since=since, now=T0)
        return [e for e in out["entries"] if e["kind"] == "action"], [u for u in out["unread"] if u["source"] == "audit"]

    def test_audit_for_admins_only(self):
        self.write([self.line(T0 - 60)])
        for role in ("viewer", "operator"):
            entries, unread = self.audit(role)
            self.assertEqual([], entries)
            self.assertEqual([{"source": "audit", "label": "Audit log", "reason": "shown to admins only"}], unread)
        entries, unread = self.audit()
        self.assertEqual(([("alice POST /api/restart", "apps/game", "info", T0 - 60)], []),
                         ([(e["text"], e["app"], e["tone"], e["at"]) for e in entries], unread))

    def test_audit_line_without_address_email_or_key(self):
        self.write([self.line(T0 - 60, via="key", key="k-0123456789", owner="alice"),
                    self.line(T0 - 30, via="mcp", tool="container_restart", status=500)])
        entries, _ = self.audit()
        text = json.dumps(entries)
        for secret in ("192.0.2.15", "example.com", "k-0123456789", "homestead-abc"):
            self.assertNotIn(secret, text)
        self.assertEqual(["alice's AI agent: container_restart failed (500)", "alice POST /api/restart (API key)"],
                         [e["text"] for e in entries])
        self.assertEqual("bad", entries[0]["tone"])

    def test_refused_requests_are_one_entry(self):
        self.write([self.line(T0 - 300, status=403), self.line(T0 - 200, status=401, user=""),
                    self.line(T0 - 100, status=200, outcome="refused", via="mcp", tool="container_restart")])
        entries, _ = self.audit()
        self.assertEqual([("3 requests were refused", "warn", T0 - 100)], [(e["text"], e["tone"], e["at"]) for e in entries])

    def test_older_file_is_read_after_the_newer_one(self):
        import homestead_audit as AUDIT
        self.write([self.line(T0 - 500, path="/api/old")], AUDIT.FILE + ".1")
        self.write([self.line(T0 - 50, path="/api/new")])
        entries, _ = self.audit()
        self.assertEqual(["alice POST /api/new", "alice POST /api/old"], [e["text"] for e in entries])
        entries, _ = self.audit(since=T0 - 100)
        self.assertEqual(["alice POST /api/new"], [e["text"] for e in entries])

    def test_audit_read_is_bounded_and_says_so(self):
        with mock.patch.object(T, "AUDIT_READ", 4096):
            self.write([self.line(T0 - 10000 + i) for i in range(200)])
            entries, unread = self.audit()
        self.assertLess(len(entries), 200)
        self.assertTrue(entries)
        self.assertEqual(["the newest 4 KiB of the audit log were read; older lines are not shown"],
                         [u["reason"] for u in unread])

    def test_cut_or_garbled_lines_are_skipped(self):
        self.write(["not json", '{"at": 5}', self.line(T0 - 60), self.line(T0 - 30)[:40]])
        entries, unread = self.audit()
        self.assertEqual(["alice POST /api/restart"], [e["text"] for e in entries])
        self.assertEqual([], unread)

    def test_no_audit_file_means_nothing_yet_not_unread(self):
        entries, unread = self.audit()
        self.assertEqual(([], []), (entries, unread))

    def test_an_unreadable_audit_file_is_named(self):
        import homestead_audit as AUDIT
        os.mkdir(os.path.join(self.tmp.name, AUDIT.FILE))      # a folder where the file should be
        entries, unread = self.audit()
        self.assertEqual(1, len(unread))
        self.assertIn("could not read", unread[0]["reason"])


class HostMaintenanceSinceTests(unittest.TestCase):
    def test_started_or_ended_after_since_and_bad_rows_left_out(self):
        import homestead_history as HISTORY
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(HISTORY, "DATA_DIR", folder):
            with open(os.path.join(folder, HISTORY.FILE), "w", encoding="utf-8") as handle:
                json.dump({"maintenance": [
                    {"id": "a", "job": "a", "node": "node-1", "reason": "OS update", "start": T0 - 900, "end": T0 - 100},
                    {"id": "b", "job": "b", "node": "node-2", "reason": "host reboot", "start": T0 - 5000, "end": T0 - 10},
                    {"id": "c", "job": "c", "node": "node-3", "reason": "host reboot", "start": T0 - 5000, "end": T0 - 4000},
                    {"id": "d", "job": "d", "node": "node-4", "reason": "OS update", "start": T0 - 50, "end": None},
                    {"id": "e", "node": "node-5", "start": T0}]}, handle)
            self.assertEqual(["b", "a", "d"], [e["job"] for e in HISTORY.maintenance_since(T0 - 1000)])


if __name__ == "__main__":
    unittest.main()
