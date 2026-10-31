"""The audit line (#738): one per change request, saying who made it, how they
came in, what, the target, the status and how long it took - and nothing a
request carried that was secret."""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
import homestead_audit as AUDIT_LOG
import homestead_routes as ROUTES

KEY = {"id": "k1", "name": "ha", "owner": "ada", "scopes": ["apps:control"], "expires": None, "kind": "key"}


def view(method="POST", path="/api/workload/scale", body=None, user="ada", role="operator", addr="192.0.2.10",
         access=None, fleet=None, key=None, headers=()):
    """What PR 1's after_request hands a hook registered with fields=HOOK_FIELDS."""
    conn = ROUTES.Connection(ROUTES.Headers(list(headers)), {}, False, False, access, fleet, key, None)
    return ROUTES.Request(method, path, None, {} if body is None else body, user, role, addr, conn)


class TheLine(unittest.TestCase):
    def setUp(self):
        # The replica field is the pod's HOSTNAME. Pinned, so a random container
        # hostname (hex, so it can hold "abc") never lands in a line a test reads.
        patch = mock.patch.dict("os.environ", {"HOSTNAME": "homestead-0"})
        patch.start()
        self.addCleanup(patch.stop)

    def line(self, request, status=200, ms=142, notes=None):
        return AUDIT_LOG.line(request, status, ms, notes or {}, now=1792574043.512)

    def test_a_browser_line_has_exactly_its_fields(self):
        with mock.patch.dict("os.environ", {"HOSTNAME": "homestead-6d9f-abcde"}):
            line = self.line(view(body={"ns": "lab", "kind": "Deployment", "name": "app-1"}))
        self.assertEqual({"v": 1, "at": "2026-10-21T09:14:03.512Z", "replica": "homestead-6d9f-abcde",
                          "via": "browser", "user": "ada", "role": "operator", "addr": "192.0.2.10",
                          "method": "POST", "path": "/api/workload/scale",
                          "target": {"ns": "lab", "kind": "Deployment", "name": "app-1"},
                          "status": 200, "ms": 142}, line)

    def test_cloudflare_access_adds_the_verified_email_only(self):
        self.assertEqual("ada@example.com", self.line(view(access={"email": "ada@example.com", "sub": "x"}))["access"])
        for bad in ({"email": "a b@example.com"}, {"email": "no-at"}, {"email": 7}, {"email": "a@" + "b" * 260},
                    {}, None):
            with self.subTest(access=bad):
                self.assertNotIn("access", self.line(view(access=bad)))

    def test_read_only_copies_of_claims_and_key_are_read(self):
        """PR 1 hands hooks MappingProxyType copies, not dicts."""
        import types
        line = self.line(view(access=types.MappingProxyType({"email": "ada@example.com"}),
                              key=types.MappingProxyType(KEY), user="api-key:ha", path="/api/v1/x"))
        self.assertEqual(("ada@example.com", "k1", "ada"), (line["access"], line["key"], line["owner"]))

    def test_an_api_key_says_which_key_and_whose(self):
        line = self.line(view(path="/api/v1/apps/restart", user="api-key:ha", key=KEY))
        self.assertEqual(("key", "api-key:ha", "k1", "ada"), (line["via"], line["user"], line["key"], line["owner"]))

    def test_mcp_needs_a_change_tool_note(self):
        request = view(path="/mcp", user="api-key:ha", key=KEY)
        self.assertIsNone(self.line(request))
        self.assertIsNone(self.line(request, notes={"tool": "list_apps", "kind": "read"}))
        line = self.line(request, notes={"tool": "restart_app", "kind": "change"})
        self.assertEqual(("mcp", "restart_app"), (line["via"], line["tool"]))
        self.assertNotIn("kind", line)

    def test_a_linked_homestead_says_where_from(self):
        line = self.line(view(user="fleet:loft:ada", role="operator", fleet="m1"))
        self.assertEqual(("linked", "fleet:loft:ada", "m1"), (line["via"], line["user"], line["from"]))

    def test_from_is_checked_like_the_notes(self):
        self.assertNotIn("from", self.line(view(user="fleet:loft:ada", fleet="a b\nc")))
        self.assertNotIn("from", self.line(view(user="fleet:loft:ada", fleet="m" * 300)))

    def test_a_stranger_is_none(self):
        line = self.line(view(user=None, role=None), status=401)
        self.assertEqual(("none", "", None, 401), (line["via"], line["user"], line["role"], line["status"]))

    def test_no_body_key_but_the_named_four(self):
        text = json.dumps(self.line(view(body={"ns": "lab", "name": "app-1", "password": "Pw-0123456789",
                                               "env": "SECRET=1"})))
        self.assertNotIn("Pw-0123456789", text)
        self.assertNotIn("SECRET", text)
        self.assertNotIn("password", text)

    def test_query_never_and_path_capped_and_one_line(self):
        text = AUDIT_LOG.text(self.line(view(path="/api/a\nb", headers=[("X-Q", "token=abc")])))
        self.assertNotIn("\n", text)
        self.assertEqual("/api/a\nb", json.loads(text)["path"])
        self.assertNotIn("abc", text)
        self.assertEqual(200, len(self.line(view(path="/api/" + "a" * 300))["path"]))

    def test_target_from_a_delete_path_and_bad_names_dropped(self):
        self.assertEqual({"ns": "lab", "name": "app-1"},
                         self.line(view("DELETE", "/api/workload/lab/app-1", body={}))["target"])
        self.assertEqual({"ns": "lab"}, self.line(view(body={"namespace": "lab", "name": "-x"}))["target"])
        for body in ({"name": "a b"}, {"name": "x" * 300}, {"ns": ""}):
            with self.subTest(body=body):
                self.assertNotIn("target", self.line(view(body=body)))

    def test_notes_are_checked_too(self):
        line = self.line(view(), notes={"to": "member-2", "held": "a b"})
        self.assertEqual("member-2", line["to"])
        self.assertNotIn("held", line)

    def test_consoles_carry_their_length(self):
        line = self.line(view("GET", "/api/node/shell", body={}), status=101, ms=65_400)
        self.assertEqual((101, 65), (line["status"], line["session_s"]))


class WhatIsAudited(unittest.TestCase):
    def test_changes_and_consoles(self):
        for method, path in (("POST", "/api/x"), ("PUT", "/api/x"), ("PATCH", "/api/x"), ("DELETE", "/api/x"),
                             ("POST", "/mcp"), ("GET", "/api/console"), ("GET", "/api/node/shell"),
                             ("GET", "/api/vm/console")):
            with self.subTest(method=method, path=path):
                self.assertTrue(AUDIT_LOG.audited(method, path))
        for method, path in (("GET", "/api/overview"), ("POST", "/index.html"), ("GET", "/mcp"),
                             ("POST", "/api/auth/login"), ("POST", "/api/auth/preferences/dashboard"),
                             ("HEAD", "/api/x"), ("", "")):
            with self.subTest(method=method, path=path):
                self.assertFalse(AUDIT_LOG.audited(method, path))

    def test_notes_refuse_unknown_keys(self):
        with self.assertRaises(ValueError):
            AUDIT_LOG.note(password="x")
        ROUTES._clear_notes()


class Recording(unittest.TestCase):
    def setUp(self):
        self.out = []
        self.enterContext(mock.patch.object(AUDIT_LOG, "emit", side_effect=lambda entry, keep=True: self.out.append(entry)))
        AUDIT_LOG._buckets.clear()
        self.addCleanup(AUDIT_LOG._buckets.clear)

    def test_a_read_writes_nothing_and_a_change_one_line(self):
        AUDIT_LOG.record(view("GET", "/api/overview"), 200, 3)
        AUDIT_LOG.record(view(), 200, 3)
        self.assertEqual(["/api/workload/scale"], [line["path"] for line in self.out])

    def test_record_never_raises(self):
        with mock.patch.object(AUDIT_LOG, "emit", side_effect=OSError("disk")):
            self.assertIsNone(AUDIT_LOG.record(view(), 200, 1))
        with mock.patch.object(AUDIT_LOG, "line", side_effect=RuntimeError("bug")):
            self.assertIsNone(AUDIT_LOG.record(view(), 200, 1))


class Throttle(unittest.TestCase):
    """A flood of refused, unauthenticated or no-op requests cannot wash real
    changes out of audit.jsonl's two files: on the data volume they are kept a
    few a minute per address and per user, and summed up after; stdout still
    carries every line, and a signed-in person's successful change is always kept."""

    def setUp(self):
        self.clock = [1000.0]
        self.enterContext(mock.patch.object(AUDIT_LOG.time, "time", side_effect=lambda: self.clock[0]))
        self.enterContext(mock.patch.object(AUDIT_LOG, "_file_on", return_value=True))
        self.stdout, self.kept = [], []
        self.enterContext(mock.patch.object(AUDIT_LOG, "_stdout", side_effect=lambda t: self.stdout.append(json.loads(t))))
        self.enterContext(mock.patch.object(AUDIT_LOG, "_append", side_effect=lambda t: self.kept.append(json.loads(t))))
        AUDIT_LOG._buckets.clear()
        self.addCleanup(AUDIT_LOG._buckets.clear)
        AUDIT_LOG._swept[0] = 0

    def stranger(self, addr="192.0.2.7", status=401, user=None):
        AUDIT_LOG.record(view(user=user, role=None if user is None else "viewer", addr=addr), status, 1)

    def summaries(self):
        return [line for line in self.kept if "suppressed" in line]

    def test_signed_in_successes_are_never_throttled(self):
        for _ in range(500):
            AUDIT_LOG.record(view(), 200, 1)
        self.assertEqual(500, len(self.kept))
        self.assertEqual(500, len(self.stdout))

    def test_refusals_keep_a_few_per_address_then_a_summary(self):
        for _ in range(20):
            self.stranger()
        self.assertEqual(20, len(self.stdout), "stdout carries every line")
        self.assertEqual(AUDIT_LOG.BURST, len(self.kept))
        self.clock[0] += AUDIT_LOG.WINDOW_SECONDS + 1
        self.stranger()
        (summary,) = self.summaries()
        self.assertEqual({"suppressed": 20 - AUDIT_LOG.BURST, "addr": "192.0.2.7", "window_s": AUDIT_LOG.WINDOW_SECONDS},
                         {k: summary[k] for k in ("suppressed", "addr", "window_s")})
        self.assertIn("more refused requests from 192.0.2.7", summary["detail"])
        self.assertEqual(401, self.kept[-1]["status"], "a new minute keeps lines again")

    def test_a_summary_comes_even_when_the_flood_stops(self):
        for _ in range(9):
            self.stranger()
        self.clock[0] += AUDIT_LOG.WINDOW_SECONDS + AUDIT_LOG.SWEEP_SECONDS + 1
        AUDIT_LOG.record(view(), 200, 1)                     # anyone's next change
        self.assertEqual([9 - AUDIT_LOG.BURST], [line["suppressed"] for line in self.summaries()])

    def test_a_signed_in_users_refusals_are_throttled_across_addresses(self):
        for n in range(20):
            self.stranger(addr=f"198.51.100.{n}", status=403, user="node1")
        self.assertEqual(20, len(self.stdout))
        self.assertEqual(AUDIT_LOG.BURST, len([line for line in self.kept if line.get("status") == 403]))

    def test_only_refusals_are_throttled(self):
        """Successes, anonymous ones too (first-admin setup), and 5xx are always kept."""
        for _ in range(20):
            AUDIT_LOG.record(view("POST", "/api/auth/setup", user=None, role=None), 200, 1)
            self.stranger(status=500)
            self.stranger(status=None)
        self.assertEqual(60, len(self.kept))
        for _ in range(20):
            self.stranger(status=409, user="ada")
        self.assertEqual(60 + AUDIT_LOG.BURST, len(self.kept), "a 4xx refusal is throttled")

    def test_a_flood_from_one_address_suppresses_nothing_else(self):
        for _ in range(300):
            self.stranger(addr="192.0.2.7")
        kept_before = len(self.kept)
        self.stranger(addr="192.0.2.8", status=403, user="ada")             # (a) a signed-in refusal
        AUDIT_LOG.record(view("POST", "/api/auth/setup", user=None, role=None, addr="192.0.2.7"), 200, 1)  # (b)
        self.stranger(addr="192.0.2.7", status=500)                          # (c)
        self.stranger(addr="192.0.2.9")                                       # (d) a second address's first 401
        self.assertEqual([403, 200, 500, 401], [line["status"] for line in self.kept[kept_before:]])

    def test_first_refusals_are_capped_overall(self):
        """Review I2: rotating addresses (more than the table holds) cannot each
        claim a "first" line; past FIRST_CAP a minute they are summed up."""
        addresses = [f"198.51.{n // 250}.{n % 250}" for n in range(2000)]
        for _ in range(2):
            for addr in addresses:
                self.stranger(addr=addr)
        self.assertEqual(4000, len(self.stdout), "stdout carries every line")
        self.assertGreaterEqual(len(self.kept), AUDIT_LOG.FIRST_CAP)
        self.assertLessEqual(len(self.kept), AUDIT_LOG.FIRST_CAP + AUDIT_LOG.ALL_BURST)
        self.clock[0] += AUDIT_LOG.WINDOW_SECONDS + AUDIT_LOG.SWEEP_SECONDS + 1
        AUDIT_LOG.record(view(), 200, 1)
        lines = [line for line in self.kept if "suppressed" not in line and line.get("status") == 401]
        self.assertEqual(4000, len(lines) + sum(line["suppressed"] for line in self.summaries()),
                         "the summaries count everything left out")

    def test_an_ipv6_64_is_one_source(self):
        for n in range(1000):
            self.stranger(addr=f"2001:db8:1:2::{n:x}")
        self.assertEqual(AUDIT_LOG.BURST, len(self.kept))
        self.stranger(addr="2001:db8:1:3::1")
        self.assertEqual(AUDIT_LOG.BURST + 1, len(self.kept), "another /64 is another source")

    def test_users_behind_one_address_each_keep_their_first_refusal(self):
        """Review I3: behind a tunnel or ingress everyone has the same address."""
        for _ in range(6):
            self.stranger(addr="192.0.2.50", status=403, user="node1")
        self.stranger(addr="192.0.2.50", status=403, user="ada")
        self.assertEqual("ada", self.kept[-1]["user"])
        self.assertEqual(AUDIT_LOG.BURST + 1, len(self.kept))

    def test_anonymous_503s_are_refusals_but_real_server_errors_are_kept(self):
        """Review I1: a full server's slot refusals are throttled; a 5xx a
        signed-in person or key got, or any other anonymous 5xx, is kept."""
        for _ in range(20):
            self.stranger(status=503)
        self.assertEqual(AUDIT_LOG.BURST, len(self.kept))
        for _ in range(20):
            self.stranger(status=503, user="ada")
            self.stranger(status=500)
        self.assertEqual(AUDIT_LOG.BURST + 40, len(self.kept))

    def test_mcp_refusals_are_refusals_but_tool_errors_are_kept(self):
        """M2: a tool call refused before anything ran answers 200, and is
        throttled like a refusal. A change tool's error happened while it
        acted, so it is kept like a server error, even in a flood of refusals."""
        key = mock.Mock(**{"get.side_effect": {"id": "k1", "owner": "ada"}.get})

        def call(**notes):
            AUDIT_LOG.record(view(path="/mcp", user="api-key:ha", key=key), 200, 1,
                             notes={"tool": "scale_container", "kind": "change", **notes})
        for _ in range(30):
            call(outcome="refused")
        self.assertEqual(AUDIT_LOG.BURST, len(self.kept))
        for _ in range(3):
            call(outcome="error")
        call()
        self.assertEqual(["error", "error", "error", None], [line.get("outcome") for line in self.kept[-4:]])

    def test_the_table_is_bounded_and_an_evicted_flood_is_still_summed_up(self):
        for _ in range(9):
            self.stranger(addr="192.0.2.7")
        for n in range(AUDIT_LOG.KEYS_KEPT + 50):
            self.clock[0] += 0.001
            self.stranger(addr=f"198.51.{n // 250}.{n % 250}")
        self.assertLessEqual(len(AUDIT_LOG._buckets), AUDIT_LOG.KEYS_KEPT)
        self.assertEqual([9 - AUDIT_LOG.BURST],
                         [line["suppressed"] for line in self.summaries() if line.get("addr") == "192.0.2.7"])

    def test_summaries_are_file_only(self):
        for _ in range(9):
            self.stranger()
        self.clock[0] += AUDIT_LOG.WINDOW_SECONDS + 1
        self.stranger()
        self.assertFalse([line for line in self.stdout if "suppressed" in line])


class Drops(unittest.TestCase):
    """When the hooks' queue is full, PR 1 drops a request's hooks and counts
    it; the next line written says how many, on stdout and in the file."""

    def setUp(self):
        self.lines = []
        self.enterContext(mock.patch.object(AUDIT_LOG, "_stdout", side_effect=lambda t: self.lines.append(json.loads(t))))
        self.enterContext(mock.patch.object(AUDIT_LOG, "_file_on", return_value=False))
        self.dropped = self.enterContext(mock.patch.object(ROUTES, "hooks_dropped", return_value=0))
        AUDIT_LOG._drops_seen[0] = 0

    def test_dropped_requests_are_reported_once(self):
        AUDIT_LOG.record(view(), 200, 1)
        self.assertEqual(1, len(self.lines))
        self.dropped.return_value = 7
        AUDIT_LOG.record(view("GET", "/api/overview"), 200, 1)        # even a read reports it
        AUDIT_LOG.record(view(), 200, 1)
        events = [line for line in self.lines if line.get("event") == "audit-lines-dropped"]
        self.assertEqual([7], [line["dropped"] for line in events])
        self.assertIn("pod log", events[0]["detail"])


class Notes(unittest.TestCase):
    """What a request notes is its own: never the next request's on the same thread."""

    def setUp(self):
        self.out = []
        self.enterContext(mock.patch.object(AUDIT_LOG, "emit", side_effect=lambda entry, keep=True: self.out.append(entry)))
        ROUTES._clear_notes()

    def test_notes_never_reach_the_next_request(self):
        """Each request's notes are taken with it, and only its notes."""
        capture = ROUTES._take_notes         # what after_request does on the request's thread
        AUDIT_LOG.note(tool="container_restart", kind="change")
        AUDIT_LOG.record(view("GET", "/api/overview"), 200, 1, notes=capture())    # a read takes them too
        AUDIT_LOG.record(view(), 200, 1, notes=capture())
        self.assertNotIn("tool", self.out[0])
        AUDIT_LOG.note(to="member-2")
        taken = capture()
        with mock.patch.object(AUDIT_LOG, "line", side_effect=RuntimeError("bug")):
            AUDIT_LOG.record(view(), 200, 1, notes=taken)
        AUDIT_LOG.record(view(), 200, 1, notes=capture())
        self.assertNotIn("to", self.out[1])

    def test_a_relayed_request_names_the_cluster(self):
        import homestead_fleet as FLEET
        handler = mock.Mock(headers={}, command="POST", path="/api/workload/scale")
        with mock.patch.object(FLEET, "member", return_value={"id": "member-2", "name": "loft", "url": "https://192.0.2.20"}), \
                mock.patch.object(FLEET, "_connect", side_effect=OSError("no route")):
            with self.assertRaises(FLEET.Unreachable):
                FLEET.forward(handler, "member-2", b"{}", "ada", "operator")
        AUDIT_LOG.record(view(), 502, 1, notes=ROUTES._take_notes())
        self.assertEqual(("member-2", 502), (self.out[0]["to"], self.out[0]["status"]))


class Stdout(unittest.TestCase):
    def test_one_json_line_per_emit(self):
        with mock.patch.object(AUDIT_LOG.sys, "stdout") as out:
            AUDIT_LOG.emit({"v": 1, "path": "/api/é\n"})
        written = "".join(call.args[0] for call in out.write.call_args_list)
        self.assertEqual('{"v":1,"path":"/api/\\u00e9\\n"}\n', written)
        out.flush.assert_called()


if __name__ == "__main__":
    unittest.main()
