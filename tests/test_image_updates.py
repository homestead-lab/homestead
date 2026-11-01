import copy
import json
import sys
import tempfile
import time
import threading
import unittest
from unittest import mock
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_updates as updates
import homestead_update_registry as REGISTRY
import homestead_update_rollouts as ROLLOUTS
import homestead_update_checks as checks


DEPLOYMENT = {
    "metadata": {"name": "demo", "namespace": "lab", "generation": 2, "annotations": {}, "uid": "dep-demo", "resourceVersion": "10"},
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
        self.pods = []
        self.tmp = tempfile.TemporaryDirectory()

        def get(path):
            if path.endswith("/deployments/demo"):
                return self.dep
            if path == "/api/v1/pods":
                return {"items": self.pods}
            if path.endswith("/replicasets"):
                return {"items": [{"metadata": {"namespace": "lab", "uid": "rs-demo",
                    "ownerReferences": [{"kind": "Deployment", "uid": "dep-demo", "controller": True}]}}]}
            if "/serviceaccounts/" in path:
                return {"imagePullSecrets": []}
            raise AssertionError(path)

        def send(method, path, body, **kwargs):
            self.sent.append((method, path, copy.deepcopy(body)))
            self.dep = copy.deepcopy(body)
            return self.dep

        updates.bind(get, send, "lab", self.tmp.name)
        REGISTRY._CACHE.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def test_review_discovery_does_not_persist_running_digest(self):
        pod = {"metadata": {"namespace": "lab", "labels": {"app": "demo"}},
               "status": {"containerStatuses": [{"name": "demo", "imageID": "nginx@sha256:" + "a" * 64}]}}
        before = copy.deepcopy(self.dep)
        with mock.patch.object(REGISTRY, "_secret_credentials", return_value={}), \
                mock.patch.object(REGISTRY, "registry_tags", return_value=["1.28.0"]), \
                mock.patch.object(REGISTRY, "manifest_info", return_value={"digest": "sha256:" + "b" * 64, "children": []}):
            check = checks._check_deployment(self.dep, [pod], True, persist=False)
        self.assertTrue(check["available"])
        self.assertEqual(before, self.dep)
        self.assertEqual([], self.sent)

    def test_an_image_set_to_another_tag_since_the_last_update_follows_that_tag(self):
        # Frigate: updated to 0.18.0 (pinned, recorded), then Edit set :stable,
        # which ran 0.18.2. Checking the old record offered 0.18.0's build as
        # a "new build" - an update that rolled it back.
        stable, old = "sha256:" + "a" * 64, "sha256:" + "b" * 64
        self.dep["metadata"]["annotations"][updates.TRACKED] = json.dumps({"demo": "ghcr.io/x/frigate:0.18.0"})
        self.dep["spec"]["template"]["spec"]["containers"][0]["image"] = "ghcr.io/x/frigate:stable"
        pod = {"metadata": {"namespace": "lab", "labels": {"app": "demo"}},
               "status": {"containerStatuses": [{"name": "demo", "imageID": "ghcr.io/x/frigate@" + stable, "state": {"running": {}}}]}}
        builds = {"stable": stable, "0.18.0": old}
        with mock.patch.object(REGISTRY, "_secret_credentials", return_value={}), \
                mock.patch.object(REGISTRY, "registry_tags", return_value=["0.18.0", "stable"]), \
                mock.patch.object(REGISTRY, "manifest_info", side_effect=lambda ref, *a, **k: {
                    "digest": builds[REGISTRY.parse_image(ref)["tag"]], "children": []}):
            check = checks._check_deployment(self.dep, [pod], True, persist=False)
        image = check["images"][0]
        self.assertEqual("ghcr.io/x/frigate:stable", image["source"])
        self.assertFalse(image["available"], image)

    def test_the_recorded_tag_still_counts_while_the_image_is_its_digest_pin(self):
        pin = "ghcr.io/x/frigate@sha256:" + "a" * 64
        self.assertEqual("ghcr.io/x/frigate:stable", checks.followed("ghcr.io/x/frigate:stable", pin))
        self.assertEqual("esphome/esphome:latest", checks.followed("esphome/esphome:latest", "docker.io/esphome/esphome@sha256:" + "c" * 64))
        self.assertEqual("ghcr.io/x/other@sha256:" + "a" * 64, checks.followed("ghcr.io/x/frigate:stable", "ghcr.io/x/other@sha256:" + "a" * 64),
                         "a pin of another repository is not the record's")
        self.assertEqual("nginx:1.27.0", checks.followed("", "nginx:1.27.0"))
        self.assertEqual("postgres:17-alpine@sha256:" + "d" * 64, checks.followed("postgres:16-alpine", "postgres:17-alpine@sha256:" + "d" * 64),
                         "pinned in git as tag@digest: it names the tag it follows itself")

    def test_apps_gitops_or_an_administrator_updates_are_named_and_not_updated_here(self):
        # #296: Flux and Argo CD by the marks they leave; an administrator's mark for the rest.
        cases = {
            "Flux Kustomization": ({"kustomize.toolkit.fluxcd.io/name": "apps", "kustomize.toolkit.fluxcd.io/namespace": "flux-system"}, {},
                                   {"by": "Flux", "source": "Kustomization flux-system/apps", "detected": True}),
            "Flux HelmRelease": ({"helm.toolkit.fluxcd.io/name": "loki", "helm.toolkit.fluxcd.io/namespace": "monitoring"}, {},
                                 {"by": "Flux", "source": "HelmRelease monitoring/loki", "detected": True}),
            "Argo CD annotation": ({}, {"argocd.argoproj.io/tracking-id": "media:apps/Deployment:lab/demo"},
                                   {"by": "Argo CD", "source": "Application media", "detected": True}),
            "Argo CD label": ({"argocd.argoproj.io/instance": "media"}, {}, {"by": "Argo CD", "source": "Application media", "detected": True}),
            "administrator": ({}, {updates.MANAGED: "Renovate"}, {"by": "Renovate", "source": "", "detected": False}),
        }
        for why, (labels, annotations, expected) in cases.items():
            with self.subTest(why=why):
                self.dep = copy.deepcopy(DEPLOYMENT)
                self.dep["metadata"]["labels"] = labels
                self.dep["metadata"]["annotations"] = annotations
                self.assertEqual(expected, updates.managed_by(self.dep))
                for prepare in (ROLLOUTS.prepare_update, ROLLOUTS.prepare_rollback):
                    with self.assertRaisesRegex(ValueError, expected["by"]):
                        prepare("lab", "demo")
                self.assertEqual([], self.sent, "nothing written")

    def test_helm_alone_is_not_gitops(self):
        self.dep["metadata"]["labels"] = {"app.kubernetes.io/instance": "demo", "app.kubernetes.io/managed-by": "Helm"}
        self.assertEqual({}, updates.managed_by(self.dep))

    def test_an_administrator_marks_and_clears_but_never_unmarks_what_gitops_owns(self):
        updates.set_managed("lab", "demo", "  Renovate ")
        method, path, body = self.sent[-1]
        self.assertEqual(("PATCH", "/apis/apps/v1/namespaces/lab/deployments/demo"), (method, path))
        self.assertEqual({updates.MANAGED: "Renovate"}, body["metadata"]["annotations"])
        self.dep = copy.deepcopy(DEPLOYMENT)
        updates.set_managed("lab", "demo", "")
        self.assertEqual({updates.MANAGED: None}, self.sent[-1][2]["metadata"]["annotations"], "cleared")
        with self.assertRaisesRegex(ValueError, "60 characters"):
            updates.set_managed("lab", "demo", "x" * 61)
        self.dep = copy.deepcopy(DEPLOYMENT)
        self.dep["metadata"]["labels"] = {"kustomize.toolkit.fluxcd.io/name": "apps"}
        with self.assertRaisesRegex(ValueError, "no mark to clear"):
            updates.set_managed("lab", "demo", "")

    def test_an_image_without_a_tag_is_given_latest(self):
        self.assertEqual("n8nio/n8n:latest", REGISTRY.with_tag("n8nio/n8n"))
        self.assertEqual("localhost:5000/app:latest", REGISTRY.with_tag("localhost:5000/app"))
        self.assertEqual("ghcr.io/x/app:1.2", REGISTRY.with_tag("ghcr.io/x/app:1.2"))
        digest = "ghcr.io/x/app@sha256:" + "a" * 64
        self.assertEqual(digest, REGISTRY.with_tag(digest))

    def _latest(self, remote_digest, running_digest=None, replicas=1):
        self.dep["spec"]["replicas"] = replicas
        self.dep["spec"]["template"]["spec"]["containers"] = [{"name": "demo", "image": "n8nio/n8n:latest"}]
        pods = [] if running_digest is None else [{
            "metadata": {"namespace": "lab", "labels": {"app": "demo"}},
            "status": {"phase": "Running", "containerStatuses": [{"name": "demo", "state": {"running": {}},
                                                                  "imageID": "docker.io/n8nio/n8n@" + running_digest}]}}]
        originals = REGISTRY.registry_tags, REGISTRY.manifest_info, REGISTRY._secret_credentials
        try:
            REGISTRY.registry_tags = lambda *a, **k: ["latest"]
            REGISTRY.manifest_info = lambda *a, **k: {"digest": remote_digest, "children": []}
            REGISTRY._secret_credentials = lambda *args: {}
            return checks._check_deployment(self.dep, pods)
        finally:
            REGISTRY.registry_tags, REGISTRY.manifest_info, REGISTRY._secret_credentials = originals

    def test_a_flux_app_with_an_update_gets_the_image_line_to_commit(self):
        # #735: Flux applies it, so the bump is made in git - here is the line.
        self.dep["metadata"]["labels"] = {"kustomize.toolkit.fluxcd.io/name": "apps",
                                          "kustomize.toolkit.fluxcd.io/namespace": "flux-system"}
        old, new = "sha256:" + "1" * 64, "sha256:" + "2" * 64
        report = self._latest(new, running_digest=old)
        self.assertTrue(report["available"])
        self.assertTrue(report["yaml"].startswith("apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: demo\n  namespace: lab"))
        # The tag did not change, only what it points at: the line pins the digest.
        self.assertIn(f"      - name: demo\n        image: n8nio/n8n:latest@{new}", report["yaml"])

    def test_an_app_homestead_updates_gets_no_yaml(self):
        report = self._latest("sha256:" + "2" * 64, running_digest="sha256:" + "1" * 64)
        self.assertEqual("", report["yaml"])

    def test_a_stopped_workload_is_compared_with_what_it_last_ran(self):
        old, new = "sha256:" + "1" * 64, "sha256:" + "2" * 64
        before = copy.deepcopy(self.dep)
        self._latest(old, running_digest=old)
        recorded = json.loads(self.sent[-1][2]["metadata"]["annotations"]["homestead.io/ran-digests"])
        self.assertEqual({"demo": old}, recorded)
        self.dep = before
        self.dep["metadata"]["annotations"]["homestead.io/ran-digests"] = json.dumps(recorded)

        report = self._latest(new, replicas=0)

        self.assertTrue(report["available"], "latest moved on while it was stopped")
        self.assertFalse(report["unchecked"])

    def test_a_pending_pod_does_not_read_an_old_record_as_a_new_build(self):
        # #380: the host was down, the pod Pending; what last ran is behind.
        old, new = "sha256:" + "1" * 64, "sha256:" + "2" * 64
        self.dep["metadata"].setdefault("annotations", {})["homestead.io/ran-digests"] = json.dumps({"demo": old})
        self.dep["spec"]["replicas"] = 1
        self.dep["spec"]["template"]["spec"]["containers"] = [{"name": "demo", "image": "n8nio/n8n:latest"}]
        pods = [{"metadata": {"namespace": "lab", "labels": {"app": "demo"}},
                 "status": {"phase": "Pending", "containerStatuses": [{"name": "demo", "state": {"waiting": {}},
                                                                       "imageID": "docker.io/n8nio/n8n@" + old}]}}]
        originals = REGISTRY.registry_tags, REGISTRY.manifest_info, REGISTRY._secret_credentials
        try:
            REGISTRY.registry_tags = lambda *a, **k: ["latest"]
            REGISTRY.manifest_info = lambda *a, **k: {"digest": new, "children": []}
            REGISTRY._secret_credentials = lambda *args: {}
            report = checks._check_deployment(self.dep, pods)
        finally:
            REGISTRY.registry_tags, REGISTRY.manifest_info, REGISTRY._secret_credentials = originals
        self.assertFalse(report["available"], "a pod not yet running is checked once it runs")
        self.assertTrue(report["unchecked"])

    def test_a_workload_that_never_ran_here_says_it_was_not_checked(self):
        report = self._latest("sha256:" + "2" * 64, replicas=0)
        self.assertFalse(report["available"])
        self.assertTrue(report["unchecked"])
        self.assertEqual([], self.sent, "nothing to record")

    def test_parses_docker_and_private_registry_references(self):
        docker = REGISTRY.parse_image("nginx:1.27")
        self.assertEqual("docker.io/library/nginx", docker["base"])
        self.assertEqual("registry-1.docker.io", docker["endpoint"])
        private = REGISTRY.parse_image("ghcr.io/example/app:v2.1.0")
        self.assertEqual("ghcr.io", private["registry"])
        self.assertEqual("example/app", private["repo"])

    def test_semver_stays_in_current_major(self):
        tags = ["1.9.1", "1.10.0", "2.0.0", "edge", "1.10.0-rc1"]
        self.assertEqual("1.10.0", REGISTRY.newer_semver("1.9.0", tags))
        self.assertIsNone(REGISTRY.newer_semver("latest", tags))

    def test_multiarch_child_digest_is_not_a_false_update(self):
        child = "sha256:" + "a" * 64
        pod = {"metadata": {"namespace": "lab", "labels": {"app": "demo"}},
               "status": {"containerStatuses": [{"name": "demo", "imageID": "repo@" + child}]}}
        original_tags, original_manifest = REGISTRY.registry_tags, REGISTRY.manifest_info
        original_creds = REGISTRY._secret_credentials
        try:
            REGISTRY.registry_tags = lambda *args, **kwargs: ["1.27.0"]
            REGISTRY.manifest_info = lambda *args, **kwargs: {
                "digest": "sha256:" + "b" * 64, "children": [child]}
            REGISTRY._secret_credentials = lambda *args: {}
            result = checks._check_deployment(self.dep, [pod])
            self.assertFalse(result["available"])
        finally:
            REGISTRY.registry_tags, REGISTRY.manifest_info = original_tags, original_manifest
            REGISTRY._secret_credentials = original_creds

    def test_stopped_workload_is_not_reported_as_a_failed_registry_check(self):
        """An import lands scaled to zero, so there is no digest to compare."""
        original_tags, original_manifest = REGISTRY.registry_tags, REGISTRY.manifest_info
        original_creds = REGISTRY._secret_credentials
        try:
            REGISTRY.registry_tags = lambda *args, **kwargs: ["1.27.0"]
            REGISTRY.manifest_info = lambda *args, **kwargs: {
                "digest": "sha256:" + "b" * 64, "children": []}
            REGISTRY._secret_credentials = lambda *args: {}

            stopped = copy.deepcopy(self.dep)
            stopped["spec"]["replicas"] = 0
            self.assertEqual("", checks._check_deployment(stopped, [])["images"][0]["error"])

            running = {"metadata": {"namespace": "lab", "labels": {"app": "demo"}},
                       "status": {"containerStatuses": [{"name": "demo", "imageID": ""}]}}
            item = checks._check_deployment(self.dep, [running])["images"][0]
            # Just installed and still pulling: a check still to come, not a failed one.
            self.assertEqual("", item["error"])
            self.assertTrue(item["unchecked"] and item["starting"])
        finally:
            REGISTRY.registry_tags, REGISTRY.manifest_info = original_tags, original_manifest
            REGISTRY._secret_credentials = original_creds

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

        original = checks._check_deployment
        try:
            updates.bind(get, lambda *args, **kwargs: None, "lab", self.tmp.name,
                         {"cattle-fleet-local-system"})
            checks._check_deployment = lambda dep, pods, force=False: {
                "ns": dep["metadata"]["namespace"], "name": dep["metadata"]["name"],
                "images": [], "available": False, "can_rollback": False,
                "last_action": "",
            }
            report = checks.scan()
        finally:
            checks._check_deployment = original

        self.assertEqual([("lab", "demo")],
                         [(x["ns"], x["name"]) for x in report["workloads"]])

    def test_apply_pins_digest_and_records_exact_rollback(self):
        digest = "sha256:" + "c" * 64
        original = checks._check_deployment
        try:
            checks._check_deployment = lambda *args, **kwargs: {
                "images": [{"container": "demo", "available": True,
                            "source": "docker.io/library/nginx:1.27.0",
                            "current_digest": "sha256:" + "d" * 64,
                            "candidate": "docker.io/library/nginx:1.28.0",
                            "remote_digest": digest}]}
            result = ROLLOUTS.apply_update("lab", "demo")
        finally:
            checks._check_deployment = original
        image = self.dep["spec"]["template"]["spec"]["containers"][0]["image"]
        self.assertEqual("docker.io/library/nginx@" + digest, image)
        previous = json.loads(self.dep["metadata"]["annotations"][updates.PREVIOUS])
        self.assertEqual("docker.io/library/nginx@sha256:" + "d" * 64,
                         previous["images"]["demo"])
        self.assertEqual("ready", result["phase"])

        ROLLOUTS.rollback("lab", "demo")
        restored = self.dep["spec"]["template"]["spec"]["containers"][0]["image"]
        self.assertEqual("docker.io/library/nginx@sha256:" + "d" * 64, restored)

    def test_an_init_container_on_the_same_image_moves_with_it(self):
        """Homestead's own data-permissions init container is built from the
        Homestead image: leaving it behind pins it to the release the workload
        was installed with, however many times the app is updated."""
        spec = self.dep["spec"]["template"]["spec"]
        spec["containers"] = [{"name": "homestead",
                               "image": "ghcr.io/homestead-lab/homestead:2.8.2",
                               "env": [{"name": "PORT", "value": "8080"},
                                       {"name": "HOMESTEAD_VERSION", "value": "2.8.2"}]}]
        spec["initContainers"] = [{"name": "data-permissions",
                                   "image": "ghcr.io/homestead-lab/homestead:2.8.2"},
                                  {"name": "wait", "image": "busybox:1.36"}]
        new_digest = "sha256:" + "e" * 64
        old_digest = "sha256:" + "f" * 64
        self.pods = [{"metadata": {"name": "demo-pod", "namespace": "lab", "labels": {"app": "demo"},
                     "ownerReferences": [{"kind": "ReplicaSet", "uid": "rs-demo", "controller": True}]},
                     "spec": copy.deepcopy(spec), "status": {"phase": "Running",
                     "initContainerStatuses": [{"name": "data-permissions",
                       "imageID": "ghcr.io/homestead-lab/homestead@" + old_digest}]}}]
        original = checks._check_deployment
        try:
            checks._check_deployment = lambda *args, **kwargs: {
                "images": [{"container": "homestead", "available": True,
                            "source": "ghcr.io/homestead-lab/homestead:2.8.2",
                            "current_digest": old_digest,
                            "candidate": "ghcr.io/homestead-lab/homestead:2.8.37",
                            "remote_digest": new_digest}]}
            ROLLOUTS.apply_update("lab", "demo")
        finally:
            checks._check_deployment = original

        spec = self.dep["spec"]["template"]["spec"]
        wanted = "ghcr.io/homestead-lab/homestead@" + new_digest
        self.assertEqual(wanted, spec["containers"][0]["image"])
        self.assertEqual(wanted, spec["initContainers"][0]["image"],
                         "the init container is the same release as the app")
        self.assertEqual("busybox:1.36", spec["initContainers"][1]["image"],
                         "an unrelated init container is left alone")
        self.assertEqual([{"name": "PORT", "value": "8080"}], spec["containers"][0]["env"],
                         "a pinned version env var would now be a lie")

        ROLLOUTS.rollback("lab", "demo")
        spec = self.dep["spec"]["template"]["spec"]
        was = "ghcr.io/homestead-lab/homestead@" + old_digest
        self.assertEqual(was, spec["containers"][0]["image"])
        self.assertEqual(was, spec["initContainers"][0]["image"],
                         "rollback pins the init container to the exact previous digest")
        self.assertEqual("busybox:1.36", spec["initContainers"][1]["image"])

    def homestead_on(self, tag):
        """This workload as Homestead itself, running tag's build, with its
        data-permissions init container on the same image."""
        old = "sha256:" + "f" * 64
        spec = self.dep["spec"]["template"]["spec"]
        spec["containers"] = [{"name": "homestead", "image": "ghcr.io/homestead-lab/homestead:" + tag}]
        spec["initContainers"] = [{"name": "data-permissions", "image": "ghcr.io/homestead-lab/homestead@" + old}]
        self.pods = [{"metadata": {"name": "demo-pod", "namespace": "lab", "labels": {"app": "demo"},
                      "ownerReferences": [{"kind": "ReplicaSet", "uid": "rs-demo", "controller": True}]},
                      "spec": copy.deepcopy(spec), "status": {"phase": "Running",
                      "containerStatuses": [{"name": "homestead", "state": {"running": {}},
                                             "imageID": "ghcr.io/homestead-lab/homestead@" + old}]}}]
        self.enterContext(mock.patch.object(updates, "PART", lambda ns, name: "self"))
        self.enterContext(mock.patch.object(REGISTRY, "_secret_credentials", return_value={}))
        return old

    def test_homestead_goes_on_a_release_an_admin_names_pinned_to_its_digest(self):
        """Its own update check can be what is broken (2.8.333): the release
        is named, not found, and is still pinned when the registry answers."""
        self.homestead_on("2.8.333-dev.4")
        new = "sha256:" + "e" * 64
        with mock.patch.object(REGISTRY, "manifest_digest", return_value=new) as asked,                 mock.patch.object(checks, "_check_deployment", side_effect=AssertionError("the check is not asked")):
            prepared = ROLLOUTS.prepare_update("lab", "demo", "2.8.333-dev.5")
        asked.assert_called_once_with("ghcr.io/homestead-lab/homestead:2.8.333-dev.5", {}, force=True)
        spec = prepared["proposed"]["spec"]["template"]["spec"]
        self.assertEqual("ghcr.io/homestead-lab/homestead@" + new, spec["containers"][0]["image"])
        self.assertEqual("ghcr.io/homestead-lab/homestead@" + new, spec["initContainers"][0]["image"],
                         "the init container goes with it, as kubectl set image left it behind")
        tracked = json.loads(prepared["proposed"]["metadata"]["annotations"][updates.TRACKED])
        self.assertEqual("ghcr.io/homestead-lab/homestead:2.8.333-dev.5", tracked["homestead"])
        self.assertNotIn("unread", prepared)
        self.assertEqual([], self.sent, "a review writes nothing")

    def test_a_named_release_the_registry_cannot_be_read_for_goes_by_its_tag(self):
        old = self.homestead_on("2.8.333-dev.4")
        with mock.patch.object(REGISTRY, "manifest_digest", side_effect=OSError(9, "Bad file descriptor")):
            prepared = ROLLOUTS.prepare_update("lab", "demo", "2.8.333-dev.5")
        spec = prepared["proposed"]["spec"]["template"]["spec"]
        self.assertEqual("ghcr.io/homestead-lab/homestead:2.8.333-dev.5", spec["containers"][0]["image"],
                         "the cluster pulls the tag and checks it itself")
        self.assertIn("Bad file descriptor", prepared["unread"])
        self.assertEqual("ghcr.io/homestead-lab/homestead@" + old, prepared["before"]["homestead"],
                         "rollback still goes to the exact build that ran")

    def test_only_homestead_and_only_a_release_can_be_named(self):
        self.homestead_on("2.8.333-dev.4")
        with self.assertRaisesRegex(ValueError, "name a Homestead release"):
            ROLLOUTS.prepare_update("lab", "demo", "latest")
        with mock.patch.object(updates, "PART", lambda ns, name: ""),                 self.assertRaisesRegex(ValueError, "only Homestead itself"):
            ROLLOUTS.prepare_update("lab", "demo", "2.8.334")

    def test_a_digest_with_no_recorded_tag_says_so_rather_than_up_to_date(self):
        self.dep["spec"]["template"]["spec"]["containers"] = [
            {"name": "demo", "image": "ghcr.io/x/demo@sha256:" + "f" * 64}]
        pod = {"metadata": {"namespace": "lab", "labels": {"app": "demo"}},
               "status": {"containerStatuses": [
                   {"name": "demo", "imageID": "ghcr.io/x/demo@sha256:" + "f" * 64}]}}
        original_tags, original_manifest = REGISTRY.registry_tags, REGISTRY.manifest_info
        original_creds = REGISTRY._secret_credentials
        try:
            REGISTRY.registry_tags = lambda *a, **k: ["2.8.43"]
            REGISTRY.manifest_info = lambda *a, **k: {"digest": "sha256:" + "f" * 64,
                                                     "children": []}
            REGISTRY._secret_credentials = lambda *args: {}
            image = checks._check_deployment(self.dep, [pod])["images"][0]
        finally:
            REGISTRY.registry_tags, REGISTRY.manifest_info = original_tags, original_manifest
            REGISTRY._secret_credentials = original_creds

        self.assertIn("no release tag recorded", image["error"])

    def _pinned(self, image, tags, now):
        """#874: an image pinned as tag@digest, run on that digest. The
        registry answers a tag with what it points to now, and a digest with
        itself."""
        self.dep["spec"]["template"]["spec"]["containers"] = [{"name": "demo", "image": image}]
        pinned = image.split("@", 1)[1]
        pod = {"metadata": {"namespace": "lab", "labels": {"app": "demo"}},
               "status": {"phase": "Running",
                          "containerStatuses": [{"name": "demo", "state": {"running": {}},
                                                 "imageID": "docker.io/library/x@" + pinned}]}}
        asked = []

        def manifest(ref, *args, **kwargs):
            asked.append(ref)
            return {"digest": ref.split("@", 1)[1] if "@" in ref else now, "children": []}
        with mock.patch.object(REGISTRY, "_secret_credentials", return_value={}), \
                mock.patch.object(REGISTRY, "registry_tags", return_value=tags), \
                mock.patch.object(REGISTRY, "manifest_info", side_effect=manifest):
            report = checks._check_deployment(self.dep, [pod], persist=False)
        return report["images"][0], asked

    def test_a_pinned_tag_that_is_not_a_release_is_current_when_the_tag_still_points_there(self):
        old = "sha256:" + "a" * 64
        image, asked = self._pinned("postgres:16-alpine@" + old, ["16-alpine", "17-alpine"], now=old)

        self.assertEqual("", image["error"], "the tag is recorded: it is 16-alpine")
        self.assertFalse(image["available"])
        self.assertEqual(["postgres:16-alpine"], asked, "asks what the tag points to now, not the pin")

    def test_a_pinned_tag_that_is_not_a_release_offers_a_new_build_when_the_tag_moved(self):
        old, new = "sha256:" + "a" * 64, "sha256:" + "b" * 64
        image, _ = self._pinned("valkey/valkey:9-alpine@" + old, ["9-alpine"], now=new)

        self.assertEqual("", image["error"])
        self.assertTrue(image["available"], "the tag was rebuilt upstream")
        self.assertEqual("9-alpine", image["candidate_tag"], "the page reads it as 9-alpine · new build")
        self.assertEqual("valkey/valkey:9-alpine", image["candidate"])
        self.assertEqual((old, new), (image["current_digest"], image["remote_digest"]))

    def test_which_pinned_references_name_a_tag(self):
        digest = "@sha256:" + "a" * 64
        cases = {"postgres:16-alpine": True, "postgres": False, "img:latest": True,
                 "registry.example.com:5000/img": False, "registry.example.com:5000/img:9-alpine": True}
        for ref, named in cases.items():
            self.assertIs(named, checks._names_tag(ref + digest), ref)

    def test_a_pinned_release_tag_is_checked_as_before(self):
        old, new = "sha256:" + "a" * 64, "sha256:" + "b" * 64
        image, _ = self._pinned("grafana/alloy:v1.21.1@" + old, ["v1.21.1", "v1.22.0"], now=new)

        self.assertEqual("", image["error"])
        self.assertTrue(image["available"])
        self.assertEqual(("docker.io/grafana/alloy:v1.22.0", "v1.22.0"), (image["candidate"], image["candidate_tag"]))

        image, asked = self._pinned("grafana/alloy:v1.21.1@" + old, ["v1.21.1"], now=new)
        self.assertEqual("", image["error"])
        self.assertFalse(image["available"], "no newer release; the pin itself is what is checked")
        self.assertEqual(["grafana/alloy:v1.21.1@" + old], asked)

    def test_copy_as_yaml_keeps_the_pin_of_a_pinned_image(self):
        import homestead_gitops as GITOPS
        old, new = "sha256:" + "a" * 64, "sha256:" + "b" * 64
        image, _ = self._pinned("postgres:16-alpine@" + old, ["16-alpine"], now=new)
        self.assertEqual("postgres:16-alpine@" + new, GITOPS.git_image(image))

        image, _ = self._pinned("grafana/alloy:v1.21.1@" + old, ["v1.21.1", "v1.22.0"], now=new)
        self.assertEqual("docker.io/grafana/alloy:v1.22.0@" + new, GITOPS.git_image(image),
                         "a pinned image stays pinned in git: the new release at its digest")

    def test_copy_as_yaml_of_an_unpinned_image_is_unchanged(self):
        import homestead_gitops as GITOPS
        new = "sha256:" + "b" * 64
        self.assertEqual("docker.io/grafana/alloy:v1.22.0", GITOPS.git_image(
            {"deployed": "grafana/alloy:v1.21.1", "candidate": "docker.io/grafana/alloy:v1.22.0", "remote_digest": new}))
        self.assertEqual("postgres:16-alpine@" + new, GITOPS.git_image(
            {"deployed": "postgres:16-alpine", "candidate": "postgres:16-alpine", "remote_digest": new}))

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
        original = (REGISTRY.registry_tags, REGISTRY.manifest_info, REGISTRY._secret_credentials)
        try:
            REGISTRY.registry_tags = lambda *a, **k: tags
            REGISTRY.manifest_info = lambda *a, **k: {"digest": "sha256:" + remote,
                                                     "children": []}
            REGISTRY._secret_credentials = lambda *args: {}
            return checks._check_deployment(self.dep, pods)
        finally:
            (REGISTRY.registry_tags, REGISTRY.manifest_info,
             REGISTRY._secret_credentials) = original

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
        original = checks._check_deployment

        def slow(dep, pods, force=False, channel=None):
            seen.append(checks.scan_progress())
            return {"ns": "lab", "name": dep["metadata"]["name"], "images": [],
                    "available": False, "can_rollback": False, "last_action": ""}

        deployments = [{"metadata": {"name": f"app{n}", "namespace": "lab"}} for n in range(4)]
        original_get = updates.kget
        try:
            checks._check_deployment = slow
            updates.kget = lambda path: ({"items": deployments} if "deployments" in path
                                         else {"items": []})
            report = checks.scan()
        finally:
            checks._check_deployment = original
            updates.kget = original_get

        self.assertEqual(4, len(report["workloads"]))
        self.assertTrue(any(state["running"] for state in seen), "progress shows while it runs")
        self.assertEqual(4, seen[0]["total"], "the total is known before the first check")

        after = checks.scan_progress()
        self.assertFalse(after["running"])
        self.assertEqual(4, after["done"])
        self.assertEqual("", after["current"], "nothing is being checked once it is over")

    def _fake_scans(self, delay=0.0):
        """scan() replaced by one that counts its runs and can be made slow."""
        runs = []
        original = checks.scan

        def scan(force=False):
            runs.append(force)
            time.sleep(delay)
            return {"checked_at": f"run-{len(runs)}", "updates": len(runs), "workloads": []}

        checks.scan = scan
        self.addCleanup(setattr, checks, "scan", original)
        checks.invalidate()
        self.addCleanup(checks.invalidate)
        return runs

    def test_a_quiet_request_reuses_the_last_report(self):
        runs = self._fake_scans()

        first = checks.report()
        second = checks.report()

        self.assertEqual([False], runs)
        self.assertIs(first, second)

    def test_a_forced_check_replaces_what_quiet_requests_see(self):
        """The bug: the button found an update, the page kept the older answer."""
        runs = self._fake_scans()
        checks.report()

        forced = checks.report(force=True)

        self.assertEqual([False, True], runs)
        self.assertIs(forced, checks.report(), "the forced answer is now everyone's")

    def test_a_quiet_request_during_a_forced_check_waits_for_it_not_a_second_scan(self):
        runs = self._fake_scans(delay=0.3)
        results = {}
        forced = threading.Thread(target=lambda: results.update(forced=checks.report(True)))
        forced.start()
        time.sleep(0.05)

        results["quiet"] = checks.report()
        forced.join()

        self.assertEqual([True], runs, "one scan, not two racing each other")
        self.assertIs(results["forced"], results["quiet"])

    def test_a_forced_check_does_not_settle_for_a_scan_already_running(self):
        runs = self._fake_scans(delay=0.3)
        quiet = threading.Thread(target=checks.report)
        quiet.start()
        time.sleep(0.05)

        forced = checks.report(force=True)
        quiet.join()

        self.assertEqual([False, True], runs, "it began before the button was pressed")
        self.assertEqual("run-2", forced["checked_at"])

    def test_checking_homestead_checks_only_homestead_and_keeps_every_apps_answer(self):
        app = {"ns": "lab", "name": "photos", "homestead": "", "available": True, "images": []}
        old_self = {"ns": "lab", "name": "homestead", "homestead": "self", "available": False, "images": []}
        new_self = dict(old_self, available=True)
        asked = []
        original = checks.scan

        def scan(force=False, homestead_only=False):
            asked.append((force, homestead_only))
            return checks._summary([new_self] if homestead_only else [app, old_self])

        checks.scan = scan
        self.addCleanup(setattr, checks, "scan", original)
        checks.invalidate()
        self.addCleanup(checks.invalidate)
        self.assertTrue(checks.homestead_report()["partial"], "no full report yet: Homestead's parts alone, not kept")
        full = checks.report()
        merged = checks.homestead_report()
        self.assertEqual([(True, True), (False, False), (True, True)], asked, "no app is checked again")
        self.assertEqual([new_self, app], merged["workloads"], "sorted by namespace and name, as a full scan is")
        self.assertEqual((1, 1), (merged["updates"], merged["homestead"]["updates"]))
        self.assertEqual(full["checked_at"], merged["checked_at"], "the apps' answers are as old as they were")
        self.assertIs(merged, checks.report(), "and it is everyone's now")

    def test_an_image_change_makes_the_next_quiet_request_look_again(self):
        runs = self._fake_scans()
        checks.report()

        checks.invalidate()
        checks.report()

        self.assertEqual([False, False], runs)

    def test_one_odd_deployment_does_not_sink_the_scan(self):
        original = checks._check_deployment

        def check(dep, pods, force=False, channel=None):
            if dep["metadata"]["name"] == "odd":
                raise KeyError("template")
            return {"ns": "lab", "name": dep["metadata"]["name"], "images": [],
                    "available": True, "can_rollback": False, "last_action": ""}

        deployments = [{"metadata": {"name": name, "namespace": "lab"}} for name in ("odd", "plex")]
        original_get = updates.kget
        try:
            checks._check_deployment = check
            updates.kget = lambda path: ({"items": deployments} if "deployments" in path
                                         else {"items": []})
            report = checks.scan()
        finally:
            checks._check_deployment = original
            updates.kget = original_get

        self.assertEqual(1, report["updates"], "plex's update still shows")
        self.assertEqual(1, report["errors"])
        self.assertEqual(["odd", "plex"], [w["name"] for w in report["workloads"]])

    def test_homesteads_own_updates_are_counted_apart_from_apps(self):
        original, original_get, original_part = checks._check_deployment, updates.kget, updates.PART

        def check(dep, pods, force=False, channel=None):
            name = dep["metadata"]["name"]
            return {"ns": "lab", "name": name, "available": True, "can_rollback": False, "last_action": "",
                    "images": [{"container": name, "available": True,
                                **({"error": "registry returned HTTP 500"} if name == "homestead-nfs" else {})}]}

        deployments = [{"metadata": {"name": name, "namespace": "lab"}}
                       for name in ("plex", "homestead", "homestead-nfs")]
        try:
            checks._check_deployment = check
            updates.kget = lambda path: {"items": deployments} if "deployments" in path else {"items": []}
            updates.PART = lambda ns, name: {"homestead": "self", "homestead-nfs": "nfs"}.get(name, "")
            report = checks.scan()
        finally:
            checks._check_deployment, updates.kget, updates.PART = original, original_get, original_part

        self.assertEqual((1, 0), (report["updates"], report["errors"]), "only plex is an app update")
        self.assertEqual({"updates": 2, "errors": 1}, report["homestead"])
        self.assertEqual({"plex": "", "homestead": "self", "homestead-nfs": "nfs"},
                         {w["name"]: w["homestead"] for w in report["workloads"]})

    def test_progress_is_readable_before_any_scan_has_run(self):
        checks.SCAN.update({"running": False, "done": 0, "total": 0, "current": "",
                             "started_at": 0.0, "finished_at": 0.0, "updates": 0})

        state = checks.scan_progress()

        self.assertFalse(state["running"])
        self.assertEqual(0, state["elapsed"])

    def test_progress_waits_for_surge_replacement_to_be_ready(self):
        self.dep["status"].update({"replicas": 2, "updatedReplicas": 1,
                                   "readyReplicas": 1, "availableReplicas": 1,
                                   "unavailableReplicas": 1})
        result = ROLLOUTS.progress("lab", "demo", self.dep)
        self.assertEqual("progressing", result["phase"])

        self.dep["status"].update({"replicas": 1, "updatedReplicas": 1,
                                   "readyReplicas": 1, "availableReplicas": 1,
                                   "unavailableReplicas": 0})
        result = ROLLOUTS.progress("lab", "demo", self.dep)
        self.assertEqual("ready", result["phase"])


    def test_progress_says_which_rollout_the_pod_template_is_from(self):
        template = self.dep["spec"]["template"]
        template.setdefault("metadata", {}).setdefault("annotations", {})[updates.ROLLOUT_AT] = "2026-09-30T06:33:03Z"
        self.assertEqual("2026-09-30T06:33:03Z", ROLLOUTS.progress("lab", "demo", self.dep)["rollout_at"])
        template["metadata"]["annotations"].pop(updates.ROLLOUT_AT)
        self.assertEqual("", ROLLOUTS.progress("lab", "demo", self.dep)["rollout_at"])

    def test_ha_progress_waits_for_both_updated_copies_and_the_old_pod_to_leave(self):
        self.dep["spec"]["replicas"] = 2
        self.dep["status"].update(replicas=2, updatedReplicas=1, readyReplicas=2, availableReplicas=2)
        self.assertEqual("progressing", ROLLOUTS.progress("lab", "demo", self.dep)["phase"],
                         "one old healthy pod and one new healthy pod are not a completed HA update")
        self.dep["status"].update(replicas=3, updatedReplicas=2, readyReplicas=2, availableReplicas=2)
        self.assertEqual("progressing", ROLLOUTS.progress("lab", "demo", self.dep)["phase"])
        self.dep["status"].update(replicas=2)
        result = ROLLOUTS.progress("lab", "demo", self.dep)
        self.assertEqual(("ready", 2, 2), (result["phase"], result["updated"], result["ready"]))

if __name__ == "__main__":
    unittest.main()


class RateLimitTests(unittest.TestCase):
    """A registry answering 429 - Docker Hub does, often - is a check put off,
    not a failed one: faster-whisper, piper and rustdesk showed "check failed"."""
    def limited(self):
        import urllib.error
        return urllib.error.HTTPError("https://registry/v2/x", 429, "Too Many Requests", {"Retry-After": "0"}, None)

    def test_a_429_is_waited_on_and_asked_again(self):
        calls = []
        def opener(req, credential=None, timeout=12):
            calls.append(req.full_url)
            if len(calls) < 3:
                raise self.limited()
            class Answer:
                headers = {}
                body = [b'{"tags": ["1.0.0"]}']
                def read(self, size=-1): return self.body.pop() if self.body else b""
                def __enter__(self): return self
                def __exit__(self, *a): return False
            return Answer()
        with mock.patch.object(REGISTRY, "_open", opener), mock.patch.object(updates.time, "sleep"):
            body, _ = REGISTRY._registry_json(REGISTRY.parse_image("nginx:1.0.0"), "tags/list", {})
        self.assertEqual(3, len(calls))
        self.assertIn(b"1.0.0", body)

    def test_still_limited_it_is_unchecked_with_what_the_last_scan_found(self):
        dep = copy.deepcopy(DEPLOYMENT)
        pod = {"metadata": {"namespace": "lab", "labels": {"app": "demo"}},
               "status": {"containerStatuses": [{"name": "demo", "imageID": "repo@sha256:" + "a" * 64}]}}
        last = {"workloads": [{"ns": "lab", "name": "demo", "images": [
            {"container": "demo", "available": True, "candidate_tag": "1.28.0", "candidate": "nginx:1.28.0"}]}]}
        def limited(*args, **kwargs):
            raise self.limited()
        with mock.patch.object(REGISTRY, "registry_tags", limited), mock.patch.object(REGISTRY, "manifest_info", limited), \
             mock.patch.object(REGISTRY, "_secret_credentials", lambda *a: {}), \
             mock.patch.dict(checks._LATEST, {"report": last}):
            result = checks._check_deployment(dep, [pod], persist=False)
        image = result["images"][0]
        self.assertEqual("", image["error"], "not a failed check")
        self.assertTrue(image["unchecked"] and image["rate_limited"] and image["stale"])
        self.assertTrue(image["available"], "a known update stays visible")
        self.assertTrue(result["available"])
        self.assertEqual(0, checks._summary([dict(result, homestead=False)])["errors"])
        self.assertEqual(1, checks._summary([dict(result, homestead=False)])["rate_limited"])

    def test_a_report_with_checks_put_off_is_asked_again_in_half_an_hour(self):
        self.assertEqual(checks.RETRY_RATE_LIMITED, checks._fresh_for({"rate_limited": 2}))
        self.assertEqual(checks.FRESH_FOR, checks._fresh_for({"rate_limited": 0}))
