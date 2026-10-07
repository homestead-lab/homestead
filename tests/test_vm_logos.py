import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import homestead_compose as COMPOSE
import homestead_logos as LOGOS


class OsLogoTests(unittest.TestCase):
    def test_what_guest_agents_and_labels_say(self):
        cases = {
            "ubuntu": "ubuntu", "Ubuntu 24.04.1 LTS": "ubuntu", "mswindows": "windows",
            "Microsoft Windows Server 2022 Datacenter": "windows", "Debian GNU/Linux 12 (bookworm)": "debian",
            "Rocky Linux 9.4 (Blue Onyx)": "rockylinux", "AlmaLinux 9.4": "almalinux", "Home Assistant OS 13.2": "homeassistant",
            "openSUSE Leap 15.6": "opensuse", "Linux Mint 22": "linuxmint", "Pop!_OS 22.04 LTS": "popos",
            "CentOS Stream 9": "centos", "Red Hat Enterprise Linux 9": "redhat", "alpine": "alpinelinux",
            "TrueNAS SCALE": "truenas", "OPNsense 24.7": "opnsense", "Fedora Linux 40": "linux", "": "",
            "something else": "",
        }
        for text, key in cases.items():
            with self.subTest(text=text):
                self.assertEqual(key, LOGOS.os_logo(text))

    def test_the_first_name_that_says_something_wins_over_blanks(self):
        self.assertEqual("ubuntu", LOGOS.os_logo("", None, "ubuntu"))

    def test_every_os_has_its_logo_and_a_credit(self):
        credits = (ROOT / "web" / "assets" / "OS-LOGOS.md").read_text()
        for key in LOGOS.OS_KEYS:
            with self.subTest(key=key):
                svg = (ROOT / "web" / "assets" / f"os-{key}.svg").read_text()
                self.assertTrue(svg.startswith("<svg"))
                self.assertNotIn("<script", svg)
                self.assertIn(f"os-{key}.svg", credits)

    def test_store_images_name_their_os(self):
        for store_id, distro, key in (("ubuntu-24.04", "Ubuntu", "ubuntu"), ("rocky-9", "Rocky Linux", "rockylinux"),
                                      ("opensuse-leap-15.6", "openSUSE", "opensuse"), ("alpine", "Alpine", "alpinelinux"),
                                      ("arch", "Arch Linux", "archlinux"), ("debian-12", "Debian", "debian")):
            self.assertEqual(key, LOGOS.os_logo(distro, store_id))


class ChartLogoTests(unittest.TestCase):
    APPS = [{"name": "Grafana", "repo": "grafana/grafana", "icon": "https://example.com/grafana-store.png"}]

    def test_the_charts_own_logo_comes_first(self):
        chart = {"name": "grafana", "icon": "https://example.com/grafana-chart.svg"}
        tiles = LOGOS.suggest(self.APPS, ["grafana/grafana:11"], "grafana", lambda a, t: a, chart=chart)
        self.assertEqual(("chart", "https://example.com/grafana-chart.svg"), (tiles[0]["match"], tiles[0]["icon"]))
        self.assertEqual("image", tiles[1]["match"])
        self.assertEqual("chart", LOGOS.best_guess(self.APPS, ["grafana/grafana"], "grafana", chart)["match"])

    def test_a_chart_icon_that_is_not_a_web_address_is_ignored(self):
        self.assertIsNone(LOGOS.chart_tile({"name": "x", "icon": "data:image/png;base64,AA"}))
        self.assertIsNone(LOGOS.chart_tile({"name": "x", "icon": "file:///etc/passwd"}))

    def test_missing_uses_each_apps_release(self):
        workloads = [{"ns": "monitoring", "name": "grafana", "images": ["example/other"], "helm_release": "grafana"},
                     {"ns": "lab", "name": "grafana", "images": ["example/other"], "helm_release": ""}]
        rows = LOGOS.missing(workloads, self.APPS, {("monitoring", "grafana"): {"name": "grafana", "icon": "https://example.com/c.svg"}})
        found = {(r["ns"], r["name"]): r["suggestion"] for r in rows}
        self.assertEqual("chart", found[("monitoring", "grafana")]["match"])
        self.assertEqual("name", found[("lab", "grafana")]["match"])


class SaveVmLogoTests(unittest.TestCase):
    def setUp(self):
        import server
        self.server, self.sent = server, []
        self.patches = [mock.patch.object(server, "ksend", side_effect=lambda *a, **k: self.sent.append((a, k))),
                        mock.patch.object(server.ICONS, "persist", side_effect=lambda src, d: "/api/icons/" + "a" * 64 + ".png")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_an_os_choice_an_image_and_back_to_automatic(self):
        self.server.set_vm_logos({"items": [{"ns": "lab", "name": "nas", "os": "truenas"},
                                            {"ns": "lab", "name": "ha", "icon": "https://example.com/ha.png"},
                                            {"ns": "lab", "name": "web", "icon": "", "os": ""}]})
        (method, path, body), kwargs = self.sent[0]
        self.assertEqual(("PATCH", "/apis/kubevirt.io/v1/namespaces/lab/virtualmachines/nas"), (method, path))
        self.assertEqual(["metadata"], list(body), "the VM's spec is untouched, so it does not restart")
        self.assertEqual("truenas", body["metadata"]["annotations"]["homestead.io/logo-os"])
        self.assertIsNone(body["metadata"]["annotations"]["homestead.io/icon"])
        self.assertTrue(self.sent[1][0][2]["metadata"]["annotations"]["homestead.io/icon"].startswith("/api/icons/"))
        self.assertEqual({"homestead.io/icon": None, "homestead.io/icon-source": None, "homestead.io/logo-os": None},
                         self.sent[2][0][2]["metadata"]["annotations"])

    def test_inputs_are_checked_before_anything_is_written(self):
        for items in ([], [{"ns": "lab", "name": "x", "os": "beos"}], [{"ns": "lab", "name": "../x", "os": "ubuntu"}],
                      [{"ns": "lab", "name": "x", "icon": "javascript:alert(1)"}]):
            with self.subTest(items=items), self.assertRaises(ValueError):
                self.server.set_vm_logos({"items": items})
        self.assertEqual([], self.sent)


class VmRowTests(unittest.TestCase):
    def vm(self, labels=None, annotations=None):
        return {"metadata": {"name": "web", "namespace": "lab", "labels": labels or {}, "annotations": annotations or {}},
                "spec": {"template": {"spec": {"domain": {"devices": {}}}}}, "status": {}}

    def row(self, vm, vmi=None):
        import homestead_vms as VMS
        return VMS._row(vm, vmi or {})

    def test_the_os_comes_from_the_agent_or_harvesters_label(self):
        self.assertEqual("ubuntu", self.row(self.vm(labels={"harvesterhci.io/os": "ubuntu"}))["os_logo"])
        vmi = {"status": {"guestOSInfo": {"id": "debian", "prettyName": "Debian GNU/Linux 12"}}}
        self.assertEqual("debian", self.row(self.vm(), vmi)["os_logo"])
        self.assertEqual("", self.row(self.vm())["os_logo"])

    def test_a_chosen_os_wins_and_an_unknown_one_is_ignored(self):
        row = self.row(self.vm(labels={"harvesterhci.io/os": "ubuntu"}, annotations={"homestead.io/logo-os": "truenas"}))
        self.assertEqual(("truenas", True), (row["os_logo"], row["logo_os_set"]))
        self.assertEqual("ubuntu", self.row(self.vm(labels={"harvesterhci.io/os": "ubuntu"},
                                                    annotations={"homestead.io/logo-os": "beos"}))["os_logo"])


class ComposeLogoTests(unittest.TestCase):
    def test_an_icon_label_with_a_web_address_is_the_logo(self):
        doc = """services:
  sonarr:
    image: lscr.io/linuxserver/sonarr
    labels:
      - net.unraid.docker.icon=https://example.com/sonarr.png
  radarr:
    image: lscr.io/linuxserver/radarr
    labels:
      homestead.icon: https://example.com/radarr.svg
  plain:
    image: nginx
    labels:
      homepage.icon: nginx.png
"""
        icons = {s["name"]: (s.get("config") or {}).get("icon", "") for s in COMPOSE.convert(doc)["services"]}
        self.assertEqual({"sonarr": "https://example.com/sonarr.png", "radarr": "https://example.com/radarr.svg", "plain": ""}, icons)


if __name__ == "__main__":
    unittest.main()
