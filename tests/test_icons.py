import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import harvui_icons as icons
import server


PNG = b"\x89PNG\r\n\x1a\n" + b"harvui-test-icon"


class IconCacheTests(unittest.TestCase):
    def test_persist_is_content_addressed_and_resolvable(self):
        with tempfile.TemporaryDirectory() as data_dir, \
                mock.patch.object(icons, "_download", return_value=(PNG, "image/png")):
            url = icons.persist("https://example.com/logo.png", data_dir)
            digest = hashlib.sha256(PNG).hexdigest()
            self.assertEqual(f"/api/icons/{digest}.png", url)
            path, mime = icons.resolve(url, data_dir)
            self.assertEqual("image/png", mime)
            self.assertEqual(PNG, Path(path).read_bytes())
            self.assertTrue(icons.data_url(url, data_dir).startswith("data:image/png;base64,"))

    def test_private_and_credentialed_sources_are_rejected(self):
        with mock.patch.object(icons.socket, "getaddrinfo", return_value=[
                (icons.socket.AF_INET, icons.socket.SOCK_STREAM, 6, "", ("192.0.2.2", 80))]):
            with self.assertRaisesRegex(ValueError, "public addresses"):
                icons._validate_public_url("http://internal.example/icon.png")
        with self.assertRaisesRegex(ValueError, "credentials"):
            icons._validate_public_url("https://user:pass@example.com/icon.png")

    def test_non_image_and_oversized_content_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "supported"):
            icons._sniff_mime(b"<svg><script>alert(1)</script></svg>")
        with tempfile.TemporaryDirectory() as data_dir, mock.patch.object(
                icons, "_download", side_effect=ValueError("logo is too large (maximum 256 KiB)")):
            with self.assertRaisesRegex(ValueError, "too large"):
                icons.persist("https://example.com/large.png", data_dir)

    def test_resolve_does_not_accept_traversal_or_unknown_extensions(self):
        with tempfile.TemporaryDirectory() as data_dir:
            for path in ("/api/icons/../../secret", "/api/icons/" + "a" * 64 + ".svg"):
                with self.assertRaises(FileNotFoundError):
                    icons.resolve(path, data_dir)

    def test_prepare_keeps_source_and_deployment_uses_cached_reference(self):
        cfg = {"name": "demo", "image": "nginx:alpine",
               "icon": "https://example.com/logo.png"}
        with mock.patch.object(server.ICONS, "persist", return_value="/api/icons/" + "a" * 64 + ".png"):
            server.persist_icon_config(cfg)
        dep, _ = server.build_deployment(cfg)
        annotations = dep["metadata"]["annotations"]
        self.assertEqual("/api/icons/" + "a" * 64 + ".png", annotations["harvui.io/icon"])
        self.assertEqual("https://example.com/logo.png", annotations["harvui.io/icon-source"])

    def test_appstore_template_carries_icon(self):
        cfg = server.template_to_cfg({
            "name": "Demo App", "repo": "example/demo:latest",
            "icon": "https://example.com/demo.png", "config": [],
        })
        self.assertEqual("https://example.com/demo.png", cfg["icon"])

    def test_cached_icon_requests_are_release_versioned(self):
        core = (ROOT / "web" / "js" / "core.js").read_text(encoding="utf-8")
        self.assertIn('icon.startsWith("/api/icons/")', core)
        self.assertIn('fetch(url, { credentials: "same-origin", cache: "no-store" })', core)
        self.assertIn("URL.createObjectURL(blob)", core)
        self.assertIn("loadAppIcons(host)", core)

    def test_only_content_addressed_icon_route_is_public(self):
        self.assertTrue(server.is_public_path("/api/icons/" + "a" * 64 + ".png"))
        self.assertFalse(server.is_public_path("/api/workloads"))
        self.assertFalse(server.is_public_path("/api/icons"))

    def test_workload_display_uses_persisted_bytes_not_remote_source(self):
        with tempfile.TemporaryDirectory() as data_dir, \
                mock.patch.object(icons, "_download", return_value=(PNG, "image/png")):
            reference = icons.persist("https://example.com/logo.png", data_dir)
            with mock.patch.object(server, "DATA_DIR", data_dir):
                rendered = server.display_icon({"harvui.io/icon": reference})
        self.assertTrue(rendered.startswith("data:image/png;base64,"))
        self.assertNotIn("example.com", rendered)


if __name__ == "__main__":
    unittest.main()
