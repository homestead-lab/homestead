"""A VM start whose instance crashes fails at once, with KubeVirt's reason,
and a start puts the VM's run strategy back as it found it. In November 2026
a kernel's AppArmor change made QEMU exit at every start, and the start job
cycled between 35% and 60% without saying why."""
import copy
import tempfile
import time
import unittest
import urllib.error
from unittest import mock

import test_vm_resources as fixtures
import homestead_operations as ops
import homestead_vm_power_job as power

QEMU = ("server error. command SyncVMI failed: \"LibvirtError(Code=1, Domain=10, Message='internal error: "
        "QEMU unexpectedly closed the monitor: qemu-kvm: qemu:cpus_kick_thread: Permission denied')\"")


def stamp(offset=0):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + offset))


class StartFailsFastTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.vm = fixtures.vm()
        self.vm["spec"]["runStrategy"] = "Always"
        self.vm["status"] = {"printableStatus": "Starting"}
        self.vmi = fixtures.child(self.vm, "VirtualMachine", "guest", "new-vmi")
        self.vmi["status"]["phase"] = "Scheduled"
        self.events = []
        self.sent = []
        self.context = {"observations": {"vm": self.vm["metadata"], "vmi": {}}}
        self.body = {"action": "start", "capacity_token": f"{int(time.time()) + 600}.test-approval"}
        for patch in (mock.patch.object(ops, "DATA_DIR", self.tmp.name),
                      mock.patch.dict(ops.RESOLVERS, {"vm-power": lambda item: power.status(item, self.read, self.send)}),
                      mock.patch.dict(ops.LOGGERS, {"vm-power": power.log_sources}),
                      mock.patch.dict(ops.CANCELLERS, {"vm-power": (power.cancel_plan, power.cancel_run)})):
            patch.start()
            self.addCleanup(patch.stop)

    def read(self, path):
        if "/events?" in path:
            return {"items": copy.deepcopy(self.events)}
        if path.startswith("/api/v1/nodes/"):
            return {"status": {"nodeInfo": {"kernelVersion": "7.0.0-43-generic"}}}
        if "/virtualmachines/" in path:
            return copy.deepcopy(self.vm)
        if self.vmi is None:
            raise urllib.error.HTTPError(path, 404, "missing", {}, None)
        return copy.deepcopy(self.vmi)

    def send(self, method, path, body=None, **options):
        self.sent.append((method, path, body, options))
        return {}

    def event(self, reason, message, offset=0, kind="VirtualMachineInstance"):
        self.events.append({"involvedObject": {"kind": kind, "name": "guest"}, "type": "Warning", "reason": reason,
                            "message": message, "lastTimestamp": stamp(offset), "source": {"host": "node1"}})

    def start(self, strategy="Halted"):
        power.dispatch(self.body, self.context, ops, lambda: {"ok": True}, lambda: None, strategy)

    def test_a_crash_fails_the_start_with_the_cause_and_puts_the_strategy_back(self):
        self.start()
        self.assertEqual("running", ops.list_operations()[0]["status"])
        self.event("SyncFailed", QEMU)
        self.event("Stopped", "The VirtualMachineInstance crashed.")
        job = ops.list_operations()[0]
        self.assertEqual("failed", job["status"])
        self.assertTrue(job["message"].startswith("The VM crashed while starting, so this job stopped. "))
        self.assertIn("refused permission to signal its own threads", job["message"])
        self.assertIn("kernel 7.0.0-43-generic", job["message"])
        self.assertIn("set back to stay off", job["message"])
        # One fenced write: the strategy the start set, back to the one found.
        self.assertEqual(1, len(self.sent))
        method, path, patch, options = self.sent[0]
        self.assertEqual(("PATCH", "/apis/kubevirt.io/v1/namespaces/lab/virtualmachines/guest"), (method, path))
        self.assertEqual("application/json-patch+json", options["ctype"])
        self.assertEqual([{"op": "test", "path": "/metadata/uid", "value": "vm-uid"},
                          {"op": "test", "path": "/metadata/resourceVersion", "value": "1"},
                          {"op": "test", "path": "/spec/runStrategy", "value": "Always"},
                          {"op": "replace", "path": "/spec/runStrategy", "value": "Halted"}], patch)
        # The full QEMU text is behind the log, never clipped to the message.
        log = ops.log(job["id"])
        self.assertEqual("Why the VM stopped", log["sources"][0]["title"])
        self.assertIn("qemu:cpus_kick_thread: Permission denied", log["sources"][0]["text"])
        # Later reads of the ended job write nothing more.
        ops.list_operations()
        self.assertEqual(1, len(self.sent))

    def test_backoff_without_a_crash_event_also_fails_with_kubevirts_words(self):
        self.start()
        self.vm["status"]["printableStatus"] = "CrashLoopBackOff"
        self.event("SyncFailed", "server error. command SyncVMI failed: the disk is missing. More detail")
        job = ops.list_operations()[0]
        self.assertEqual("failed", job["status"])
        self.assertIn("KubeVirt says: server error.", job["message"])

    def test_a_restart_never_writes_and_says_how_to_end_the_retries(self):
        self.body["action"] = "restart"
        self.context["observations"]["vmi"] = {"uid": "old-vmi"}
        self.start("Always")
        self.event("Stopped", "The VirtualMachineInstance crashed.")
        job = ops.list_operations()[0]
        self.assertEqual("failed", job["status"])
        self.assertIn("KubeVirt gave no reason.", job["message"])
        self.assertIn("Stop the VM to end the retries.", job["message"])
        self.assertEqual([], self.sent)

    def test_a_crash_before_this_request_is_not_this_jobs(self):
        self.event("Stopped", "The VirtualMachineInstance crashed.", offset=-600)
        self.start()
        self.assertEqual("running", ops.list_operations()[0]["status"])
        self.assertEqual([], self.sent)

    def test_a_refused_put_back_is_said_and_not_retried(self):
        def refuse(method, path, body=None, **options):
            self.sent.append(path)
            raise urllib.error.HTTPError(path, 422, "test failed", {}, None)
        self.send = refuse
        self.start()
        self.event("Stopped", "The VirtualMachineInstance crashed.")
        job = ops.list_operations()[0]
        self.assertEqual("failed", job["status"])
        self.assertIn("could not set it back to stay off (HTTP 422)", job["message"])
        ops.list_operations()
        self.assertEqual(1, len(self.sent))

    def test_a_ready_instance_still_succeeds(self):
        self.start()
        self.vmi["status"].update(phase="Running", conditions=[{"type": "Ready", "status": "True"}])
        self.assertEqual("succeeded", ops.list_operations()[0]["status"])


if __name__ == "__main__":
    unittest.main()
