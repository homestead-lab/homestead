"""Notifications: signing pushes, choosing where they go, and deciding what to say."""
import base64
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_alerts as alerts
import homestead_ecdsa as ec
import homestead_push as push
import server


def unb64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class EcdsaTests(unittest.TestCase):
    """RFC 6979 A.2.5: P-256 with SHA-256, deterministic nonces."""
    KEY = 0xC9AFA9D845BA75166B5C215767B1D6934E50C3DB36E89B127B8A622B120F6721

    def test_the_public_key_matches_the_rfc(self):
        self.assertEqual((0x60FED4BA255A9D31C961EB74C6356D68C049B8923B61FA6CE669622E60F29FB6,
                          0x7903FE1008B8BC99A41AE9E95628BC64F2F1B20C2D7E9F5177A3C294D4462299),
                         ec.public_key(self.KEY))

    def test_signatures_match_the_rfc(self):
        self.assertEqual((0xEFD48B2AACB6A8FD1140DD9CD45E81D69D2C877B56AAF991C34D0EA84EAF3716,
                          0xF7CB1C942D657C41D436C7A1B6E29F65F3E900DBB9AFF4064DC4AB2F843ACDA8),
                         ec.sign(self.KEY, b"sample"))
        self.assertEqual((0xF1ABB023518351CD71D881567B1EA663ED3EFCF6C5132B354F28D3B0B7D38367,
                          0x019F4113742A2B14BD25926B49C649155F267E60D3814B4C0CC84250E46F0083),
                         ec.sign(self.KEY, b"test"))

    def test_a_signature_is_for_its_own_message_only(self):
        point = ec.public_key(self.KEY)
        signature = ec.sign(self.KEY, b"sample")
        self.assertTrue(ec.verify(point, b"sample", signature))
        self.assertFalse(ec.verify(point, b"samplE", signature))
        self.assertFalse(ec.verify(point, b"sample", (signature[0], signature[1] ^ 1)))


class PushTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        push.bind(self.dir.name)
        push._key_cache.clear()
        self.addCleanup(push._key_cache.clear)

    def test_the_vapid_token_is_signed_for_the_push_service(self):
        endpoint = "https://fcm.googleapis.com/fcm/send/abc123"
        header = push.vapid_authorization(endpoint)
        token = header.split("t=", 1)[1].split(",", 1)[0]
        key = header.split("k=", 1)[1]
        head, body, sig = token.split(".")
        claims = json.loads(unb64(body))

        self.assertEqual("https://fcm.googleapis.com", claims["aud"])
        self.assertGreater(claims["exp"], time.time())
        self.assertLessEqual(claims["exp"], time.time() + 24 * 3600, "push services refuse more than a day")
        self.assertEqual({"typ": "JWT", "alg": "ES256"}, json.loads(unb64(head)))
        raw = unb64(key)
        point = (int.from_bytes(raw[1:33], "big"), int.from_bytes(raw[33:], "big"))
        signature = unb64(sig)
        self.assertEqual(65, len(raw))
        self.assertTrue(ec.verify(point, f"{head}.{body}".encode(),
                                  (int.from_bytes(signature[:32], "big"), int.from_bytes(signature[32:], "big"))))

    def test_the_key_survives_a_restart(self):
        first = push.public_key()
        push._key_cache.clear()
        self.assertEqual(first, push.public_key())

    def test_only_real_push_services_are_accepted(self):
        for endpoint in ["https://fcm.googleapis.com/fcm/send/x", "https://updates.push.services.mozilla.com/wpush/v2/x",
                         "https://web.push.apple.com/QG9", "https://wns2-par02p.notify.windows.com/w/?token=x"]:
            with self.subTest(endpoint):
                self.assertTrue(push.endpoint_allowed(endpoint))
        for endpoint in ["http://fcm.googleapis.com/fcm/send/x", "https://192.0.2.10/push",
                         "https://fcm.googleapis.com.evil.example/x", "https://user@fcm.googleapis.com/x",
                         "https://fcm.googleapis.com:8443/x", "https://kubernetes.default.svc/api", ""]:
            with self.subTest(endpoint):
                self.assertFalse(push.endpoint_allowed(endpoint))
        with self.assertRaises(ValueError):
            push.subscribe("me", {"endpoint": "https://10.0.0.1/"})

    def test_a_renewed_subscription_keeps_its_choices(self):
        push.subscribe("me", {"endpoint": "https://fcm.googleapis.com/a"}, ["outage"], "Phone")
        push.subscribe("me", {"endpoint": "https://fcm.googleapis.com/b"}, replaces="https://fcm.googleapis.com/a")

        rows = push._read()
        self.assertEqual(["https://fcm.googleapis.com/b"], [r["endpoint"] for r in rows])
        self.assertEqual(["outage"], rows[0]["categories"])
        self.assertEqual("Phone", rows[0]["device"])

    def test_nobody_can_replace_someone_elses_device(self):
        push.subscribe("alice", {"endpoint": "https://fcm.googleapis.com/a"})
        push.subscribe("mallory", {"endpoint": "https://fcm.googleapis.com/m"}, replaces="https://fcm.googleapis.com/a")
        push.unsubscribe("mallory", "https://fcm.googleapis.com/a")

        self.assertEqual({"alice", "mallory"}, {r["user"] for r in push._read()})

    def test_a_subscription_the_browser_dropped_is_forgotten(self):
        push.subscribe("me", {"endpoint": "https://fcm.googleapis.com/gone"}, ["outage"])
        push.subscribe("me", {"endpoint": "https://fcm.googleapis.com/live"}, ["outage"])
        push.subscribe("me", {"endpoint": "https://fcm.googleapis.com/quiet"}, ["updates"])
        posted = []

        def poster(endpoint, urgency):
            posted.append(endpoint)
            return 410 if endpoint.endswith("gone") else 201

        result = push.send(lambda row: "outage" in row["categories"], poster=poster)

        self.assertEqual({"sent": 1, "removed": 1}, {k: result[k] for k in ("sent", "removed")})
        self.assertNotIn("https://fcm.googleapis.com/quiet", posted)
        self.assertEqual({"https://fcm.googleapis.com/live", "https://fcm.googleapis.com/quiet"},
                         {r["endpoint"] for r in push._read()})


def node_down(name="h1"):
    return {"health_issues": [{"severity": "critical", "kind": "Node", "name": name, "reason": "node is NotReady"}]}


class AlertTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        alerts.bind(self.dir.name)
        self.now = 1_000_000.0

    def tick(self, health=None, jobs=None, seconds=20):
        self.now += seconds
        results = {"health": alerts.health_facts(health) if health is not None else None,
                   "jobs": alerts.job_facts(jobs) if jobs is not None else []}
        return alerts.observe(results, self.now)

    def test_a_problem_is_announced_once_it_has_lasted(self):
        self.tick({})
        self.assertEqual([], self.tick(node_down()))
        self.assertEqual([], self.tick(node_down()))
        fresh = self.tick(node_down(), seconds=60)

        self.assertEqual(["Node h1 is down"], [a["title"] for a in fresh])
        self.assertEqual("outage", fresh[0]["category"])
        self.assertEqual([], self.tick(node_down()), "announced once")

    def test_a_blip_is_not_announced(self):
        self.tick({})
        self.tick(node_down())
        self.assertEqual([], self.tick({}))
        self.assertEqual([], self.tick({}, seconds=120))
        self.assertEqual([], alerts.log()["alerts"])

    def test_its_end_is_announced_once_it_is_really_over(self):
        self.tick({})
        self.tick(node_down())
        self.tick(node_down(), seconds=90)
        self.assertEqual([], self.tick({}), "not yet: it could come straight back")
        fresh = self.tick({}, seconds=90)

        self.assertEqual([("resolved", "Node h1 is back")], [(a["phase"], a["title"]) for a in fresh])

    def test_not_being_able_to_look_is_not_everything_recovering(self):
        self.tick({})
        self.tick(node_down())
        self.tick(node_down(), seconds=90)
        for _ in range(10):
            self.assertEqual([], self.tick(None, seconds=60))
        self.assertEqual(1, len(alerts.active()))

    def test_history_is_not_announced_but_what_happens_next_is(self):
        old = {"id": "a1", "status": "failed", "title": "Update web", "message": "rollout failed"}
        self.assertEqual([], self.tick({}, jobs=[old]))
        new = {"id": "b2", "status": "failed", "title": "Restore db", "message": "no backup", "href": "/volumes"}
        fresh = self.tick({}, jobs=[old, new])

        self.assertEqual(["Failed: Restore db"], [a["title"] for a in fresh])
        self.assertEqual([], self.tick({}, jobs=[old, new]))
        self.assertEqual([], self.tick({}, jobs=[]), "a dismissed job is not news")

    def test_a_new_host_is_news_but_the_hosts_already_there_are_not(self):
        def node(name, uid, ready):
            return {"metadata": {"name": name, "uid": uid},
                    "status": {"conditions": [{"type": "Ready", "status": "True" if ready else "False"}]}}
        old = [node("harvester-1", "u1", True), node("harvester-2", "u2", True)]
        self.assertEqual([], alerts.observe({"joins": alerts.join_facts(old)}, self.now))
        registering = alerts.observe({"joins": alerts.join_facts(old + [node("harvester-3", "u3", False)])}, self.now + 1)
        ready = alerts.observe({"joins": alerts.join_facts(old + [node("harvester-3", "u3", True)])}, self.now + 2)

        self.assertEqual(["harvester-3 is joining the cluster"], [a["title"] for a in registering])
        self.assertEqual(["harvester-3 joined the cluster"], [a["title"] for a in ready])

    def test_a_source_added_by_an_upgrade_starts_quiet(self):
        alerts.observe({"jobs": []}, self.now)
        node = {"metadata": {"name": "harvester-1", "uid": "u1"}, "status": {"conditions": []}}
        self.assertEqual([], alerts.observe({"jobs": [], "joins": alerts.join_facts([node])}, self.now + 1))

    def test_a_newer_image_is_news_again(self):
        def report(digest):
            return {"workloads": [{"ns": "lab", "name": "web", "available": True,
                                   "images": [{"available": True, "remote_digest": digest, "candidate_tag": "1.2"}]}]}
        alerts.observe({"updates": alerts.update_facts(report("sha256:a"))}, self.now)
        self.assertEqual([], alerts.observe({"updates": alerts.update_facts(report("sha256:a"))}, self.now + 1))
        fresh = alerts.observe({"updates": alerts.update_facts(report("sha256:b"))}, self.now + 2)
        self.assertEqual(["Update for web"], [a["title"] for a in fresh])


class DeliveryTests(unittest.TestCase):
    """What a device's service worker is handed after a push wakes it."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        push.bind(self.dir.name)
        alerts.bind(self.dir.name)

    def test_each_device_sees_its_own_kinds_once(self):
        alerts.observe({"jobs": []}, 1)
        push.subscribe("me", {"endpoint": "https://fcm.googleapis.com/phone"}, ["jobs"])
        push.subscribe("me", {"endpoint": "https://fcm.googleapis.com/tablet"}, ["outage"])
        alerts.observe({"jobs": alerts.job_facts([{"id": "j", "status": "failed", "title": "Backup"}])}, 2)

        phone = server.alerts_pending("me", "https://fcm.googleapis.com/phone")
        self.assertEqual(["Failed: Backup"], [a["title"] for a in phone["alerts"]])
        self.assertEqual([], server.alerts_pending("me", "https://fcm.googleapis.com/phone")["alerts"])
        self.assertEqual([], server.alerts_pending("me", "https://fcm.googleapis.com/tablet")["alerts"])

    def test_a_device_does_not_answer_for_another_user(self):
        push.subscribe("alice", {"endpoint": "https://fcm.googleapis.com/a"}, ["jobs"])
        self.assertFalse(server.alerts_pending("bob", "https://fcm.googleapis.com/a")["known"])

    def test_a_new_device_starts_from_now(self):
        alerts.observe({"jobs": []}, 1)
        alerts.observe({"jobs": alerts.job_facts([{"id": "j", "status": "failed", "title": "Old"}])}, 2)
        push.subscribe("me", {"endpoint": "https://fcm.googleapis.com/new"}, ["jobs"],
                       cursor=alerts.log(limit=0)["latest"])
        self.assertEqual([], server.alerts_pending("me", "https://fcm.googleapis.com/new")["alerts"])

    def test_a_test_reaches_only_the_device_that_asked(self):
        for name in ("one", "two"):
            push.subscribe("me", {"endpoint": f"https://fcm.googleapis.com/{name}"}, ["outage"])
        alerts.note({"key": "test:1", "category": "test", "title": "Test", "to": push.tag("https://fcm.googleapis.com/one")})

        self.assertEqual(["Test"], [a["title"] for a in server.alerts_pending("me", "https://fcm.googleapis.com/one")["alerts"]])
        self.assertEqual([], server.alerts_pending("me", "https://fcm.googleapis.com/two")["alerts"])

    def test_pushes_skip_devices_of_users_who_are_gone(self):
        push.subscribe("gone", {"endpoint": "https://fcm.googleapis.com/g"}, ["outage"])
        push.subscribe("here", {"endpoint": "https://fcm.googleapis.com/h"}, ["outage"])
        posted = []
        with mock.patch.object(server.AUTH, "list_users", return_value=[{"name": "here"}]), \
                mock.patch.object(push, "_post", side_effect=lambda e, u: posted.append((e, u)) or 201):
            server.push_alerts([{"category": "outage", "severity": "critical", "phase": "raised"}])

        self.assertEqual([("https://fcm.googleapis.com/h", "high")], posted)


class RouteTests(unittest.TestCase):
    def test_the_app_shell_files_need_no_session(self):
        for path in ("/sw.js", "/manifest.webmanifest", "/icons/icon-192.png", "/icons/maskable-512.png"):
            with self.subTest(path):
                self.assertTrue(server.is_public_path(path))
        self.assertFalse(server.is_public_path("/icons/../server.py"))
        self.assertFalse(server.is_public_path("/api/alerts"))

    def test_anyone_signed_in_manages_their_own_devices(self):
        for path in ("/api/push/subscribe", "/api/push/unsubscribe", "/api/push/test", "/api/alerts/pending"):
            with self.subTest(path):
                self.assertEqual("viewer", server.needed_role(path, "POST"))

    def test_each_release_brings_a_new_worker(self):
        """A browser updates its worker only when the file changes; the version is that change."""
        import re
        root = Path(__file__).resolve().parents[1]
        worker = re.search(r'const VERSION = "([^"]+)"', (root / "web" / "sw.js").read_text(encoding="utf-8"))
        release = re.search(r'"HOMESTEAD_VERSION", "([^"]+)"', (root / "server" / "server.py").read_text(encoding="utf-8"))
        self.assertEqual(release.group(1), worker.group(1))

    def test_every_icon_the_manifest_names_exists(self):
        web = Path(__file__).resolve().parents[1] / "web"
        manifest = json.loads((web / "manifest.webmanifest").read_text(encoding="utf-8"))
        for icon in manifest["icons"]:
            self.assertTrue((web / icon["src"].lstrip("/")).is_file(), icon["src"])
        self.assertIn("maskable", {i["purpose"] for i in manifest["icons"]})


if __name__ == "__main__":
    unittest.main()
