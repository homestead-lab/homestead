"""The setup guide's memory: skips per person or for the cluster, never 'done'."""
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_setup as SETUP


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        SETUP.bind(self.dir)

    def test_a_cluster_step_is_skipped_for_everyone_and_only_by_an_admin(self):
        with self.assertRaises(PermissionError):
            SETUP.skip("backups", True, "kiosk", admin=False)
        SETUP.skip("backups", True, "ada", admin=True)
        self.assertEqual(["backups"], SETUP.skips("kiosk"))
        SETUP.skip("backups", False, "ada", admin=True)
        self.assertEqual([], SETUP.skips("kiosk"))

    def test_a_persons_own_step_is_skipped_for_them_alone(self):
        SETUP.skip("phone", True, "kiosk", admin=False)
        self.assertEqual(["phone"], SETUP.skips("kiosk"))
        self.assertEqual([], SETUP.skips("ada"))
        with self.assertRaises(ValueError):
            SETUP.skip("nonsense", True, "ada", admin=True)

    def test_lan_configuration_needs_both_workload_types(self):
        bridge = {"name": "default/lan", "lan": True, "vms": True, "containers": True}
        vm = {"name": "default/guests", "lan": True, "vms": True, "containers": False}
        container = {"name": "default/apps", "lan": True, "vms": False, "containers": True}
        for networks in ([], [vm], [container], [{**bridge, "lan": False}]):
            self.assertFalse(SETUP.lan_state(networks)["done"])
        self.assertTrue(SETUP.lan_state([bridge])["done"])
        state = SETUP.lan_state([vm, container])
        self.assertTrue(state["done"])
        self.assertEqual(["default/guests"], state["vms"])
        self.assertEqual(["default/apps"], state["containers"])
        self.assertFalse(SETUP.lan_state([])["done"], "removing configuration reopens the step")

    def test_lan_is_an_optional_cluster_step(self):
        with self.assertRaises(PermissionError):
            SETUP.skip("lan", True, "kiosk", admin=False)
        SETUP.skip("lan", True, "ada", admin=True)
        self.assertIn("lan", SETUP.skips("kiosk"))

    def test_hiding_is_each_persons_and_opening_happens_once(self):
        SETUP.hide("ada", True)
        self.assertTrue(SETUP.hidden("ada"))
        self.assertFalse(SETUP.hidden("bob"))
        self.assertFalse(SETUP.opened())
        SETUP.mark_opened()
        self.assertTrue(SETUP.opened())

    def test_a_cluster_that_closed_the_old_welcome_counts_as_opened(self):
        Path(self.dir, "welcome.json").write_text(json.dumps({"done": True}))
        self.assertTrue(SETUP.opened(), "an upgraded cluster is not shown the guide unasked")

    def test_the_https_check_wants_https_and_homestead_answering(self):
        for bad in ("http://example.com", "https://user:pw@example.com", "https://example.com/?x=1", "ftp://x", ""):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                SETUP.https_check(bad, fetch=lambda target: io.BytesIO(b'{"ok": true}'))
        asked = []

        def fetch(target):
            asked.append(target)
            return io.BytesIO(b'{"ok": true, "read_only": false}')
        result = SETUP.https_check("https://homestead.example.com/", fetch=fetch)
        self.assertEqual(["https://homestead.example.com/healthz"], asked)
        self.assertEqual("https://homestead.example.com", result["url"])
        self.assertEqual("https://homestead.example.com", SETUP.load()["https_url"])
        with self.assertRaisesRegex(ValueError, "Homestead health response"):
            SETUP.https_check("https://other.example.com", fetch=lambda target: io.BytesIO(b"<html>"))

    def test_the_guide_never_stores_whether_a_step_is_done(self):
        SETUP.skip("disks", True, "ada", admin=True)
        SETUP.note("config_backup_at", 1700000000)
        self.assertNotIn("done", json.dumps(SETUP.load()))

    def test_guide_completion_is_per_user_and_does_not_pass_cluster_checks(self):
        SETUP.skip("backups", True, "ada", admin=True)
        SETUP.complete("ada")
        self.assertTrue(SETUP.completed("ada"))
        self.assertFalse(SETUP.completed("bob"))
        self.assertEqual(["backups"], SETUP.skips("ada"))
        self.assertNotIn("steps", SETUP.load())
        SETUP.bind(self.dir)
        self.assertTrue(SETUP.completed("ada"), "completion survives a restart")
        SETUP.complete("ada", False)
        self.assertFalse(SETUP.completed("ada"))



class DeployArgsTests(unittest.TestCase):
    """The tunnel's deploy: arguments to the image, as given, never a shell."""

    def test_args_reach_the_container_and_bad_ones_are_refused(self):
        import server
        dep, _ = server.build_deployment({"name": "cloudflared", "image": "cloudflare/cloudflared:latest",
                                          "args": ["tunnel", "--no-autoupdate", "run"], "network_mode": "internal"})
        self.assertEqual(["tunnel", "--no-autoupdate", "run"], dep["spec"]["template"]["spec"]["containers"][0]["args"])
        for bad in ("tunnel run", [1], ["x"] * 33, ["a" * 600]):
            with self.subTest(bad=str(bad)[:20]), self.assertRaises(ValueError):
                server.build_deployment({"name": "c", "image": "busybox", "args": bad})


if __name__ == "__main__":
    unittest.main()
