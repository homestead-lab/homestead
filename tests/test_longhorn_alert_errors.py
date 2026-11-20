"""What the Longhorn volumes alert says about a failed read (#1082).

Alerts go to notification channels, so the error is short and plain, and the
alert says which kind of failure it was: the cluster's API did not answer, it
answered with an error, or the read failed otherwise. Built on real
exceptions and on homestead_kube's own client against a local server, not on
hand-written strings only."""
import errno
import http.client
import http.server
import os
import re
import socket
import ssl
import sys
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import server  # noqa: F401  (binds homestead_kube)
import homestead_alerts as ALERTS
import homestead_kube as KUBE
import homestead_read_deadline as DEADLINE
import homestead_storage as STORAGE

TOKEN = "eyJhbGciOi" + "JSUzI1NiIs" + "InR5cCI6Ik" + "pXVCJ9"      # built in pieces: push protection


def wrapped(error):
    """As homestead_storage.get_volumes raises it: a RuntimeError from the read's error."""
    try:
        raise RuntimeError(f"Longhorn's volumes could not be read: {error}") from error
    except RuntimeError as outer:
        return outer


class KindTests(unittest.TestCase):
    def says(self, error):
        return ALERTS.read_failure(wrapped(error))

    def http(self, code, reason):
        return urllib.error.HTTPError(f"https://192.0.2.10:6443/apis/longhorn.io/v1beta2/volumes?token={TOKEN}",
                                      code, reason, {}, None)

    def test_403_says_a_permission_may_be_missing(self):
        self.assertEqual("the cluster's API answered 403 Forbidden: Homestead may be missing a permission",
                         self.says(self.http(403, "Forbidden")))

    def test_404_and_429_say_what_it_answered(self):
        self.assertEqual("the cluster's API answered 404 Not Found", self.says(self.http(404, "Not Found")))
        self.assertEqual("the cluster's API answered 429 Too Many Requests",
                         self.says(self.http(429, "Too Many Requests")))

    def test_a_timeout_did_not_answer(self):
        self.assertEqual("the cluster's API did not answer (timed out)", self.says(TimeoutError("timed out")))
        self.assertEqual("the cluster's API did not answer (TimeoutError)", self.says(TimeoutError()),
                         "an empty message names its kind")

    def test_a_request_s_spent_time_is_worded_for_an_alert_not_a_page(self):
        """A read cut by a request's deadline (#984) carries the page's
        message ("this page could not be read in full. Try again"), which
        an alert must not repeat."""
        self.addCleanup(DEADLINE.end)      # spent() counts a cut read on this thread
        self.assertEqual("the cluster's API did not answer in time", self.says(DEADLINE.spent()))
        self.assertEqual("the cluster's API did not answer in time", ALERTS.read_failure(DEADLINE.Spent(DEADLINE.MESSAGE)))

    def test_a_dropped_connection_did_not_answer(self):
        error = urllib.error.URLError(http.client.RemoteDisconnected("Remote end closed connection without response"))
        self.assertEqual("the cluster's API did not answer (<urlopen error Remote end closed connection without response>)",
                         self.says(error))
        self.assertTrue(self.says(ConnectionResetError(104, "Connection reset by peer")).startswith(
            "the cluster's API did not answer"))

    def test_a_certificate_that_does_not_match_did_not_answer_and_names_no_host(self):
        error = ssl.SSLCertVerificationError(
            1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: Hostname mismatch, certificate is not "
               "valid for 'kube.example.com'. (_ssl.c:1006)")
        for raised in (error, urllib.error.URLError(error)):
            text = self.says(raised)
            self.assertTrue(text.startswith("the cluster's API did not answer ("), text)
            self.assertIn("Hostname mismatch", text)
            self.assertNotIn("kube.example.com", text)

    def test_a_list_cut_short_is_a_failed_read(self):
        self.assertEqual("the read failed (Kubernetes returned an incomplete list for "
                         "/apis/longhorn.io/v1beta2/volumes; refresh and try again)",
                         self.says(KUBE.Incomplete("Kubernetes returned an incomplete list for "
                                                   "/apis/longhorn.io/v1beta2/volumes; refresh and try again")))

    def test_an_error_without_a_cause_is_a_failed_read(self):
        self.assertEqual("the read failed (KeyError 'status')", ALERTS.read_failure(KeyError("status")))

    def test_the_error_is_cut_at_160_characters(self):
        text = ALERTS.read_failure(KUBE.Incomplete("x " * 300))
        error = text[len("the read failed ("):-1]
        self.assertLessEqual(len(error), 160)
        self.assertTrue(error.endswith("…"))


class RealErrorTests(unittest.TestCase):
    """The str() of what homestead_kube's client really raises."""

    def test_a_socket_that_never_answers_times_out(self):
        listener = socket.create_server(("127.0.0.1", 0))
        self.addCleanup(listener.close)
        with socket.create_connection(listener.getsockname(), timeout=0.2) as client:
            with self.assertRaises(TimeoutError) as caught:
                client.recv(1)
        text = ALERTS.read_failure(wrapped(caught.exception))
        self.assertEqual("the cluster's API did not answer (timed out)", text)

    def test_every_real_error_s_text_is_plain(self):
        errors = [
            urllib.error.URLError(ssl.SSLCertVerificationError(
                1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: IP address mismatch, certificate is "
                   "not valid for '192.0.2.10'. (_ssl.c:1006)")),
            urllib.error.URLError(ConnectionRefusedError(111, "Connection refused")),
            urllib.error.URLError(socket.gaierror(-2, "Name or service not known")),
            OSError(f"connect to [2001:db8::1]:6443 at https://198.51.100.7:6443/?token={TOKEN} failed"),
            http.client.IncompleteRead(b"x" * 400, 100),
        ]
        for error in errors:
            with self.subTest(error=repr(error)[:60]):
                text = ALERTS.read_failure(wrapped(error))
                for part in ("192.0.2.10", "198.51.100.7", "2001:db8", "6443", TOKEN, "token="):
                    self.assertNotIn(part, text)
                self.assertLessEqual(len(text), len("the cluster's API did not answer ()") + 160)


class Api(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, mode):
        self.mode = mode
        super().__init__(("127.0.0.1", 0), ApiHandler)


class ApiHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.server.mode == "drop":
            self.close_connection = True       # read the request, close without an answer
            return
        body = b'{"kind":"Status","code":403,"message":"volumes.longhorn.io is forbidden"}'
        self.send_response(403, "Forbidden")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ThroughTheClientTests(unittest.TestCase):
    """STORAGE.get_volumes through homestead_kube's own client and a local
    server: the exception chain the overview sees on a cluster."""

    def serve(self, mode):
        api = Api(mode)
        threading.Thread(target=api.serve_forever, daemon=True).start()
        self.addCleanup(api.server_close)
        self.addCleanup(api.shutdown)
        host, port = api.server_address
        self.enterContext(mock.patch.object(KUBE, "API", f"http://{host}:{port}"))
        self.enterContext(mock.patch.object(KUBE, "TOKEN", TOKEN))
        KUBE.close_connections()
        self.addCleanup(KUBE.close_connections)
        DEADLINE.end()
        return host, port

    def failure(self):
        with self.assertRaises(Exception) as caught:
            STORAGE.get_volumes()
        return ALERTS.read_failure(caught.exception)

    def test_a_403_says_a_permission_may_be_missing(self):
        host, port = self.serve("forbidden")
        text = self.failure()
        self.assertEqual("the cluster's API answered 403 Forbidden: Homestead may be missing a permission", text)

    def test_a_connection_closed_without_an_answer_did_not_answer(self):
        host, port = self.serve("drop")
        text = self.failure()
        self.assertTrue(text.startswith("the cluster's API did not answer ("), text)
        for part in (host, str(port), TOKEN):
            self.assertNotIn(part, text)

    def test_a_host_that_cannot_be_reached_did_not_answer(self):
        """A powered-off host or a moved virtual IP: the connect itself fails
        with a socket OSError, which homestead_kube passes on as it is."""
        host, port = self.serve("forbidden")
        for code in (errno.EHOSTUNREACH, errno.ENETUNREACH):
            with self.subTest(code=errno.errorcode[code]):
                KUBE.close_connections()
                with mock.patch.object(socket, "create_connection", side_effect=OSError(code, os.strerror(code))):
                    with self.assertRaises(RuntimeError) as caught:
                        STORAGE.get_volumes()
                self.assertIs(type(caught.exception.__cause__), OSError, "unwrapped, as on a cluster")
                text = ALERTS.read_failure(caught.exception)
                self.assertEqual(f"the cluster's API did not answer ([Errno {code}] {os.strerror(code)})", text)


class HostPortTests(unittest.TestCase):
    def test_a_host_name_with_a_port_is_hidden(self):
        for raw in ("connect to kube.internal:6443 failed", "dial tcp api.example.com:443: i/o timeout",
                    "node-a.cluster.example.com:10250 refused"):
            with self.subTest(raw=raw):
                text = ALERTS.plain_error(raw)
                self.assertIn("<address>", text)
                for part in ("internal", "example.com", "6443", "10250", ":443"):
                    self.assertNotIn(part, text)

    def test_versions_times_and_paths_pass_unchanged(self):
        for plain in ("Longhorn 1.37.1 is running", "k3s v1.31.4+k3s1", "at 2026-11-01T12:30:45Z the list was cut short",
                      "12:30:45", "/apis/longhorn.io/v1beta2/volumes", "longhorn.io/v1beta2", "python3.12:latest"):
            with self.subTest(plain=plain):
                self.assertEqual(plain, ALERTS.plain_error(plain))


# Texts for the tables below, token-shaped ones built in pieces: GitHub's
# push protection refuses them even when they are fake. Every value is invented.
HEX32 = "9f86d081" + "884c7d65" + "9a2feaa0" + "c55ad015"
HEX64 = HEX32 + "a3bf4f1b" + "2b0b822c" + "d15d6c15" + "b0f00a08"
JWT = TOKEN + "." + "eyJzdWIiOi" + "IxMjM0NTY3" + "ODkwIn0" + "." + "SflKxwRJSM" + "eKKF2QT4fw" + "pMeJf36POk"
UUID = "3f1c2a9e" + "-5b7d-4e21-9c0a-" + "1d2e3f4a5b6c"
VOLUME = "pvc-" + UUID
SECRET = "aB3dE5fG7h" + "Q1w2E3r4T5" + "y6U7"

# _PLAIN as it was on dev at 32228189f, before the kept names: pattern, flags
# and replacement. It must not change in this branch.
DEV_PLAIN = [
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

# What the branch may keep, copied here so a widened list or pattern in the
# module fails the pin tests and the oracle below (which keeps only these).
KEPT_GROUPS = {
    "longhorn.io", "kubevirt.io", "cdi.kubevirt.io", "subresources.kubevirt.io", "upload.cdi.kubevirt.io",
    "storage.k8s.io", "snapshot.storage.k8s.io", "coordination.k8s.io", "networking.k8s.io", "scheduling.k8s.io",
    "metrics.k8s.io", "discovery.k8s.io", "rbac.authorization.k8s.io", "apiextensions.k8s.io",
    "admissionregistration.k8s.io", "certificates.k8s.io", "cluster.x-k8s.io", "helm.cattle.io", "upgrade.cattle.io",
    "k8s.cni.cncf.io", "harvesterhci.io", "network.harvesterhci.io", "loadbalancer.harvesterhci.io",
    "devices.harvesterhci.io", "metallb.io", "cert-manager.io",
    "volumes.longhorn.io", "engines.longhorn.io", "replicas.longhorn.io", "nodes.longhorn.io",
    "settings.longhorn.io", "volumeattachments.longhorn.io",
}
KEPT_VOLUME = r"pvc-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?:-r-[0-9a-f]{8}|-e-\d{1,3})?"
# By the rule's place in DEV_PLAIN: the quoted dotted name rule and the run rule.
KEEP = {2: lambda found: found[1:-1] in KEPT_GROUPS,
        len(DEV_PLAIN) - 1: lambda found: re.fullmatch(KEPT_VOLUME, found)}


def trace(chars, keeping):
    """_PLAIN's rules applied as re.sub applies them, to (character, where
    it came from) pairs: what a rule writes comes from nowhere (None), what
    it copies from a group keeps where it came from. keeping: a match that
    is a kept name by this file's own copies (KEEP, not the module's) becomes one private-use character, the next after those
    already set aside, from ("kept", where its characters came from, the
    marker the rule would have written)."""
    slots = 0
    for index, (pattern, word) in enumerate(ALERTS._PLAIN):
        text, out, at = "".join(c for c, _ in chars), [], 0
        keep = KEEP.get(index) if keeping else None
        for m in pattern.finditer(text):
            out += chars[at:m.start()]
            if keep and keep(m.group(0)):
                out.append((chr(0xe000 + slots), ("kept", chars[m.start():m.end()], m.expand(word))))
                slots += 1
            else:
                for i, part in enumerate(re.split(r"\\(\d)", word)):
                    out += chars[m.start(int(part)):m.end(int(part))] if i % 2 else [(c, None) for c in part]
            at = m.end()
        chars = out + chars[at:]
    return chars


def hidden_more(chars):
    """Dev's _hidden_more applied to (character, where it came from) pairs:
    the spans it hides are read from the module's own function (the list it
    sorts at its end), each becomes the marker from nowhere, the rest keeps
    where it came from. Checked against the function's own output, so a
    change to how it hides fails here, not silently."""
    text, found = "".join(c for c, _ in chars), []

    def capture(spans):
        found.append(list(spans))
        return sorted(found[-1])
    with mock.patch.object(ALERTS, "sorted", capture, create=True):
        plain = ALERTS._hidden_more(text)
    if len(found) != 1:
        raise AssertionError("_hidden_more no longer sorts its spans once: update hidden_more()")
    out, shown = [], 0
    for start, end in sorted(s for s in found[0] if s[1] > s[0]):
        if start > shown or not out:
            out += chars[shown:start] + [(c, None) for c in "<hidden>"]
        shown = max(shown, end)
    out += chars[shown:]
    if "".join(c for c, _ in out) != plain:
        raise AssertionError(f"hidden_more() is not _hidden_more on {text!r}")
    return out


def outcomes(raw):
    """For one input: dev's output (_PLAIN's rules, then _hidden_more) and
    where its characters came from; the branch's text with kept names, the
    same with each kept name's marker instead (it must equal dev's output
    for the names to be kept), and where the branch's characters and its
    kept names came from."""
    text = " ".join(raw.split())
    dev_chars = hidden_more(trace(list(zip(text, range(len(text)))), False))
    dev_out = "".join(c for c, _ in dev_chars)
    dev = {o for _, o in dev_chars if o is not None}
    slotted = re.sub("[\ue000-\uf8ff]", "\ufffd", text)
    named, marked, branch, kept_at = [], [], set(), set()
    slots = trace(list(zip(slotted, range(len(slotted)))), True)
    aside = sum(isinstance(o, tuple) for _, o in slots)
    for c, o in hidden_more(slots):
        if isinstance(o, tuple):
            named += [k for k, _ in o[1]]
            marked.append(o[2])
            kept_at |= {w for _, w in o[1] if w is not None}
        else:
            named.append(c)
            marked.append(c)
            if o is not None:
                branch.add(o)
    return dev_out, dev, "".join(named), "".join(marked), branch | kept_at, kept_at, aside


class SecretsStayHiddenTests(unittest.TestCase):
    """The branch shows exactly what dev shows (_PLAIN's rules, then
    _hidden_more), plus the kept names: every character dev hid is hidden,
    unless it lies inside a kept name."""

    # Texts with a secret in them that dev's rules hide; whatever the branch
    # keeps, these stay hidden.
    HIDDEN = [
        "Authorization: Basic " + "YWRtaW46" + "aHVudGVy" + "Mg==",
        "expected Basic " + "dGVzdDp0" + "-" + "Zz9kXq2Lm4Wp7Rt" + " refused",
        "expected Basic " + "dGVzdDp0" + "_" + "Zz9kXq2Lm4Wp7Rt",
        "Basic " + "dGVzdDp0" + "==" + "Zz9kXq2Lm4Wp7Rt",
        "Authorization: Bearer " + JWT,
        "HTTP Error 401: Bearer " + "s3cr" + "3t",
        "client_secret=" + "s3cr" + "et was rejected",
        "kubeconfig user token: " + JWT,
        "a JWT " + JWT + " expired",
        "key " + "AKIA" + "Q3X7EXAM" + "PLEZ2K9W" + " is not allowed",
        "posted to /services/" + "T0A1B2C3D" + "/" + "B4E5F6G7H" + "/" + "q9W8e7R6" + "t5Y4u3I2" + "o1P0aSdF",
        "key " + HEX32 + " was refused",
        "key " + HEX64 + " was refused",
        "key-" + HEX32 + " was refused",
        "api key " + UUID + " refused",
        "blob " + "c2VjcmV0" + "LXZhbHVl" + "LWZvci10" + "ZXN0cw==" + " not valid",
        "https://" + "admin:pa55" + "@kube.example.com/api failed",
        "node token " + "K10" + HEX64 + "::server:" + HEX32 + " refused",
        "passphrase correct-horse-battery-staple-" + "42" + " rejected",
        "token " + "qwhzkxprmvbtlsnfgdjc" + "-" + "a9k2z",
        "read " + "a1b2c3d4" + "_e5f6g7h8" + "_i9j0k1l2" + " refused",
        "read " + VOLUME + "-" + UUID + " refused",
        "x-auth-token-v2=" + "t0k" + "3nv2",
        "passwordHash=" + "h4s" + "hval",
        "a" * 50 + "_password=" + "l0ng" + "k3y",
        'Authorization: "Bearer ' + "mF_9.B5f-4" + '.1JqM"', "Authorization: 'Bearer " + "mF_9.B5f-4" + "'",
        "Authorization: (Bearer " + "mF_9.B5f-4" + ")", "Authorization: [Bearer " + "mF_9.B5f-4" + "]",
        'Authorization: "Bearer ' + "ya29" + ".a0AfB_byC" + '.xYz-Qw_Er~Ty/Ui+Op"',
        "aB3dE5fG7h" + "+" + "xyzAuth9==", "abcdefghij" + "+" + "tok" + "en=" + "s3cr3t",
        "x" * 60 + "_password=" + "hunter2xyz",
    ]
    GLUE = "+/=_-.:@%"

    def fault(self, raw):
        """None when plain_error's output is dev's output, or dev's output
        with kept names standing where dev wrote their markers, and every
        character dev hid is hidden unless it lies in a kept name; else why.
        self.path: "kept" when the output differs from dev's, "fell back"
        when a name was set aside and the output is dev's, else None."""
        out = ALERTS.plain_error(raw, limit=10 ** 7)
        dev_out, dev, named, marked, branch, kept_at, aside = outcomes(raw)
        self.path = "kept" if out != dev_out else "fell back" if aside else None
        if out == dev_out:
            return None
        if out != named or marked != dev_out:
            return f"output {out!r} is neither dev's {dev_out!r} nor it with kept names"
        text = " ".join(raw.split())
        extra = sorted(branch - dev - kept_at)
        return f"shows {[text[i] for i in extra]}" if extra else None

    def test_the_rules_are_dev_s_unchanged(self):
        self.assertEqual(DEV_PLAIN, [(p.pattern, p.flags & re.I, w) for p, w in ALERTS._PLAIN])

    def test_what_may_be_kept_is_the_closed_list_and_the_volume_pattern(self):
        self.assertEqual(KEPT_GROUPS, ALERTS._API_GROUPS)
        self.assertEqual(KEPT_VOLUME, ALERTS._VOLUME.pattern)
        self.assertEqual(0, ALERTS._VOLUME.flags & re.I)
        self.assertEqual(set(KEEP), set(ALERTS._KEPT))

    def test_nothing_dev_hides_is_shown_but_the_kept_names(self):
        glued = [a + g + b for g in list(self.GLUE) + [""]
                 for a, b in ((VOLUME, SECRET), (SECRET, VOLUME), ('"longhorn.io"', SECRET), (SECRET, '"longhorn.io"'),
                              (VOLUME + "-r-1a2b3c4d", SECRET), ("Bearer ", VOLUME), ("host=", '"longhorn.io"'))]
        forged = ["\ue000 " + VOLUME, "\ue000" + SECRET, VOLUME + " \ue000 \ue001 \uf8ff", "token \ue000",
                  '"longhorn.io" \ue001' + SECRET]
        for raw in (self.HIDDEN + glued + forged
                    + list(NamesAreKeptTests.PLAIN) + [raw for raw, _ in NamesAreKeptTests.COST]):
            with self.subTest(raw=raw[:60]):
                self.assertIsNone(self.fault(raw))

    def test_nothing_dev_hides_is_shown_in_generated_texts(self):
        """50,000 texts of secrets, credential words, addresses and kept names
        glued by 45 separators, including control, zero-width, combining and
        full-width characters, and forged slots; seeded, so repeatable."""
        import random
        rng = random.Random(1243)
        alnum = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        shapes = [
            lambda: "".join(rng.choice(alnum + "+/") for _ in range(rng.randint(8, 40))) + rng.choice(["", "=", "=="]),
            lambda: "".join(rng.choice("0123456789abcdef") for _ in range(rng.randint(8, 64))),
            lambda: "-".join("".join(rng.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(rng.randint(1, 10)))
                             for _ in range(rng.randint(1, 5))),
            lambda: "eyJ" + "".join(rng.choice(alnum + "_-") for _ in range(rng.randint(5, 20))) + "."
                    + "".join(rng.choice(alnum + "_-") for _ in range(rng.randint(5, 20))),
            lambda: "".join(rng.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(6)) + "."
                    + "".join(rng.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(16)),
        ]
        fragments = ["Bearer ", "Basic ", "Authorization: ", "token=", "password=", "host=", "server=", "port=",
                     "https://", "http://x/", "192.0.2.5", "[2001:db8::1]", "2001:db8::5", "kube.example.com",
                     ":6443", VOLUME, VOLUME + "-r-1a2b3c4d", VOLUME + "-e-0", "pvc-", UUID, '"longhorn.io"',
                     "'volumes.longhorn.io'", '"kube.example.com"', '"LONGHORN.IO"', "eyJ", "key-", "x.", "API group ",
                     "\ue000", "\ue001", "\uf8ff", "auth", "session", "1.2.3", "abc", "_", "=", "a" * 20,
                     "<hidden>", "<address>"]
        separators = [" ", "", "+", "/", "=", "_", "-", ".", ":", "@", "%", "\x00", "\r", "\t", "\u200b", "\u200f",
                      "\u0301", "\uff1d", '\\"', "\\'", '"', "'", ",", ";", "(", ")", "[", "]", "{", "}", "<", ">",
                      "&", "?", "#", "!", "*", "|", "~", "^", "$", "`", "\n", "  ", ". "]
        hits, paths = [], {"kept": 0, "fell back": 0}
        for _ in range(50000):
            raw = "".join(rng.choice(separators) + (rng.choice(shapes)() if rng.random() < 0.5 else rng.choice(fragments))
                          for _ in range(rng.randint(1, 5)))
            if self.fault(raw):
                hits.append(raw)
            elif self.path:
                paths[self.path] += 1
        self.assertEqual([], hits[:5], f"{len(hits)} of 50000 texts show what dev hides")
        # The texts reach both sides of the check: names kept, and names set
        # aside whose output is dev's (the check fell back, or what dev hides
        # took the name with it). Seeded: 6199 and 1289 when written.
        self.assertGreaterEqual(paths["kept"], 2000, paths)
        self.assertGreaterEqual(paths["fell back"], 400, paths)

    def test_a_text_cannot_forge_a_kept_name(self):
        """A private-use character in the input is never read back as a kept
        name: the input is then given exactly as dev's rules make it."""
        for raw, plain in (("\ue000 " + VOLUME, "\ue000 <hidden>"), ("token \ue000", "token \ue000"),
                           ("\ue000" + SECRET, "\ue000<hidden>"), ('"longhorn.io" \ue001', "<address> \ue001")):
            with self.subTest(raw=raw):
                self.assertEqual(plain, ALERTS.plain_error(raw))

    def test_a_kept_name_next_to_more_text(self):
        """A kept name directly before or after a dot, colon, slash, at sign,
        dash, digit or letter: the output is dev's, or dev's with the whole
        name where dev wrote its marker."""
        texts = ["'" + '"longhorn.io".kube.example.com:6443' + "'"]
        for mark in ".:/@-1a":
            for name in ('"longhorn.io"', VOLUME):
                texts += [name + mark + "kube.example.com:6443", "kube.example.com" + mark + name,
                          name + mark + SECRET, SECRET + mark + name, "Bearer " + name + mark, mark + name + ":443"]
        for raw in texts:
            with self.subTest(raw=raw):
                self.assertIsNone(self.fault(raw))

    def test_dev_s_extra_hiding_takes_a_kept_name_with_what_it_hides(self):
        """_hidden_more runs on the text with names set aside: a kept name
        next to ordinary text stays, one that is the value of a field named
        like a secret or part of a login before an @ goes with it."""
        field = "tok" + "en: "                          # with "=" the last rule hides field and name as one run
        cases = (
            (f"volume {VOLUME} is degraded; see the Longhorn UI", (VOLUME,), ()),
            (f"replica {VOLUME}-r-1a2b3c4d on node-a, engine {VOLUME}-e-0 stopped",
             (VOLUME + "-r-1a2b3c4d", VOLUME + "-e-0"), ()),
            (f"refused {field}{VOLUME} for the volume", ("refused", "for the volume"), (UUID,)),
            ('refused {"pass' + 'word": "longhorn.io"} twice', ("refused",), ("longhorn.io",)),
            ("refused pass" + 'word: "longhorn.io" twice', ("refused",), ("longhorn.io",)),
            (f"dial {VOLUME}:pw@db.example.com failed", ("db.example.com failed",), (UUID, "pw")),
            ('dial ann:"longhorn.io"@db.example.com failed', ("db.example.com failed",), ("longhorn.io", "ann")),
            (f'volumes.longhorn.io "{VOLUME}" is forbidden: User "system:serviceaccount:homestead:homestead" '
             'cannot get resource "volumes" in API group "longhorn.io" in the namespace "longhorn-system"',
             (f'"{VOLUME}"', 'API group "longhorn.io"'), ()),
        )
        for raw, shown, hidden in cases:
            with self.subTest(raw=raw[:50]):
                text = ALERTS.plain_error(raw, limit=400)
                for part in shown:
                    self.assertIn(part, text)
                for part in hidden:
                    self.assertNotIn(part, text)
                self.assertIsNone(self.fault(raw))

    def test_a_name_whose_marker_would_read_differently_is_not_kept(self):
        """The check on every call: were a rule's marker to join what follows
        it (here a marker of 19 letters, which the run rule then reads with a
        digit after it), the slot would not, and the output is dev's."""
        rules = list(ALERTS._PLAIN)
        rules[2] = (rules[2][0], "a" * 19)
        with mock.patch.object(ALERTS, "_PLAIN", rules):
            self.assertEqual("<hidden> x", ALERTS.plain_error('"longhorn.io"1 x'))
            self.assertEqual('is "longhorn.io"', ALERTS.plain_error('is "longhorn.io"'))

    def test_the_whole_call_stays_within_about_twice_dev_s_time(self):
        """Dev's work is _PLAIN's rules, then _hidden_more. With a name set
        aside the branch does that twice, once on the text with names set
        aside and once as dev does, then compares; with none, once. Each
        text here is 100,000 characters, of which the rules read only the
        head (about 2,000, LongTextTests): so this guards that with a name
        kept the second pass on a text at the cap stays within four times
        dev's work on it plus 50 ms, not the ratio on the whole text."""
        import time
        for text in (('"longhorn.io"' + " ") * 7142, (VOLUME + " ") * 2439, ('"kube.example.com"' + " ") * 5263,
                     ("x" * 30 + "1 ") * 3225, "pvc-" * 25000, "\ue000" * 100000):
            with self.subTest(text=text[:20]):
                start = time.perf_counter()
                plain = ALERTS._head(" ".join(text.split()))[0]
                for pattern, word in ALERTS._PLAIN:
                    plain = pattern.sub(word, plain)
                ALERTS._hidden_more(plain)
                dev = time.perf_counter() - start
                start = time.perf_counter()
                ALERTS.plain_error(text, limit=10 ** 7)
                self.assertLess(time.perf_counter() - start, 4 * dev + 0.05)


class NamesAreKeptTests(unittest.TestCase):
    """Real error shapes from a cluster's Longhorn and Kubernetes objects,
    every name and value replaced. A Longhorn volume's name and the API
    groups Homestead reads pass unchanged (#1082's follow-up); every other
    name is as on dev."""

    PLAIN = (
        f'volumes.longhorn.io "{VOLUME}" not found',
        f"replica {VOLUME}-r-1a2b3c4d is stopped",
        f"engine {VOLUME}-e-0 is not running",
        f"the volume {VOLUME}.",
        f"volumes ({VOLUME}, {VOLUME}-r-1a2b3c4d; {VOLUME}-e-12)",
        f"path /v1/{VOLUME}/x", f"x.{VOLUME}",
        f'Operation cannot be fulfilled on volumes.longhorn.io "{VOLUME}": the object has been '
        "modified; please apply your changes to the latest version and try again",
        'volumes.longhorn.io is forbidden: User "system:serviceaccount:homestead:homestead" cannot list '
        'resource "volumes" in API group "longhorn.io" in the namespace "longhorn-system"',
        'customresourcedefinitions.apiextensions.k8s.io "volumes.longhorn.io" not found',
        'nodes.longhorn.io "worker-03" not found',
        "list /apis/longhorn.io/v1beta2/namespaces/longhorn-system/volumes was cut short",
        "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")
    # Names dev hides and the branch still hides: each with what goes.
    COST = (
        ("pod longhorn-manager-x7k2p on node-a is not ready", "x7k2p"),
        ("pod example-app-54dcf96795-wl7b7 is pending", "54dcf96795"),
        ('persistentvolumeclaims "data-example-postgres-0" not found', "postgres-0"),
        ("instance-manager-" + HEX32 + " is not running", HEX32),
        ("backup backup-" + HEX32[:16] + " is in progress", HEX32[:16]),
        ("snapshot " + UUID + " is not ready", UUID),
        (f"{VOLUME}+{SECRET}", UUID),
        (f"{VOLUME}-{UUID}", UUID),
        ("volume pvc-" + "3f1c2a9g" + "-5b7d-4e21-9c0a-" + "1d2e3f4a5b6z" + " is degraded", "3f1c2a9g"),
    )

    def test_a_volume_s_name_and_api_groups_pass_unchanged(self):
        for plain in self.PLAIN:
            with self.subTest(plain=plain[:50]):
                self.assertEqual(plain, ALERTS.plain_error(plain, limit=400))

    def test_other_names_and_names_joined_to_more_stay_hidden(self):
        for raw, gone in self.COST:
            with self.subTest(raw=raw[:40]):
                text = ALERTS.plain_error(raw, limit=400)
                self.assertNotIn(gone, text)
                self.assertIn("<", text)

    def test_the_alert_names_the_volume(self):
        body = ALERTS.longhorn_facts({"volumes_unread": True,
                                      "volumes_unread_reason": f'volumes.longhorn.io "{VOLUME}" not found'})[0]["body"]
        self.assertIn(VOLUME, body)

    def test_a_quoted_host_name_is_still_hidden(self):
        """A quoted dotted name is kept only when it is exactly one of the
        API groups Homestead reads or a Longhorn object it reads."""
        for raw in ("certificate is not valid for 'kube.example.com'.",
                    'cannot list resource "volumes" in API group "kube.example.com"',
                    'volumes.longhorn.io "kube.example.com" not found',
                    "customresourcedefinitions.apiextensions.k8s.io 'longhorn.io.example.com' not found",
                    'x "myhost.home.k8s.io"', 'x "LONGHORN.IO"',
                    'x "' + "a1b2c3d4e5f6g7h8" + "." + "i9j0k1l2m3n4o5p6" + '.longhorn.io"'):
            with self.subTest(raw=raw):
                text = ALERTS.plain_error(raw, limit=400)
                quoted = re.findall(r"['\"]([^'\"]+)['\"]", raw)[-1]
                self.assertNotIn(quoted, text)
                self.assertIn("<address>", text)


def filler(length):
    """Ordinary words, one space between them, ending in a space: exactly length characters."""
    words = ("abcd " * (length // 5 + 2))[:length - 1]
    return (words[:-1] + "x" if words.endswith(" ") else words) + " "


class LongTextTests(unittest.TestCase):
    """A text longer than _HEAD characters (once made one line) is made plain
    from its head only, a start of it that ends before a space, so its time
    is bounded; " …" says it was cut. Every character the head's output
    shows, the whole text's output shows at the same place (dev's rules on
    the whole text, by outcomes() above), or it is in a kept name."""

    # What a rule reads across a space, or what a value hides to its end.
    ACROSS = [
        'host="kube example com" next', "server='a b c' next", 'ADDRESS="x y z" next',
        "host=\"a server='" + "kube.example.com" + "' b\" end",      # a closed match inside an open one
        'host="a b c://x"y d" end', "host=\"a b c://x'y d\" end",     # the first rule takes a quote
        'host="a b" c" end', "x-host='a b' end", "pass" + "word=b'" + "two words' next",
        "pass" + "word = b'" + "two words' next", "token: [[a, b], c] next",
        "Bearer " + "s3cr" + "3tvalue next", "Bearer Bearer " + "s3cr" + "3t next", "x.Bearer " + "s3cr" + "3t",
        "bearer " + "s3cr" + "3t next",
        "Authorization: Basic " + "dXNl" + "cjpw" + " and more words", "Authorization: Custom a b c",
        'password="hunter two three" next', "token: [a, b, c] next", "secret: | a b c", "secret: >- a b",
        '\\"password\\": \\"hunter two\\" next', "Cookie: a=b; c=d; e=f", "token=a; b=c; d=e",
        "spec.token: Invalid value: hunter two", 'password=abc"hunter two" next', "pass" + "word : hunter next",
        "dial ann:hunter@db.example.com failed", VOLUME + " " + SECRET + " " + VOLUME,
        '"longhorn.io" password=' + "hunter" + ' "longhorn.io"',
    ]

    def check(self, raw):
        """Fails unless plain_error(raw) is the head's output and " …", and
        the head shows nothing the whole text hides. Returns the head."""
        text = " ".join(raw.split())
        head, cut = ALERTS._head(text)
        self.assertEqual(len(text) > ALERTS._HEAD, cut)
        if not cut:
            self.assertEqual(text, head)
            return head
        self.assertLessEqual(len(head), ALERTS._HEAD)
        self.assertEqual(text[:len(head)], head)
        self.assertTrue(head == "" or text[len(head)] == " ", "the head ends before a space")
        self.assertEqual(f"{ALERTS.plain_error(head, limit=10 ** 7)} …".lstrip(), ALERTS.plain_error(raw, limit=10 ** 7))
        for limit in (160, 240):
            out = ALERTS.plain_error(raw, limit=limit)
            self.assertLessEqual(len(out), limit)
        self.assertEqual([], self.shows(text, head), raw[len(head) - 40:len(head) + 60])
        return head

    @staticmethod
    def shows(text, head):
        """The characters the head's output shows that the whole text's does
        not (dev's rules on the whole text), kept names aside. The plain pass
        on the head (dev's rules, which plain_error returns when nothing is
        kept or its check fails) is always compared; the pass with names set
        aside only when it is what plain_error returns (as fault() does)."""
        dev = outcomes(text)[1]
        dev_out, plain, named, marked, branch, kept_at, _ = outcomes(head)
        shown = plain - dev
        out = ALERTS.plain_error(head, limit=10 ** 7)
        if out != dev_out:
            if out != named or marked != dev_out:
                return [f"output {out!r} is neither pass"]
            shown |= branch - dev - kept_at
        return [text[i] for i in sorted(shown)]

    def test_a_text_up_to_the_cap_is_not_cut(self):
        self.assertGreaterEqual(ALERTS._HEAD, 1000)             # well above a real error and the 240 shown
        for raw in ("x" * ALERTS._HEAD, filler(ALERTS._HEAD + 1).strip(), "  " + "a b " * 500):
            with self.subTest(raw=raw[:20]):
                self.assertEqual(raw.split(), self.check(raw).split())
                self.assertNotIn("…", ALERTS.plain_error(raw, limit=10 ** 7))

    def test_a_kept_name_near_the_start_of_a_long_text_is_kept(self):
        """The cut comes before names are set aside, and both passes read the
        same head: a kept name in it stays in the output."""
        raw = (f'volumes.longhorn.io "{VOLUME}" is forbidden in API group "longhorn.io": '
               + filler(3 * ALERTS._HEAD))
        self.check(raw)
        for limit in (160, 240):
            said = ALERTS.plain_error(raw, limit=limit)
            self.assertIn(f'"{VOLUME}"', said)
            self.assertIn('API group "longhorn.io"', said)
            self.assertTrue(said.endswith("…"), said)

    def test_what_crosses_the_cut_stays_hidden(self):
        """Each text above and each of _PLAIN's secrets, placed so the cut
        falls at every character of it and just around it."""
        naive = 0
        texts = self.ACROSS + SecretsStayHiddenTests.HIDDEN
        for secret in texts:
            for into in range(-2, len(secret) + 2):
                raw = filler(ALERTS._HEAD - into) + secret + " tail" * 20
                with self.subTest(secret=secret[:30], into=into):
                    self.check(raw)
                text = " ".join(raw.split())
                if self.shows(text, text[:max(text.rfind(" ", 0, ALERTS._HEAD + 1), 0)]):
                    naive += 1
        # A cut back to the last space alone shows what the whole text hides
        # (the address rule's quotes, Bearer): these texts reach that.
        # 95 of 2855 when written.
        self.assertGreaterEqual(naive, 50)

    def test_generated_long_texts_show_nothing_the_whole_text_hides(self):
        """400 texts of 2,000 to 2,400 characters of the generated test's
        pieces, seeded, so repeatable."""
        import random
        rng = random.Random(1495)
        pieces = (self.ACROSS + SecretsStayHiddenTests.HIDDEN + list(NamesAreKeptTests.PLAIN)
                  + ["Bearer", "host=", "server='", 'address="', '"', "'", "://", "x://", "[", "|", "Cookie:",
                     "Authorization:", "token:", "password=", "<hidden>", VOLUME, '"longhorn.io"', "\ue000", "word"])
        cut = shortened = 0
        for _ in range(400):
            size, parts = rng.randint(ALERTS._HEAD, ALERTS._HEAD + 400), []
            while sum(map(len, parts)) < size:
                parts.append(rng.choice(pieces) + rng.choice([" ", " ", "", "-", ".", ":", "=", '"', "'"]))
            raw = "".join(parts)
            text = " ".join(raw.split())
            with self.subTest(raw=raw[-60:]):
                head = self.check(raw)
                cut += len(head) < len(text)
                shortened += len(head) < text.rfind(" ", 0, ALERTS._HEAD + 1)
        # Every text is cut; some heads end before the last space in reach
        # (an open host=" or a Bearer). Seeded: 400 and 59 when written.
        self.assertGreaterEqual(cut, 350)
        self.assertGreaterEqual(shortened, 25)

    def test_a_long_text_is_made_plain_quickly(self):
        """Joined words make the first rule's time grow with the square of
        their length: 100,000 characters took about 20 seconds, 1,000,000
        would take about a hundred times that. Now under a second; also with
        a name kept (two passes on the head), and as an HTTP reason of the
        most http.client reads (65,536 bytes)."""
        import time
        for size in (10 ** 5, 10 ** 6):
            for unit in ("a.", "a-", "pvc-", "eyJ.", "a1-", "a." * 999 + " "):
                raw = (unit * (size // len(unit) + 1))[:size]
                start = time.perf_counter()
                ALERTS.plain_error(raw)
                self.assertLess(time.perf_counter() - start, 1.0, (unit[:8], size))
        raw = f"volume {VOLUME} " + ("a." * 999 + " ") * 500
        start = time.perf_counter()
        said = ALERTS.plain_error(raw, limit=10 ** 7)
        self.assertLess(time.perf_counter() - start, 1.0)
        self.assertTrue(said.startswith(f"volume {VOLUME} "), said[:80])
        start = time.perf_counter()
        said = ALERTS.read_failure(urllib.error.HTTPError("https://example.com/x", 500, "a." * 32768, {}, None))
        self.assertLess(time.perf_counter() - start, 1.0)
        self.assertTrue(said.startswith("the cluster's API answered 500"), said)


if __name__ == "__main__":
    unittest.main()
