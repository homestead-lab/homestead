"""The audit log in one list (#738, part 2).

audit.jsonl (every change request, part 1) and agent-audit.jsonl (every call an
API key makes) stay two files, as decided on #817; an admin reads them together,
newest first, from GET /api/audit/log.
"""
import datetime
import json
import os
import sys
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
import homestead_agent_audit as AGENT
import homestead_audit as CHANGES
import homestead_audit_view as VIEW
import homestead_route_policy as POLICY
import homestead_routes as ROUTES
import homestead_shared as SHARED

T0 = 1_790_000_000      # a fixed moment; rows are offsets from it


def iso(seconds):
    at = datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc)
    return at.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def change(offset, user="ada", status=200, path="/api/workload/scale", **extra):
    row = {"v": 1, "at": iso(T0 + offset), "replica": "hs-1", "via": "browser", "user": user, "role": "admin",
           "addr": "192.0.2.10", "method": "POST", "path": path, "status": status, "ms": 12}
    row.update(extra)
    return row


def call(offset, owner="ada", outcome="ok", action="list_containers", **extra):
    row = {"at": T0 + offset, "key": "5b72d9e0c314", "name": "Claude Code", "owner": owner, "via": "mcp",
           "action": action, "args": {"password": "(hidden)", "name": "app-1"}, "outcome": outcome,
           "detail": "", "addr": "192.0.2.31"}
    row.update(extra)
    return row


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(mock.patch.object(SHARED, "DIR", self.dir))
        self.enterContext(mock.patch.object(AGENT, "DATA_DIR", self.dir))

    def write(self, name, rows, raw=()):
        with open(os.path.join(self.dir, name), "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row) + "\n")
            for line in raw:
                handle.write(line + "\n")

    def read(self, **query):
        return VIEW.log({k: [str(v)] for k, v in query.items()})


class TheFilesStayApart(Base):
    def test_reading_writes_nothing(self):
        self.write(CHANGES.FILE, [change(1)])
        self.write(AGENT.FILE, [call(2)])
        before = {name: Path(self.dir, name).read_bytes() for name in os.listdir(self.dir)}
        self.read()
        after = {name: Path(self.dir, name).read_bytes() for name in os.listdir(self.dir)}
        self.assertEqual(before, after)


class Merge(Base):
    def test_both_logs_merged_newest_first_each_row_naming_its_log(self):
        self.write(CHANGES.FILE, [change(10), change(30), change(50)])
        self.write(AGENT.FILE, [call(20), call(40), call(60)])
        rows = self.read()["rows"]
        self.assertEqual([60, 50, 40, 30, 20, 10], [round(r["at"] - T0) for r in rows])
        self.assertEqual(["agents", "changes"] * 3, [r["source"] for r in rows])

    def test_the_rotated_change_file_follows_the_current_one(self):
        self.write(CHANGES.FILE + ".1", [change(1), change(2)])
        self.write(CHANGES.FILE, [change(3), change(4)])
        self.write(AGENT.FILE, [call(2.5)])
        rows = self.read()["rows"]
        self.assertEqual([4, 3, 2.5, 2, 1], [round(r["at"] - T0, 1) for r in rows])

    def test_a_change_row_says_who_what_and_the_result(self):
        self.write(CHANGES.FILE, [change(1, target={"ns": "lab", "kind": "Deployment", "name": "app-1"})])
        row = self.read()["rows"][0]
        self.assertEqual({"source": "changes", "who": "ada", "via": "browser", "what": "POST /api/workload/scale",
                          "target": "lab/Deployment/app-1", "result": "ok", "code": 200, "addr": "192.0.2.10"},
                         {k: row[k] for k in ("source", "who", "via", "what", "target", "result", "code", "addr")})

    def test_change_results(self):
        self.write(CHANGES.FILE, [change(1, status=403), change(2, status=500), change(3, status=None),
                                  change(4, status=200, via="mcp", tool="scale_container", outcome="refused"),
                                  change(5, status=503, user=""), change(6, status=503)])
        rows = {round(r["at"] - T0): r for r in self.read()["rows"]}
        self.assertEqual("refused", rows[1]["result"])
        self.assertEqual("error", rows[2]["result"])
        self.assertEqual("unknown", rows[3]["result"])
        self.assertEqual(("refused", "scale_container"), (rows[4]["result"], rows[4]["what"]))
        self.assertEqual("refused", rows[5]["result"])      # a full server shedding load: part 1's rule
        self.assertEqual("error", rows[6]["result"])

    def test_a_key_change_names_its_owner(self):
        self.write(CHANGES.FILE, [change(1, user="", via="key", key="5b72d9e0c314", owner="robin")])
        self.assertEqual("robin", self.read()["rows"][0]["who"])

    def test_a_note_line_from_the_change_log_is_shown_as_a_note(self):
        self.write(CHANGES.FILE, [{"v": 1, "at": iso(T0 + 1), "replica": "hs-1", "event": "audit-suppressed",
                                   "suppressed": 27, "addr": "192.0.2.9", "detail": "27 more refused requests"}])
        row = self.read()["rows"][0]
        self.assertEqual(("note", "audit-suppressed", "27 more refused requests"), (row["result"], row["what"], row["detail"]))

    def test_an_agent_row_says_who_what_and_the_result_without_its_arguments(self):
        self.write(AGENT.FILE, [call(1, outcome="refused", detail="the key lacks containers:manage")])
        row = self.read()["rows"][0]
        self.assertEqual({"source": "agents", "who": "ada", "via": "mcp", "what": "list_containers", "key": "Claude Code",
                          "result": "refused", "detail": "the key lacks containers:manage", "addr": "192.0.2.31"},
                         {k: row[k] for k in ("source", "who", "via", "what", "key", "result", "detail", "addr")})
        self.assertNotIn("args", row)
        self.assertNotIn("hidden", json.dumps(row))

    def test_unreadable_lines_and_missing_files_are_skipped(self):
        self.write(CHANGES.FILE, [change(1)], raw=["{not json", '{"at": "yesterday"}', "[]", '"x"'])
        self.write(AGENT.FILE, [call(2)], raw=['{"at": "x"}', "{broken"])
        self.assertEqual([2, 1], [round(r["at"] - T0) for r in self.read()["rows"]])
        os.remove(os.path.join(self.dir, AGENT.FILE))
        os.remove(os.path.join(self.dir, CHANGES.FILE))
        self.assertEqual([], self.read()["rows"])

    def test_a_time_that_is_not_finite_is_skipped(self):
        """Review M5: NaN would make the answer invalid JSON."""
        self.write(AGENT.FILE, [call(1)], raw=['{"at": NaN, "owner": "x"}', '{"at": Infinity, "owner": "x"}'])
        rows = self.read()["rows"]
        self.assertEqual([1], [round(r["at"] - T0) for r in rows])
        json.dumps(rows, allow_nan=False)

    def test_crlf_lines_and_a_half_written_last_line(self):
        with open(os.path.join(self.dir, CHANGES.FILE), "wb") as handle:
            handle.write((json.dumps(change(1)) + "\r\n" + json.dumps(change(2)) + "\r\n").encode())
            handle.write(json.dumps(change(3)).encode()[:40])
        self.assertEqual([2, 1], [round(r["at"] - T0) for r in self.read()["rows"]])

    def test_a_line_longer_than_a_chunk_is_read_whole(self):
        self.write(CHANGES.FILE, [change(1, path="/api/" + "x" * (VIEW.CHUNK * 2)), change(2)])
        rows = self.read()["rows"]
        self.assertEqual(2, len(rows))
        self.assertTrue(rows[1]["what"].startswith("POST /api/xxx"))


class Filters(Base):
    def setUp(self):
        super().setUp()
        self.write(CHANGES.FILE, [change(1, user="ada"), change(3, user="Robin")])
        self.write(AGENT.FILE, [call(2, owner="ada"), call(4, owner="robin")])

    def test_source(self):
        self.assertEqual(["changes", "changes"], [r["source"] for r in self.read(source="changes")["rows"]])
        self.assertEqual(["agents", "agents"], [r["source"] for r in self.read(source="agents")["rows"]])
        self.assertEqual(4, len(self.read(source="")["rows"]))

    def test_person_matches_either_log_ignoring_case(self):
        rows = self.read(person="ROBIN")["rows"]
        self.assertEqual([(4, "agents"), (3, "changes")], [(round(r["at"] - T0), r["source"]) for r in rows])

    def test_source_and_person_together(self):
        self.assertEqual([2], [round(r["at"] - T0) for r in self.read(source="agents", person="ada")["rows"]])

    def test_an_unknown_source_is_refused(self):
        with self.assertRaises(ValueError):
            self.read(source="signins")


class Limits(Base):
    def setUp(self):
        super().setUp()
        self.write(CHANGES.FILE, [change(i * 2) for i in range(50)])
        self.write(AGENT.FILE, [call(i * 2 + 1) for i in range(50)])

    def test_a_page_and_the_next(self):
        first = self.read(limit=10)
        self.assertEqual(list(range(99, 89, -1)), [round(r["at"] - T0) for r in first["rows"]])
        self.assertTrue(first["more"])
        second = self.read(limit=10, offset=10)
        self.assertEqual(list(range(89, 79, -1)), [round(r["at"] - T0) for r in second["rows"]])
        last = self.read(limit=10, offset=95)
        self.assertEqual([4, 3, 2, 1, 0], [round(r["at"] - T0) for r in last["rows"]])
        self.assertFalse(last["more"])

    def test_the_default_and_the_caps(self):
        self.assertEqual(VIEW.LIMIT, self.read()["limit"])
        self.assertEqual(VIEW.MAX_LIMIT, self.read(limit=10 ** 6)["limit"])
        self.assertEqual(1, self.read(limit=0)["limit"])
        self.assertEqual(VIEW.MAX_OFFSET, self.read(offset=10 ** 9)["offset"])
        self.assertEqual(0, self.read(offset=-5)["offset"])

    def test_a_limit_that_is_not_a_number_is_refused(self):
        with self.assertRaises(ValueError):
            self.read(limit="all")
        with self.assertRaises(ValueError):
            self.read(offset="x")

    def test_a_page_reads_only_the_end_of_a_large_file(self):
        """The newest page of a full 10 MiB file reads a few chunks, not the file."""
        self.write(CHANGES.FILE, [change(i) for i in range(20000)])
        opened = []
        real_open = open

        def counting(path, mode="r", *a, **k):
            handle = real_open(path, mode, *a, **k)
            if "b" in mode:
                original = handle.read
                handle.read = lambda n=-1: opened.append(n) or original(n)
            return handle
        with mock.patch("builtins.open", counting):
            rows = self.read(limit=20)["rows"]
        self.assertEqual(20, len(rows))
        self.assertNotIn(-1, opened, "a whole file was read")
        self.assertLess(sum(opened), 8 * VIEW.CHUNK)

    def test_a_filter_that_matches_little_stops_at_the_scan_cap(self):
        self.write(CHANGES.FILE, [change(i, user="ada") for i in range(300)] + [change(1000, user="robin")])
        with mock.patch.object(VIEW, "SCAN", 100):
            answer = self.read(person="nobody")
        self.assertEqual([], answer["rows"])
        self.assertTrue(answer["partial"])
        self.assertFalse(self.read(person="robin")["partial"])

    def test_when_one_log_hits_the_cap_the_page_ends_there_and_says_so(self):
        """Review I1: rows of the other log older than where the search stopped
        would hide the stopped log's unread rows, so they are not shown."""
        self.write(CHANGES.FILE, [change(500 + i, user="ada") for i in range(100)]
                   + [change(700 + i, user="robin") for i in range(150)])
        self.write(AGENT.FILE, [call(i, owner="ada") for i in range(200)])
        with mock.patch.object(VIEW, "SCAN", 100):
            answer = self.read(person="ada", limit=50)
        self.assertTrue(answer["partial"])
        self.assertFalse(answer["more"])
        self.assertTrue(all(r["source"] == "changes" or r["at"] - T0 >= 600 for r in answer["rows"]),
                        "no agent row from before the change log's search stopped")

    def test_older_stops_at_the_offset_cap(self):
        """Review M1: the last page reachable says there is no more."""
        with mock.patch.object(VIEW, "MAX_OFFSET", 20):
            answer = self.read(limit=10, offset=20)
            self.assertEqual(20, answer["offset"])
            self.assertFalse(answer["more"])
            self.assertTrue(self.read(limit=10, offset=10)["more"])


class AdminsOnly(unittest.TestCase):
    def test_the_route_is_declared_admin_in_a_listed_module(self):
        self.assertIn("homestead_audit_view", ROUTES.MODULES)
        route = ROUTES.find("GET", "/api/audit/log")
        self.assertIsNotNone(route)
        self.assertEqual(("admin", "homestead_audit_view"), (route.role, route.module))
        self.assertEqual("admin", POLICY.role("/api/audit/log", "GET"))


class ThroughTheServer(unittest.TestCase):
    """A real server.H: viewers and operators are refused, an admin reads."""

    @classmethod
    def setUpClass(cls):
        import homestead_http as HTTP
        import server
        cls.server = server
        ROUTES.table()
        cls.listener = HTTP.BoundedHTTPServer(("127.0.0.1", 0), server.H)
        threading.Thread(target=cls.listener.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.listener.shutdown()
        cls.listener.server_close()

    def setUp(self):
        self.dir = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(mock.patch.object(SHARED, "DIR", self.dir))
        self.enterContext(mock.patch.object(AGENT, "DATA_DIR", self.dir))
        self.enterContext(mock.patch.object(self.server.H, "_signin", lambda *a, **k: None))
        with open(os.path.join(self.dir, AGENT.FILE), "w", encoding="utf-8") as handle:
            handle.write(json.dumps(call(1)) + "\n")

    def get(self, role, query="limit=5"):
        who = {"user": "ada", "role": role, "stale": False, "remember": False, "started": None}
        with mock.patch.object(self.server.H, "_who", return_value=who):
            client = HTTPConnection(*self.listener.server_address, timeout=5)
            try:
                client.request("GET", f"/api/audit/log?{query}", headers={"X-Homestead-Auth": "1"})
                response = client.getresponse()
                return response.status, json.loads(response.read() or b"null")
            finally:
                client.close()

    def test_viewer_and_operator_are_refused(self):
        for role in ("viewer", "operator"):
            with self.subTest(role=role):
                status, body = self.get(role)
                self.assertEqual(403, status)
                self.assertNotIn("rows", body or {})

    def test_an_admin_reads_it(self):
        status, body = self.get("admin")
        self.assertEqual(200, status)
        self.assertEqual(["agents"], [r["source"] for r in body["rows"]])

    def test_a_bad_filter_or_limit_is_a_400(self):
        for query in ("source=signins", "limit=all", "offset=x"):
            with self.subTest(query=query):
                self.assertEqual(400, self.get("admin", query)[0])


if __name__ == "__main__":
    unittest.main()
