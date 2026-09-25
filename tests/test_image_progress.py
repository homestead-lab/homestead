import json, sys, unittest, urllib.error
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_pullwatch as PULL
import homestead_updates as UPDATES
import homestead_imports as IMP
import homestead_operations as OPS

A, B, C = "a" * 64, "b" * 64, "c" * 64
LAYERS = [{"digest": f"sha256:{A}", "size": 100 * 1024 ** 2}, {"digest": f"sha256:{B}", "size": 50 * 1024 ** 2},
          {"digest": f"sha256:{C}", "size": 50 * 1024 ** 2}]


class WatchParseTests(unittest.TestCase):
    """A pull reported only Pulling, for minutes; containerd knows the bytes."""

    def test_layers_held_and_those_being_fetched_make_the_count(self):
        log = "\n".join([
            "== 1700000000", f"HAVE sha256:{A}",
            f"layer-sha256:{B}   25.00 MiB  50.00 MiB  1 second ago",
            "== 1700000002", f"HAVE sha256:{A}",                     # still being written
        ])
        done, total = PULL.parse(log, LAYERS)
        self.assertEqual((125 * 1024 ** 2, 200 * 1024 ** 2), (done, total))

    def test_a_finished_watch_is_everything(self):
        self.assertEqual((200 * 1024 ** 2,) * 2, PULL.parse("== 1\nHAVE x\n== done\n", LAYERS))

    def test_nothing_yet_is_nothing(self):
        self.assertEqual((0, 200 * 1024 ** 2), PULL.parse("", LAYERS))

    def test_the_watcher_asks_containerd_for_each_layer(self):
        script = PULL.script([f"sha256:{A}"])
        self.assertIn(f"content info $d", script)
        self.assertIn(f"for d in sha256:{A}", script)
        self.assertIn("content active", script)

    def test_the_first_ask_starts_a_watcher_on_that_node(self):
        sent = []

        def kget(path):
            if path == "/api/v1/nodes/node2":
                return {"metadata": {"labels": {"kubernetes.io/arch": "amd64"}}}
            raise urllib.error.HTTPError(path, 404, "missing", {}, None)
        PULL.bind(kget, lambda m, p, b=None, **k: sent.append((m, p, b)), lambda p: "", lambda image, arch: LAYERS)
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
        with unittest.mock.patch.object(UPDATES, "_registry_json", fake):
            self.assertEqual([{"digest": f"sha256:{A}", "size": 7}], UPDATES.image_layers("nginx:1.27", "amd64"))


class TrayMessageTests(unittest.TestCase):
    def test_the_job_says_how_far_the_pull_has_got(self):
        OPS.deployment_progress = lambda ns, name: {
            "desired": 1, "ready": 0, "phase": "progressing",
            "pull": {"state": "pulling", "node": "node2", "seconds": 75, "percent": 42, "total_bytes": 300 * 1024 ** 2}}
        status, progress, message = OPS._deployment({"ref": {"namespace": "lab", "name": "frigate"}})
        self.assertEqual("running", status)
        self.assertIn("42% of 300 MB", message)
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
            return {"items": []}
        IMP.kget, IMP.ksend, IMP.NS, IMP._cache = kget, lambda m, p, b=None, **k: self.sent.append((m, p, b)), "lab", {}
        import homestead_names as NAMES
        self.addCleanup(setattr, NAMES, "kget", NAMES.kget)
        NAMES.kget = kget
        IMP._SCANS.clear()
        # Scans saved to disk belong to the tests that ask for it.
        self.addCleanup(setattr, IMP, "SCAN_DIR", IMP.SCAN_DIR)
        IMP.SCAN_DIR = ""

    def test_a_scan_starts_a_pod_per_ready_node(self):
        with unittest.mock.patch("homestead_runtime.binaries", lambda: {"crictl": "/c", "ctr": "/t"}):
            IMP.start_image_scan()
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
            report = IMP.image_cache()
        self.assertTrue(report["complete"])
        self.assertEqual({"big:1", "small:2"}, {i["name"] for i in report["images"]})
        self.assertTrue(any(m == "DELETE" for m, p, b in self.sent))

    def test_forgetting_a_rollback_drops_the_kept_image(self):
        IMP.kget = lambda path: {"metadata": {"annotations": {"homestead.io/update-previous": "{}"}}}
        IMP.forget_rollback("lab", "frigate")
        method, path, body = self.sent[0]
        self.assertEqual({"homestead.io/update-previous": None}, body["metadata"]["annotations"])


import unittest.mock  # noqa: E402

if __name__ == "__main__":
    unittest.main()
