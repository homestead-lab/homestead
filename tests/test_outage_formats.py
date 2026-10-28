"""#741 part 1: the outage webhook speaks generic JSON, ntfy or Gotify, and
Send a test is safe to offer: admin-only, rate-limited, no redirect to another
host, and a token in the address never reaches the browser, a log or an error."""
import contextlib
import http.server
import io
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_names as NAMES
import homestead_outage as O

SECRET = "AbCdEf123SECRET"
ITEM = {"kind": "app", "ns": "lab", "name": "frigate"}


class Fake:
    """A small HTTP server on loopback that keeps what it was sent, and
    answers with status (and Location) as told."""

    def __init__(self, status=200, location=None):
        self.got = []
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                size = int(self.headers.get("Content-Length") or 0)
                fake.got.append({"path": self.path, "headers": dict(self.headers), "body": self.rfile.read(size)})
                # A redirect answers everything but where it points.
                redirect = location and self.path != location
                self.send_response(status if redirect or not location else 200)
                if redirect:
                    self.send_header("Location", location)
                self.send_header("Content-Length", "0")
                self.end_headers()

            do_GET = do_POST

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def url(self, path="/"):
        return f"http://127.0.0.1:{self.port}{path}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class FakeCase(unittest.TestCase):
    """Fakes listen on loopback, which a webhook may never reach: here a
    name of example.com resolves to the fake and counts as a public address."""

    def setUp(self):
        self.resolved = []
        resolve = mock.patch.object(O, "_resolve", side_effect=lambda host, port: self.resolved.append(host) or ["127.0.0.1"])
        kind = mock.patch.object(O, "_kind", return_value="public")
        resolve.start(), kind.start()
        self.addCleanup(resolve.stop)
        self.addCleanup(kind.stop)

    def fake(self, **kw):
        f = Fake(**kw)
        self.addCleanup(f.close)
        return f

    def named(self, fake, path):
        return f"http://hooks.example.com:{fake.port}{path}"


class CleanTests(unittest.TestCase):
    def test_generic_is_the_default(self):
        # Stored even when generic: a save since #741 is told from one before it (test_outage_carry).
        self.assertEqual({"webhook": "https://h.example.com/x", "format": "generic"}, O.clean({"webhook": "https://h.example.com/x"}))

    def test_ntfy_and_gotify_are_kept(self):
        for fmt in ("ntfy", "gotify"):
            with self.subTest(fmt=fmt):
                self.assertEqual({"webhook": "https://h.example.com/x", "format": fmt},
                                 O.clean({"webhook": "https://h.example.com/x", "format": fmt}))

    def test_an_unknown_format_is_refused(self):
        with self.assertRaises(ValueError):
            O.clean({"webhook": "https://h.example.com/x", "format": "telegram"})

    def test_a_format_without_a_webhook_is_dropped(self):
        self.assertIsNone(O.clean({"format": "ntfy"}))

    def test_the_existing_address_check_stays(self):
        for bad in ("ftp://h.example.com", "https://a b", "https://" + "a" * 500, "javascript:alert(1)"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                O.clean({"webhook": bad, "format": "ntfy"})


class RequestTests(unittest.TestCase):
    def body(self, event):
        return O.payload({**ITEM}, event, {"state": event, "last": {"error": "HTTP 502"}}, now=10)

    def test_generic_is_todays_json(self):
        url, data, headers = O.request_for("https://h.example.com/x", self.body("down"), "generic")
        self.assertEqual("https://h.example.com/x", url)
        self.assertEqual("frigate is down: HTTP 502", json.loads(data)["text"])
        self.assertEqual("application/json", headers["Content-Type"])

    def test_ntfy_is_plain_text_with_title_priority_and_tag(self):
        _, data, headers = O.request_for("https://ntfy.example.com/homestead", self.body("down"), "ntfy")
        self.assertEqual(b"frigate is down: HTTP 502", data)
        self.assertTrue(headers["Content-Type"].startswith("text/plain"))
        self.assertEqual(("Homestead", "high"), (headers["Title"], headers["Priority"]))
        self.assertTrue(headers["Tags"])
        _, _, back = O.request_for("https://ntfy.example.com/homestead", self.body("up"), "ntfy")
        self.assertEqual("default", back["Priority"])

    def test_gotify_has_title_message_and_priority(self):
        _, data, headers = O.request_for("https://gotify.example.com/message?token=x", self.body("down"), "gotify")
        sent = json.loads(data)
        self.assertEqual(("Homestead", "frigate is down: HTTP 502"), (sent["title"], sent["message"]))
        self.assertEqual("application/json", headers["Content-Type"])
        _, up, _ = O.request_for("https://gotify.example.com/message?token=x", self.body("up"), "gotify")
        self.assertGreater(sent["priority"], json.loads(up)["priority"])


class FakeServerTests(FakeCase):
    def test_each_format_reaches_a_server_as_it_expects(self):
        body = O.payload(dict(ITEM), "test")
        for fmt, check in (("generic", lambda got: json.loads(got["body"])["event"] == "test"),
                           ("ntfy", lambda got: got["body"].startswith(b"A test from Homestead") and got["headers"]["Title"] == "Homestead"),
                           ("gotify", lambda got: json.loads(got["body"])["title"] == "Homestead")):
            with self.subTest(fmt=fmt):
                fake = self.fake()
                O.post(self.named(fake, f"/message?token={SECRET}"), body, fmt)
                self.assertEqual(1, len(fake.got))
                self.assertTrue(check(fake.got[0]))
                self.assertIn(SECRET, fake.got[0]["path"], "the server itself is sent its token")

    def test_the_connection_goes_to_the_address_checked(self):
        fake = self.fake()
        O.post(self.named(fake, "/x"), O.payload(dict(ITEM), "test"), "generic")
        self.assertEqual(["hooks.example.com"], self.resolved, "resolved once, then that address is used")
        self.assertEqual(f"hooks.example.com:{fake.port}", fake.got[0]["headers"]["Host"])

    def test_a_password_before_the_host_is_sent_as_basic_auth(self):
        fake = self.fake()
        O.post(f"http://user:{SECRET}@hooks.example.com:{fake.port}/x", O.payload(dict(ITEM), "test"), "ntfy")
        self.assertTrue(fake.got[0]["headers"]["Authorization"].startswith("Basic "))

    def test_a_redirect_to_another_host_is_not_followed(self):
        elsewhere = self.fake()
        hop = self.fake(status=302, location=elsewhere.url(f"/steal?token={SECRET}"))
        with self.assertRaises(OSError) as caught:
            O.post(self.named(hop, f"/x?token={SECRET}"), O.payload(dict(ITEM), "test"), "generic")
        self.assertEqual([], elsewhere.got, "nothing reaches the other host")
        self.assertNotIn(SECRET, str(caught.exception))

    def test_no_redirect_is_followed_even_on_the_same_host(self):
        same = self.fake(status=302, location="/landed")
        with self.assertRaises(OSError):
            O.post(self.named(same, "/x"), O.payload(dict(ITEM), "test"), "generic")
        self.assertEqual(["/x"], [g["path"] for g in same.got])


class EveryAddressTests(FakeCase):
    """#931: a host with more than one address (IPv6 and IPv4, say) is called
    on the next checked address when one cannot be reached."""

    def setUp(self):
        super().setUp()
        self.tried = []
        real = O.socket.create_connection

        def connect(address, timeout=None, *args, **kw):
            self.tried.append((address[0], timeout))
            if address[0] != "127.0.0.1":
                raise OSError(f"{address[0]} is unreachable")
            return real(address, timeout, *args, **kw)

        patch = mock.patch.object(O.socket, "create_connection", side_effect=connect)
        patch.start()
        self.addCleanup(patch.stop)

    def addresses(self, ips):
        O._resolve.side_effect = lambda host, port: self.resolved.append(host) or list(ips)

    def test_the_next_address_is_called_when_the_first_cannot_be_reached(self):
        fake = self.fake()
        self.addresses(["2001:db8::5", "192.0.2.7", "127.0.0.1"])
        O.post(self.named(fake, f"/x?token={SECRET}"), O.payload(dict(ITEM), "test"), "generic")
        self.assertEqual(["2001:db8::5", "192.0.2.7", "127.0.0.1"], [ip for ip, _ in self.tried])
        self.assertEqual(1, len(fake.got), "sent once, to the address that answered")
        self.assertEqual(["hooks.example.com"], self.resolved, "still resolved once")

    def test_the_tries_share_the_timeout(self):
        # Addresses that never answer each use all the time they are given.
        now, tried = [0.0], []

        def blackhole(address, timeout):
            tried.append(timeout)
            now[0] += timeout
            raise TimeoutError("timed out")

        with mock.patch.object(O.socket, "create_connection", side_effect=blackhole), self.assertRaises(OSError):
            O._connect(["2001:db8::5", "192.0.2.7", "198.51.100.7"], 443, 9, clock=lambda: now[0])
        self.assertEqual([3.0, 3.0, 3.0], [round(t, 6) for t in tried])
        self.assertLessEqual(now[0], 9)

    def test_a_quick_refusal_leaves_the_rest_for_the_others(self):
        now, tried = [0.0], []

        def refuse(address, timeout):
            tried.append(timeout)
            raise ConnectionRefusedError("refused")

        with mock.patch.object(O.socket, "create_connection", side_effect=refuse), self.assertRaises(OSError):
            O._connect(["2001:db8::5", "192.0.2.7"], 443, 10, clock=lambda: now[0])
        self.assertEqual([5.0, 10.0], tried)

    def test_when_none_can_be_reached_it_says_so_without_the_token(self):
        self.addresses(["2001:db8::5", "192.0.2.7"])
        with self.assertRaises(OSError) as caught:
            O.post(f"https://hooks.example.com/x?token={SECRET}", O.payload(dict(ITEM), "test"), "generic")
        self.assertEqual(["2001:db8::5", "192.0.2.7"], [ip for ip, _ in self.tried])
        self.assertIn("unreachable", str(caught.exception))
        self.assertNotIn(SECRET, str(caught.exception))

    def test_an_address_that_answered_is_never_called_twice(self):
        fake = self.fake(status=500)
        self.addresses(["127.0.0.1", "192.0.2.7"])
        with self.assertRaises(OSError):
            O.post(self.named(fake, "/x"), O.payload(dict(ITEM), "test"), "generic")
        self.assertEqual(["127.0.0.1"], [ip for ip, _ in self.tried])
        self.assertEqual(1, len(fake.got))

    def test_every_address_is_still_checked_before_any_is_called(self):
        self.addresses(["192.0.2.7", "127.0.0.1"])
        O._kind.side_effect = lambda ip: "never" if ip == "127.0.0.1" else "public"
        with self.assertRaises(O.Refused):
            O.post("https://hooks.example.com/x", O.payload(dict(ITEM), "test"), "generic")
        self.assertEqual([], self.tried)


class DestinationTests(unittest.TestCase):
    def check(self, ips, allow_lan=False, url="https://hooks.example.com/x"):
        with mock.patch.object(O, "_resolve", return_value=ips):
            return O.destination(url, allow_lan)

    def test_a_public_address_is_used_as_resolved(self):
        self.assertEqual(("93.184.215.14", 443), self.check(["93.184.215.14"]))

    def test_loopback_link_local_and_metadata_are_never_reached(self):
        for ip in ("127.0.0.1", "::1", "169.254.169.254", "fe80::1", "::ffff:127.0.0.1", "0.0.0.0",
                   "fd00:ec2::254", "224.0.0.1"):
            with self.subTest(ip=ip), self.assertRaises(O.Refused):
                self.check([ip], allow_lan=True)

    def test_a_lan_or_cluster_address_needs_an_admins_allowance(self):
        for ip in ("10.20.30.40", "10.43.0.10", "172.16.0.5", "100.64.0.1", "fd12:3456::1"):
            with self.subTest(ip=ip):
                with self.assertRaises(O.Refused):
                    self.check([ip])
                self.assertEqual(ip, self.check([ip], allow_lan=True)[0])

    def test_ipv6_translation_prefixes_are_checked_as_the_ipv4_inside(self):
        # NAT64 (64:ff9b::/96, 64:ff9b:1::/48) and 6to4 (2002::/16) carry an IPv4 address.
        for ip in ("64:ff9b::7f00:1", "64:ff9b::a9fe:a9fe", "2002:7f00:1::1", "2002:a9fe:a9fe::1",
                   "64:ff9b:1::7f00:1", "64:ff9b:1::a9fe:a9fe", "64:ff9b:1::a14:1e28"):
            with self.subTest(ip=ip), self.assertRaises(O.Refused):
                self.check([ip], allow_lan=True)
        for ip in ("64:ff9b::a14:1e28", "64:ff9b::a00:5", "2002:ac10:5::1", "2002:a00:5::1"):
            with self.subTest(ip=ip):
                with self.assertRaises(O.Refused):
                    self.check([ip])
                self.assertEqual(ip, self.check([ip], allow_lan=True)[0])
        self.assertEqual("public", O._kind("64:ff9b::5db8:d70e"), "a public IPv4 inside stays public")

    def test_every_address_a_name_resolves_to_is_checked(self):
        with self.assertRaises(O.Refused):
            self.check(["93.184.215.14", "127.0.0.1"], allow_lan=True)
        with self.assertRaises(O.Refused):
            self.check(["93.184.215.14", "10.0.0.5"])

    def test_a_name_that_does_not_resolve_is_said(self):
        with mock.patch.object(O, "_resolve", side_effect=OSError("no such name")), self.assertRaises(OSError):
            O.destination("https://nowhere.example.com/x")


class SecretTests(unittest.TestCase):
    STORED = {"webhook": f"https://gotify.example.com/message?token={SECRET}", "format": "gotify"}

    def annotations(self, actions=None):
        return {NAMES.key(O.KEY): json.dumps(actions or self.STORED)}

    LEAKY = ({"webhook": f"https://api.example.com/bot123456:{SECRET}/sendMessage?chat_id=42"},   # Telegram-style
             {"webhook": f"https://h.example.com/x?a=1&whatever={SECRET}&b=2"},                    # an unknown query field
             {"webhook": f"https://h.example.com/x#{SECRET}"},                                     # the fragment
             {"webhook": f"https://user:{SECRET}@h.example.com/x"},                                # userinfo
             {"webhook": f"https://{SECRET}@h.example.com/"},
             {"webhook": f"https://ntfy.example.com/{SECRET}", "format": "ntfy"},
             {"webhook": f"https://gotify.example.com/message?token={SECRET}", "format": "gotify"},
             {"webhook": f"https://hooks.example.com/api/webhooks/123/{SECRET}"})

    def test_only_scheme_host_and_port_are_ever_shown(self):
        for actions in self.LEAKY:
            with self.subTest(actions=actions):
                shown = O.read(self.annotations(actions), NAMES)
                self.assertNotIn(SECRET, json.dumps(shown))
                parsed = urllib.parse.urlsplit(actions["webhook"])
                self.assertEqual(f"{parsed.scheme}://{parsed.hostname}/{O.HIDDEN}", shown["webhook"])
        self.assertEqual("http://ntfy.example.com:8080/(hidden)", O.hide("http://ntfy.example.com:8080/alerts"))
        self.assertEqual("https://hooks.example.com", O.hide("https://hooks.example.com"), "nothing after the host: nothing hidden")

    def test_a_format_change_never_shows_a_hidden_part(self):
        for before in self.LEAKY:
            shown = O.read(self.annotations(before), NAMES)
            for fmt in O.FORMATS:
                with self.subTest(before=before, fmt=fmt):
                    after = O.clean({"webhook": shown["webhook"], "format": fmt}, before=before)
                    self.assertEqual(before["webhook"], after["webhook"], "the stored address is kept")
                    self.assertNotIn(SECRET, json.dumps(O.public(after)))

    def test_saving_the_hidden_address_keeps_the_stored_token(self):
        shown = O.read(self.annotations(), NAMES)["webhook"]
        self.assertEqual(self.STORED["webhook"], O.restore(shown, self.STORED["webhook"]))

    def test_the_hidden_parts_never_follow_a_changed_host_or_scheme(self):
        shown = O.read(self.annotations(), NAMES)["webhook"]
        for moved in (shown.replace("gotify.example.com", "elsewhere.example.com"), shown.replace("https:", "http:"),
                      shown.replace("example.com/", "example.com:8443/")):
            with self.subTest(moved=moved), self.assertRaises(ValueError):
                O.restore(moved, self.STORED["webhook"])
        with self.assertRaises(ValueError):
            O.restore(shown, None)

    def test_errors_are_scrubbed(self):
        for url in [a["webhook"] for a in self.LEAKY]:
            with self.subTest(url=url):
                text = O.scrub(f"<urlopen error {url}> {SECRET} {urllib.parse.quote(SECRET)}", url)
                self.assertNotIn(SECRET, text)


class RouteBase(FakeCase):
    def setUp(self):
        super().setUp()
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.stored, self.secrets = {}, {}
        O.bind(self.dir.name, kget=self.kget, ksend=self.ksend, dns_name=lambda value, what: value,
               guard=lambda ns, name: None)
        self.sent = []
        O._tests.clear()

    def kget(self, path):
        """The app with its annotations; the Secrets its webhook address is kept in (#931)."""
        if "/secrets/" in path:
            if path not in self.secrets:
                raise urllib.error.HTTPError(path, 404, "missing", {}, None)
            return json.loads(json.dumps(self.secrets[path]))
        return {"metadata": {"annotations": dict(self.stored)}}

    def ksend(self, method, path, body=None, **options):
        self.sent.append((method, path, body))
        if "/secrets" in path:
            if method == "DELETE":
                self.secrets.pop(path, None)
            else:
                self.secrets[f"{path}/{body['metadata']['name']}" if method == "POST" else path] = body

    def keep(self, actions):
        self.stored = {NAMES.key(O.KEY): json.dumps(actions)}

    def kept(self):
        """The whole address saved, read back as the leader reads it."""
        return O._stored_actions("app", "lab", "frigate").get("webhook")

    def saved(self):
        return json.loads(self.sent[-1][2]["metadata"]["annotations"][NAMES.key(O.KEY)])

    def route(self, method, path):
        return O.ROUTES[(method, path)]

    def send_later(self, shown):
        done = threading.Event()
        with mock.patch.object(O.LEADER_STATE, "put", side_effect=lambda *a: done.set()):
            O._send_later(shown["webhook"], O.payload(dict(ITEM), "down"), shown)
            self.assertTrue(done.wait(10))
        return O._last_webhook["app:lab/frigate"]


class RouteTests(RouteBase):
    def test_send_a_test_is_admin_only(self):
        self.assertEqual("admin", self.route("POST", "/api/monitoring/actions/test")[0])

    def test_send_a_test_is_rate_limited(self):
        fake = self.fake()
        role, handler = self.route("POST", "/api/monitoring/actions/test")
        request = lambda: handler(O.ROUTER.Request("POST", "/api/monitoring/actions/test", {},
                                                   {**ITEM, "webhook": self.named(fake, "/x"), "format": "ntfy"}, "ada", role))
        answers = [request() for _ in range(O.TEST_LIMIT + 1)]
        self.assertTrue(all(isinstance(a, dict) and a["ok"] for a in answers[:-1]))
        self.assertIsInstance(answers[-1], O.ROUTER.Reply)
        self.assertEqual(429, answers[-1].code)
        self.assertEqual(O.TEST_LIMIT, len(fake.got))

    def test_a_test_of_the_hidden_address_uses_the_stored_token_and_says_none(self):
        fake = self.fake(status=401)
        self.keep({"webhook": self.named(fake, f"/message?token={SECRET}"), "format": "gotify"})
        shown = O.read(self.stored, NAMES)["webhook"]
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            answer = O.test_webhook({**ITEM, "webhook": shown, "format": "gotify"})
        self.assertIn(SECRET, fake.got[0]["path"], "the stored token went to its own server")
        self.assertFalse(answer["ok"])
        self.assertNotIn(SECRET, json.dumps(answer))
        self.assertNotIn(SECRET, out.getvalue())

    def test_saving_keeps_the_token_and_never_stores_the_mask(self):
        real = f"https://gotify.example.com/message?token={SECRET}"
        self.keep({"webhook": real, "format": "gotify"})
        shown = O.read(self.stored, NAMES)["webhook"]
        answer = O.set_actions({**ITEM, "actions": {"webhook": shown, "format": "gotify"}})
        self.assertNotIn(SECRET, json.dumps(self.saved()), "kept in its Secret, not on the annotation (#931)")
        self.keep(self.saved())
        self.assertEqual(real, self.kept())
        self.assertNotIn(SECRET, json.dumps(answer), "the answer to the browser hides it too")

    def test_the_leader_sends_with_the_stored_token_and_keeps_no_token_in_its_record(self):
        fake = self.fake(status=500)
        self.keep({"webhook": self.named(fake, f"/message?token={SECRET}"), "format": "ntfy"})
        shown = O.read(self.stored, NAMES)
        record = self.send_later(shown)
        self.assertIn(SECRET, fake.got[0]["path"])
        self.assertTrue(fake.got[0]["body"].startswith(b"frigate is down"))
        self.assertFalse(record["ok"])
        self.assertNotIn(SECRET, json.dumps(record))


class LanTests(RouteBase):
    """A LAN address (a self-hosted ntfy or Gotify) is allowed only by an admin."""

    def setUp(self):
        super().setUp()
        self._kind_now = "lan"
        O._kind.side_effect = lambda ip: self._kind_now
        O._kind.return_value = None

    def test_an_operator_cannot_save_a_lan_address(self):
        with self.assertRaises(ValueError) as caught:
            O.set_actions({**ITEM, "actions": {"webhook": "http://ntfy.example.com/alerts", "format": "ntfy"}}, "operator")
        self.assertIn("admin", str(caught.exception))
        with self.assertRaises(PermissionError):
            O.set_actions({**ITEM, "actions": {"webhook": "http://ntfy.example.com/alerts", "lan": True}}, "operator")
        self.assertEqual([], self.sent)

    def test_an_admin_allows_it_and_an_operator_keeps_it_unchanged(self):
        O.set_actions({**ITEM, "actions": {"webhook": "http://ntfy.example.com/alerts", "format": "ntfy", "lan": True}}, "admin")
        self.assertEqual({"webhook": "http://ntfy.example.com/(hidden)", "format": "ntfy", "lan": True,
                          "secret": "homestead-outage-app-frigate"}, self.saved())
        self.keep(self.saved())
        self.assertEqual("http://ntfy.example.com/alerts", self.kept())
        shown = O.read(self.stored, NAMES)
        O.set_actions({**ITEM, "actions": {**shown, "restart": {"after_min": 5, "max": 2}}}, "operator")
        self.assertTrue(self.saved()["lan"], "an operator's other edit keeps the admin's allowance")
        with self.assertRaises(PermissionError):
            O.set_actions({**ITEM, "actions": {"webhook": "http://other.example.com/x", "lan": True}}, "operator")

    def test_the_route_passes_the_role(self):
        role, handler = self.route("POST", "/api/monitoring/actions")
        self.assertEqual("operator", role)
        with self.assertRaises(PermissionError):
            handler(O.ROUTER.Request("POST", "/api/monitoring/actions", {},
                                     {**ITEM, "actions": {"webhook": "http://ntfy.example.com/a", "lan": True}}, "ada", "operator"))

    def test_loopback_is_refused_even_for_an_admin(self):
        self._kind_now = "never"
        with self.assertRaises(ValueError):
            O.set_actions({**ITEM, "actions": {"webhook": "http://ntfy.example.com/a", "lan": True}}, "admin")

    def test_the_leader_does_not_send_to_a_lan_address_nobody_allowed(self):
        fake = self.fake()
        self.keep({"webhook": self.named(fake, "/alerts"), "format": "ntfy"})
        record = self.send_later(O.read(self.stored, NAMES))
        self.assertEqual([], fake.got)
        self.assertFalse(record["ok"])
        self.keep({"webhook": self.named(fake, "/alerts"), "format": "ntfy", "lan": True})
        self.assertTrue(self.send_later(O.read(self.stored, NAMES))["ok"])

    def test_an_admins_test_may_reach_the_lan(self):
        fake = self.fake()
        self.assertTrue(O.test_webhook({**ITEM, "webhook": self.named(fake, "/alerts"), "format": "ntfy"})["ok"])


class ReaderTests(unittest.TestCase):
    """Every reader of the annotation hides its tokens; only the admin reveal shows them."""
    REAL = {"webhook": f"https://gotify.example.com/message?token={SECRET}", "format": "gotify"}

    def setUp(self):
        import homestead_resources as R
        self.R = R
        self.obj = {"apiVersion": "apps/v1", "kind": "Deployment",
                    "metadata": {"name": "frigate", "namespace": "lab", "resourceVersion": "7",
                                 "annotations": {NAMES.key(O.KEY): json.dumps(self.REAL)}}}
        self.put = []
        kind = {"kind": "Deployment", "group": "apps", "version": "v1", "resource": "deployments", "namespaced": True}
        patch = mock.patch.object(R, "_kind", return_value=kind)
        patch.start()
        self.addCleanup(patch.stop)
        old = (R.kget, R.ksend, R.table_get)
        self.addCleanup(lambda: setattr(R, "kget", old[0]) or setattr(R, "ksend", old[1]) or setattr(R, "table_get", old[2]))
        R.kget = lambda path: json.loads(json.dumps(self.obj))
        R.ksend = lambda method, path, body=None, **k: self.put.append(body) or body

    def test_the_resources_page_hides_it(self):
        for actions in SecretTests.LEAKY:
            with self.subTest(actions=actions):
                self.obj["metadata"]["annotations"][NAMES.key(O.KEY)] = json.dumps(actions)
                self.assertNotIn(SECRET, json.dumps(self.R.get_object("apps", "v1", "deployments", "lab", "frigate")))
        self.obj["metadata"]["annotations"][NAMES.key(O.KEY)] = json.dumps(self.REAL)
        self.assertIn(SECRET, json.dumps(self.R.get_object("apps", "v1", "deployments", "lab", "frigate", reveal=True)))

    def test_saving_the_hidden_yaml_keeps_the_token(self):
        text = self.R.get_object("apps", "v1", "deployments", "lab", "frigate")["yaml"]
        self.R.save_object("apps", "v1", "deployments", "lab", "frigate", text)
        self.assertEqual(self.REAL, json.loads(self.put[-1]["metadata"]["annotations"][NAMES.key(O.KEY)]))

    def test_a_hidden_token_never_moves_to_another_host_through_the_yaml(self):
        text = self.R.get_object("apps", "v1", "deployments", "lab", "frigate")["yaml"].replace("gotify.example.com", "evil.example.com")
        with self.assertRaises(ValueError):
            self.R.save_object("apps", "v1", "deployments", "lab", "frigate", text)
        self.assertEqual([], self.put)

    def test_the_app_and_vm_lists_hide_it(self):
        import homestead_vm_list
        import homestead_workloads
        for module in (homestead_workloads, homestead_vm_list):
            self.assertIn("OUTAGE.read(annotations, NAMES)", Path(module.__file__).read_text(), module.__name__)
        for actions in SecretTests.LEAKY:
            self.assertNotIn(SECRET, json.dumps(O.read({NAMES.key(O.KEY): json.dumps(actions)}, NAMES)))


if __name__ == "__main__":
    unittest.main()
