"""A move, both halves, against a pretend pair of clusters.

The source and destination are the same fake here, in different namespaces -
which is also exactly how a single-cluster self-test of a move behaves.
"""
import copy
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_move as client
import homestead_move_engine as engine
import homestead_move_source as source


def merge(target, patch):
    for key, value in patch.items():
        if value is None:
            target.pop(key, None)
        elif isinstance(value, dict) and isinstance(target.get(key), dict):
            merge(target[key], value)
        else:
            target[key] = copy.deepcopy(value)


class FakeCluster:
    def __init__(self):
        self.objects = {}
        self.calls = []

    def put(self, path, obj):
        self.objects[path] = copy.deepcopy(obj)

    def get(self, path):
        path = path.split("?")[0]
        if path in self.objects:
            return copy.deepcopy(self.objects[path])
        prefix = path.rstrip("/") + "/"
        items = [copy.deepcopy(v) for k, v in self.objects.items()
                 if k.startswith(prefix) and "/" not in k[len(prefix):]]
        if items or path.endswith(("s", "deployments")) and not path.endswith(".io"):
            return {"items": items}
        raise urllib.error.HTTPError(path, 404, "missing", {}, None)

    def send(self, method, path, body=None, **kwargs):
        self.calls.append((method, path.split("?")[0], copy.deepcopy(body)))
        path = path.split("?")[0]
        if method == "POST":
            name = body["metadata"].get("name") or f"gen-{len(self.calls)}"
            body["metadata"]["name"] = name
            self.objects[f"{path}/{name}"] = copy.deepcopy(body)
            return copy.deepcopy(body)
        if method == "PUT":
            self.objects[path] = copy.deepcopy(body)
            return body
        if method == "PATCH":
            if path not in self.objects:
                raise urllib.error.HTTPError(path, 404, "missing", {}, None)
            merge(self.objects[path], body)
            return copy.deepcopy(self.objects[path])
        if method == "DELETE":
            if path not in self.objects:
                raise urllib.error.HTTPError(path, 404, "missing", {}, None)
            del self.objects[path]
            return {}
        raise AssertionError(method)


class FakeLonghorn:
    STORAGE_CLASS = "longhorn-r2"

    def __init__(self, cluster):
        self.cluster = cluster
        self.target = {"configured": True, "url": "s3://homestead-backups@us-east-1/",
                       "secret": "homestead-backup-credentials"}
        self.made = []
        self.restored = []

    def backup_target(self):
        return dict(self.target)

    def set_backup_target(self, url, secret="", poll="5m"):
        self.target = {"configured": True, "url": url, "secret": secret}

    def create_backup(self, volume, name=None):
        name = f"homestead-{len(self.made) + 1}"
        self.made.append({"name": name, "volume": volume})
        return {"backup": name, "volume": volume}

    def backups(self, volume=None):
        return [{"name": b["name"], "volume": b["volume"], "state": "Completed",
                 "progress": 100, "error": "", "restorable": True} for b in self.made]

    def restore_backup(self, cfg):
        self.restored.append(cfg)
        ns, name = cfg["namespace"], cfg["name"]
        self.cluster.put(f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{name}", {
            "metadata": {"name": name, "annotations": dict(cfg.get("annotations") or {})},
            "spec": {"volumeName": f"pvc-{name}-{ns}"}, "status": {"phase": "Bound"}})
        self.cluster.put(f"/apis/longhorn.io/v1beta2/namespaces/longhorn-system/volumes/"
                         f"pvc-{name}-{ns}", {"status": {"restoreRequired": False,
                                                          "state": "detached"}})
        return {"ok": True}


class FakeNetwork:
    def service_plan(self, cfg, require_workload=True):
        vip = cfg.get("vip") or ("192.0.2.250" if cfg["vip_mode"] != "cluster" else "")
        return {"vip": vip, "vip_mode": cfg["vip_mode"], "warnings": []}


class FakeOps:
    def __init__(self):
        self.started = []

    def start(self, kind, title, resource, href, ref, message=""):
        self.started.append((kind, title, ref))
        return {"id": f"op{len(self.started)}"}


def seed_frigate(cluster):
    """A running workload with one claim and a Service in lab."""
    cluster.put("/api/v1/namespaces/lab", {"metadata": {"name": "lab"}})
    cluster.put("/apis/apps/v1/namespaces/lab/deployments/frigate", {
        "metadata": {"name": "frigate", "namespace": "lab", "uid": "abc",
                     "resourceVersion": "9", "annotations": {
                         "deployment.kubernetes.io/revision": "4", "keep": "me"}},
        "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "frigate"}},
                 "template": {"metadata": {"labels": {"app": "frigate"}}, "spec": {
                     "containers": [{"name": "frigate", "image": "frigate:1",
                                     "ports": [{"name": "web", "containerPort": 5000}],
                                     "volumeMounts": [{"name": "c", "mountPath": "/config"}]}],
                     "volumes": [{"name": "c", "persistentVolumeClaim":
                                  {"claimName": "frigate-config"}}]}}},
        "status": {"readyReplicas": 1}})
    cluster.put("/api/v1/namespaces/lab/persistentvolumeclaims/frigate-config", {
        "metadata": {"name": "frigate-config"},
        "spec": {"volumeName": "pv-frigate", "storageClassName": "longhorn-r2",
                 "accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": "10Gi"}}}})
    cluster.put("/api/v1/persistentvolumes/pv-frigate", {
        "spec": {"csi": {"driver": "driver.longhorn.io", "volumeHandle": "pv-frigate"}}})
    cluster.put("/apis/storage.k8s.io/v1/storageclasses/longhorn-r2", {
        "provisioner": "driver.longhorn.io", "parameters": {"numberOfReplicas": "2"}})
    cluster.put("/api/v1/namespaces/lab/services/frigate", {
        "metadata": {"name": "frigate", "annotations": {"kube-vip.io/loadbalancerIPs": "192.0.2.242"}},
        "spec": {"type": "LoadBalancer", "selector": {"app": "frigate"}, "clusterIP": "10.0.0.9",
                 "ports": [{"name": "web", "port": 5000, "targetPort": "web", "nodePort": 31000}]}})
    cluster.put("/api/v1/namespaces/lab/pods/frigate-1", {
        "metadata": {"name": "frigate-1", "labels": {"app": "frigate"}}})


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.cluster = FakeCluster()
        self.lh = FakeLonghorn(self.cluster)
        seed_frigate(self.cluster)
        source.bind(self.cluster.get, self.cluster.send, self.lh, "lab")

    def stop_pods(self):
        self.cluster.objects.pop("/api/v1/namespaces/lab/pods/frigate-1", None)

    def deployment(self):
        return self.cluster.objects["/apis/apps/v1/namespaces/lab/deployments/frigate"]

    def test_the_definition_carries_what_the_far_side_needs_and_nothing_of_this_side(self):
        described = source.definition("container", "frigate")

        meta = described["object"]["metadata"]
        self.assertNotIn("uid", meta)
        self.assertNotIn("resourceVersion", meta)
        self.assertEqual({"keep": "me"}, meta["annotations"])
        self.assertEqual(0, described["object"]["spec"]["replicas"], "arrives stopped")
        self.assertEqual({"replicas": 1}, described["origin"])
        service = described["services"][0]
        self.assertNotIn("clusterIP", service["spec"])
        self.assertNotIn("nodePort", service["spec"]["ports"][0])
        self.assertNotIn("kube-vip.io/loadbalancerIPs",
                         service["metadata"].get("annotations", {}),
                         "the far side gets its own address")
        self.assertEqual([{"claim": "frigate-config", "volume": "pv-frigate", "size_gb": 10}],
                         [{k: c[k] for k in ("claim", "volume", "size_gb")}
                          for c in described["claims"]])

    def test_stopping_twice_remembers_how_it_was_running_the_first_time(self):
        """Otherwise a retry would record it as stopped, and putting it back
        would leave it stopped."""
        source.quiesce("container", "frigate")
        source.quiesce("container", "frigate")

        self.assertEqual(0, self.deployment()["spec"]["replicas"])
        self.assertEqual({"replicas": 1}, source.status("container", "frigate")["origin"])

    def test_no_backup_while_it_still_runs(self):
        source.quiesce("container", "frigate")

        with self.assertRaisesRegex(ValueError, "still running"):
            source.backup("container", "frigate")

    def test_no_backup_of_something_not_stopped_for_a_move(self):
        self.stop_pods()
        with self.assertRaisesRegex(ValueError, "not been stopped for a move"):
            source.backup("container", "frigate")

    def test_backing_up_twice_makes_one_backup_per_claim(self):
        source.quiesce("container", "frigate")
        self.stop_pods()

        first = source.backup("container", "frigate")["backups"]
        second = source.backup("container", "frigate")["backups"]

        self.assertEqual(first, second)
        self.assertEqual(1, len(self.lh.made))

    def test_putting_it_back_runs_it_as_it_was_and_forgets_the_move(self):
        source.quiesce("container", "frigate")
        source.release("container", "frigate")

        deployment = self.deployment()
        self.assertEqual(1, deployment["spec"]["replicas"])
        self.assertFalse(source.status("container", "frigate")["stopped_for_move"])

    def test_removing_is_only_for_something_a_move_stopped(self):
        self.stop_pods()
        with self.assertRaisesRegex(ValueError, "not stopped for a move"):
            source.remove("container", "frigate")

    def test_removing_takes_the_workload_and_its_service_and_keeps_volumes_unless_asked(self):
        source.quiesce("container", "frigate")
        self.stop_pods()

        source.remove("container", "frigate")

        self.assertNotIn("/apis/apps/v1/namespaces/lab/deployments/frigate", self.cluster.objects)
        self.assertNotIn("/api/v1/namespaces/lab/services/frigate", self.cluster.objects)
        self.assertIn("/api/v1/namespaces/lab/persistentvolumeclaims/frigate-config",
                      self.cluster.objects)

    def test_the_bucket_keys_are_handed_over_with_whether_they_reach_off_cluster(self):
        import base64
        self.cluster.put("/api/v1/namespaces/longhorn-system/secrets/homestead-backup-credentials", {
            "data": {"AWS_ENDPOINTS": base64.b64encode(b"http://192.0.2.244:9000").decode(),
                     "AWS_ACCESS_KEY_ID": base64.b64encode(b"homestead").decode()}})

        handed = source.target()

        self.assertEqual("http://192.0.2.244:9000", handed["endpoint"])
        self.assertTrue(handed["reachable_off_cluster"])
        self.assertEqual("homestead", handed["credentials"]["AWS_ACCESS_KEY_ID"])

    def test_no_target_no_keys(self):
        self.lh.target = {"configured": False}

        with self.assertRaisesRegex(ValueError, "no Longhorn backup target"):
            source.target()


class VmSourceTests(unittest.TestCase):
    def setUp(self):
        self.cluster = FakeCluster()
        self.lh = FakeLonghorn(self.cluster)
        source.bind(self.cluster.get, self.cluster.send, self.lh, "lab")
        self.cluster.put("/apis/kubevirt.io/v1/namespaces/lab/virtualmachines/ha", {
            "metadata": {"name": "ha", "annotations": {
                "harvesterhci.io/volumeClaimTemplates": "[...]"}},
            "spec": {"runStrategy": "RerunOnFailure",
                     "dataVolumeTemplates": [{"metadata": {"name": "ha-disk"}}],
                     "template": {"spec": {"domain": {"cpu": {"cores": 2}}, "volumes": [
                         {"name": "root", "dataVolume": {"name": "ha-disk"}},
                         {"name": "ci", "cloudInitNoCloud": {
                             "userDataSecretRef": {"name": "ha-cloudinit"}}}]}}}})
        self.cluster.put("/api/v1/namespaces/lab/persistentvolumeclaims/ha-disk", {
            "spec": {"volumeName": "pv-ha", "volumeMode": "Block", "storageClassName": "vmclass",
                     "accessModes": ["ReadWriteMany"], "resources": {"requests": {"storage": "40Gi"}}}})
        self.cluster.put("/api/v1/persistentvolumes/pv-ha", {
            "spec": {"csi": {"driver": "driver.longhorn.io", "volumeHandle": "pv-ha"}}})
        self.cluster.put("/apis/storage.k8s.io/v1/storageclasses/vmclass", {
            "parameters": {"migratable": "true", "numberOfReplicas": "3"}})
        self.cluster.put("/apis/longhorn.io/v1beta2/namespaces/longhorn-system/volumes/pv-ha", {
            "spec": {"backingImage": "ubuntu-image"}})
        self.cluster.put("/api/v1/namespaces/lab/secrets/ha-cloudinit", {
            "metadata": {"name": "ha-cloudinit", "uid": "x"}, "data": {"userdata": "e30="}})

    def test_a_vm_travels_with_plain_claims_and_its_cloud_init(self):
        described = source.definition("vm", "ha")

        spec = described["object"]["spec"]
        self.assertNotIn("dataVolumeTemplates", spec, "the claims will already exist")
        self.assertEqual("Halted", spec["runStrategy"], "arrives stopped")
        self.assertEqual({"claimName": "ha-disk"},
                         spec["template"]["spec"]["volumes"][0]["persistentVolumeClaim"])
        self.assertNotIn("harvesterhci.io/volumeClaimTemplates",
                         described["object"]["metadata"].get("annotations", {}))
        self.assertEqual(["ha-cloudinit"], [s["metadata"]["name"] for s in described["secrets"]])
        self.assertEqual({"runStrategy": "RerunOnFailure"}, described["origin"])
        disk = described["claims"][0]
        self.assertEqual(("Block", True, "ubuntu-image", "ReadWriteMany", 3),
                         (disk["volume_mode"], disk["migratable"], disk["backing_image"],
                          disk["access_mode"], disk["replicas"]))

    def test_stopping_a_vm_halts_it_and_putting_it_back_restores_its_strategy(self):
        source.quiesce("vm", "ha")
        vm = self.cluster.objects["/apis/kubevirt.io/v1/namespaces/lab/virtualmachines/ha"]
        self.assertEqual("Halted", vm["spec"]["runStrategy"])

        source.release("vm", "ha")
        vm = self.cluster.objects["/apis/kubevirt.io/v1/namespaces/lab/virtualmachines/ha"]
        self.assertEqual("RerunOnFailure", vm["spec"]["runStrategy"])

    def test_a_vm_disk_backup_also_backs_up_the_image_it_is_built_on(self):
        source.quiesce("vm", "ha")

        source.backup("vm", "ha")

        self.assertIn(("POST", "/apis/longhorn.io/v1beta2/namespaces/longhorn-system/"
                               "backupbackingimages"),
                      [(m, p) for m, p, _ in self.cluster.calls])


class EngineTests(unittest.TestCase):
    """The destination drives the source over HTTP; here, over a function call."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cluster = FakeCluster()
        self.lh = FakeLonghorn(self.cluster)
        self.ops = FakeOps()
        seed_frigate(self.cluster)
        source.bind(self.cluster.get, self.cluster.send, self.lh, "lab")
        self.unreachable = False
        self.remote_calls = []

        real_remote = client.remote

        def remote(name, path, body=None):
            self.remote_calls.append(path.split("?")[0])
            if self.unreachable:
                raise client.Unreachable("could not reach shed")
            query = dict(part.split("=", 1) for part in path.split("?", 1)[1].split("&")) \
                if "?" in path else {}
            route = path.split("?")[0]
            if route == "/api/move/target":
                return {"url": self.lh.target["url"], "endpoint": "", "credentials": {},
                        "reachable_off_cluster": True}
            if route == "/api/move/definition":
                return source.definition(query["kind"], query["name"])
            if route == "/api/move/source-status":
                return source.status(query["kind"], query["name"])
            if route == "/api/move/source":
                action = {"quiesce": source.quiesce, "backup": source.backup,
                          "release": source.release}.get(body["action"])
                if body["action"] == "remove":
                    return source.remove(body["kind"], body["name"], body.get("volumes"))
                return action(body["kind"], body["name"])
            raise AssertionError(route)

        client.remote = remote
        self.addCleanup(setattr, client, "remote", real_remote)
        engine.bind(self.cluster.get, self.cluster.send, self.lh, client, FakeNetwork(),
                    self.ops, self.tmp.name, "lab")
        # The destination's own endpoint for the bucket matches the source's, so
        # this pair counts as already sharing backup storage.
        engine._here_endpoint = lambda target: ""

    def run_until_settled(self, limit=40):
        for _ in range(limit):
            move = engine.moves()[0]
            if move["status"] != "running":
                return move
            if move["phase"] == "quiescing":
                self.cluster.objects.pop("/api/v1/namespaces/lab/pods/frigate-1", None)
            if move["phase"] == "starting":
                path = "/apis/apps/v1/namespaces/moved/deployments/frigate"
                if path in self.cluster.objects:
                    self.cluster.objects[path].setdefault("status", {})["readyReplicas"] = 1
            engine.tick_all()
        return engine.moves()[0]

    def test_a_clean_plan_has_no_blockers(self):
        planned = engine.plan("shed", "container", "frigate", "moved", "automatic")

        self.assertTrue(planned["ok"], planned["blockers"])
        self.assertTrue(planned["joined"])
        self.assertEqual(10, planned["total_gb"])
        self.assertTrue(any("will be created" in w for w in planned["warnings"]))

    def test_a_name_already_taken_here_blocks_the_move(self):
        planned = engine.plan("shed", "container", "frigate", "lab", "automatic")

        self.assertFalse(planned["ok"])
        self.assertTrue(any("already exists" in b for b in planned["blockers"]))

    def test_a_bucket_only_the_source_can_reach_blocks_a_move_that_needs_it(self):
        engine._here_endpoint = lambda target: "http://elsewhere:9000"
        real = client.remote

        def remote(name, path, body=None):
            if path.startswith("/api/move/target"):
                return {"url": "s3://other@us-east-1/", "endpoint": "http://x.svc:9000",
                        "credentials": {}, "reachable_off_cluster": False}
            return real(name, path, body)

        client.remote = remote
        planned = engine.plan("shed", "container", "frigate", "moved", "automatic")

        self.assertTrue(any("only reachable inside" in b for b in planned["blockers"]))
        self.assertTrue(any("changes from" in w for w in planned["warnings"]))

    def test_a_move_runs_every_phase_and_lands_running(self):
        engine.start("shed", "container", "frigate", "moved", "automatic")

        move = self.run_until_settled()

        self.assertEqual("succeeded", move["status"], move["message"])
        self.assertEqual(100, move["progress"])
        created = self.cluster.objects["/apis/apps/v1/namespaces/moved/deployments/frigate"]
        self.assertEqual(1, created["spec"]["replicas"], "started as it ran on the source")
        self.assertEqual(move["id"], created["metadata"]["annotations"]["homestead.io/move-id"])
        service = self.cluster.objects["/api/v1/namespaces/moved/services/frigate"]
        self.assertEqual("192.0.2.250",
                         service["metadata"]["annotations"]["kube-vip.io/loadbalancerIPs"])
        self.assertEqual("frigate-config", self.lh.restored[0]["name"])
        self.assertEqual("moved", self.lh.restored[0]["namespace"])
        source_copy = self.cluster.objects["/apis/apps/v1/namespaces/lab/deployments/frigate"]
        self.assertEqual(0, source_copy["spec"]["replicas"], "the source stays stopped")
        self.assertEqual(("move", "Move frigate from shed"), self.ops.started[0][:2])

    def test_a_restart_part_way_through_picks_up_where_it_was(self):
        """The move lives on disk, not in memory."""
        engine.start("shed", "container", "frigate", "moved", "automatic")
        engine.tick_all()
        engine.tick_all()
        on_disk = json.loads((Path(self.tmp.name) / "moves.json").read_text())

        self.assertEqual("quiescing", on_disk[0]["phase"])
        move = self.run_until_settled()
        self.assertEqual("succeeded", move["status"])
        self.assertEqual(1, len(self.lh.made), "resuming does not back up twice")

    def test_an_unreachable_source_is_waited_out_not_failed(self):
        engine.start("shed", "container", "frigate", "moved", "automatic")
        self.unreachable = True

        for _ in range(5):
            engine.tick_all()

        move = engine.moves()[0]
        self.assertEqual("running", move["status"])
        self.assertIn("could not reach shed", move["message"])

        self.unreachable = False
        self.assertEqual("succeeded", self.run_until_settled()["status"])

    def test_a_refusal_fails_the_move_and_retry_resumes_it(self):
        engine.start("shed", "container", "frigate", "moved", "automatic")
        # Someone creates the claim by hand in the meantime.
        self.cluster.put("/api/v1/namespaces/moved/persistentvolumeclaims/frigate-config",
                         {"metadata": {"name": "frigate-config"}})

        move = self.run_until_settled()
        self.assertEqual("failed", move["status"])
        self.assertIn("not made by this move", move["message"])

        del self.cluster.objects["/api/v1/namespaces/moved/persistentvolumeclaims/frigate-config"]
        engine.retry(move["id"])
        self.assertEqual("succeeded", self.run_until_settled()["status"])
        self.assertEqual(2, len(self.ops.started), "a retry is a fresh entry in Activity")

    def test_putting_it_back_restarts_the_source_and_removes_only_what_the_move_made(self):
        engine.start("shed", "container", "frigate", "moved", "automatic")
        move = self.run_until_settled()
        self.cluster.put("/api/v1/namespaces/moved/services/unrelated",
                         {"metadata": {"name": "unrelated"}})

        engine.abandon(move["id"])

        self.assertNotIn("/apis/apps/v1/namespaces/moved/deployments/frigate", self.cluster.objects)
        self.assertNotIn("/api/v1/namespaces/moved/persistentvolumeclaims/frigate-config",
                         self.cluster.objects)
        self.assertIn("/api/v1/namespaces/moved/services/unrelated", self.cluster.objects)
        source_copy = self.cluster.objects["/apis/apps/v1/namespaces/lab/deployments/frigate"]
        self.assertEqual(1, source_copy["spec"]["replicas"])
        self.assertEqual("cancelled", engine.moves()[0]["status"])

    def test_finishing_removes_the_stopped_original_and_nothing_can_be_put_back_after(self):
        engine.start("shed", "container", "frigate", "moved", "automatic")
        move = self.run_until_settled()

        engine.finish(move["id"])

        self.assertNotIn("/apis/apps/v1/namespaces/lab/deployments/frigate", self.cluster.objects)
        self.assertTrue(engine.moves()[0]["source_removed"])
        with self.assertRaisesRegex(ValueError, "nothing to put back"):
            engine.abandon(move["id"])

    def test_the_browser_never_sees_definitions_or_keys(self):
        engine.start("shed", "container", "frigate", "moved", "automatic")
        self.run_until_settled()

        public = json.dumps(engine.moves())

        self.assertNotIn("containers", public)
        self.assertNotIn("credentials", public)
        self.assertNotIn("origin", public)


if __name__ == "__main__":
    unittest.main()
