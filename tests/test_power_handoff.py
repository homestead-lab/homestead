"""The last step of a single host's reboot: Homestead stops itself so its
data volume detaches, and a helper on the host sends the power and starts
Homestead again on the new boot.

Rebooting k3s-test with Homestead still on its data volume brought
homestead-data back faulted: Longhorn stopped under a mounted filesystem."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_kube as K
import homestead_operations as OPS
import homestead_power_handoff as H
from fakes.kube import Kube

DEP = "/apis/apps/v1/namespaces/lab/deployments/homestead"
PVC = "/api/v1/namespaces/lab/persistentvolumeclaims/homestead-data"
VOL = f"{H.LH}/pvc-1"


class Cluster:
    """The host side (commands, sleep, clock) around the shared fake API
    (tests/fakes/kube.py), which the helper talks to directly."""
    def __init__(self, boot="old", held=True, running=0, state="detached"):
        self.boot, self.clock_now, self.commands = boot, 0.0, []
        self.kube = Kube(
            {"apiVersion": "apps/v1", "kind": "Deployment",
             "metadata": {"name": "homestead", "namespace": "lab",
                          "annotations": {H.HELD_BY: "job-1", H.HELD_AS: "2"} if held else {}},
             "spec": {"replicas": 0}, "status": {"replicas": running}},
            {"apiVersion": "v1", "kind": "PersistentVolumeClaim",
             "metadata": {"name": "homestead-data", "namespace": "lab"}, "spec": {"volumeName": "pvc-1"}},
            {"apiVersion": "longhorn.io/v1beta2", "kind": "Volume",
             "metadata": {"name": "pvc-1", "namespace": "longhorn-system"}, "status": {"state": state}})
        self.on_sleep = lambda: None

    def stored(self, path):
        return self.kube.object(path)

    def set_status(self, path, **status):
        obj = self.kube.object(path)
        obj["status"].update(status)
        self.kube.add(obj)

    @property
    def sent(self):
        """Every write that reached the API, of any kind."""
        return [(method, path) for method, path, _, _ in self.kube.writes]

    def execute(self, command, timeout=None):
        self.commands.append(command[-1])
        return (self.boot + "\n").encode() if "boot_id" in command[-1] else b""

    def sleep(self, seconds):
        self.clock_now += seconds
        self.on_sleep()

    def handoff(self, action="reboot"):
        return H.Handoff(self.kube, "lab", "homestead", "job-1", action, "old", "homestead-data",
                         execute=self.execute, sleep=self.sleep, clock=lambda: self.clock_now)


class BeforePowerTests(unittest.TestCase):
    def test_power_waits_for_homestead_to_stop_and_its_volume_to_detach(self):
        c = Cluster(running=1, state="attached")
        steps = iter([lambda: c.set_status(DEP, replicas=0),
                      lambda: c.set_status(VOL, state="detached")])
        def on_sleep():
            step = next(steps, None)
            if step:
                step()
            elif any("systemd-run" in cmd for cmd in c.commands):
                raise SystemExit("powered")     # the power stops the helper here
        c.on_sleep = on_sleep
        with self.assertRaisesRegex(SystemExit, "powered"):
            c.handoff().run()
        timer = next(cmd for cmd in c.commands if "systemd-run" in cmd)
        self.assertIn("--on-active=3s", timer)
        self.assertTrue(timer.endswith(" reboot"))
        self.assertEqual([], c.sent, "Homestead is not started again before the power")

    def test_a_volume_that_does_not_detach_sends_nothing_and_starts_homestead(self):
        c = Cluster(state="attached")
        self.assertEqual(0, c.handoff().run())
        self.assertFalse(any("systemd-run" in cmd for cmd in c.commands))
        self.assertEqual(2, c.stored(DEP)["spec"]["replicas"])
        self.assertNotIn(H.HELD_BY, c.stored(DEP)["metadata"].get("annotations", {}))

    def test_homestead_changed_by_someone_else_is_left_alone(self):
        c = Cluster(held=False)
        self.assertEqual(0, c.handoff().run())
        self.assertEqual([], c.sent)
        self.assertFalse(any("systemd-run" in cmd for cmd in c.commands))

    def test_a_host_that_never_goes_down_gets_homestead_back(self):
        c = Cluster()
        self.assertEqual(0, c.handoff("poweroff").run())
        self.assertTrue(next(cmd for cmd in c.commands if "systemd-run" in cmd).endswith(" poweroff"))
        self.assertEqual(2, c.stored(DEP)["spec"]["replicas"])


class AfterBootTests(unittest.TestCase):
    def test_on_the_new_boot_homestead_starts_again_with_its_replicas(self):
        c = Cluster(boot="new")
        self.assertEqual(0, c.handoff().run())
        self.assertEqual(2, c.stored(DEP)["spec"]["replicas"])
        self.assertFalse(any("systemd-run" in cmd for cmd in c.commands), "never a second power request")


class SendTests(unittest.TestCase):
    """The job: the helper's identity is recorded before it exists, and
    Homestead stops only after the helper was confirmed."""
    def test_helper_then_homestead_stops(self):
        import server
        recorded = []
        own = {"spec": {"serviceAccountName": "homestead", "imagePullSecrets": [{"name": "pull"}]}}
        kube = self.enterContext(Kube({"apiVersion": "apps/v1", "kind": "Deployment",
                                       "metadata": {"name": "homestead", "namespace": "lab"}, "spec": {"replicas": 1}}))
        # The shared fake runs the real write guards; the storage guard takes the job store's lock under DATA_DIR.
        self.enterContext(mock.patch.object(OPS, "DATA_DIR", self.enterContext(tempfile.TemporaryDirectory())))
        with mock.patch.object(server.POWER_JOBS, "_self_data_helper_image", return_value=(own, "img@sha256:" + "a" * 64)), \
             mock.patch.object(server.POWER_JOBS, "homestead_running_on", return_value=("homestead-data", True)), \
             mock.patch.object(server.POWER_JOBS, "kget", K.kget), \
             mock.patch.object(server.POWER_JOBS, "ksend", K.ksend):
            server.POWER_JOBS.send_handoff("job-1", {"node": "k3s-test", "action": "reboot", "boot_id": "old"}, [], {},
                                lambda phase, pct, msg, **kw: recorded.append((phase, kw)))
        (post, pod_path, pod, _), (patch, dep_path, scale, _) = kube.writes
        self.assertEqual(("POST", "PATCH"), (post, patch))
        self.assertEqual("OnFailure", pod["spec"]["restartPolicy"], "it restarts on the new boot")
        self.assertEqual("homestead", pod["spec"]["serviceAccountName"])
        self.assertIn("homestead_power_handoff.py", " ".join(pod["spec"]["containers"][0]["command"]))
        self.assertEqual(0, scale["spec"]["replicas"])
        self.assertEqual("1", scale["metadata"]["annotations"]["homestead.io/held-as"])
        self.assertEqual(0, kube.object(DEP)["spec"]["replicas"])
        self.assertEqual("sending", recorded[0][0])
        self.assertTrue(recorded[0][1]["handoff"])
        helper = kube.object(f"{pod_path}/{pod['metadata']['name']}")
        self.assertEqual(helper["metadata"]["uid"], recorded[1][1]["helper_uid"])


if __name__ == "__main__":
    unittest.main()


class StatusTests(unittest.TestCase):
    def status(self, ref, boot="old", helper_phase=None):
        import homestead_power as POWER
        import time
        self.enterContext(Kube(
            {"apiVersion": "v1", "kind": "Node", "metadata": {"name": "k3s-test", "uid": "u"},
             "status": {"conditions": [{"type": "Ready", "status": "True"}], "nodeInfo": {"bootID": boot}}},
            {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "p", "namespace": "lab", "uid": "h"},
             "status": {"phase": helper_phase or "Running"}}))
        with mock.patch.object(POWER, "kget", K.kget):
            return POWER.status({"id": "job-1", "progress": 20, "ref": {"node": "k3s-test", "node_uid": "u", "boot_id": "old",
                                 "phase": "observing", "started_epoch": time.time() - 60, "helper_pod": "p", "helper_uid": "h", **ref}})

    def test_a_single_host_power_off_is_confirmed_by_its_new_boot(self):
        state, _, message = self.status({"action": "poweroff", "planned_outage": True}, boot="new")
        self.assertEqual("succeeded", state, "Homestead was down too and never saw the host leave Ready")
        self.assertIn("powered off and has started again", message)

    def test_a_helper_that_stood_down_fails_the_job_with_its_reason(self):
        state, _, message = self.status({"action": "reboot", "handoff": True}, helper_phase="Succeeded")
        self.assertEqual("failed", state)
        self.assertIn("did not detach", message)
