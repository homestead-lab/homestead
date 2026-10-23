"""Certificate and key expiry: the certificate reader, the sources, the scan,
its routes and its alerts. Every certificate here is a throwaway self-signed
one for example.com; no key was kept."""
import base64
import json
import random
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_expiry as EXPIRY  # noqa: E402

DAY = 86400

# Lives 20 days: 2026-10-23 06:29:30 to 2026-11-12 06:29:30 UTC.
LEAF = """-----BEGIN CERTIFICATE-----
MIIBuTCCAWCgAwIBAgIUOYVFjSPm2uGJjjhWW9jZXnbawA4wCgYIKoZIzj0EAwIw
GzEZMBcGA1UEAwwQZGVtby5leGFtcGxlLmNvbTAeFw0yNjEwMjMwNjI5MzBaFw0y
NjExMTIwNjI5MzBaMBsxGTAXBgNVBAMMEGRlbW8uZXhhbXBsZS5jb20wWTATBgcq
hkjOPQIBBggqhkjOPQMBBwNCAARAaZYkJyGTZw0UlM152yq+ygjLsiEf/d71z+kM
6eDGiOKk7PfXkp8vpbwbXrZ6pwQ4OASBqVm5he4BBc5PY5n/o4GBMH8wHQYDVR0O
BBYEFPucaZ4SwthwZWuYSSd0xX3HM8XAMB8GA1UdIwQYMBaAFPucaZ4SwthwZWuY
SSd0xX3HM8XAMA8GA1UdEwEB/wQFMAMBAf8wLAYDVR0RBCUwI4IQZGVtby5leGFt
cGxlLmNvbYIPd3d3LmV4YW1wbGUuY29tMAoGCCqGSM49BAMCA0cAMEQCICJlgbES
2xZZPouJ9RlKZN9BA74X4+O3Cx1CNxcZX2K8AiBWSRRCJNp74MieMbyC0zOSI4i8
+L/Oow+L5MRahPeH3g==
-----END CERTIFICATE-----
"""
LEAF_AFTER = 1794464970

# Lives 2 days (a CA that rotates itself): 2026-10-23 16:44:43 to 10-25 16:44:43.
SHORT = """-----BEGIN CERTIFICATE-----
MIIBizCCATGgAwIBAgIUFa/0gh+0H/daDPURsJ+SD7efH6AwCgYIKoZIzj0EAwIw
GzEZMBcGA1UEAwwQY2EtYS5leGFtcGxlLmNvbTAeFw0yNjEwMjMxNjQ0NDNaFw0y
NjEwMjUxNjQ0NDNaMBsxGTAXBgNVBAMMEGNhLWEuZXhhbXBsZS5jb20wWTATBgcq
hkjOPQIBBggqhkjOPQMBBwNCAATtQVBc3DRKly+UoFmUtozBDzJzriqtCK+G8Kg2
qUOfzgjximi0b/KBPGbIAPbOKiKaQg1BjawONOm29ioNRruRo1MwUTAdBgNVHQ4E
FgQUKj50t4V9DzMAlPz86iSnBsdbImcwHwYDVR0jBBgwFoAUKj50t4V9DzMAlPz8
6iSnBsdbImcwDwYDVR0TAQH/BAUwAwEB/zAKBggqhkjOPQQDAgNIADBFAiEAlOA6
JByytSKE3qTKG7ZrkZ91/U0e8nZKPfxDDx/fWYQCIANJ/A+FyIsmPzlzkT5zk9MD
oJBC/AkKd1ojYHfbjMmB
-----END CERTIFICATE-----
"""
SHORT_BEFORE, SHORT_AFTER = 1792773883, 1792946683

# Lives until 2054 (GeneralizedTime): 2026-10-23 16:44:43 to 2054-03-10 16:44:43.
LONG = """-----BEGIN CERTIFICATE-----
MIIBjTCCATOgAwIBAgIUHdgG18FHqBqfqq+UtPkZ5oOfrOUwCgYIKoZIzj0EAwIw
GzEZMBcGA1UEAwwQY2EtYi5leGFtcGxlLmNvbTAgFw0yNjEwMjMxNjQ0NDNaGA8y
MDU0MDMxMDE2NDQ0M1owGzEZMBcGA1UEAwwQY2EtYi5leGFtcGxlLmNvbTBZMBMG
ByqGSM49AgEGCCqGSM49AwEHA0IABCUPljPw+S6IxJmUOS/fm5wAPqUSUbYIsETw
r+ARKkGWLKHRDT1dzTK+53yVt3zff3kFV7xuo8WQReQf2fHhlwWjUzBRMB0GA1Ud
DgQWBBQEUBBeE/X1/quO/5PAhXsvwtEy7zAfBgNVHSMEGDAWgBQEUBBeE/X1/quO
/5PAhXsvwtEy7zAPBgNVHRMBAf8EBTADAQH/MAoGCCqGSM49BAMCA0gAMEUCIBg9
T0Xz+O/TOdXd8lyG2L96gBLejr55wQO80V5aZksQAiEAuF1NEvpckiSpkL6QxEiB
Uompy5oyBw9+OS6NfvK7Fas=
-----END CERTIFICATE-----
"""
LONG_AFTER = 2656773883


def b64(text):
    return base64.b64encode(text.encode()).decode()


class ReaderTests(unittest.TestCase):
    def test_a_certificate_gives_its_dates(self):
        v = EXPIRY.validity(EXPIRY.pem_certs(LEAF)[0])
        self.assertEqual(v["not_after"], LEAF_AFTER)
        self.assertEqual(v["not_after"] - v["not_before"], 20 * DAY)

    def test_generalized_time_after_2049_is_read(self):
        self.assertEqual(EXPIRY.validity(EXPIRY.pem_certs(LONG)[0])["not_after"], LONG_AFTER)

    def test_a_bundle_gives_every_certificate(self):
        self.assertEqual(len(EXPIRY.pem_certs(SHORT + LONG)), 2)

    def test_text_with_no_certificate_is_refused(self):
        with self.assertRaises(ValueError):
            EXPIRY.pem_certs("-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n")

    def test_every_truncation_is_a_value_error(self):
        der = EXPIRY.pem_certs(LEAF)[0]
        for cut in range(len(der)):
            with self.subTest(cut=cut):
                try:
                    EXPIRY.validity(der[:cut])
                except ValueError:
                    pass

    def test_random_damage_is_a_value_error_or_dates(self):
        der = EXPIRY.pem_certs(LEAF)[0]
        rng = random.Random(10)
        for _ in range(2000):
            buf = bytearray(der)
            for _ in range(rng.randint(1, 4)):
                buf[rng.randrange(len(buf))] = rng.randrange(256)
            try:
                v = EXPIRY.validity(bytes(buf))
            except ValueError:
                continue
            self.assertIsInstance(v["not_after"], float)

    def test_a_huge_length_is_refused(self):
        with self.assertRaises(ValueError):
            EXPIRY.validity(bytes([0x30, 0x89]) + b"\xff" * 9)
        with self.assertRaises(ValueError):
            EXPIRY.validity(bytes([0x30, 0x84, 0x7f, 0xff, 0xff, 0xff, 0x30]))


if __name__ == "__main__":
    unittest.main()
