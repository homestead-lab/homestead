import copy
import json
import sys
import tempfile
import time
import threading
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_updates as updates


DEPLOYMENT = {
    "metadata": {"name": "demo", "namespace": "lab", "generation": 2, "annotations": {}},
    "spec": {
        "replicas": 1,
        "selector": {"matchLabels": {"app": "demo"}},
        "template": {
            "metadata": {"labels": {"app": "demo"}},
            "spec": {"containers": [{"name": "demo", "image": "nginx:1.27.0"}]},
        },
    },
    "status": {"observedGeneration": 2, "replicas": 1, "updatedReplicas": 1,
               "readyReplicas": 1, "availableReplicas": 1, "unavailableReplicas": 0},
}


class ImageUpdateTests(unittest.TestCase):
    def setUp(self):
        self.dep = copy.deepcopy(DEPLOYMENT)
        self.sent = []
        self.tmp = tempfile.TemporaryDirectory()

        def get(path):
            if path.endswith("/deployments/demo"):
                return self.dep
            if path == "/api/v1/pods":
                return {"items": []}
            if "/serviceaccounts/" in path:
                return {"imagePullSecrets": []}
            raise AssertionError(path)

        def send(method, path, body, **kwargs):
            self.sent.append((method, path, copy.deepcopy(body)))
            self.dep = copy.deepcopy(body)
            return self.dep

        updates.bind(get, send, "lab", self.tmp.name)
        updates._CACHE.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def test_parses_docker_and_private_registry_references(self):
        docker = updates.parse_image("nginx:1.27")
        self.assertEqual("docker.io/library/nginx", docker["base"])
        self.assertEqual("registry-1.docker.io", docker["endpoint"])
        private = updates.parse_image("ghcr.io/example/app:v2.1.0")
        self.assertEqual("ghcr.io", private["registry"])
        self.assertEqual("example/app", private["repo"])

    def test_semver_stays_in_current_major(self):
        tags = ["1.9.1", "1.10.0", "2.0.0", "edge", "1.10.0-rc1"]
        self.assertEqual("1.10.0", updates.newer_semver("1.9.0", tags))
        self.assertIsNone(updates.newer_semver("latest", tags))

    def test_multiarch_child_digest_is_not_a_false_update(self):
        child = "sha256:" + "a" * 64
        pod = {"metadata": {"namespace": "lab", "labels": {"app": "demo"}},
               "status": {"containerStatuses": [{"name": "demo", "imageID": "repo@" + child}]}}
        original_tags, original_manifest = updates.registry_tags, updates.manifest_info
        original_creds = updates._secret_credentials
        try:
            updates.registry_tags = lambda *args, **kwargs: ["1.27.0"]
            updates.manifest_info = lambda *args, **kwargs: {
                "digest": "sha256:" + "b" * 64, "children": [child]}
            updates._secret_credentials = lambda *args: {}
            result = updates._check_deployment(self.dep, [pod])
            self.assertFalse(result["available"])
        finally:
            updates.registry_tags, updates.manifest_info = original_tags, original_manifest
            updates._secret_credentials = original_creds

    def test_stopped_workload_is_not_reported_as_a_failed_registry_check(self):
        """An import lands scaled to zero, so there is no digest to compare."""
        original_tags, original_manifest = updates.registry_tags, updates.manifest_info
        original_creds = updates._secret_credentials
        try:
            updates.registry_tags = lambda *args, **kwargs: ["1.27.0"]
            updates.manifest_info = lambda *args, **kwargs: {
                "digest": "sha256:" + "b" * 64, "children": []}
            updates._secret_credentials = lambda *args: {}

            stopped = copy.deepcopy(self.dep)
            stopped["spec"]["replicas"] = 0
            self.assertEqual("", updates._check_deployment(stopped, [])["images"][0]["error"])

            running = {"metadata": {"namespace": "lab", "labels": {"app": "demo"}},
                       "status": {"containerStatuses": [{"name": "demo", "imageID": ""}]}}
            item = updates._check_deployment(self.dep, [running])["images"][0]
            self.assertEqual("running image digest is not available yet", item["error"])
        finally:
            updates.registry_tags, updates.manifest_info = original_tags, original_manifest
            updates._secret_credentials = original_creds

    def test_scan_uses_shared_system_namespace_filter(self):
        user = copy.deepcopy(self.dep)
        system = copy.deepcopy(self.dep)
        system["metadata"].update({"namespace": "cattle-fleet-local-system", "name": "fleet"})

        def get(path):
            if path == "/apis/apps/v1/deployments":
                return {"items": [system, user]}
            if path == "/api/v1/pods":
                return {"items": []}
            raise AssertionError(path)

        original = updates._check_deployment
        try:
            updates.bind(get, lambda *args, **kwargs: None, "lab", self.tmp.name,
                         {"cattle-fleet-local-system"})
            updates._check_deployment = lambda dep, pods, force=False: {
                "ns": dep["metadata"]["namespace"], "name": dep["metadata"]["name"],
                "images": [], "available": False, "can_rollback": False,
                "last_action": "",
            }
            report = updates.scan()
        finally:
            updates._check_deployment = original

        self.assertEqual([("lab", "demo")],
                         [(x["ns"], x["name"]) for x in report["workloads"]])

    def test_apply_pins_digest_and_records_exact_rollback(self):
        digest = "sha256:" + "c" * 64
        original = updates._check_deployment
        try:
            updates._check_deployment = lambda *args, **kwargs: {
                "images": [{"container": "demo", "available": True,
                            "source": "docker.io/library/nginx:1.27.0",
                            "current_digest": "sha256:" + "d" * 64,
                            "candidate": "docker.io/library/nginx:1.28.0",
                            "remote_digest": digest}]}
            result = updates.apply_update("lab", "demo")
        finally:
            updates._check_deployment = original
        image = self.dep["spec"]["template"]["spec"]["containers"][0]["image"]
        self.assertEqual("docker.io/library/nginx@" + digest, image)
        previous = json.loads(self.dep["metadata"]["annotations"][updates.PREVIOUS])
        self.assertEqual("docker.io/library/nginx@sha256:" + "d" * 64,
                         previous["images"]["demo"])
        self.assertEqual("ready", result["phase"])

        updates.rollback("lab", "demo")
        restored = self.dep["spec"]["template"]["spec"]["containers"][0]["image"]
        self.assertEqual("docker.io/library/nginx@sha256:" + "d" * 64, restored)

    def test_an_init_container_on_the_same_image_moves_with_it(self):
        """Homestead's own data-permissions init container is built from the
        Homestead image: leaving it behind pins it to the release the workload
        was installed with, however many times the app is updated."""
        spec = self.dep["spec"]["template"]["spec"]
        spec["containers"] = [{"name": "homestead",
                               "image": "ghcr.io/wjcloudy/homestead:2.8.2",
                               "env": [{"name": "PORT", "value": "8080"},
                                       {"name": "HOMESTEAD_VERSION", "value": "2.8.2"}]}]
        spec["initContainers"] = [{"name": "data-permissions",
                                   "image": "ghcr.io/wjcloudy/homestead:2.8.2"},
                                  {"name": "wait", "image": "busybox:1.36"}]
        new_digest = "sha256:" + "e" * 64
        old_digest = "sha256:" + "f" * 64
        original = updates._check_deployment
        try:
            updates._check_deployment = lambda *args, **kwargs: {
                "images": [{"container": "homestead", "available": True,
                            "source": "ghcr.io/wjcloudy/homestead:2.8.2",
                            "current_digest": old_digest,
                            "candidate": "ghcr.io/wjcloudy/homestead:2.8.37",
                            "remote_digest": new_digest}]}
            updates.apply_update("lab", "demo")
        finally:
            updates._check_deployment = original

        spec = self.dep["spec"]["template"]["spec"]
        wanted = "ghcr.io/wjcloudy/homestead@" + new_digest
        self.assertEqual(wanted, spec["containers"][0]["image"])
        self.assertEqual(wanted, spec["initContainers"][0]["image"],
                         "the init container is the same release as the app")
        self.assertEqual("busybox:1.36", spec["initContainers"][1]["image"],
                         "an unrelated init container is left alone")
        self.assertEqual([{"name": "PORT", "value": "8080"}], spec["containers"][0]["env"],
                         "a pinned version env var would now be a lie")

        updates.rollback("lab", "demo")
        spec = self.dep["spec"]["template"]["spec"]
        was = "ghcr.io/wjcloudy/homestead@" + old_digest
        self.assertEqual(was, spec["containers"][0]["image"])
        self.assertEqual("ghcr.io/wjcloudy/homestead:2.8.2", spec["initContainers"][0]["image"],
                         "rollback takes the init container back with it")
        self.assertEqual("busybox:1.36", spec["initContainers"][1]["image"])

    def test_a_source_recorded_before_the_rename_is_still_followed(self):
        """An install made as harvUI records what it tracks under harvui.io.
        Reading only the current key loses the tag the image came from - and a
        digest has no successor, so every check comes back "up to date"."""
        self.dep["metadata"]["annotations"] = {
            "harvui.io/update-sources": json.dumps({"demo": "ghcr.io/x/demo:2.8.41"})}
        self.dep["spec"]["template"]["spec"]["containers"] = [
            {"name": "demo", "image": "ghcr.io/x/demo@sha256:" + "f" * 64}]
        pod = {"metadata": {"namespace": "lab", "labels": {"app": "demo"}},
               "status": {"containerStatuses": [
                   {"name": "demo", "imageID": "ghcr.io/x/demo@sha256:" + "f" * 64}]}}
        asked = []
        original_tags, original_manifest = updates.registry_tags, updates.manifest_info
        original_creds = updates._secret_credentials
        try:
            updates.registry_tags = lambda ref, *a, **k: (asked.append(ref)
                                                          or ["2.8.41", "2.8.42", "2.8.43"])
            updates.manifest_info = lambda *a, **k: {"digest": "sha256:" + "e" * 64,
                                                     "children": []}
            updates._secret_credentials = lambda *args: {}
            report = updates._check_deployment(self.dep, [pod])
        finally:
            updates.registry_tags, updates.manifest_info = original_tags, original_manifest
            updates._secret_credentials = original_creds

        image = report["images"][0]
        self.assertEqual("ghcr.io/x/demo:2.8.41", asked[0], "the recorded tag is the one followed")
        self.assertEqual("2.8.43", image["candidate_tag"])
        self.assertTrue(report["available"])
        self.assertEqual("", image["error"])

    def test_a_digest_with_no_recorded_tag_says_so_rather_than_up_to_date(self):
        self.dep["spec"]["template"]["spec"]["containers"] = [
            {"name": "demo", "image": "ghcr.io/x/demo@sha256:" + "f" * 64}]
        pod = {"metadata": {"namespace": "lab", "labels": {"app": "demo"}},
               "status": {"containerStatuses": [
                   {"name": "demo", "imageID": "ghcr.io/x/demo@sha256:" + "f" * 64}]}}
        original_tags, original_manifest = updates.registry_tags, updates.manifest_info
        original_creds = updates._secret_credentials
        try:
            updates.registry_tags = lambda *a, **k: ["2.8.43"]
            updates.manifest_info = lambda *a, **k: {"digest": "sha256:" + "f" * 64,
                                                     "children": []}
            updates._secret_credentials = lambda *args: {}
            image = updates._check_deployment(self.dep, [pod])["images"][0]
        finally:
            updates.registry_tags, updates.manifest_info = original_tags, original_manifest
            updates._secret_credentials = original_creds

        self.assertIn("no release tag recorded", image["error"])

    def test_an_update_clears_the_key_it_replaces(self):
        self.dep["metadata"]["annotations"] = {
            "harvui.io/update-sources": json.dumps({"demo": "ghcr.io/x/demo:1.0.0"})}
        original = updates._check_deployment
        try:
            updates._check_deployment = lambda *args, **kwargs: {
                "images": [{"container": "demo", "available": True,
                            "source": "ghcr.io/x/demo:1.0.0",
                            "current_digest": "sha256:" + "d" * 64,
                            "candidate": "ghcr.io/x/demo:1.1.0",
                            "remote_digest": "sha256:" + "c" * 64}]}
            updates.apply_update("lab", "demo")
        finally:
            updates._check_deployment = original

        annotations = self.dep["metadata"]["annotations"]
        self.assertNotIn("harvui.io/update-sources", annotations,
                         "one place records what is tracked, not two")
        self.assertIn("homestead.io/update-sources", annotations)

    def _check(self, tags, remote, current, running=True):
        self.dep["spec"]["template"]["spec"]["containers"] = [
            {"name": "demo", "image": "ghcr.io/x/demo@sha256:" + current if current
                                      else "ghcr.io/x/demo:2.8.41"}]
        self.dep["metadata"]["annotations"] = {
            "homestead.io/update-sources": json.dumps({"demo": "ghcr.io/x/demo:2.8.41"})}
        pods = [{"metadata": {"namespace": "lab", "labels": {"app": "demo"}},
                 "status": {"containerStatuses": [
                     {"name": "demo",
                      "imageID": ("ghcr.io/x/demo@sha256:" + current) if current else ""}]}}]
        if not running:
            self.dep["spec"]["replicas"] = 0
            pods = []
        original = (updates.registry_tags, updates.manifest_info, updates._secret_credentials)
        try:
            updates.registry_tags = lambda *a, **k: tags
            updates.manifest_info = lambda *a, **k: {"digest": "sha256:" + remote,
                                                     "children": []}
            updates._secret_credentials = lambda *args: {}
            return updates._check_deployment(self.dep, pods)
        finally:
            (updates.registry_tags, updates.manifest_info,
             updates._secret_credentials) = original

    def test_a_newer_tag_you_are_already_running_is_not_an_update(self):
        """After the image is set by hand the recorded source lags behind, so a
        newer tag exists that resolves to the digest already running."""
        report = self._check(["2.8.41", "2.8.45"], "a" * 64, current="a" * 64)

        self.assertEqual("2.8.45", report["images"][0]["candidate_tag"])
        self.assertFalse(report["available"], "it is the version already deployed")

    def test_a_newer_tag_with_a_different_image_is_an_update(self):
        report = self._check(["2.8.41", "2.8.45"], "b" * 64, current="a" * 64)

        self.assertTrue(report["available"])

    def test_the_same_tag_rebuilt_underneath_is_an_update(self):
        report = self._check(["2.8.41"], "b" * 64, current="a" * 64)

        self.assertTrue(report["available"], "the tag moved to a different image")

    def test_a_stopped_workload_is_still_offered_a_newer_release(self):
        report = self._check(["2.8.41", "2.8.45"], "b" * 64, current="", running=False)

        self.assertTrue(report["available"])

    def test_a_stopped_workload_on_the_newest_release_is_not(self):
        report = self._check(["2.8.41"], "b" * 64, current="", running=False)

        self.assertFalse(report["available"], "no newer tag and no digest to compare")

    def test_a_scan_reports_how_far_it_has_got(self):
        """A button that only goes quiet looks like one that did not work."""
        seen = []
        original = updates._check_deployment

        def slow(dep, pods, force=False):
            seen.append(updates.scan_progress())
            return {"ns": "lab", "name": dep["metadata"]["name"], "images": [],
                    "available": False, "can_rollback": False, "last_action": ""}

        deployments = [{"metadata": {"name": f"app{n}", "namespace": "lab"}} for n in range(4)]
        original_get = updates.kget
        try:
            updates._check_deployment = slow
            updates.kget = lambda path: ({"items": deployments} if "deployments" in path
                                         else {"items": []})
            report = updates.scan()
        finally:
            updates._check_deployment = original
            updates.kget = original_get

        self.assertEqual(4, len(report["workloads"]))
        self.assertTrue(any(state["running"] for state in seen), "progress shows while it runs")
        self.assertEqual(4, seen[0]["total"], "the total is known before the first check")

        after = updates.scan_progress()
        self.assertFalse(after["running"])
        self.assertEqual(4, after["done"])
        self.assertEqual("", after["current"], "nothing is being checked once it is over")

    def _fake_scans(self, delay=0.0):
        """scan() replaced by one that counts its runs and can be made slow."""
        runs = []
        original = updates.scan

        def scan(force=False):
            runs.append(force)
            time.sleep(delay)
            return {"checked_at": f"run-{len(runs)}", "updates": len(runs), "workloads": []}

        updates.scan = scan
        self.addCleanup(setattr, updates, "scan", original)
        updates.invalidate()
        self.addCleanup(updates.invalidate)
        return runs

    def test_a_quiet_request_reuses_the_last_report(self):
        runs = self._fake_scans()

        first = updates.report()
        second = updates.report()

        self.assertEqual([False], runs)
        self.assertIs(first, second)

    def test_a_forced_check_replaces_what_quiet_requests_see(self):
        """The bug: the button found an update, the page kept the older answer."""
        runs = self._fake_scans()
        updates.report()

        forced = updates.report(force=True)

        self.assertEqual([False, True], runs)
        self.assertIs(forced, updates.report(), "the forced answer is now everyone's")

    def test_a_quiet_request_during_a_forced_check_waits_for_it_not_a_second_scan(self):
        runs = self._fake_scans(delay=0.3)
        results = {}
        forced = threading.Thread(target=lambda: results.update(forced=updates.report(True)))
        forced.start()
        time.sleep(0.05)

        results["quiet"] = updates.report()
        forced.join()

        self.assertEqual([True], runs, "one scan, not two racing each other")
        self.assertIs(results["forced"], results["quiet"])

    def test_a_forced_check_does_not_settle_for_a_scan_already_running(self):
        runs = self._fake_scans(delay=0.3)
        quiet = threading.Thread(target=updates.report)
        quiet.start()
        time.sleep(0.05)

        forced = updates.report(force=True)
        quiet.join()

        self.assertEqual([False, True], runs, "it began before the button was pressed")
        self.assertEqual("run-2", forced["checked_at"])

    def test_an_image_change_makes_the_next_quiet_request_look_again(self):
        runs = self._fake_scans()
        updates.report()

        updates.invalidate()
        updates.report()

        self.assertEqual([False, False], runs)

    def test_one_odd_deployment_does_not_sink_the_scan(self):
        original = updates._check_deployment

        def check(dep, pods, force=False):
            if dep["metadata"]["name"] == "odd":
                raise KeyError("template")
            return {"ns": "lab", "name": dep["metadata"]["name"], "images": [],
                    "available": True, "can_rollback": False, "last_action": ""}

        deployments = [{"metadata": {"name": name, "namespace": "lab"}} for name in ("odd", "plex")]
        original_get = updates.kget
        try:
            updates._check_deployment = check
            updates.kget = lambda path: ({"items": deployments} if "deployments" in path
                                         else {"items": []})
            report = updates.scan()
        finally:
            updates._check_deployment = original
            updates.kget = original_get

        self.assertEqual(1, report["updates"], "plex's update still shows")
        self.assertEqual(1, report["errors"])
        self.assertEqual(["odd", "plex"], [w["name"] for w in report["workloads"]])

    def test_progress_is_readable_before_any_scan_has_run(self):
        updates.SCAN.update({"running": False, "done": 0, "total": 0, "current": "",
                             "started_at": 0.0, "finished_at": 0.0, "updates": 0})

        state = updates.scan_progress()

        self.assertFalse(state["running"])
        self.assertEqual(0, state["elapsed"])

    def test_progress_waits_for_surge_replacement_to_be_ready(self):
        self.dep["status"].update({"replicas": 2, "updatedReplicas": 1,
                                   "readyReplicas": 1, "availableReplicas": 1,
                                   "unavailableReplicas": 1})
        result = updates.progress("lab", "demo", self.dep)
        self.assertEqual("progressing", result["phase"])

        self.dep["status"].update({"replicas": 1, "updatedReplicas": 1,
                                   "readyReplicas": 1, "availableReplicas": 1,
                                   "unavailableReplicas": 0})
        result = updates.progress("lab", "demo", self.dep)
        self.assertEqual("ready", result["phase"])


if __name__ == "__main__":
    unittest.main()
