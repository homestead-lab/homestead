"""Certificate and key expiry: the certificate reader, the sources, the scan,
its routes and its alerts. Every certificate here is a throwaway self-signed
one for example.com; no key was kept."""
import base64
import json
import random
import sys
import tempfile
import time
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


def secret(ns, name, crt, key="PRIVATE-KEY-MARKER", **meta):
    return {"metadata": {"namespace": ns, "name": name, **meta}, "type": "kubernetes.io/tls",
            "data": {"tls.crt": b64(crt), "tls.key": b64(key)}}


def pages(*batches):
    """KUBE.get answers: one list page per batch, each pointing at the next."""
    answers = []
    for i, batch in enumerate(batches):
        answers.append({"items": batch, "metadata": {"continue": f"c{i}" if i < len(batches) - 1 else ""}})
    return mock.Mock(side_effect=answers)


def webhook(name, *bundles, **meta):
    return {"metadata": {"name": name, **meta},
            "webhooks": [{"clientConfig": {"caBundle": b64(b)}} for b in bundles]}


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def certificate(ns, name, not_after, renewal=None, ready=True, reason="Ready"):
    status = {"conditions": [{"type": "Ready", "status": "True" if ready else "False", "reason": reason}]}
    if not_after:
        status["notAfter"] = iso(not_after)
    if renewal:
        status["renewalTime"] = iso(renewal)
    return {"metadata": {"namespace": ns, "name": name}, "spec": {"secretName": name + "-tls"}, "status": status}


class StateTests(unittest.TestCase):
    def test_the_windows(self):
        now = 1_800_000_000
        self.assertEqual(EXPIRY.state_for(now + 30 * DAY, now), "ok")
        self.assertEqual(EXPIRY.state_for(now + 21 * DAY, now), "warn")
        self.assertEqual(EXPIRY.state_for(now + 7 * DAY, now), "critical")
        self.assertEqual(EXPIRY.state_for(now, now), "expired")
        self.assertEqual(EXPIRY.state_for(now + 2 * DAY, now, renews=True), "renews")
        self.assertEqual(EXPIRY.state_for(now - 1, now, renews=True), "expired")
        self.assertEqual(EXPIRY.state_for(now + 60 * DAY, now, overdue=True), "warn")

    def test_days_count_whole_days_either_side(self):
        now = 1_800_000_000
        self.assertEqual(EXPIRY._row("tls-secret", "lab", "a", now + 10.6 * DAY, now)["days_left"], 10)
        self.assertEqual(EXPIRY._row("tls-secret", "lab", "a", now - 3.1 * DAY, now)["days_left"], -3)

    def test_rotators_are_recognised(self):
        year = 365 * DAY
        self.assertTrue(EXPIRY.self_rotating({}, 2 * DAY))
        self.assertFalse(EXPIRY.self_rotating({}, year))
        self.assertTrue(EXPIRY.self_rotating({"annotations": {"certificates.kubevirt.io/secret": "x"}}, year))
        self.assertTrue(EXPIRY.self_rotating({"labels": {"cdi.kubevirt.io": ""}}, year))
        self.assertTrue(EXPIRY.self_rotating({"annotations": {"auth.openshift.io/certificate-not-after": "x"}}, year))
        self.assertTrue(EXPIRY.self_rotating({"ownerReferences": [{"kind": "CDI"}]}, year))
        self.assertTrue(EXPIRY.self_rotating({"labels": {"app.kubernetes.io/managed-by": "virt-operator"}}, year))
        self.assertFalse(EXPIRY.self_rotating({"labels": {"app": "kubevirt.io-lookalike"}}, year))


class TlsSecretTests(unittest.TestCase):
    def test_only_dates_and_names_leave_and_the_key_never(self):
        get = pages([secret("lab", "web-tls", LONG)])
        with mock.patch.object(EXPIRY.KUBE, "get", get):
            rows, partial = EXPIRY.tls_secrets(LONG_AFTER - 10 * DAY, set())
        self.assertFalse(partial)
        self.assertEqual(rows[0]["state"], "warn")
        self.assertEqual(rows[0]["days_left"], 10)
        self.assertEqual(set(rows[0]), set(EXPIRY.ROW_FIELDS))
        text = json.dumps(rows)
        for leak in ("PRIVATE-KEY-MARKER", b64("PRIVATE-KEY-MARKER"), "BEGIN", "example.com"):
            self.assertNotIn(leak, text)
        self.assertIn("type%3Dkubernetes.io%2Ftls", get.call_args_list[0].args[0])

    def test_cert_manager_secrets_are_left_to_their_certificate(self):
        get = pages([secret("lab", "a", LONG, annotations={"cert-manager.io/certificate-name": "a"}),
                     secret("lab", "b-tls", LONG)])
        with mock.patch.object(EXPIRY.KUBE, "get", get):
            rows, _ = EXPIRY.tls_secrets(LONG_AFTER - 100 * DAY, {("lab", "b-tls")})
        self.assertEqual(rows, [])

    def test_a_short_lived_certificate_renews_itself_until_it_expires(self):
        with mock.patch.object(EXPIRY.KUBE, "get", pages([secret("virt", "ca", SHORT)])):
            rows, _ = EXPIRY.tls_secrets(SHORT_AFTER - DAY, set())
        self.assertEqual(rows[0]["state"], "renews")
        with mock.patch.object(EXPIRY.KUBE, "get", pages([secret("virt", "ca", SHORT)])):
            rows, _ = EXPIRY.tls_secrets(SHORT_AFTER + 1, set())
        self.assertEqual(rows[0]["state"], "expired")

    def test_an_unreadable_certificate_is_unknown(self):
        with mock.patch.object(EXPIRY.KUBE, "get", pages([secret("lab", "odd", "not a certificate")])):
            rows, _ = EXPIRY.tls_secrets(LONG_AFTER - 100 * DAY, set())
        self.assertEqual(rows[0]["state"], "unknown")
        self.assertIn("could not read", rows[0]["note"])

    def test_pages_are_followed_and_the_count_is_bounded(self):
        get = pages([secret("lab", "a", LONG)], [secret("lab", "b", LONG)])
        with mock.patch.object(EXPIRY.KUBE, "get", get):
            rows, partial = EXPIRY.tls_secrets(LONG_AFTER - 100 * DAY, set())
        self.assertEqual([r["name"] for r in rows], ["a", "b"])
        self.assertIn("continue=c0", get.call_args_list[1].args[0])
        self.assertFalse(partial)
        endless = mock.Mock(side_effect=lambda path: {"items": [secret("lab", "x", LONG)] * EXPIRY.PAGE,
                                                       "metadata": {"continue": "more"}})
        with mock.patch.object(EXPIRY.KUBE, "get", endless):
            rows, partial = EXPIRY.tls_secrets(LONG_AFTER - 100 * DAY, set())
        self.assertTrue(partial)
        self.assertEqual(len(rows), EXPIRY.MAX_SECRETS)
        self.assertEqual(endless.call_count, EXPIRY.MAX_SECRETS // EXPIRY.PAGE)

    def test_a_list_without_items_is_an_error(self):
        with mock.patch.object(EXPIRY.KUBE, "get", mock.Mock(return_value={"kind": "Status"})):
            with self.assertRaises(ValueError):
                EXPIRY.tls_secrets(0, set())


class WebhookTests(unittest.TestCase):
    def test_a_bundle_counts_its_latest_certificate(self):
        hooks = [webhook("cdi-api-validate", SHORT + LONG)]
        with mock.patch.object(EXPIRY.KUBE, "items", side_effect=[hooks, []]):
            rows = EXPIRY.webhook_cas(SHORT_AFTER + DAY)
        self.assertEqual(rows[0]["not_after"], LONG_AFTER)
        self.assertEqual(rows[0]["state"], "ok")

    def test_operator_rotated_cas_are_quiet(self):
        hooks = [webhook("virt-api-validator", SHORT, annotations={"certificates.kubevirt.io/secret": "x"})]
        with mock.patch.object(EXPIRY.KUBE, "items", side_effect=[hooks, []]):
            rows = EXPIRY.webhook_cas(SHORT_AFTER - DAY)
        self.assertEqual(rows[0]["state"], "renews")
        self.assertEqual(rows[0]["kind"], "webhook-ca")

    def test_a_webhook_with_no_bundle_is_skipped(self):
        with mock.patch.object(EXPIRY.KUBE, "items", side_effect=[[{"metadata": {"name": "x"}, "webhooks": [{}]}], []]):
            self.assertEqual(EXPIRY.webhook_cas(0), [])


class CertManagerTests(unittest.TestCase):
    NOW = 1_800_000_000

    def read(self, *certs):
        with mock.patch.object(EXPIRY.KUBE, "items", return_value=list(certs)) as items:
            rows, owned = EXPIRY.certificates(self.NOW)
        self.assertEqual(items.call_args.kwargs.get("optional_group"), "cert-manager.io")
        return rows, owned

    def test_ready_with_a_renewal_ahead_renews_itself(self):
        rows, owned = self.read(certificate("apps", "wiki", self.NOW + 10 * DAY, renewal=self.NOW + 2 * DAY))
        self.assertEqual(rows[0]["state"], "renews")
        self.assertEqual(owned, {("apps", "wiki-tls")})

    def test_an_overdue_renewal_warns_even_far_from_expiry(self):
        rows, _ = self.read(certificate("apps", "wiki", self.NOW + 60 * DAY, renewal=self.NOW - 2 * DAY))
        self.assertEqual(rows[0]["state"], "warn")
        self.assertIn("overdue", rows[0]["note"])

    def test_not_ready_is_judged_by_its_dates(self):
        rows, _ = self.read(certificate("apps", "wiki", self.NOW + 5 * DAY, renewal=self.NOW + DAY,
                                        ready=False, reason="Failed"))
        self.assertEqual(rows[0]["state"], "critical")
        self.assertIn("Failed", rows[0]["note"])

    def test_not_issued_yet_is_unknown(self):
        rows, _ = self.read(certificate("apps", "new", None, ready=False))
        self.assertEqual(rows[0]["state"], "unknown")


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        patch = mock.patch.object(EXPIRY.SHARED, "DIR", self.dir.name)
        patch.start()
        self.addCleanup(patch.stop)

    def run_scan(self, now, get=None, items=None, peer=None):
        get = get or pages([secret("lab", "web-tls", LONG)])
        items = items or (lambda path, optional_group=None: [])
        peer = peer or (lambda host, port: EXPIRY.pem_certs(LONG)[0])
        with mock.patch.object(EXPIRY.KUBE, "get", get), mock.patch.object(EXPIRY.KUBE, "items", side_effect=items), \
                mock.patch.object(EXPIRY, "_peer_cert", side_effect=peer):
            return EXPIRY.scan(now)

    def test_a_scan_is_saved_and_holds_no_key(self):
        rep = self.run_scan(LONG_AFTER - 10 * DAY)
        self.assertEqual(rep["errors"], {})
        self.assertEqual(sorted(r["kind"] for r in rep["items"]), ["api-server", "tls-secret"])
        self.assertEqual(rep["counts"]["warn"], 2)
        self.assertEqual(rep["warn_days"], 21)
        saved = Path(self.dir.name, "expiry.json").read_text()
        self.assertEqual(json.loads(saved)["checked_at"], LONG_AFTER - 10 * DAY)
        self.assertNotIn("PRIVATE-KEY-MARKER", saved)
        self.assertNotIn(b64("PRIVATE-KEY-MARKER"), saved)

    def test_one_source_failing_leaves_the_others_and_says_so(self):
        def refuse(path):
            raise OSError("HTTP Error 403: Forbidden")
        rep = self.run_scan(LONG_AFTER - 100 * DAY, get=refuse)
        self.assertEqual(list(rep["errors"]), ["tls-secrets"])
        self.assertIn("403", rep["errors"]["tls-secrets"])
        self.assertEqual([r["kind"] for r in rep["items"]], ["api-server"])

    def test_the_api_server_handshake_failing_is_an_error(self):
        def away(host, port):
            raise OSError("timed out")
        rep = self.run_scan(LONG_AFTER - 100 * DAY, peer=away)
        self.assertIn("api-server", rep["errors"])

    def test_load_and_refresh_recount_the_days(self):
        self.run_scan(LONG_AFTER - 30 * DAY)
        rep = EXPIRY.refresh(EXPIRY.load(), LONG_AFTER - 5 * DAY)
        self.assertEqual({r["state"] for r in rep["items"]}, {"critical"})
        self.assertEqual(rep["counts"]["critical"], 2)

    def test_load_with_no_scan_is_none(self):
        self.assertIsNone(EXPIRY.load())


if __name__ == "__main__":
    unittest.main()
