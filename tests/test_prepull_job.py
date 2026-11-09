"""An Image cache pre-pull job, followed on the shared Kubernetes fake.

A pull DaemonSet gets no Progressing or Available conditions, so a pull that
can never succeed (a typo in the name, a private registry without
credentials) has to be read from its pods: every pod still waiting failing
its pull for PULL_STUCK_FOR ends the job, with the error, and stops the
pull."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_image_cache as IMAGES
import homestead_imports as imports
import homestead_kube as K
import homestead_names as NAMES
import homestead_operations as operations
from fakes.kube import Kube

NAME = "homestead-pull-frigate-0a1b2c3d"
IMAGE = "ghcr.example.com/x/frigate:0.18.0"
DS_PATH = f"/apis/apps/v1/namespaces/lab/daemonsets/{NAME}"


def daemonset(desired, ready):
    return {"apiVersion": "apps/v1", "kind": "DaemonSet",
            "metadata": {"name": NAME, "namespace": "lab",
                         "labels": {"homestead.io/task": "prepull", "homestead.io/pull-by": "image-cache"}},
            "spec": {"selector": {"matchLabels": {"app": NAME}},
                     "template": {"metadata": {"labels": {"app": NAME}},
                                  "spec": {"initContainers": [{"name": "pull", "image": IMAGE}],
                                           "containers": [{"name": "pause", "image": "registry.k8s.io/pause:3.9"}]}}},
            "status": {"desiredNumberScheduled": desired, "numberReady": ready,
                       "numberUnavailable": desired - ready}}


def pod(node, waiting=None, message="", ready=False, name=None, terminating=False, pause=None):
    """One pull pod on node: ready, or its pull container waiting for a reason
    (pause: the pull has run and the pause container waits for that reason)."""
    init = {"name": "pull", "image": IMAGE, "ready": ready or bool(pause),
            "state": {"terminated": {"exitCode": 0}} if ready or pause else
                     {"waiting": {"reason": waiting or "PodInitializing", "message": message}}}
    meta = {"name": name or f"{NAME}-{node}", "namespace": "lab", "labels": {"app": NAME},
            "uid": f"uid-{name or node}"}
    if terminating:
        meta["deletionTimestamp"] = "2026-11-01T10:00:00Z"
    status = {"phase": "Running" if ready else "Pending",
              "conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
              "initContainerStatuses": [init]}
    if pause:
        status["containerStatuses"] = [{"name": "pause", "image": PAUSE, "ready": False,
                                        "state": {"waiting": {"reason": pause, "message": message}}}]
    return {"apiVersion": "v1", "kind": "Pod", "metadata": meta, "spec": {"nodeName": node}, "status": status}


PAUSE = "registry.k8s.io/pause:3.9"
_EVENTS = iter(range(1, 10_000))


def event(node, reason, at, message="", uid=None):
    """A kubelet event on node's pull pod: Pulling, Failed or BackOff, at a
    time of day given as HH:MM:SS."""
    return {"apiVersion": "v1", "kind": "Event",
            "metadata": {"name": f"{NAME}-{node}.{next(_EVENTS):x}", "namespace": "lab"},
            "involvedObject": {"kind": "Pod", "name": f"{NAME}-{node}", "namespace": "lab", "uid": uid or f"uid-{node}",
                               "fieldPath": "spec.initContainers{pull}"},
            "reason": reason, "message": message, "type": "Normal" if reason == "Pulling" else "Warning",
            "lastTimestamp": f"2026-11-01T{at}Z"}


NOT_FOUND = 'failed to resolve reference "ghcr.example.com/x/frigate:0.18.0": not found'


class PullCase(unittest.TestCase):
    """A pull job's world: the shared fake, a job store and a fake clock."""

    def setUp(self):
        self.kube = self.enterContext(Kube())
        self.now = [1_000_000.0]
        self.enterContext(mock.patch.object(IMAGES, "_clock", lambda: self.now[0]))
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        for module, name, value in ((operations, "kget", K.kget), (operations, "DATA_DIR", tmp),
                                    (imports, "kget", K.kget), (imports, "ksend", K.ksend),
                                    (NAMES, "kget", K.kget)):
            self.enterContext(mock.patch.object(module, name, value))
        self.enterContext(mock.patch.dict(operations.RESOLVERS, {"image-pull": operations._prepull}))

    def start(self, *objects):
        for obj in objects:
            self.kube.add(obj)
        return operations.start("image-pull", f"Pull {IMAGE}", {"kind": "Image", "name": IMAGE, "namespace": "lab"},
                                "/image-cache", {"namespace": "lab", "name": NAME})

    def step(self, after=0):
        self.now[0] += after
        return operations.list_operations()[0]


class PrepullJobTests(PullCase):
    def test_a_pull_that_fails_on_every_node_ends_the_job_failed_and_stops_the_pull(self):
        self.start(daemonset(2, 0), pod("node-a", "ErrImagePull", NOT_FOUND), pod("node-b", "ImagePullBackOff",
                                                                                 f'Back-off pulling image "{IMAGE}"'))
        first = self.step()
        self.assertEqual("running", first["status"], "a pull is retried for a while: a registry can be down briefly")
        self.assertIn("ErrImagePull", first["message"], "the error is shown while it is retried")
        self.assertIsNotNone(self.kube.object(DS_PATH))

        self.assertEqual("running", self.step(IMAGES.PULL_STUCK_FOR - 10)["status"])
        failed = self.step(20)
        self.assertEqual("failed", failed["status"])
        self.assertIn(IMAGE, failed["message"])
        self.assertIn("not found", failed["message"], "the registry's own words, not only the kubelet's reason")
        self.assertIsNone(self.kube.object(DS_PATH), "a failed pull's DaemonSet is removed, not left retrying")

    def test_a_pull_failing_on_one_node_while_the_others_finished_fails_and_says_where(self):
        self.start(daemonset(2, 1), pod("node-a", ready=True), pod("node-b", "ErrImagePull", NOT_FOUND))
        self.step()
        failed = self.step(IMAGES.PULL_STUCK_FOR + 1)
        self.assertEqual("failed", failed["status"])
        self.assertIn("node-b", failed["message"])
        self.assertIn("1/2", failed["message"])

    def test_a_pull_that_recovers_starts_its_wait_again(self):
        self.start(daemonset(1, 0), pod("node-a", "ErrImagePull", NOT_FOUND))
        self.step()
        self.kube.add(pod("node-a"))                    # pulling again, the registry answered
        self.assertEqual("running", self.step(IMAGES.PULL_STUCK_FOR - 10)["status"])
        self.kube.add(pod("node-a", "ImagePullBackOff", "Back-off pulling image"))
        again = self.step(20)
        self.assertEqual("running", again["status"], "the wait starts again from the new error")
        self.assertNotIn("not found", again["message"], "an error from before the recovery is not quoted")
        self.assertEqual("failed", self.step(IMAGES.PULL_STUCK_FOR + 1)["status"])

    def test_flapping_between_the_two_pull_errors_keeps_the_clock(self):
        self.start(daemonset(1, 0), pod("node-a", "ErrImagePull", NOT_FOUND))
        self.step()
        self.kube.add(pod("node-a", "ImagePullBackOff", "Back-off pulling image"))
        self.assertEqual("running", self.step(IMAGES.PULL_STUCK_FOR - 10)["status"])
        self.kube.add(pod("node-a", "ErrImagePull", NOT_FOUND))
        self.assertEqual("failed", self.step(20)["status"])

    def test_the_wait_survives_a_restart(self):
        self.start(daemonset(1, 0), pod("node-a", "ErrImagePull", NOT_FOUND))
        self.step()
        stored = json.loads((Path(operations.DATA_DIR) / operations.STORE).read_text())
        self.assertEqual(self.now[0], stored[0]["ref"]["pull_failing_since"])
        operations._memo.clear()                        # a new process reads the file afresh
        self.assertEqual("failed", self.step(IMAGES.PULL_STUCK_FOR + 1)["status"])

    def test_a_retry_that_is_pulling_again_is_not_failed_however_long_it_takes(self):
        """The kubelet keeps showing the last pull error while a retry pulls:
        its events say which came last."""
        self.start(daemonset(1, 0), pod("node-a", "ImagePullBackOff", "Back-off pulling image"),
                   event("node-a", "Pulling", "10:00:00"), event("node-a", "Failed", "10:00:01", NOT_FOUND),
                   event("node-a", "BackOff", "10:00:05"), event("node-a", "Pulling", "10:00:20"))
        self.step()
        slow = self.step(10 * IMAGES.PULL_STUCK_FOR)
        self.assertEqual("running", slow["status"])
        self.assertIsNotNone(self.kube.object(DS_PATH))
        self.kube.add(event("node-a", "Failed", "11:00:00", NOT_FOUND))  # that retry failed too
        self.assertEqual("running", self.step(1)["status"])
        self.assertEqual("failed", self.step(IMAGES.PULL_STUCK_FOR + 1)["status"])

    def test_an_earlier_pod_s_events_of_the_same_name_are_not_read_as_this_one_s(self):
        self.start(daemonset(1, 0), pod("node-a", "ImagePullBackOff", "Back-off pulling image"),
                   event("node-a", "Failed", "10:00:01", NOT_FOUND),
                   event("node-a", "Pulling", "10:00:20", uid="uid-replaced-pod"))
        self.step()
        self.assertEqual("failed", self.step(IMAGES.PULL_STUCK_FOR + 1)["status"])

    def test_events_that_stay_unreadable_fall_back_to_the_pod_status(self):
        self.start(daemonset(1, 0), pod("node-a", "ErrImagePull", NOT_FOUND))
        self.kube.fail("/api/v1/namespaces/lab/events", 500, times=100)
        self.assertEqual("running", self.step()["status"])
        failed = self.step(IMAGES.PULL_STUCK_FOR + 1)
        self.assertEqual("failed", failed["status"], "an events outage does not keep a bad pull going for ever")
        self.assertIn("download events could not be read", failed["message"])

    def test_a_pod_list_that_cannot_be_read_keeps_the_clock(self):
        self.start(daemonset(1, 0), pod("node-a", "ErrImagePull", NOT_FOUND))
        self.step()
        self.kube.fail("/api/v1/namespaces/lab/pods", 500)
        self.assertEqual("running", self.step(IMAGES.PULL_STUCK_FOR - 10)["status"])
        self.assertEqual("failed", self.step(20)["status"], "one unreadable list does not start the wait again")

    def test_a_terminating_pod_is_not_counted(self):
        self.start(daemonset(1, 0), pod("node-a", "ErrImagePull", NOT_FOUND, name=f"{NAME}-old", terminating=True))
        self.step()
        self.assertEqual("running", self.step(IMAGES.PULL_STUCK_FOR + 1)["status"])

    def test_the_failing_container_s_own_image_is_named(self):
        self.start(daemonset(1, 0), pod("node-a", pause="ErrImagePull", message="dial tcp: i/o timeout"))
        self.step()
        failed = self.step(IMAGES.PULL_STUCK_FOR + 1)
        self.assertEqual("failed", failed["status"])
        self.assertIn(f"could not download {PAUSE}", failed["message"])

    def test_no_credentials_from_the_registry_s_words_are_kept_or_shown(self):
        # Built in pieces: push protection refuses token-shaped strings even when fake.
        signed = ("https://blob.example.com/sha256/abc?X-Amz-Credential=" + "AKIA" + "EXAMPLEEXAMPLE1"
                  + "&X-Amz-Security-Token=" + "tok" + "en123" + "&X-Amz-Signature=" + "f" * 40)
        userinfo = "https://robot:" + "hunter" + "2pass@mirror.example.com/v2/x/frigate/manifests/0.18.0"
        words = f'failed to pull: GET "{signed}": 403 Forbidden; fallback {userinfo}: unauthorized'
        self.start(daemonset(1, 0), pod("node-a", "ErrImagePull", words))
        shown = [self.step()["message"], self.step(IMAGES.PULL_STUCK_FOR + 1)["message"]]
        stored = (Path(operations.DATA_DIR) / operations.STORE).read_text()
        for text in shown + [stored]:
            for secret in ("X-Amz", "en123", "f" * 40, "hunter", "robot:"):
                self.assertNotIn(secret, text)
        self.assertIn("blob.example.com/sha256/abc", shown[-1], "the place is still named")
        self.assertIn("mirror.example.com/v2/", shown[-1])

    def test_a_slow_pull_is_never_failed_for_taking_long(self):
        self.start(daemonset(2, 1), pod("node-a", ready=True), pod("node-b"))
        self.step()
        self.assertEqual("running", self.step(10 * IMAGES.PULL_STUCK_FOR)["status"])
        self.assertIsNotNone(self.kube.object(DS_PATH))

    def test_a_pod_not_made_yet_can_still_pull(self):
        self.start(daemonset(2, 0), pod("node-a", "ErrImagePull", NOT_FOUND))
        self.step()
        self.assertEqual("running", self.step(IMAGES.PULL_STUCK_FOR + 1)["status"])

    def test_a_finished_pull_still_succeeds(self):
        self.start(daemonset(2, 2), pod("node-a", ready=True), pod("node-b", ready=True))
        done = self.step()
        self.assertEqual("succeeded", done["status"])
        self.assertEqual("Image cached on 2/2 nodes", done["message"])


class PullSweepTests(PullCase):
    """The leader's minute removes finished pulls; a pull a job still follows
    is that job's to read first. It used to be removed under the job, whose
    next look got a 404 and failed a pull that had worked."""

    def test_a_finished_pull_a_running_job_follows_is_left_for_the_job(self):
        self.start(daemonset(2, 2))
        IMAGES.prepull_status(sweep=True)               # the tick, before the job's next look
        self.assertIsNotNone(self.kube.object(DS_PATH))
        self.assertEqual("succeeded", self.step()["status"])
        IMAGES.prepull_status(sweep=True)               # the job has ended: cleared away now
        self.assertIsNone(self.kube.object(DS_PATH))

    def test_a_finished_pull_no_job_follows_is_still_cleared_away(self):
        self.kube.add(daemonset(1, 1))
        IMAGES.prepull_status(sweep=True)
        self.assertIsNone(self.kube.object(DS_PATH))

    def test_nothing_is_cleared_away_while_the_jobs_cannot_be_read(self):
        self.kube.add(daemonset(1, 1))
        with mock.patch.object(operations, "stored", side_effect=ValueError("store damaged")):
            IMAGES.prepull_status(sweep=True)
        self.assertIsNotNone(self.kube.object(DS_PATH), "what a job follows is not known")


class ImageCleanupJobTests(PullCase):
    def test_a_cleaner_that_ran_out_of_time_says_so(self):
        name = "homestead-image-clean-0123456789-abc"
        self.kube.add({"apiVersion": "v1", "kind": "Pod", "metadata": {"name": name, "namespace": "lab"},
                       "spec": {"activeDeadlineSeconds": 600},
                       "status": {"phase": "Failed", "reason": "DeadlineExceeded",
                                  "message": "Pod was active on the node longer than the specified deadline",
                                  "containerStatuses": [{"name": "runtime", "state": {"terminated": {
                                      "exitCode": 137, "reason": "Error", "message": "removing image"}}}]}})
        self.enterContext(mock.patch.dict(operations.RESOLVERS, {"image-cleanup": operations._image_cleanup}))
        operations.start("image-cleanup", "Clean cached image", {"kind": "Image", "name": IMAGE, "namespace": "lab"},
                         "/image-cache", {"namespace": "lab", "pods": [name]})
        done = operations.list_operations()[0]
        self.assertEqual("failed", done["status"])
        self.assertIn("longer than 10 minutes", done["message"])


if __name__ == "__main__":
    unittest.main()
