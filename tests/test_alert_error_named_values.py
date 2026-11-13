"""An alert's error text hides the value of a field named like a password,
key or token, a login written before an @, a signed token and a long key.

plain_error makes an error safe for notification channels. What it hid
before stays hidden: the new rule only ever turns more of the text into the
hidden marker. Every value below is invented; token-shaped ones are built in
pieces (GitHub's push protection refuses them even when fake)."""
import http.server
import random
import re
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import server  # noqa: F401  (binds homestead_kube)
import homestead_alerts as ALERTS
import homestead_kube as KUBE
import homestead_read_deadline as DEADLINE
import homestead_storage as STORAGE

HIDDEN = "<hidden>"
WIDE = 10 ** 6          # no cut: the whole text is compared

# The rules plain_error applied before this change: pattern, flags and
# replacement. They stay as they are; the new rule runs on their output.
BEFORE = [
    (r"\b[a-z][a-z0-9+.-]*://\S+", re.I, "<address>"),
    (r"\b(host|server|address)=(['\"])[^'\"]*\2", re.I, r"\1=<address>"),
    (r"(['\"])[a-z0-9-]+(?:\.[a-z0-9-]+)+\1", re.I, "<address>"),
    (r"(?<![\w.-])[a-z0-9-]+(?:\.[a-z0-9-]+)+:\d{1,5}\b", re.I, "<address>"),
    (r"\bport=\d+", re.I, "port=<hidden>"),
    (r"\bBearer\s+\S+", re.I, "Bearer <hidden>"),
    (r"\[[0-9a-f:.]*:[0-9a-f:.]*\](?::\d+)?", re.I, "<address>"),
    (r"(?<![\w:])(?:[0-9a-f]{1,4}:){3,7}[0-9a-f]{1,4}(?![\w:])"
     r"|(?<![\w:])(?:[0-9a-f]{1,4}:)+:(?:[0-9a-f]{1,4}(?::[0-9a-f]{1,4})*)?(?![\w:])", re.I, "<address>"),
    (r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b", 0, "<address>"),
    (r"(?<![A-Za-z0-9+_=-])(?=[A-Za-z0-9+_=-]*\d)[A-Za-z0-9+_=-]{20,}", 0, "<hidden>"),
]


def before(raw):
    """What plain_error showed before this change, without the cut."""
    text = " ".join(str(raw or "").split())
    for pattern, flags, word in BEFORE:
        text = re.sub(pattern, word, text, flags=flags)
    return text


def only_more_hidden(old, new):
    """True when new is old with some of its parts replaced by the hidden
    marker, and nothing else changed: every part of new outside a marker is
    a part of old, in order, so nothing old hid can show in new."""
    pieces = new.split(HIDDEN)
    if len(pieces) == 1:
        return old == new
    if not old.startswith(pieces[0]):
        return False
    at = len(pieces[0])
    for piece in pieces[1:-1]:
        found = old.find(piece, at)
        if found < 0:
            return False
        at = found + len(piece)
    return len(old) - len(pieces[-1]) >= at and old.endswith(pieces[-1])


# Invented values, built in pieces.
SIGNED_HEAD = "eyJhbGciOi" + "J9"
SIGNED_BODY = "eyJzdWIi" + "fQ"
SIGNED = SIGNED_HEAD + "." + SIGNED_BODY + "." + "abcDEFghi"
LONG_HEAD = "eyJhbGciOi" + "JSUzI1NiIs" + "InR5cCI6Ik" + "pXVCJ9"
JOIN = "abcdef" + "." + "0123456789" + "abcdef"
SLASHED = "qWeRtYuIoPaSd" + "/K7ZxCvB/" + "nMlKjHgFdSaPoIuYtR"
HOOK = "services/" + "T0AB1CD2E" + "/" + "B0FG3HI4J" + "/" + "aBcDeFgHiJkLmNoPqRsT"
LETTERS = "qWeRtYuIoPaSdFgHjKlZxCvBnMqWeRtY"


class NamedValueTests(unittest.TestCase):
    def hides(self, raw, *secrets, keeps=()):
        text = ALERTS.plain_error(raw, limit=WIDE)
        self.assertIn(HIDDEN, text, raw)
        for secret in secrets:
            self.assertNotIn(secret, text, raw)
        for kept in keeps:
            self.assertIn(kept, text, raw)
        return text

    def test_a_field_named_like_a_password_or_key_hides_its_value(self):
        for name in ("password", "passwd", "pwd", "passphrase", "token", "secret", "client_secret", "secret_key",
                     "api_key", "api-key", "apikey", "access_key", "private_key", "credential", "session", "auth",
                     "Password", "X-Api-Key"):
            for joint in ("=", ": ", ":"):
                with self.subTest(name=name, joint=joint):
                    text = self.hides(f"login refused for {name}{joint}hunter at the gate", "hunter",
                                      keeps=("login refused for", "at the gate"))
                    self.assertIn(name, text)

    def test_a_quoted_field_hides_the_whole_quoted_value(self):
        self.hides('body {"password": "hunter two", "user": "ann"}', "hunter", "two", keeps=('"user": "ann"',))
        self.hides("body {'token': 'abc'} sent", "abc", keeps=("sent",))
        self.hides('body {"secret": "a\\"b c"} sent', "b c", keeps=("sent",))
        self.hides('cut short {"password": "hunter', "hunter")

    def test_a_value_is_hidden_up_to_the_next_space(self):
        self.hides("GET /v1?token=abc&page=2 refused", "abc", "page=2", keeps=("refused",))

    def test_a_cookie_hides_every_pair(self):
        self.hides("sent Cookie: sid=abcdef; theme=dark and failed", "abcdef", "dark", keeps=("and failed",))

    def test_authorization_keeps_the_scheme_and_hides_the_credential(self):
        for scheme in ("Basic", "Digest", "Negotiate", "Bearer"):
            with self.subTest(scheme=scheme):
                text = self.hides(f"proxy said Authorization: {scheme} dXNlcjpw was refused", "dXNlcjpw")
                self.assertIn(f"Authorization: {scheme} {HIDDEN} was refused", text)

    def test_a_login_before_an_at_is_hidden(self):
        text = self.hides("dial ann:hunter@db.example.com failed", "ann", "hunter")
        self.assertIn(f"dial {HIDDEN}db.example.com failed", text)

    def test_a_signed_token_is_hidden_whole(self):
        self.hides(f"rejected {SIGNED} today", SIGNED_HEAD, SIGNED_BODY, "abcDEFghi", keeps=("today",))
        self.hides(f"rejected {LONG_HEAD}.{SIGNED_BODY}.abcDEFghi today", SIGNED_BODY, "abcDEFghi")

    def test_a_join_token_is_hidden(self):
        self.hides(f"kubeadm join with {JOIN} failed", "abcdef", "0123456789")

    def test_a_long_key_is_hidden(self):
        self.hides(f"rejected {SLASHED} today", "qWeRtYuIoPaSd", "nMlKjHgFdSaPoIuYtR", keeps=("today",))
        self.hides(f"posted to {HOOK} failed", "T0AB1CD2E", "B0FG3HI4J", "aBcDeFgHiJkLmNoPqRsT")
        self.hides(f"rejected {LETTERS} today", LETTERS, keeps=("today",))

    def test_a_value_in_any_quoting_is_hidden_whole(self):
        q = '\\"'                                          # a quote written inside a quoted text
        self.hides(f"body {{{q}password{q}: {q}hunter two{q}, {q}user{q}: {q}ann{q}}}", "hunter", "two",
                   keeps=(f"{q}user{q}: {q}ann{q}",))
        self.hides("got {'token': b'hunter two'} back", "hunter", "two", keeps=("back",))
        self.hides('set password=abc"hunter two" and stopped', "hunter", "two", keeps=("and stopped",))
        self.hides("got token: [hunter, two] back", "hunter", "two", keeps=("back",))
        self.hides("config password: | hunter two", "hunter", "two")

    def test_authorization_without_a_scheme_hides_its_first_word(self):
        self.hides("proxy said Authorization: hunter was refused", "hunter", keeps=("Authorization:", "refused"))

    def test_a_field_error_keeps_its_type_and_hides_the_value_it_repeats(self):
        text = self.hides('spec.signerName: Invalid value: "hunter": must be set', "hunter")
        self.assertEqual(f"spec.signerName: Invalid value: {HIDDEN} must be set", text)
        text = self.hides("spec.tokenRef: Unsupported value: hunter", "hunter")
        self.assertEqual(f"spec.tokenRef: Unsupported value: {HIDDEN}", text)

    def test_a_value_across_the_cut_stays_hidden(self):
        for pad in range(120, 170):
            with self.subTest(pad=pad):
                text = ALERTS.plain_error("x" * pad + " password=hunterhunterhunter tail")
                self.assertLessEqual(len(text), 160)
                self.assertNotIn("hun", text)

    def test_the_text_is_still_cut_at_the_limit(self):
        text = ALERTS.plain_error("password=hunter " + "word " * 100)
        self.assertLessEqual(len(text), 160)
        self.assertNotIn("hunter", text)


# Ordinary errors a person reads to act on: they pass as they did before.
PLAIN = [
    "Longhorn 1.37.1 is running", "k3s v1.31.4+k3s1", "at 2026-11-01T12:30:45Z the list was cut short", "12:30:45",
    "/apis/longhorn.io/v1beta2/volumes", "longhorn.io/v1beta2", "python3.12:latest",
    "[Errno 111] Connection refused", "context deadline exceeded", "the server could not find the requested resource",
    'volumes.longhorn.io is forbidden: User "system:serviceaccount:homestead:homestead" cannot list resource '
    '"volumes" in API group "longhorn.io" at the cluster scope',
    "Operation cannot be fulfilled on volumes.longhorn.io \"data\": the object has been modified; please apply your "
    "changes to the latest version and try again",
    'admission webhook "validator.longhorn.io" denied the request: spec.storageMinimalAvailablePercentage: '
    "Invalid value: 101: must be at most 100",
    "ValidatingAdmissionPolicyBinding not found", "concurrentAutomaticEngineUpgradePerNodeLimit is not a number",
    'Failed to pull image "docker.io/library/nginx:1.27@sha256:' + "9f86d081" * 8 + '"',
    "/var/lib/longhorn/replicas is full", "IncompleteRead(100 bytes read, 300 more expected)",
    "error: You must be logged in to the server (Unauthorized)",
    "HTTP Error 401: Unauthorized", "certificate signed by unknown authority",
    "spec.containers[0].env[0].valueFrom.secretKeyRef.name: Required value",
    "spec.tokenSecret: Forbidden: may not be changed", "spec.passwordRef: Too long: may not be more than 63 bytes",
]


class PlainTextTests(unittest.TestCase):
    def test_ordinary_errors_pass_as_before(self):
        for raw in PLAIN:
            with self.subTest(raw=raw):
                self.assertEqual(before(raw), ALERTS.plain_error(raw, limit=WIDE))


class OnlyMoreHiddenTests(unittest.TestCase):
    """Whatever the input, the output is the earlier output with some parts
    turned into the marker: never a character the earlier rules hid."""

    PARTS = ["password", "Password", "token", "pass", "auth", "Authorization", "Cookie", "X-Api-Key", "secret_key",
             "signal", "eyJ", "eyJhbGciOi", ".", ":", "=", ": ", " ", "  ", '"', "'", "@", "/", "+", "&", ";", ",",
             "\\", "Bearer ", "Basic ", "https://", "host=", "port=", "192.0.2.10", "[2001:db8::1]", ":6443",
             "example.com", HIDDEN, "<address>", "abc", "ABC", "a1B2c3D4e5F6g7H8i9J0", "x" * 25, LETTERS,
             "0123456789abcdef", "abcdef", "sha256:", "longhorn", "volumes", "not found", "{", "}", "\t", "\n",
             "é", "-", "_", "<", ">", SIGNED, JOIN, SLASHED, HOOK, "T0AB1CD2E", "1", "Z", '\\"', "b'", 'u"',
             "[", "]", "|", ">", "Required value", "Invalid value: ", "Unsupported value: ", "Not found: ",
             "Forbidden: ", "Too long: ", "Internal error: ", "hunter two"]

    def inputs(self):
        rng = random.Random(1082)
        yield from PLAIN
        yield from self.PARTS
        for _ in range(4000):
            yield "".join(rng.choice(self.PARTS) for _ in range(rng.randint(1, 30)))

    def test_rules_that_were_there_are_unchanged(self):
        self.assertEqual([(p, f, w) for p, f, w in BEFORE],
                         [(rule.pattern, rule.flags & re.I, word) for rule, word in ALERTS._PLAIN])

    def test_every_output_only_hides_more(self):
        for raw in self.inputs():
            old, new = before(raw), ALERTS.plain_error(raw, limit=WIDE)
            self.assertTrue(only_more_hidden(old, new), f"{raw!r}\nbefore: {old!r}\nnow:    {new!r}")

    def test_with_the_cut_every_shown_character_is_one_shown_before(self):
        """The cut text is the start of the uncut text, which only hides
        more than the earlier rules did on the whole input: so every
        character the cut text shows, the earlier rules showed too."""
        for raw in self.inputs():
            old, full = before(raw), ALERTS.plain_error(raw, limit=WIDE)
            self.assertTrue(only_more_hidden(old, full), raw)
            for limit in (40, 160):
                cut = ALERTS.plain_error(raw, limit=limit)
                self.assertEqual(full if len(full) <= limit else full[:limit - 1] + "…", cut, raw)

    def test_the_checker_refuses_a_text_that_shows_more(self):
        self.assertTrue(only_more_hidden("a <hidden> b c", "a <hidden> <hidden> c"))
        self.assertFalse(only_more_hidden("a <hidden> b", "a xyz b"))
        self.assertFalse(only_more_hidden("a <hidden> b", "a <hidden>x b"))
        self.assertFalse(only_more_hidden("a b", "a b c"))

    def test_made_plain_twice_is_made_plain_once(self):
        for raw in self.inputs():
            once = ALERTS.plain_error(raw, limit=WIDE)
            self.assertEqual(once, ALERTS.plain_error(once, limit=WIDE), raw)


class TimeTests(unittest.TestCase):
    """The text comes from outside: no shape of 10,000 characters makes the
    rules slow."""

    SHAPES = ["pass" * 2500, "password=" + "a" * 9991, '"password": "' + "\\" * 9987, "a:" * 4999 + "@",
              "eyJ." * 2500, "a/" * 5000, "A1/" * 3333, "abcdef." * 1428, "x" * 10000, HIDDEN * 1250,
              "Cookie: " + "a=b; " * 1998, "<hidden>." * 1111, "aB" * 5000, "@sha1:" * 1666, ":" * 9999 + "@",
              "password:" * 1111, "'" * 10000, "Authorization: " + "Basic " * 1663, "_" * 9999 + "/", "eyJ" * 3333,
              "token: [" * 1250, "pass: \"a b" * 1000]

    def test_long_hostile_texts_stay_fast(self):
        for shape in self.SHAPES:
            self.assertLessEqual(len(shape), 10000)
            with self.subTest(shape=shape[:20]):
                started = time.monotonic()
                ALERTS.plain_error(shape)
                self.assertLess(time.monotonic() - started, 1.0)


class Api(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, reason):
        self.reason = reason
        super().__init__(("127.0.0.1", 0), ApiHandler)


class ApiHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(401, self.server.reason)
        self.send_header("Content-Length", "0")
        self.end_headers()


class ThroughTheClientTests(unittest.TestCase):
    """A reason phrase as a real server sends it, read through
    homestead_kube's own client."""

    def test_a_reason_naming_a_value_hides_it(self):
        api = Api("Unauthorized token=hunter")
        threading.Thread(target=api.serve_forever, daemon=True).start()
        self.addCleanup(api.server_close)
        self.addCleanup(api.shutdown)
        host, port = api.server_address
        self.enterContext(mock.patch.object(KUBE, "API", f"http://{host}:{port}"))
        self.enterContext(mock.patch.object(KUBE, "TOKEN", "x"))
        KUBE.close_connections()
        self.addCleanup(KUBE.close_connections)
        DEADLINE.end()
        with self.assertRaises(Exception) as caught:
            STORAGE.get_volumes()
        self.assertEqual(f"the cluster's API answered 401 Unauthorized token={HIDDEN}",
                         ALERTS.read_failure(caught.exception))


if __name__ == "__main__":
    unittest.main()
