"""Published through a Cloudflare Tunnel: what the internet can and cannot reach."""
import base64
import hashlib
import io
import json
import random
import sys
import time
import unittest
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_auth as auth
import homestead_cfaccess as cfaccess
import server


def _probable_prime(bits, rng):
    while True:
        n = rng.getrandbits(bits) | (1 << (bits - 1)) | 1
        if all(n % p for p in (3, 5, 7, 11, 13, 17, 19, 23, 29, 31)) and _miller_rabin(n, rng):
            return n


def _miller_rabin(n, rng, rounds=20):
    d, r = n - 1, 0
    while d % 2 == 0:
        d, r = d // 2, r + 1
    for _ in range(rounds):
        x = pow(rng.randrange(2, n - 1), d, n)
        if x in (1, n - 1):
            continue
        for _ in range(r - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


RNG = random.Random(7)
E = 65537
while True:
    P, Q = _probable_prime(384, RNG), _probable_prime(384, RNG)
    PHI = (P - 1) * (Q - 1)
    if P != Q and PHI % E:
        break
N, D = P * Q, pow(E, -1, (P - 1) * (Q - 1))


def b64(data):
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def sign(claims, kid="k1", alg="RS256", key=(N, D)):
    head = b64(json.dumps({"alg": alg, "kid": kid}).encode())
    body = b64(json.dumps(claims).encode())
    size = (key[0].bit_length() + 7) // 8
    digest = hashlib.sha256(f"{head}.{body}".encode()).digest()
    info = bytes.fromhex("3031300d060960864801650304020105000420") + digest
    padded = b"\x00\x01" + b"\xff" * (size - 3 - len(info)) + b"\x00" + info
    signature = pow(int.from_bytes(padded, "big"), key[1], key[0]).to_bytes(size, "big")
    return f"{head}.{body}.{b64(signature)}"


def claims(**changes):
    base = {"iss": "https://lab.cloudflareaccess.com", "aud": ["aud-tag"], "exp": time.time() + 600,
            "iat": time.time(), "email": "me@example.com"}
    base.update(changes)
    return base


KEYS = {"k1": (N, E)}


class AccessTokenTests(unittest.TestCase):
    def setUp(self):
        cfaccess.configure("lab.cloudflareaccess.com", "aud-tag")
        self.addCleanup(cfaccess.configure, "", "")

    def verify(self, token):
        return cfaccess.verify(token, key_for=KEYS.get)

    def test_a_token_access_signed_for_this_application_passes(self):
        self.assertEqual("me@example.com", self.verify(sign(claims()))["email"])

    def test_everything_else_is_refused(self):
        other = (_probable_prime(384, RNG) * _probable_prime(384, RNG), 3)
        cases = {
            "another application": sign(claims(aud=["someone-else"])),
            "another team": sign(claims(iss="https://evil.cloudflareaccess.com")),
            "expired": sign(claims(exp=time.time() - 3600)),
            "not valid yet": sign(claims(nbf=time.time() + 3600)),
            "unknown key": sign(claims(), kid="k9"),
            "not RS256": sign(claims(), alg="none"),
            "wrong key": sign(claims(), key=(N, D + 2)),
            "no token": "",
            "garbage": "a.b.c",
        }
        for name, token in cases.items():
            with self.subTest(name):
                with self.assertRaises(ValueError):
                    self.verify(token)

    def test_a_tampered_body_breaks_the_signature(self):
        head, _, sig = sign(claims()).split(".")
        forged = b64(json.dumps(claims(email="attacker@example.com")).encode())

        with self.assertRaises(ValueError):
            self.verify(f"{head}.{forged}.{sig}")


def handler(path="/", method="GET", headers=None, body=b""):
    h = object.__new__(server.H)
    h.path, h.command = path, method
    h.headers = {"Content-Length": str(len(body)), **(headers or {})}
    h.client_address = ("192.0.2.50", 40000)
    h.rfile = io.BytesIO(body)
    sent = []
    h._send = lambda code, payload, ctype="application/json": sent.append((code, payload))
    h._file = lambda *a, **k: sent.append((200, "file"))
    return h, sent


CF = {"Cf-Connecting-Ip": "203.0.113.9", "Cf-Ray": "8a1b2c3d4e5f-LHR"}


class TunnelTests(unittest.TestCase):
    def test_setup_is_not_offered_through_the_tunnel(self):
        h, sent = handler("/api/auth/setup", "POST", CF, b'{"username": "x", "password": "y"}')
        h.do_POST()
        self.assertEqual(403, sent[0][0])
        self.assertIn("LAN", sent[0][1]["error"])

    def test_a_request_without_access_is_refused_when_access_is_configured(self):
        cfaccess.configure("lab.cloudflareaccess.com", "aud-tag")
        self.addCleanup(cfaccess.configure, "", "")
        h, sent = handler("/", headers=CF)
        h.do_GET()
        self.assertEqual(403, sent[0][0])
        self.assertIn("Cloudflare Access", sent[0][1]["error"])

    def test_the_app_can_be_installed_without_an_access_token(self):
        cfaccess.configure("lab.cloudflareaccess.com", "aud-tag")
        self.addCleanup(cfaccess.configure, "", "")
        for path in ("/manifest.webmanifest", "/icons/icon-192.png"):
            with self.subTest(path):
                h, sent = handler(path, headers=CF)
                h.do_GET()
                self.assertEqual((200, "file"), sent[0])
        h, sent = handler("/sw.js", headers=CF)
        h.do_GET()
        self.assertEqual(403, sent[0][0], "the worker is fetched with Access's cookie like any page")

    def test_the_lan_is_not_asked_for_an_access_token(self):
        cfaccess.configure("lab.cloudflareaccess.com", "aud-tag")
        self.addCleanup(cfaccess.configure, "", "")
        h, sent = handler("/")
        h.do_GET()
        self.assertEqual((200, "file"), sent[0])

    def test_an_oversized_request_is_refused_before_it_is_read(self):
        h, sent = handler("/api/auth/login", "POST", {"Content-Length": str(64 * 1024 * 1024)})
        h.do_POST()
        self.assertEqual(413, sent[0][0])

    def test_the_client_address_is_cloudflares_word_not_the_clients(self):
        with mock.patch.object(server.CFACCESS, "enabled", return_value=True):
            h, _ = handler(headers={"X-Forwarded-For": "1.2.3.4", **CF})
            self.assertEqual("203.0.113.9", h._client_ip())
        h, _ = handler(headers={"X-Forwarded-For": "1.2.3.4"})
        self.assertEqual("192.0.2.50", h._client_ip(), "X-Forwarded-For is not trusted")

    def test_without_access_cloudflares_header_is_anyones_to_send(self):
        """Without Access nothing proves a request came through Cloudflare, so
        its address header would let anyone dodge the sign-in limit."""
        with mock.patch.object(server.CFACCESS, "enabled", return_value=False):
            h, _ = handler(headers=dict(CF))
            self.assertEqual("192.0.2.50", h._client_ip())


class ThrottleTests(unittest.TestCase):
    def setUp(self):
        auth._attempts.clear()
        self.addCleanup(auth._attempts.clear)

    def test_changing_address_does_not_reset_an_accounts_limit(self):
        for n in range(auth.MAX_USER_ATTEMPTS):
            auth._rate_hit(f"ip:10.0.0.{n}")
            auth._rate_hit("user:admin")

        self.assertFalse(auth._rate_ok("user:admin", auth.MAX_USER_ATTEMPTS))
        self.assertTrue(auth._rate_ok("ip:10.0.0.250"), "a fresh address alone would have passed")

    def test_the_record_of_attempts_cannot_grow_without_bound(self):
        for n in range(auth.MAX_TRACKED + 50):
            auth._rate_hit(f"ip:{n}")

        self.assertLessEqual(len(auth._attempts), auth.MAX_TRACKED + 1)


class HeaderTests(unittest.TestCase):
    def test_every_response_forbids_framing_and_foreign_scripts(self):
        h = object.__new__(server.H)
        h.headers = {}
        h.connection = None
        sent = []
        h.send_header = lambda name, value: sent.append((name, value))
        h._security_headers()
        headers = dict(sent)
        self.assertEqual("DENY", headers["X-Frame-Options"])
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertIn("script-src 'self' 'unsafe-inline'", headers["Content-Security-Policy"])
        self.assertEqual("nosniff", headers["X-Content-Type-Options"])
        self.assertNotIn("Strict-Transport-Security", headers, "not over plain HTTP")

    def test_behind_tls_browsers_are_told_to_stay_on_it(self):
        h = object.__new__(server.H)
        h.headers = {"X-Forwarded-Proto": "https"}
        h.connection = None
        sent = []
        h.send_header = lambda name, value: sent.append((name, value))
        h._security_headers()
        self.assertIn("Strict-Transport-Security", dict(sent))


if __name__ == "__main__":
    unittest.main()
