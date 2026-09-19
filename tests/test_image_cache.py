import json
import sys
import unittest
import urllib.error
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import harvui_imports as imports


class ImageCacheTests(unittest.TestCase):
    def setUp(self):
        self.active = "sha256:" + "a" * 64
        self.rollback = "sha256:" + "b" * 64
        self.unused = "sha256:" + "c" * 64
        self.nodes = [{"metadata": {"name": "node1"}, "status": {"images": [
            {"names": ["docker.io/library/nginx:1.27.0",
                       "docker.io/library/nginx@" + self.active], "sizeBytes": 100 * 1024**2},
            {"names": ["docker.io/library/nginx@" + self.rollback], "sizeBytes": 99 * 1024**2},
            {"names": ["example.test/unused@" + self.unused], "sizeBytes": 25 * 1024**2},
            {"names": ["docker.io/library/redis:7.2"], "sizeBytes": 40 * 1024**2},
            {"names": ["registry.k8s.io/pause@sha256:" + "d" * 64], "sizeBytes": 1 * 1024**2},
        ]}}]
        self.sent = []
        pods = [{
            "metadata": {"name": "web-abc", "namespace": "lab", "labels": {"app": "web"}},
            "spec": {"containers": [{"name": "nginx", "image": "nginx:1.27.0"},
                                      {"name": "redis", "image": "redis:7.2"}]},
            "status": {"containerStatuses": [
                {"name": "nginx", "imageID": "docker-pullable://docker.io/library/nginx@" + self.active},
                {"name": "redis", "imageID": ""},
            ]},
        }, {
            "metadata": {"name": "finished-job", "namespace": "lab"},
            "spec": {"containers": [{"name": "job", "image": "example.test/unused@" + self.unused}]},
            "status": {"phase": "Succeeded", "containerStatuses": [{
                "name": "job", "imageID": "example.test/unused@" + self.unused}]},
        }]
        deployments = [{
            "metadata": {"name": "web", "namespace": "lab", "annotations": {
                "harvui.io/update-previous": json.dumps({
                    "images": {"nginx": "docker.io/library/nginx@" + self.rollback},
                    "at": "2026-09-19T18:00:00Z",
                })}},
        }]

        def get(path):
            if path == "/api/v1/nodes":
                return {"items": self.nodes}
            if path == "/api/v1/pods":
                return {"items": pods}
            if path == "/apis/apps/v1/deployments":
                return {"items": deployments}
            raise AssertionError(path)

        imports.kget = get
        def send(method, path, body=None, **kwargs):
            if method == "DELETE":
                raise urllib.error.HTTPError(path, 404, "missing", {}, None)
            self.sent.append((method, path, body))
            return body
        imports.ksend = send

    def test_active_and_rollback_digests_are_protected(self):
        report = imports.image_cache()
        by_digest = {row["digest"]: row for row in report["images"] if row["digest"]}
        self.assertTrue(by_digest[self.active]["protected"])
        self.assertEqual("active", by_digest[self.active]["retained_by"][0]["reason"])
        self.assertTrue(by_digest[self.rollback]["protected"])
        self.assertEqual("rollback", by_digest[self.rollback]["retained_by"][0]["reason"])
        self.assertFalse(by_digest[self.unused]["protected"])
        self.assertEqual(3, report["protected"])

    def test_active_tag_without_runtime_digest_matches_canonical_alias(self):
        report = imports.image_cache()
        redis = next(row for row in report["images"] if row["name"] == "docker.io/library/redis:7.2")
        self.assertTrue(redis["protected"])
        self.assertEqual("redis", redis["retained_by"][0]["container"])

    def test_cleanup_revalidates_protection_and_builds_scoped_pod(self):
        with self.assertRaisesRegex(PermissionError, "protected by active"):
            imports.cleanup_image(self.active, ["node1"])
        result = imports.cleanup_image(self.unused, ["node1"])
        self.assertEqual(["node1"], result["nodes"])
        pod = self.sent[-1][2]
        self.assertEqual("node1", pod["spec"]["nodeName"])
        container = pod["spec"]["containers"][0]
        self.assertIn("example.test/unused@" + self.unused, container["command"])
        self.assertFalse(container["securityContext"]["allowPrivilegeEscalation"])
        self.assertEqual(["ALL"], container["securityContext"]["capabilities"]["drop"])

    def test_platform_images_cannot_be_cleaned(self):
        with self.assertRaisesRegex(PermissionError, "platform images"):
            imports.cleanup_image("sha256:" + "d" * 64, ["node1"])


if __name__ == "__main__":
    unittest.main()
