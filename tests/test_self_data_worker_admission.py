import copy
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.environ.get("HOMESTEAD_TEST_SERVER_DIR", str(Path(__file__).resolve().parents[1] / "server")))
import homestead_self_data_admission as D
import homestead_self_data_bootstrap as B
import homestead_self_data_anchor as A
import homestead_self_data_launch as L
import homestead_self_data_kube as K
from homestead_storage_journal import Held, identity
import test_self_data_admission as admission_fixture
from test_self_data_coordinator import IMAGE, OP, obj


class WorkerAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = admission_fixture.AdmissionTests(); self.fixture.setUp()
        self.c, self.pin = self.fixture.cluster, self.fixture.pin
        self.scope = K.Scope("lab", "homestead", OP, ["source", "target"], ["old-pv", "new-pv"], ["node1"])
        self.pod = L.resources(self.scope, anchor_uid="pending", image=IMAGE, node="node1", status_digest="b" * 64)[-2]

    def approval(self, pod=None):
        report = D.review(self.fixture.read, "lab", "worker", pod or self.pod, self.pin, 88, clock=lambda: 1000)
        return {"threshold": 88, "nodes": copy.deepcopy(self.pin), "receipt": report["receipt"]}

    def admitter(self, approval=None):
        return D.WorkerAdmitter(self.fixture.read, "lab", approval or self.approval(), clock=lambda: 1000)

    def setup(self, approval):
        self.c.objects.pop(self.c.anchor.path)
        anchor = A.Anchor(self.fixture.read, self.c.send, "lab", "homestead")
        anchor.create(operation=OP, deployment={"name": "homestead", **identity(self.c.dep)},
            source={"name": "source", **identity(self.c.source)}, destination="target", replicas=2)
        self.setup_anchor = anchor
        return B.Setup(anchor, self.scope, image=IMAGE, node="node1", status_digest="b" * 64, approval=approval, clock=lambda: 1000)

    def test_read_only_preview_does_not_create_an_anchor_or_any_helpers(self):
        before = copy.deepcopy(self.c.sent)
        approval = self.approval()
        D.validate_worker_approval(approval)
        self.assertEqual(before, self.c.sent)

    def test_only_future_anchor_uid_is_normalized_not_execution_config(self):
        admit = self.admitter()
        actual = L.resources(self.scope, anchor_uid="actual-api-uid", image=IMAGE, node="node1", status_digest="b" * 64)[-2]
        self.assertTrue(admit(actual))
        # Calling preview must not alter the source runtime configuration.
        raw = actual["spec"]["containers"][0]["env"][0]["value"]
        self.assertEqual("actual-api-uid", L.parse_configuration(raw)[0]["anchor_uid"])
        changed = L.resources(self.scope, anchor_uid="actual-api-uid", image=IMAGE, node="node1", status_digest="c" * 64)[-2]
        with self.assertRaises(Held): admit(changed)

    def test_scope_image_and_resource_changes_need_new_approval(self):
        admit = self.admitter()
        other = K.Scope("lab", "homestead", OP, ["source", "target", "extra"], ["old-pv", "new-pv"], ["node1"])
        candidates = [L.resources(other, anchor_uid="pending", image=IMAGE, node="node1", status_digest="b" * 64)[-2]]
        for transform in (lambda p: p["spec"]["containers"][0].update(image=IMAGE[:-1] + "c"),
                          lambda p: p["spec"]["containers"][0]["resources"]["limits"].update(memory="512Mi")):
            pod = copy.deepcopy(self.pod); transform(pod); candidates.append(pod)
        for pod in candidates:
            with self.assertRaises(Held): admit(pod)

    def test_existing_homestead_reservations_cannot_be_subtracted_for_worker(self):
        admit = self.admitter()
        old = obj("Pod", "homestead-old", {"nodeName": "node1", "containers": [{"name": "app", "resources": {"requests": {"memory": "8Gi"}}}]})
        old["status"] = {"phase": "Running"}
        self.fixture.pods = [old]
        with self.assertRaises(Held): admit(self.pod)
        old["metadata"]["deletionTimestamp"] = "1970-01-01T00:16:39Z"
        with self.assertRaises(Held): admit(self.pod)

    def test_pressure_warning_requires_explicit_review_but_is_overridable(self):
        admit = self.admitter()
        self.fixture.metrics[0]["usage"]["memory"] = "7.8Gi"
        with self.assertRaisesRegex(Held, "warnings changed"): admit(self.pod)
        approved = self.admitter()
        self.assertTrue(approved(self.pod))
        self.fixture.nodes[0]["status"]["allocatable"]["memory"] = "32Mi"
        with self.assertRaises(Held): approved(self.pod)

    def test_initial_rejection_creates_no_setup_roles_or_pod(self):
        setup = self.setup(self.approval())
        self.fixture.nodes[0]["spec"]["unschedulable"] = True
        before = len(self.c.sent)
        with self.assertRaises(Held): setup.step()
        self.assertEqual(before, len(self.c.sent))
        self.assertNotIn("setup", self.setup_anchor.state)

    def test_capacity_rechecked_between_roles_and_pod_and_approval_is_durable(self):
        approved = self.approval()
        setup = self.setup(approved)
        setup.step()
        self.assertEqual(approved, setup.anchor.state["setup"]["admission"])
        for _ in range(7): setup.step()
        self.fixture.nodes[0]["status"]["allocatable"]["pods"] = "0"
        before = len(self.c.sent)
        with self.assertRaises(Held): setup.step()
        self.assertEqual(before, len(self.c.sent))
        self.assertNotIn(setup.resources[-2]["target"]["path"], self.c.objects)
        self.assertEqual(2, self.c.objects[self.c.dep_path]["spec"]["replicas"])

    def test_resume_loads_persisted_approval_and_cannot_swap_it(self):
        setup = self.setup(self.approval()); setup.step()
        restored = A.Anchor(self.fixture.read, self.c.send, "lab", "homestead").load(**setup.anchor.handle())
        resume = B.Setup(restored, self.scope, image=IMAGE, node="node1", status_digest="b" * 64, clock=lambda: 1000)
        self.assertEqual(1, resume.step()["created"])
        changed = self.approval(); changed["threshold"] = 99
        restored = A.Anchor(self.fixture.read, self.c.send, "lab", "homestead").load(**setup.anchor.handle())
        other = B.Setup(restored, self.scope, image=IMAGE, node="node1", status_digest="b" * 64, approval=changed, clock=lambda: 1000)
        with self.assertRaisesRegex(Held, "differs"): other.step()

    def test_persisted_approval_cannot_be_bypassed_with_a_callback(self):
        setup = self.setup(self.approval())
        with self.assertRaisesRegex(Held, "alternative admission"):
            B.Setup(setup.anchor, self.scope, image=IMAGE, node="node1", status_digest="b" * 64,
                    approval=self.approval(), admit=lambda _: True)

    def test_approval_schema_rejects_duplicate_hosts_or_wildcard_acknowledgements(self):
        for mutate in (lambda a: a["nodes"].append(copy.deepcopy(a["nodes"][0])),
                       lambda a: a["receipt"]["warnings"].append("*"), lambda a: a.update(threshold=True)):
            value = self.approval(); mutate(value)
            with self.assertRaises(Held): D.validate_worker_approval(value)


if __name__ == "__main__":
    unittest.main()
