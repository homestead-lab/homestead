import json, sys, unittest, urllib.error
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_pullwatch as PULL
import homestead_update_registry as REGISTRY
import homestead_imports as IMP
import homestead_image_cache as IMAGES
import homestead_operations as OPS

A, B, C = "a" * 64, "b" * 64, "c" * 64
LAYERS = [{"digest": f"sha256:{A}", "size": 100 * 1024 ** 2}, {"digest": f"sha256:{B}", "size": 50 * 1024 ** 2},
          {"digest": f"sha256:{C}", "size": 50 * 1024 ** 2}]


class WatchParseTests(unittest.TestCase):
    """A pull reported only Pulling, for minutes; containerd knows the bytes."""

    def test_layers_held_and_those_being_fetched_make_the_count(self):
        # As containerd 2 (k3s v1.37) prints it: the layer, its bytes so far
        # (decimal units), how long - tab-separated.
        log = "\n".join([
            "== 1700000000", f"HAVE sha256:{A}",
            f"layer-sha256:{B}\t25MB\t7 seconds\t",
            "== 1700000002", f"HAVE sha256:{A}",                     # still being written
        ])
        done, total = PULL.parse(log, LAYERS)
        self.assertEqual((100 * 1024 ** 2 + 25 * 1000 ** 2, 200 * 1024 ** 2), (done, total))

    def test_the_bytes_so_far_are_read_from_containerds_own_lines(self):
        """Lines from a real pull on k3s v1.37 (containerd 2.3): one size, then an age."""
        for line, expected in ((f"layer-sha256:{A}\t3.146MB\t1 second\t", 3146000),
                               (f"layer-sha256:{A}\t74.45MB\t34 seconds\t", 74450000),
                               (f"layer-sha256:{A}\t512kB\t2 seconds\t", 512000)):
            log = "== 1\n" + line + "\n== 2\n"
            self.assertEqual(expected, PULL.parse(log, LAYERS)[0], line)

    def test_a_finished_watch_is_everything(self):
        self.assertEqual((200 * 1024 ** 2,) * 2, PULL.parse("== 1\nHAVE x\n== done\n", LAYERS))

    def test_nothing_yet_is_nothing(self):
        self.assertEqual((0, 200 * 1024 ** 2), PULL.parse("", LAYERS))

    def test_the_watcher_asks_containerd_for_each_layer(self):
        script = PULL.script([f"sha256:{A}"])
        self.assertIn("content ls -q", script)
        self.assertNotIn("content info", script, "containerd 2 has no content info")
        self.assertIn(f"for d in sha256:{A}", script)
        self.assertIn("content active", script)

    def test_the_first_ask_starts_a_watcher_on_that_node(self):
        sent = []

        def kget(path):
            if path == "/api/v1/nodes/node2":
                return {"metadata": {"labels": {"kubernetes.io/arch": "amd64"}}}
            raise urllib.error.HTTPError(path, 404, "missing", {}, None)
        import tempfile
        PULL.bind(kget, lambda m, p, b=None, **k: sent.append((m, p, b)), lambda p: "", lambda image, arch: LAYERS,
                  data_dir=tempfile.mkdtemp())
        PULL._LAYERS.clear()
        with unittest.mock.patch("homestead_runtime.binaries", lambda: {"crictl": "/c", "ctr": "/t"}):
            result = PULL.progress("node2", "ghcr.io/blakeblackshear/frigate:stable")
        self.assertEqual({"percent": 0, "done_bytes": 0, "total_bytes": 200 * 1024 ** 2}, result)
        method, path, body = sent[0]
        self.assertEqual(("POST", "node2"), (method, body["spec"]["nodeName"]))
        self.assertEqual(["ALL"], body["spec"]["containers"][0]["securityContext"]["capabilities"]["drop"])


class RegistryLayerTests(unittest.TestCase):
    def test_an_index_is_followed_to_this_architectures_manifest(self):
        index = {"manifests": [{"digest": "sha256:arm", "platform": {"os": "linux", "architecture": "arm64"}},
                               {"digest": "sha256:amd", "platform": {"os": "linux", "architecture": "amd64"}}]}
        amd = {"layers": [{"digest": f"sha256:{A}", "size": 7}]}

        def fake(parsed, path, auths, accept="application/json"):
            return json.dumps(amd if path.endswith("sha256:amd") else index).encode(), {}
        with unittest.mock.patch.object(REGISTRY, "_registry_json", fake):
            self.assertEqual([{"digest": f"sha256:{A}", "size": 7}], REGISTRY.image_layers("nginx:1.27", "amd64"))


class TrayMessageTests(unittest.TestCase):
    def test_the_job_says_how_far_the_pull_has_got(self):
        OPS.deployment_progress = lambda ns, name: {
            "desired": 1, "ready": 0, "phase": "progressing",
            "pull": {"state": "pulling", "node": "node2", "seconds": 75, "percent": 42, "total_bytes": 300 * 1024 ** 2}}
        status, progress, message = OPS._deployment({"ref": {"namespace": "lab", "name": "frigate"}})
        self.assertEqual("running", status)
        self.assertIn("42% of 300 MiB", message)
        self.assertGreater(progress, 30)


class ScanTests(unittest.TestCase):
    """Kubernetes lists only each node's largest images."""

    def setUp(self):
        self.sent, self.pods, self.logs = [], [], {}
        node = {"metadata": {"name": "node1"}, "status": {"conditions": [{"type": "Ready", "status": "True"}],
                                                          "images": [{"names": ["big:1"], "sizeBytes": 10}]}}

        def kget(path):
            if path == "/api/v1/nodes":
                return {"items": [node]}
            if "labelSelector" in path and "image-scan" in path:
                return {"items": self.pods}
            # Groups that may not be installed are not: discovery says so.
            if path == "/apis":
                return {"groups": [{"name": "apps"}]}
            if path.count("/") == 2 and path.startswith("/apis/"):
                raise urllib.error.HTTPError(path, 404, "Not Found", {}, None)
            return {"items": []}
        IMP.kget, IMP.ksend, IMP.NS, IMP._cache = kget, lambda m, p, b=None, **k: self.sent.append((m, p, b)), "lab", {}
        import homestead_names as NAMES
        self.addCleanup(setattr, NAMES, "kget", NAMES.kget)
        NAMES.kget = kget
        IMAGES._SCANS.clear()
        # Scans saved to disk belong to the tests that ask for it.
        self.addCleanup(setattr, IMP, "SCAN_DIR", IMP.SCAN_DIR)
        IMP.SCAN_DIR = ""

    def test_a_scan_starts_a_pod_per_ready_node(self):
        with unittest.mock.patch("homestead_runtime.binaries", lambda: {"crictl": "/c", "ctr": "/t"}):
            IMAGES.start_image_scan()
        body = self.sent[0][2]
        self.assertEqual("node1", body["spec"]["nodeName"])
        self.assertIn("images -o json", body["spec"]["containers"][0]["command"][-1])

    def test_a_finished_scan_replaces_the_partial_list(self):
        self.pods = [{"metadata": {"name": "homestead-image-scan-x", "annotations": {"homestead.io/cache-node": "node1"}},
                      "spec": {"nodeName": "node1"}, "status": {"phase": "Succeeded"}}]
        listing = {"images": [{"repoTags": ["big:1"], "repoDigests": [], "size": "10"},
                              {"repoTags": ["small:2"], "repoDigests": [], "size": "5"}]}
        import types
        shim = types.ModuleType("homestead_shim")
        shim.raw_get = lambda path: json.dumps(listing)
        with unittest.mock.patch.dict(sys.modules, {"homestead_shim": shim}):
            report = IMAGES.image_cache()
            self.assertEqual([], self.sent, "looking removes nothing (#622)")
            IMAGES.tick()
        self.assertTrue(report["complete"])
        self.assertEqual({"big:1", "small:2"}, {i["name"] for i in report["images"]})
        self.assertTrue(any(m == "DELETE" for m, p, b in self.sent), "the leader's tick removes the finished pod")

    def test_forgetting_a_rollback_drops_the_kept_image(self):
        IMP.kget = lambda path: {"metadata": {"annotations": {"homestead.io/update-previous": "{}"}}}
        IMAGES.forget_rollback("lab", "frigate")
        method, path, body = self.sent[0]
        self.assertEqual({"homestead.io/update-previous": None}, body["metadata"]["annotations"])


import unittest.mock  # noqa: E402

if __name__ == "__main__":
    unittest.main()
