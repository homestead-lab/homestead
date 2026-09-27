import copy
import hashlib
import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.environ.get("HOMESTEAD_TEST_SERVER_DIR", str(Path(__file__).resolve().parents[1] / "server")))
import homestead_self_data_execute as E
import homestead_self_data_route as R
import homestead_self_data_anchor as A
import homestead_self_data_worker as W
import homestead_operations as OPS
from homestead_storage_journal import Held
import test_self_data_review as review_fixture
from test_self_data_coordinator import IMAGE, OP, obj


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.f = review_fixture.ReviewTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.cluster = self.f.c
        del self.cluster.objects[self.cluster.anchor.path]
        del self.cluster.objects["/api/v1/namespaces/lab/pods/coordinator"]
        self.f.dep["spec"]["template"]["metadata"] = {"labels": {"app": "homestead"}}
        self.service = obj("Service", "homestead", {"selector": {"app": "homestead"}, "ports": [{"port": 8088, "targetPort": 8080}]})
        self.cluster.objects["/api/v1/namespaces/lab/services/homestead"] = self.service
        self.f.review.read = self.read; self.f.review.route_check = R.review
        self.approved = self.f.review.approve(self.f.approved())
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        for patch in (mock.patch.object(OPS, "DATA_DIR", self.temp.name), mock.patch.object(OPS, "WRITE_GUARD", None)):
            patch.start(); self.addCleanup(patch.stop)
        self.job = E.enqueue(OPS, self.approved)
        self.frozen, self.rechecked, self.published = False, False, False
        self.barrier = mock.Mock(directory=self.temp.name)
        self.barrier.activity.side_effect = nullcontext
        class Freeze:
            def __enter__(_): self.frozen = True
            def __exit__(_, *args): self.frozen = False
        self.barrier.freeze.side_effect = lambda **_: Freeze()

    def read(self, path):
        if path == "/api/v1/pods":
            helper = [p for p in self.cluster.objects.values() if p.get("kind") == "Pod" and p["metadata"]["name"].startswith("homestead-handoff-")]
            return {"items": copy.deepcopy(self.f.f.pods + helper)}
        if path == "/api/v1/namespaces/lab/services": return {"items": [copy.deepcopy(self.service)]}
        return self.f.f.read(path)

    def send(self, method, path, body):
        self.assertTrue(OPS._read(), "durable intent precedes every API write")
        if "?dryRun=" in path: return copy.deepcopy(body)
        result = self.cluster.send(method, path, body)
        if method == "POST" and body.get("kind") == "Pod":
            live = self.cluster.objects[path + "/" + body["metadata"]["name"]]
            live["spec"]["nodeName"] = "node1"
            live["status"] = {"phase": "Running", "conditions": [{"type": "Ready", "status": "False"}], "containerStatuses": [
                {"name": "coordinator", "ready": False, "restartCount": 0, "state": {"running": {"startedAt": "now"}},
                 "containerID": "containerd://helper", "imageID": IMAGE}]}
        return result

    def recheck(self, *_):
        self.assertTrue(self.frozen); self.rechecked = True
        return True

    def publish(self, directory, anchor):
        self.assertTrue(self.frozen); self.assertTrue(self.rechecked)
        self.assertEqual(2, self.cluster.objects[self.cluster.dep_path]["spec"]["replicas"])
        anchor.pointer_published(A.pointer_digest("lab", anchor.state, anchor.handle()["uid"]))
        self.published = True
        return anchor.handle()

    def task(self, **kwargs):
        return E.Execution(self.read, self.send, OPS, self.approved, self.job, directory=self.temp.name,
            barrier=self.barrier, recheck=self.recheck, clock=lambda: 1000, probe=lambda *_: True, **kwargs)

    def test_signed_setup_reaches_independent_worker_only_after_drain(self):
        with mock.patch.object(E.P, "publish_pointer", side_effect=self.publish):
            handle = self.task().run()
        self.assertIsNotNone(handle, OPS._read())
        self.assertTrue(self.published)
        source = self.cluster.objects[self.cluster.dep_path]
        self.assertEqual(2, source["spec"]["replicas"])
        anchor = A.Anchor(self.read, self.send, "lab", "homestead").load(**handle)
        runner = W.Runner(self.read, self.send, lambda _: self.cluster.logs_text, namespace="lab", deployment="homestead", operation=OP,
            anchor_uid=handle["uid"], worker_uid=anchor.state["plan"]["worker"]["uid"], clock=lambda: 1000, require_setup_receipts=True)
        self.assertTrue(runner.maintenance_ready())
        result = runner.tick()
        self.assertNotEqual("held", result["status"], result)
        self.assertEqual(0, self.cluster.objects[self.cluster.dep_path]["spec"]["replicas"])
        self.cluster.settle_stop(); self.f.f.pods = []
        self.assertEqual("copy", runner.tick()["phase"])
        self.assertNotEqual("held", runner.tick()["status"])
        self.cluster.finish_copy()
        self.assertEqual("verify", runner.tick()["phase"])
        runner.tick()
        self.assertEqual("verify", runner.tick()["phase"], "completed copy pod must release volumes first")
        del self.cluster.objects["/api/v1/namespaces/lab/pods/copy-pod"]
        self.assertEqual("switch", runner.tick()["phase"])
        self.assertEqual("start", runner.tick()["phase"])
        self.cluster.settle_stop()
        self.assertNotEqual("held", runner.tick()["status"])
        self.cluster.settle_start()
        self.f.f.pods = [p for p in self.cluster.objects.values() if p.get("kind") == "Pod" and p["metadata"]["name"].startswith("new-")]
        result = runner.tick()
        self.assertEqual("done", result["status"], result)
        self.assertFalse(runner.maintenance_ready())
        saved = next(i for i in OPS._read() if i["id"] == self.job["id"])
        self.assertEqual("succeeded", E.resolve(saved, self.read, clock=lambda: 1000)[0])
        for name in ("source", "target"):
            self.assertIn("/api/v1/namespaces/lab/persistentvolumeclaims/" + name, self.cluster.objects)

    def test_changed_source_or_new_job_does_not_publish_or_stop(self):
        for reason in ("source", "job"):
            with self.subTest(reason=reason):
                if reason == "job":
                    OPS.start("unrelated", "Other work", {}, "/", {})
                task = self.task()
                task.recheck = mock.Mock(side_effect=Held("Source changed"))
                with mock.patch.object(E.P, "publish_pointer") as publish:
                    self.assertIsNone(task.run()); publish.assert_not_called()
                self.assertEqual(2, self.cluster.objects[self.cluster.dep_path]["spec"]["replicas"])

    def test_lost_anchor_response_is_not_adopted_or_retried_by_polling(self):
        self.cluster.lost = lambda method, path, body: method == "POST" and body.get("kind") == "ConfigMap"
        self.assertIsNone(self.task().run())
        item = next(i for i in OPS._read() if i["id"] == self.job["id"])
        before = copy.deepcopy(self.cluster.sent)
        self.assertEqual("failed", E.resolve(item, self.read)[0])
        self.assertEqual(before, self.cluster.sent)
        self.assertNotIn("anchor_uid", item["ref"])
        self.assertNotIn("secret diagnostic", str(item))

    def test_no_new_service_or_vip_and_no_volume_delete(self):
        with mock.patch.object(E.P, "publish_pointer", side_effect=self.publish): self.task().run()
        self.assertEqual(self.service, self.cluster.objects["/api/v1/namespaces/lab/services/homestead"])
        for method, path, body in self.cluster.sent:
            self.assertFalse(method == "DELETE" and "persistentvolume" in path)
            if body and body.get("kind") == "Service": self.assertEqual("ClusterIP", body["spec"]["type"])
        self.assertFalse(self.job["cancellable"])
        self.assertFalse(OPS.cancel_plan(self.job["id"])["can"])

    def test_route_refuses_unready_local_only_and_extra_listener_services(self):
        for change in ({"publishNotReadyAddresses": True}, {"externalTrafficPolicy": "Local"}, {"ports": [{"port": 9000}]}):
            before = copy.deepcopy(self.service["spec"])
            self.service["spec"].update(change)
            with self.assertRaises(Held): R.review(self.read, self.f.dep)
            self.service["spec"] = before

    def test_maintenance_page_cookie_auth_and_readiness_do_not_mutate(self):
        runner = mock.Mock(operation=OP, anchor_uid="anchor-uid", worker_uid="pod-uid")
        runner.snapshot.return_value = {"operation": OP, "status": "running", "phase": "copy", "message": "Copying", "stages": []}
        runner.maintenance_ready.return_value = False
        token = "b" * 64
        server = W.status_server(("127.0.0.1", 0), runner, hashlib.sha256(token.encode()).hexdigest(), maintenance=True)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True); thread.start()
        def request(path, headers=None):
            client = http.client.HTTPConnection(*server.server_address, timeout=3)
            try:
                client.request("GET", path, headers=headers or {})
                response = client.getresponse()
                return response.status, response.read().decode()
            finally: client.close()
        try:
            self.assertEqual(503, request("/readyz")[0])
            runner.maintenance_ready.return_value = True
            self.assertEqual(200, request("/readyz")[0])
            path = "/api/self/data/handoff/" + OP
            code, page = request(path + "/view")
            self.assertEqual(200, code); self.assertIn("Moving Homestead data", page); self.assertNotIn(token, page)
            self.assertEqual(401, request(path)[0])
            self.assertEqual(401, request(path, {"Cookie": "homestead-data-move=wrong"})[0])
            code, body = request(path, {"Cookie": "homestead-data-move=" + token})
            self.assertEqual(200, code); self.assertEqual("copy", json.loads(body)["phase"])
            self.assertEqual(404, request(path + "?token=" + token)[0])
            runner.tick.assert_not_called()
        finally:
            server.shutdown(); server.server_close(); thread.join(1)


if __name__ == "__main__": unittest.main()
