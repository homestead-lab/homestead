"""A move, both halves, against a pretend pair of clusters.

The source and destination are the same fake here, in different namespaces -
which is also exactly how a single-cluster self-test of a move behaves.
"""
import copy
import json
import sys
import tempfile
import unittest
from unittest import mock
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
            meta = self.objects[path].get("metadata") or {}
            version = (body.get("metadata") or {}).get("resourceVersion")
            if version and meta.get("resourceVersion") != version:
                raise urllib.error.HTTPError(path, 409, "changed", {}, None)
            merge(self.objects[path], body)
            if meta.get("resourceVersion"):
                self.objects[path]["metadata"]["resourceVersion"] = str(int(meta["resourceVersion"]) + 1)
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

    def restore_support(self):
        return {"ready": True, "can_install": False}

    ensure_restore_support = restore_support

    @staticmethod
    def validate_restore_class(*args, **kwargs):
        import homestead_longhorn
        return homestead_longhorn.validate_restore_class(*args, **kwargs)

    @staticmethod
    def restore_problem(pvc):
        return ""

    def backup_target(self):
        return dict(self.target)

    harvester = False

    def on_harvester(self):
        return self.harvester

    def set_backup_target(self, url, secret="", poll="5m", keys=None):
        self.target = {"configured": bool(url), "url": url, "secret": secret, "interval": poll}
        self.keys = keys

    def backup_target_state(self):
        if self.harvester:
            return {"harvester": True, "value": copy.deepcopy(getattr(self, "setting", {
                "type": "nfs", "endpoint": self.target["url"], "refreshIntervalInSeconds": 300}))}
        return {"harvester": False, "value": {
            "backupTargetURL": self.target.get("url", ""), "credentialSecret": self.target.get("secret", ""),
            "pollInterval": self.target.get("interval", "")}}

    @staticmethod
    def transfer_target_state(*args):
        import homestead_longhorn
        return homestead_longhorn.transfer_target_state(*args)

    def replace_backup_target_state(self, expected, replacement):
        if self.backup_target_state() != expected:
            return False
        value = replacement["value"]
        if replacement["harvester"]:
            self.setting = copy.deepcopy(value)
            url = f"s3://{value['bucketName']}@{value['bucketRegion']}/" if value.get("type") == "s3" else value.get("endpoint", "")
            keys = {"access_key": value.get("accessKeyId", ""), "secret_key": value.get("secretAccessKey", ""),
                    "endpoint": value.get("endpoint", "")}
            self.set_backup_target(url, poll=f"{value.get('refreshIntervalInSeconds', 300)}s", keys=keys)
        else:
            self.set_backup_target(value["backupTargetURL"], value["credentialSecret"], value["pollInterval"])
        return True

    def create_backup(self, volume, name=None):
        name = f"homestead-{len(self.made) + 1}"
        self.made.append({"name": name, "volume": volume})
        return {"backup": name, "volume": volume}

    def ensure_move_backup(self, volume, snapshot, backup):
        if not any(b["name"] == backup for b in self.made):
            self.made.append({"name": backup, "volume": volume})
        return {"backup": backup}

    def backups(self, volume=None, strict=False):
        return [{"name": b["name"], "volume": b["volume"], "state": b.get("state", "Completed"),
                 "progress": 100, "error": b.get("error", ""),
                 "restorable": b.get("state", "Completed") == "Completed"} for b in self.made]

    def restore_backup(self, cfg):
        self.restored.append(cfg)
        ns, name = cfg["namespace"], cfg["name"]
        self.cluster.put(f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{name}", {
            "metadata": {"name": name, "annotations": dict(cfg.get("annotations") or {})},
            "spec": {"volumeName": f"pvc-{name}-{ns}"}, "status": {"phase": "Bound"}})
        self.cluster.put(f"/apis/longhorn.io/v1beta2/namespaces/longhorn-system/volumes/"
                         f"pvc-{name}-{ns}", {"status": {"restoreInitiated": True, "restoreRequired": False,
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
        "metadata": {"name": "longhorn-r2"},
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


class VolumeSourceTests(unittest.TestCase):
    """A volume moves on its own only while nothing mounts it."""

    def setUp(self):
        self.cluster = FakeCluster()
        self.lh = FakeLonghorn(self.cluster)
        seed_frigate(self.cluster)
        self.cluster.put("/api/v1/namespaces/lab/pods/frigate-1", {
            "metadata": {"name": "frigate-1", "labels": {"app": "frigate"}},
            "spec": {"volumes": [{"name": "c", "persistentVolumeClaim": {"claimName": "frigate-config"}}]},
            "status": {"phase": "Running"}})
        source.bind(self.cluster.get, self.cluster.send, self.lh, "lab")

    def claim(self):
        return self.cluster.objects["/api/v1/namespaces/lab/persistentvolumeclaims/frigate-config"]

    def test_a_volume_in_use_is_refused_naming_what_uses_it(self):
        with self.assertRaisesRegex(ValueError, "in use by frigate-1"):
            source.quiesce("volume", "frigate-config")
        self.assertNotIn("annotations", self.claim()["metadata"])

    def test_an_unused_volume_is_held_backed_up_put_back_and_removed(self):
        self.cluster.objects.pop("/api/v1/namespaces/lab/pods/frigate-1")
        source.quiesce("volume", "frigate-config")
        self.assertIn("homestead.io/move-origin", self.claim()["metadata"]["annotations"])
        described = source.definition("volume", "frigate-config")
        self.assertEqual(["frigate-config"], [c["claim"] for c in described["claims"]])
        self.assertEqual([], described["services"])
        made = source.backup("volume", "frigate-config")
        self.assertEqual(1, len(made["backups"]))
        spec = copy.deepcopy(self.claim()["spec"])
        source.release("volume", "frigate-config")
        self.assertEqual(spec, self.claim()["spec"], "putting it back leaves the claim itself alone")
        self.assertNotIn("homestead.io/move-origin", self.claim()["metadata"].get("annotations") or {})
        source.quiesce("volume", "frigate-config")
        source.remove("volume", "frigate-config")
        self.assertNotIn("/api/v1/namespaces/lab/persistentvolumeclaims/frigate-config", self.cluster.objects)
        self.assertIn("/apis/apps/v1/namespaces/lab/deployments/frigate", self.cluster.objects,
                      "the app that named it is not touched")

    def test_a_size_in_plain_bytes_is_read_as_gib(self):
        # Harvester writes claim sizes in bytes: esphome-appdata showed 32212254720 GB.
        self.claim()["spec"]["resources"]["requests"]["storage"] = "32212254720"
        self.cluster.objects.pop("/api/v1/namespaces/lab/pods/frigate-1")
        self.assertEqual(30, source.definition("volume", "frigate-config")["claims"][0]["size_gb"])
        with mock.patch.object(client, "kget", self.cluster.get), mock.patch.object(client, "NS", "lab"):
            self.assertEqual(30, client._volume_rows([])[0]["size_gb"])

    def test_the_inventory_lists_volumes_with_what_uses_them(self):
        with mock.patch.object(client, "kget", self.cluster.get), mock.patch.object(client, "NS", "lab"):
            rows = client._volume_rows([{"name": "frigate", "running": True,
                                         "volumes": [{"claim": "frigate-config"}]}])
            self.assertEqual(["frigate-config"], [r["name"] for r in rows])
            self.assertFalse(rows[0]["movable"])
            self.assertIn("in use by frigate", rows[0]["blockers"][0])
            rows = client._volume_rows([{"name": "frigate", "running": False,
                                         "volumes": [{"claim": "frigate-config"}]}])
            self.assertTrue(rows[0]["movable"])
            self.assertIn("frigate uses it and stays here", rows[0]["warnings"][0])
            self.cluster.put("/api/v1/namespaces/lab/persistentvolumeclaims/homestead-data", {
                "metadata": {"name": "homestead-data"}, "spec": {}})
            self.assertNotIn("homestead-data", [r["name"] for r in client._volume_rows([])])


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
        # Fake clusters must not invoke server.py's live API cleanup hook.
        cleanup = mock.patch.object(engine, "after_finish", None)
        cleanup.start()
        self.addCleanup(cleanup.stop)
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
            if route == "/api/move/hello":
                return client.hello()
            if route == "/api/move/target":
                return {"url": self.lh.target["url"], "endpoint": "", "credentials": {},
                        "reachable_off_cluster": True}
            if route == "/api/move/definition":
                return source.in_namespace(query.get("namespace"), source.definition, query["kind"], query["name"])
            if route == "/api/move/source-status":
                return source.in_namespace(query.get("namespace"), source.status, query["kind"], query["name"])
            if route == "/api/move/source":
                identity = {key: body[key] for key in ("transfer_id", "expected_uid") if key in body}
                if body["action"] == "quiesce":
                    identity["expected_version"] = body.get("expected_version", "")
                if body["action"] == "backup":
                    return source.in_namespace(body.get("namespace"), source.backup, body["kind"], body["name"], body.get("retry_failed", False), body.get("claims"), **identity)
                action = {"quiesce": source.quiesce, "backup": source.backup,
                          "release": source.release}.get(body["action"])
                if body["action"] == "remove":
                    return source.in_namespace(body.get("namespace"), source.remove, body["kind"], body["name"], body.get("volumes"), body.get("claims"))
                try:
                    return source.in_namespace(body.get("namespace"), action, body["kind"], body["name"], **identity)
                except source.PendingRecovery as error:
                    # The real source returns HTTP 503, mapped by remote().
                    raise client.Unreachable(str(error)) from error
            raise AssertionError(route)

        client.remote = remote
        self.addCleanup(setattr, client, "remote", real_remote)
        engine.bind(self.cluster.get, self.cluster.send, self.lh, client, FakeNetwork(),
                    self.ops, self.tmp.name, "lab")
        # The destination's own endpoint for the bucket matches the source's, so
        # this pair counts as already sharing backup storage.
        engine._here_endpoint = lambda target: ""

    def run_until_settled(self, limit=40, move_id=None):
        for _ in range(limit):
            move = next((m for m in engine.moves() if m["id"] == move_id), engine.moves()[0])
            if move["status"] != "running":
                return move
            if move["phase"] == "quiescing":
                self.cluster.objects.pop("/api/v1/namespaces/lab/pods/frigate-1", None)
            if move["phase"] == "starting":
                path = "/apis/apps/v1/namespaces/moved/deployments/frigate"
                if path in self.cluster.objects:
                    self.cluster.objects[path].setdefault("status", {})["readyReplicas"] = 1
            engine.tick_all()
        return next((m for m in engine.moves() if m["id"] == move_id), engine.moves()[0])

    def test_snapshot_installation_does_not_stop_source_and_survives_engine_restart(self):
        with mock.patch.object(self.lh, "ensure_restore_support", return_value={"ready": False, "message": "Installing snapshots"}):
            move = engine.start("shed", "container", "frigate", "moved", "automatic")
            engine.tick_all()
            self.assertEqual("joining", engine._find(move["id"])["phase"])
            self.assertEqual(1, self.cluster.objects["/apis/apps/v1/namespaces/lab/deployments/frigate"]["spec"]["replicas"])
            self.assertNotIn("/api/move/source", self.remote_calls)
        engine.bind(self.cluster.get, self.cluster.send, self.lh, client, FakeNetwork(), self.ops, self.tmp.name, "lab")
        finished = self.run_until_settled(move_id=move["id"])
        self.assertEqual("succeeded", finished["status"], finished["message"])

    def test_plan_rejects_wait_for_consumer_and_filesystem_migratable_classes(self):
        klass = self.cluster.objects["/apis/storage.k8s.io/v1/storageclasses/longhorn-r2"]
        klass["volumeBindingMode"] = "WaitForFirstConsumer"
        planned = engine.plan("shed", "container", "frigate", "moved", "automatic")
        self.assertFalse(planned["ok"])
        self.assertTrue(any("Immediate" in reason for reason in planned["blockers"]))
        klass.pop("volumeBindingMode")
        klass.setdefault("parameters", {})["migratable"] = "true"
        planned = engine.plan("shed", "container", "frigate", "moved", "automatic")
        self.assertFalse(planned["ok"])
        self.assertTrue(any("migratable disabled" in reason for reason in planned["blockers"]))

    def test_effective_destination_class_is_frozen_when_default_changes(self):
        move = engine.start("shed", "container", "frigate", "moved", "automatic")
        self.assertEqual("longhorn-r2", move["storage_class"])
        with mock.patch.object(self.lh, "STORAGE_CLASS", "missing-new-default"):
            finished = self.run_until_settled(move_id=move["id"])
        self.assertEqual("succeeded", finished["status"], finished["message"])
        self.assertEqual("longhorn-r2", self.lh.restored[0]["storage_class"])

    def fail_backup(self):
        move = engine.start("shed", "container", "frigate", "moved", "automatic")
        self.cluster.objects.pop("/api/v1/namespaces/lab/pods/frigate-1", None)
        create = self.lh.ensure_move_backup
        def pending(*args):
            result = create(*args)
            self.lh.made[-1]["state"] = "InProgress"
            return result
        with mock.patch.object(self.lh, "ensure_move_backup", side_effect=pending):
            for _ in range(10):
                engine.tick_all()
                if self.lh.made:
                    break
        self.assertEqual(1, len(self.lh.made))
        self.lh.made[0].update(state="Error", error="cannot find matched snapshot in longhorn engine")
        engine.tick_all()
        failed = engine._find(move["id"])
        self.assertEqual(("failed", "backing-up"), (failed["status"], failed["phase"]))
        return failed

    def test_retry_after_restart_replaces_failed_backup_and_restores_new_one(self):
        failed = self.fail_backup()
        old = self.lh.made[0]["name"]
        engine.bind(self.cluster.get, self.cluster.send, self.lh, client, FakeNetwork(),
                    self.ops, self.tmp.name, "lab")
        engine.retry(failed["id"])
        move = self.run_until_settled(move_id=failed["id"])
        self.assertEqual("succeeded", move["status"], move["message"])
        self.assertEqual(2, len(self.lh.made))
        self.assertNotEqual(old, self.lh.restored[0]["backup"])
        self.assertEqual(old, self.lh.made[0]["name"], "old backups are retained")

    def test_dismissed_activity_then_new_attempt_recovers_source_backup_references(self):
        import homestead_operations as operations
        operations.bind(self.cluster.get, self.tmp.name, lambda *args: {})
        with mock.patch.object(engine, "OPS", operations), \
             mock.patch.dict(operations.RESOLVERS, {"move": engine.op_state}):
            failed = self.fail_backup()
            item = next(o for o in operations.list_operations() if o["id"] == failed["op"])
            self.assertEqual("failed", item["status"])
            operations.dismiss(item["id"])
            self.assertEqual([], operations.list_operations())
            # Dismissal clears Activity, leaving the source's annotations and
            # the failed migration journal intact across a restart.
            engine.bind(self.cluster.get, self.cluster.send, self.lh, client, FakeNetwork(),
                        operations, self.tmp.name, "lab")
            new = engine.start("shed", "container", "frigate", "moved", "automatic")
            move = self.run_until_settled(move_id=new["id"])
            self.assertEqual("succeeded", move["status"], move["message"])
            self.assertEqual(2, len(self.lh.made))
            self.assertNotEqual(self.lh.made[0]["name"], self.lh.restored[0]["backup"])
            self.assertEqual("failed", engine._find(failed["id"])["status"])

    def test_new_attempt_recovers_when_old_destination_journal_was_cleared(self):
        self.fail_backup()
        engine._write([])
        new = engine.start("shed", "container", "frigate", "moved", "automatic")
        move = self.run_until_settled(move_id=new["id"])
        self.assertEqual("succeeded", move["status"], move["message"])
        self.assertEqual(2, len(self.lh.made))
        self.assertNotEqual(self.lh.made[0]["name"], self.lh.restored[0]["backup"])

    def test_selected_destination_class_is_persisted_and_used_on_restore(self):
        self.cluster.put("/apis/storage.k8s.io/v1/storageclasses/fast", {
            "metadata": {"name": "fast"}, "provisioner": "driver.longhorn.io",
            "parameters": {"numberOfReplicas": "3", "diskSelector": "ssd"}})
        planned = engine.plan("shed", "container", "frigate", "moved", "automatic", "", "fast")
        self.assertTrue(planned["ok"], planned["blockers"])
        self.assertEqual(["fast", "longhorn-r2"], planned["storage_classes"])
        engine.start("shed", "container", "frigate", "moved", "automatic", "", "fast")
        move = self.run_until_settled()
        self.assertEqual("succeeded", move["status"], move["message"])
        self.assertEqual("fast", move["storage_class"])
        self.assertEqual("fast", self.lh.restored[0]["storage_class"])
        self.assertNotIn("replicas", self.lh.restored[0], "chosen class supplies replicas")

    def test_a_clean_plan_has_no_blockers(self):
        planned = engine.plan("shed", "container", "frigate", "moved", "automatic")

        self.assertTrue(planned["ok"], planned["blockers"])
        self.assertTrue(planned["joined"])
        self.assertEqual(10, planned["total_gb"])
        self.assertTrue(any("will be created" in w for w in planned["warnings"]))

    def test_a_source_too_old_to_send_blocks_the_move_before_anything_stops(self):
        real = client.remote

        def remote(name, path, body=None):
            if path == "/api/move/hello":
                return {"version": "2.8.40", "protocol": 0}
            return real(name, path, body)

        client.remote = remote
        planned = engine.plan("shed", "container", "frigate", "moved", "automatic")

        self.assertFalse(planned["ok"])
        self.assertIn("Update shed first", planned["blockers"][0])
        with self.assertRaisesRegex(ValueError, "too old"):
            engine.start("shed", "container", "frigate", "moved", "automatic")
        self.assertEqual([], engine.moves())
        self.assertNotIn("/api/move/source", self.remote_calls, "nothing was stopped")

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

    def _join(self):
        there = {"url": "s3://homestead-backups@us-east-1/", "endpoint": "http://192.0.2.108:9000",
                 "credentials": {"AWS_ACCESS_KEY_ID": "ak", "AWS_SECRET_ACCESS_KEY": "sk",
                                 "AWS_ENDPOINTS": "http://192.0.2.108:9000", "VIRTUAL_HOSTED_STYLE": "false"}}
        self.lh.target = {"configured": True, "url": "nfs://nas:/backups", "secret": ""}
        move = {"id": "0123456789ab", "cluster": "shed", "claims": [], "phase": "joining"}
        with mock.patch.object(client, "remote", lambda name, path, body=None: there),                 mock.patch.object(client, "answers", lambda endpoint, timeout=3: self.reachable):
            engine._joining(move)
        return move

    reachable = True

    def test_a_store_this_cluster_cannot_reach_stops_the_join_and_says_so(self):
        self.reachable = False
        with self.assertRaises(ValueError) as caught:
            self._join()
        self.assertIn("cannot reach shed's backup storage at http://192.0.2.108:9000", str(caught.exception))
        self.assertEqual("nfs://nas:/backups", self.lh.target["url"], "the target is left as it was")

    def test_a_kubernetes_refusal_stops_the_move_with_its_reason(self):
        import io, urllib.error
        engine.start("shed", "container", "frigate", "moved", "automatic")
        move = engine.moves()[0]
        refusal = urllib.error.HTTPError("x", 422, "Unprocessable", {}, io.BytesIO(
            b'{"message": "admission webhook denied the request: backup target unreachable"}'))
        def refuse(m):
            raise refusal
        with mock.patch.dict(engine.HANDLERS, {move["phase"]: refuse}):
            engine.tick_all()
        move = engine.moves()[0]
        self.assertEqual("failed", move["status"])
        self.assertIn("backup target unreachable", move["message"])

    def test_cancelling_before_anything_stopped_does_not_ask_the_source(self):
        engine.start("shed", "container", "frigate", "moved", "automatic")
        move = engine.moves()[0]
        self.remote_calls.clear()
        self.unreachable = True
        result = engine.abandon(move["id"])
        self.assertEqual("cancelled", result["status"])
        self.assertFalse(result["source_stopped"])
        self.assertNotIn("/api/move/source", self.remote_calls)

    def test_on_harvester_the_join_hands_the_keys_to_its_setting(self):
        """Harvester's backup-target setting holds the keys itself; a Secret beside it is never read."""
        self.lh.harvester = True
        self._join()
        self.assertEqual("s3://homestead-backups@us-east-1/", self.lh.target["url"])
        self.assertEqual({"access_key": "ak", "secret_key": "sk", "endpoint": "http://192.0.2.108:9000"}, self.lh.keys)

    def test_on_longhorn_the_join_names_the_secret(self):
        self._join()
        self.assertTrue(self.lh.target["secret"].startswith(engine.JOIN_SECRET + "-0123456789ab-"))
        self.assertIsNone(self.lh.keys)

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

    def test_a_volume_moves_on_its_own_and_lands_restored(self):
        self.cluster.objects.pop("/api/v1/namespaces/lab/pods/frigate-1", None)
        engine.start("shed", "volume", "frigate-config", "moved")
        move = self.run_until_settled()
        self.assertEqual("succeeded", move["status"], move["message"])
        self.assertEqual(list(engine.VOLUME_PHASES), move["phases"])
        self.assertEqual("frigate-config", self.lh.restored[0]["name"])
        self.assertIn("/api/v1/namespaces/moved/persistentvolumeclaims/frigate-config", self.cluster.objects)
        self.assertNotIn("/apis/apps/v1/namespaces/moved/deployments/frigate", self.cluster.objects)
        self.assertEqual(1, self.cluster.objects["/apis/apps/v1/namespaces/lab/deployments/frigate"]["spec"]["replicas"],
                         "the app that names it is left running as it was")
        engine.finish(move["id"])
        self.assertNotIn("/api/v1/namespaces/lab/persistentvolumeclaims/frigate-config", self.cluster.objects)

    def test_putting_a_volume_back_removes_only_its_restored_copy(self):
        self.cluster.objects.pop("/api/v1/namespaces/lab/pods/frigate-1", None)
        engine.start("shed", "volume", "frigate-config", "moved")
        move = self.run_until_settled()
        engine.abandon(move["id"])
        self.assertNotIn("/api/v1/namespaces/moved/persistentvolumeclaims/frigate-config", self.cluster.objects)
        source_claim = self.cluster.objects["/api/v1/namespaces/lab/persistentvolumeclaims/frigate-config"]
        self.assertNotIn("homestead.io/move-origin", source_claim["metadata"].get("annotations") or {})
        self.assertEqual("cancelled", engine.moves()[0]["status"])

    def test_a_volume_made_blank_here_is_empty_at_its_chosen_size_and_nothing_is_backed_up(self):
        engine.start("shed", "container", "frigate", "moved", "automatic",
                     volumes={"frigate-config": {"action": "blank", "size_gb": 5}})
        move = self.run_until_settled()
        self.assertEqual("succeeded", move["status"], move["message"])
        self.assertEqual([], self.lh.made, "nothing backed up")
        self.assertEqual([], self.lh.restored)
        claim = self.cluster.objects["/api/v1/namespaces/moved/persistentvolumeclaims/frigate-config"]
        self.assertEqual("5Gi", claim["spec"]["resources"]["requests"]["storage"])
        self.assertEqual(move["id"], claim["metadata"]["annotations"]["homestead.io/move-id"])
        self.assertEqual("blank", move["claims"][0]["action"])
        engine.abandon(move["id"])
        self.assertNotIn("/api/v1/namespaces/moved/persistentvolumeclaims/frigate-config", self.cluster.objects,
                         "putting it back removes the blank volume this move made")

    def test_a_skipped_volume_uses_the_one_already_here_and_is_never_touched(self):
        plan = engine.plan("shed", "container", "frigate", "moved", "automatic",
                           volumes={"frigate-config": {"action": "skip"}})
        self.assertTrue(any("no volume of that name" in b for b in plan["blockers"]))
        mine = {"metadata": {"name": "frigate-config"}, "spec": {"storageClassName": "local-path"}}
        self.cluster.put("/api/v1/namespaces/moved/persistentvolumeclaims/frigate-config", mine)
        engine.start("shed", "container", "frigate", "moved", "automatic",
                     volumes={"frigate-config": {"action": "skip"}})
        move = self.run_until_settled()
        self.assertEqual("succeeded", move["status"], move["message"])
        self.assertEqual(([], []), (self.lh.made, self.lh.restored))
        engine.abandon(move["id"])
        self.assertEqual(mine, self.cluster.objects["/api/v1/namespaces/moved/persistentvolumeclaims/frigate-config"])

    def test_each_volume_has_its_own_storage_class_and_a_moved_one_needs_longhorn(self):
        self.cluster.put("/apis/storage.k8s.io/v1/storageclasses/local-path",
                         {"metadata": {"name": "local-path"}, "provisioner": "rancher.io/local-path"})
        self.cluster.put("/apis/storage.k8s.io/v1/storageclasses/longhorn-fast",
                         {"metadata": {"name": "longhorn-fast"}, "provisioner": "driver.longhorn.io", "parameters": {}})
        moved = engine.plan("shed", "container", "frigate", "moved", "automatic",
                            volumes={"frigate-config": {"storage_class": "local-path"}})
        self.assertTrue(any("needs a Longhorn class" in b for b in moved["blockers"]), moved["blockers"])
        blank = engine.plan("shed", "container", "frigate", "moved", "automatic",
                            volumes={"frigate-config": {"action": "blank", "storage_class": "local-path"}})
        self.assertTrue(blank["ok"], blank["blockers"])
        self.assertIn("local-path", blank["all_storage_classes"])
        engine.start("shed", "container", "frigate", "moved", "automatic",
                     volumes={"frigate-config": {"storage_class": "longhorn-fast"}})
        self.run_until_settled()
        self.assertEqual("longhorn-fast", self.lh.restored[0]["storage_class"])

    def test_removing_the_source_keeps_volumes_it_did_not_move(self):
        engine.start("shed", "container", "frigate", "moved", "automatic",
                     volumes={"frigate-config": {"action": "blank"}})
        move = self.run_until_settled()
        with mock.patch.object(client, "check_cluster", lambda name: {"version": "2.8.249"}):
            with self.assertRaisesRegex(ValueError, "update it to 2.8.250"):
                engine.finish(move["id"], volumes=True)
        with mock.patch.object(client, "check_cluster", lambda name: {"version": "2.8.250"}):
            engine.finish(move["id"], volumes=True)
        self.assertNotIn("/apis/apps/v1/namespaces/lab/deployments/frigate", self.cluster.objects)
        self.assertIn("/api/v1/namespaces/lab/persistentvolumeclaims/frigate-config", self.cluster.objects,
                      "the only copy of its data stays")

    def test_the_browser_never_sees_definitions_or_keys(self):
        engine.start("shed", "container", "frigate", "moved", "automatic")
        self.run_until_settled()

        public = json.dumps(engine.moves())

        self.assertNotIn("containers", public)
        self.assertNotIn("credentials", public)
        self.assertNotIn("origin", public)


if __name__ == "__main__":
    unittest.main()
