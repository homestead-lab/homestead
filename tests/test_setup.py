"""The setup guide's memory: skips per person or for the cluster, never 'done'."""
import io
import json
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock
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

    def test_smb_and_lan_are_optional_cluster_steps_with_admin_only_skips(self):
        for step in ("lan", "smb"):
            with self.subTest(step=step):
                with self.assertRaises(PermissionError):
                    SETUP.skip(step, True, "operator", admin=False)
                SETUP.skip(step, True, "admin", admin=True)
                self.assertIn(step, SETUP.skips("viewer"))
                SETUP.skip(step, False, "admin", admin=True)
                self.assertNotIn(step, SETUP.skips("viewer"))
        self.assertNotIn("done", json.dumps(SETUP.load()))

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



class UniFiSetupStateTests(unittest.TestCase):
    def setUp(self):
        import server
        self.server = server
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        # Isolate the other guide observations; exercise the real IPAM loader
        # against a ConfigMap response, rather than mocking its tuple away.
        observations = [(SETUP, "DATA_DIR", directory.name), (server.PLATFORM, "detect", {}),
                        (server.LC, "quorum_report", {"total": 1, "members": [], "ready": 1, "can_lose": 0}),
                        (server.SELF_ADDRESS, "report", {"on_vip": False, "url": ""}),
                        (server.NETWORK, "registered", []), (server.DISKS, "inventory", {"nodes": {}}),
                        (server, "storage_classes", []), (server, "vm_network_details", []),
                        (server, "samba_state", {}), (server.LH, "backup_target", {}),
                        (server.OS_ROLLOUT, "settings", {}), (server.AUTH, "list_users", []),
                        (server.IMP, "list_sources", []), (server.API_KEYS, "list_keys", []),
                        (server.FLEET, "summary", {}), (server.HOST_CONSOLE, "inventory", {}),
                        (server.PUSH, "devices", [])]
        for obj, name, value in observations:
            patch = mock.patch.object(obj, name, value) if name == "DATA_DIR" else mock.patch.object(obj, name, return_value=value)
            patch.start(); self.addCleanup(patch.stop)
        patch = mock.patch.object(server, "cached", side_effect=lambda key, *_: {} if key in ("ov", "network") else [])
        patch.start(); self.addCleanup(patch.stop)

    def test_unifi_observes_saved_configuration_without_a_tuple_error_or_connection_probe(self):
        ipam = self.server.IPAM
        for saved, expected in (({}, False), ({"unifi": {}}, False), ({"unifi": {"url": ""}}, False),
                                ({"unifi": {"url": "https://unifi.example", "has_key": False}}, True)):
            with self.subTest(saved=saved), mock.patch.object(ipam, "kget", return_value={"metadata": {"resourceVersion": "7"},
                    "data": {ipam.DATA_KEY: json.dumps(saved)}}) as read, \
                    mock.patch.object(ipam, "ksend") as write, mock.patch.object(ipam, "_unifi_get") as probe:
                self.assertEqual({"done": expected, "applies": True}, self.server.setup_state("admin", "admin")["steps"]["unifi"])
                read.assert_called_once_with(f"/api/v1/namespaces/{ipam.NAMESPACE}/configmaps/{ipam._map()}")
                write.assert_not_called(); probe.assert_not_called()

    def test_ip_addresses_are_done_with_subnets_scanned_and_unifi_synced_when_connected(self):
        ipam = self.server.IPAM
        lan = {"id": "s1", "cidr": "192.0.2.0/24", "name": "LAN"}
        cases = (({}, False),                                                          # nothing yet
                 ({"subnets": [lan]}, False),                                          # added, not scanned
                 ({"subnets": [lan], "scans": {"192.0.2.0/24": {"at": 100}}}, True),   # scanned; no UniFi
                 ({"subnets": [lan], "scans": {"192.0.2.0/24": {"at": 100}},
                   "unifi": {"url": "https://unifi.example", "has_key": True}}, False), # UniFi connected, not synced
                 ({"subnets": [lan], "scans": {"192.0.2.0/24": {"at": 100}},
                   "unifi": {"url": "https://unifi.example", "has_key": True, "last_sync": 200}}, True))
        for saved, done in cases:
            with self.subTest(saved=saved), mock.patch.object(ipam, "kget", return_value={"metadata": {"resourceVersion": "7"},
                    "data": {ipam.DATA_KEY: json.dumps(saved)}}):
                step = self.server.setup_state("admin", "admin")["steps"]["ipam"]
                self.assertEqual(done, step["done"], step)
                self.assertEqual(len(saved.get("subnets") or []), len(step["subnets"]))

    def test_missing_ipam_configuration_is_an_unfinished_step_without_an_error(self):
        with mock.patch.object(self.server.IPAM, "kget", side_effect=urllib.error.HTTPError("/configmap", 404, "Not found", None, None)):
            self.assertEqual({"done": False, "applies": True}, self.server.setup_state("admin", "admin")["steps"]["unifi"])

    def test_unreadable_ipam_configuration_keeps_a_real_error_and_cannot_complete_the_step(self):
        for code in (403, 503):
            with self.subTest(code=code), mock.patch.object(self.server.IPAM, "kget",
                    side_effect=urllib.error.HTTPError("/configmap", code, "Unavailable", None, None)):
                step = self.server.setup_state("admin", "admin")["steps"]["unifi"]
                self.assertFalse(step["done"])
                self.assertTrue(step["applies"])
                self.assertIn(str(code), step["error"])
                self.assertNotIn("tuple", step["error"])


class NetworkSetupStateTests(unittest.TestCase):
    def setUp(self):
        import server
        self.server = server
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        observations = [(SETUP, "DATA_DIR", directory.name), (server.PLATFORM, "detect", {}),
                        (server.LC, "quorum_report", {"total": 1, "members": [], "ready": 1, "can_lose": 0}),
                        (server.SELF_ADDRESS, "report", {"on_vip": False, "url": ""}),
                        (server.NETWORK, "registered", []), (server.DISKS, "inventory", {"nodes": {}}),
                        (server, "storage_classes", []), (server.LH, "backup_target", {}),
                        (server.OS_ROLLOUT, "settings", {}), (server.AUTH, "list_users", []),
                        (server.IPAM, "load", ({}, None)), (server.IMP, "list_sources", []),
                        (server.API_KEYS, "list_keys", []), (server.FLEET, "summary", {}),
                        (server.HOST_CONSOLE, "inventory", {}), (server.PUSH, "devices", [])]
        for obj, name, value in observations:
            patch = mock.patch.object(obj, name, value) if name == "DATA_DIR" else mock.patch.object(obj, name, return_value=value)
            patch.start(); self.addCleanup(patch.stop)
        patch = mock.patch.object(server, "cached", side_effect=lambda key, *_: {} if key in ("ov", "network") else [])
        patch.start(); self.addCleanup(patch.stop)
        self.networks, self.samba = [], {"installed": False, "enabled": False}
        patch = mock.patch.object(server, "kget", side_effect=lambda _: {"items": self.networks})
        self.read = patch.start(); self.addCleanup(patch.stop)
        patch = mock.patch.object(server, "samba_state", side_effect=lambda: self.samba)
        self.smb_read = patch.start(); self.addCleanup(patch.stop)
        patch = mock.patch.object(server, "ksend")
        self.write = patch.start(); self.addCleanup(patch.stop)

    def state(self, role="admin"):
        state = self.server.setup_state("person", role)
        self.write.assert_not_called()
        return state["steps"]

    def test_only_lan_attachments_complete_the_step_and_removal_reopens_it(self):
        self.networks = [{"metadata": {"namespace": "default", "name": kind},
                          "spec": {"config": json.dumps({"type": kind})}}
                         for kind in ("bridge", "macvlan", "macvtap", "ptp")]
        step = self.state()["lan"]
        self.assertTrue(step["done"])
        self.assertEqual(["default/bridge", "default/macvlan", "default/macvtap"], [n["name"] for n in step["networks"]])
        self.assertEqual([(True, True), (False, True), (True, False)], [(n["vms"], n["containers"]) for n in step["networks"]])
        self.networks = self.networks[-1:]
        self.assertEqual({"done": False, "applies": True, "vms": [], "containers": [], "networks": []}, self.state()["lan"])

    def test_absent_multus_is_unconfigured_and_unreadable_networks_need_attention(self):
        for code in (404, 403, 503):
            with self.subTest(code=code):
                self.read.side_effect = urllib.error.HTTPError("/networks", code, "Unavailable", None, None)
                step = self.state()["lan"]
                self.assertFalse(step["done"])
                self.assertEqual(code != 404, "error" in step)
        self.read.side_effect = RuntimeError("network inventory unavailable")
        self.assertIn("error", self.state()["lan"])

    def test_smb_needs_an_installed_enabled_server_but_does_not_claim_client_access(self):
        for installed, enabled, expected in ((False, False, False), (False, True, False), (True, False, False), (True, True, True)):
            with self.subTest(installed=installed, enabled=enabled):
                self.samba = {"installed": installed, "enabled": enabled, "address": "192.0.2.50", "shares": 2, "ready": 0}
                step = self.state()["smb"]
                self.assertEqual(expected, step["done"])
                self.assertEqual("192.0.2.50", step["address"])
                self.assertEqual(2, step["shares"])
        self.samba["enabled"] = False
        self.assertFalse(self.state()["smb"]["done"])

    def test_smb_report_errors_or_api_failures_never_complete_the_step(self):
        self.samba = {"installed": True, "enabled": True, "error": "share inventory unavailable"}
        self.assertFalse(self.state()["smb"]["done"])
        self.assertEqual("share inventory unavailable", self.state()["smb"]["error"])
        self.smb_read.side_effect = urllib.error.HTTPError("/smb", 503, "Unavailable", None, None)
        self.assertIn("error", self.state()["smb"])

    def test_non_admins_do_not_get_cluster_steps_or_read_their_configuration(self):
        for role in ("operator", "viewer"):
            with self.subTest(role=role):
                steps = self.state(role)
                self.assertNotIn("lan", steps); self.assertNotIn("smb", steps)
                self.read.assert_not_called(); self.smb_read.assert_not_called()


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
