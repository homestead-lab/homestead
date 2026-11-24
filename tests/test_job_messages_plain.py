"""What a job says is shown the way the VM disks list and alerts show the same texts.

A job's message and the steps of its Log can quote text from outside
Homestead: a disk import's status from CDI, the error of a status check that
failed. They are shown as the VM disks list shows a disk import's status and
as alerts show an error, while the names of apps, volumes and hosts and the
reason stay. What a host or a pod printed goes to the job's Log as output
steps, and the message says so in Homestead's words."""
import contextlib
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "tests"))

import homestead_alerts as ALERTS
import homestead_components as C
import homestead_job_errors as ERRORS
import homestead_host_os as HOST_OS
import homestead_kube as K
import homestead_lhv2_cleanup as LHV2_CLEANUP
import homestead_longhorn_runtime as RUNTIME
import homestead_operations as OPS
from fakes.job_store import put
from fakes.kube import Kube
from rebind import keep_bound
from test_components import Cluster, LH_VERSION_SETTING, helmchart, job, node, plan

# Built in pieces: no value here is real, and none may look like one to a scanner.
SIGNATURE = "".join(("9f8e7d6c", "5b4a3928", "1706f5e4", "d3c2b1a0"))
KEY = "".join(("Kq7", "Zr2", "Xw9", "Lm4"))
SIGNED = ("https://images.example.com/disks/router.qcow2?X-Amz-Algorithm=AWS4-HMAC-SHA256"
          f"&X-Amz-Signature={SIGNATURE}&X-Amz-Expires=600")
DATAVOLUME = "/apis/cdi.kubevirt.io/v1beta1/namespaces/lab/datavolumes/router"


def datavolume(phase, message):
    return {"apiVersion": "cdi.kubevirt.io/v1beta1", "kind": "DataVolume",
            "metadata": {"namespace": "lab", "name": "router"},
            "status": {"phase": phase, "progress": "12%",
                       "conditions": [{"type": "Running", "status": "False", "message": message}]}}


class Store(unittest.TestCase):
    """The real job store in a temporary folder, on the shared Kubernetes fake."""

    def setUp(self):
        self.tmp = self.enterContext(tempfile.TemporaryDirectory())
        keep_bound(self, OPS)

    def bind(self):
        OPS.bind(K.kget, self.tmp, lambda namespace, name: {})

    def import_job(self):
        return OPS.start("vm-disk-import", "Import VM disk router",
                         {"kind": "DataVolume", "name": "router", "namespace": "lab"},
                         "/import", {"namespace": "lab", "name": "router"})["id"]

    def shown(self, job_id):
        """Every place a person or an agent reads the job: the list, its log, the alert."""
        row = next(row for row in OPS.snapshot() if row["id"] == job_id)
        log = OPS.log(job_id)
        alerts = [fact["body"] for fact in ALERTS.job_facts(OPS.snapshot()) if fact.get("key") == f"jobs:{job_id}"]
        return row, [row["message"], log["message"], *(step["m"] for step in log["history"]), *alerts]


class DiskImportTests(Store):
    def setUp(self):
        super().setUp()
        self.kube = self.enterContext(Kube(datavolume(
            "Failed", f"Unable to connect to http data source: expected status code 200, got 403. "
                      f"Status: 403 Forbidden (url {SIGNED})")))
        self.bind()

    def test_a_failed_import_names_its_source_and_reason_but_not_the_values_of_its_link(self):
        job_id = self.import_job()
        OPS.list_operations()
        row, texts = self.shown(job_id)
        self.assertEqual("failed", row["status"])
        for text in texts:
            self.assertNotIn(SIGNATURE, text)
            self.assertNotIn("600", text)
        self.assertIn("403 Forbidden", row["message"])
        self.assertIn("https://images.example.com/disks/router.qcow2?X-Amz-Algorithm=", row["message"])
        self.assertIn("X-Amz-Signature=", row["message"], "the name of a value stays; its value goes")

    def test_a_message_saved_before_this_change_is_made_plain_when_read(self):
        job_id = self.import_job()
        stored = OPS.stored()
        for item in stored:
            item.update(status="failed", message=f"Import failed: {SIGNED}")
            item["history"].append({"t": item["started_at"], "s": "failed", "p": 12, "m": f"Import failed: {SIGNED}"})
        put(OPS, stored)
        self.assertIn(SIGNATURE, json.dumps(OPS.stored()), "saved as it was")
        _, texts = self.shown(job_id)
        for text in texts:
            self.assertNotIn(SIGNATURE, text)
        summary = OPS.diagnostic_summaries({job_id})[0]
        self.assertNotIn(SIGNATURE, json.dumps(summary), "nor in a diagnostics report")


class StatusCheckErrorTests(Store):
    """A status check that raises: its error is said while the job waits, and
    again when the job gives up (homestead_job_errors)."""
    MINUTE = 60

    def setUp(self):
        super().setUp()
        self.kube = self.enterContext(Kube())
        self.bind()
        self.clock = [10_000.0]
        self.enterContext(mock.patch.object(ERRORS, "clock", lambda: self.clock[0]))
        self.enterContext(mock.patch.dict(OPS.RESOLVERS, {"vm-disk-import": OPS.RESOLVERS["vm-disk-import"]}))
        self.error = ConnectionError(
            f"node-a.example.com: proxy said 502 for https://registry.example.com/v2/?token={KEY}x1 "
            f"(Authorization: Bearer {KEY}y2)")
        self.kube.fail(DATAVOLUME, self.error, times=10 ** 9)

    def test_the_error_of_a_failing_check_names_the_host_but_not_what_it_sent(self):
        job_id = self.import_job()
        OPS.list_operations()
        row, texts = self.shown(job_id)
        self.assertEqual("running", row["status"])
        self.assertIn("Status temporarily unavailable: node-a.example.com: proxy said 502", row["message"])
        for text in texts:
            self.assertNotIn(KEY, text)

    def test_a_job_that_gives_up_says_the_error_without_what_it_sent(self):
        job_id = self.import_job()
        end = self.clock[0] + ERRORS.limit("vm-disk-import") + 2 * self.MINUTE
        while self.clock[0] < end:
            OPS.list_operations()
            self.clock[0] += 30
        row, texts = self.shown(job_id)
        self.assertIn("node-a.example.com: proxy said 502", row["message"])
        self.assertTrue(any(said in row["message"] for said in ("without a status", "no status")), row["message"])
        for text in texts:
            self.assertNotIn(KEY, text)


class UpgradeOutputTests(Store):
    """A cluster upgrade whose job on a host failed: the job list says so in
    Homestead's words, and the job's Log holds what that job's pod wrote."""
    TARGET = "v1.32.8+k3s1"

    def upgrade(self, host):
        self.c = Cluster(self, {"distribution": "k3s", "longhorn": True, "version": "1.31.12+k3s1"},
                         node(host), node("node-2"), LH_VERSION_SETTING, helmchart("longhorn"),
                         helmchart(C.SUC_CHART), plan("homestead-server"), plan("homestead-agent"),
                         job(f"apply-homestead-server-on-{host}", "homestead-server", host, failed=6, conditions=[
                             {"type": "Failed", "status": "True", "reason": "BackoffLimitExceeded",
                              "message": "Job has reached the specified backoff limit"}]))
        self.c.upgrade_api()
        self.c.kube.add({"apiVersion": "v1", "kind": "Pod", "metadata": {
            "namespace": C.SUC_NS, "name": "apply-1-a", "creationTimestamp": "2026-10-06T12:48:00Z",
            "labels": {"job-name": f"apply-homestead-server-on-{host}"}}})
        output = "\n".join([
            "+ set -e", "[INFO]  Comparing old and new binaries",
            f"[INFO]  Fetching https://mirror.example.com/k3s/v1.32.8?token={KEY}z3",
            f"[ERROR] mirror refused password={KEY}w4 for {host}", "+ exit 1", ""])
        keep = C.ktext
        C.ktext = lambda path: output
        self.addCleanup(setattr, C, "ktext", keep)
        self.bind()
        self.enterContext(mock.patch.dict(OPS.RESOLVERS, {"platform-upgrade": C.status}))
        job_id = OPS.start("platform-upgrade", "Upgrade the cluster", {"kind": "Cluster", "name": "cluster"},
                           "/cluster", {"component": "cluster", "to": self.TARGET, "phase": "nodes",
                                        "started": time.time()})["id"]
        OPS.list_operations()
        return job_id

    def test_a_failed_upgrade_says_so_and_its_log_holds_what_its_pod_wrote_without_what_it_sent(self):
        job_id = self.upgrade("node-1")
        row, texts = self.shown(job_id)
        self.assertEqual("failed", row["status"])
        self.assertEqual("The upgrade job on node-1 failed (Job has reached the specified backoff limit). "
                         "Its last output is in the job's Log.", row["message"])
        output = [step["m"] for step in OPS.log(job_id)["history"] if step.get("output")]
        self.assertEqual(3, len(output), output)
        self.assertIn("[INFO] Fetching https://mirror.example.com/k3s/v1.32.8?token=", output[1])
        self.assertIn("[ERROR] mirror refused password=", output[2])
        for text in texts:
            self.assertNotIn(KEY, text)
        self.assertFalse([row for row in OPS.snapshot() if "[INFO]" in row["message"] or "[ERROR]" in row["message"]],
                         "the job list carries no line the pod wrote")

    def test_a_host_named_like_a_sign_in_app_keeps_every_word_of_the_message(self):
        job_id = self.upgrade("authentik-1")
        row, _ = self.shown(job_id)
        self.assertEqual("The upgrade job on authentik-1 failed (Job has reached the specified backoff limit). "
                         "Its last output is in the job's Log.", row["message"])


class OutputTests(Store):
    """What a job's work printed goes to its Log once, while its message stays Homestead's sentence."""

    def setUp(self):
        super().setUp()
        self.kube = self.enterContext(Kube())
        self.bind()

        def resolve(item):
            OPS.note_output(item, ["Setting up curl", f"Fetching https://h.example.com/a?token={KEY}"])
            return "running", 40, "Installing updates on authelia-host. Its latest output is in the job's Log."
        self.enterContext(mock.patch.dict(OPS.RESOLVERS, {"vm-disk-import": resolve}))

    def test_output_is_logged_once_and_the_message_once(self):
        job_id = self.import_job()
        for _ in range(3):
            OPS.list_operations()
        steps = OPS.log(job_id)["history"]
        self.assertEqual(["Setting up curl", "Fetching https://h.example.com/a?token=<hidden>"],
                         [step["m"] for step in steps if step.get("output")])
        self.assertEqual(1, sum(1 for step in steps if step["m"].startswith("Installing updates")))
        row, texts = self.shown(job_id)
        self.assertEqual("Installing updates on authelia-host. Its latest output is in the job's Log.", row["message"])
        for text in texts:
            self.assertNotIn(KEY, text)


class HostOutputTests(Store):
    """An error that came with what a host printed: the message is Homestead's
    sentence, and the host's lines are output steps of the job's Log."""

    def setUp(self):
        super().setUp()
        self.kube = self.enterContext(Kube())
        self.bind()

        def resolve(item):
            raise OPS.HostOutput("The updates on node-a did not start", f"E: dpkg was interrupted\nauth token={KEY}\n")
        self.enterContext(mock.patch.dict(OPS.RESOLVERS, {"vm-disk-import": resolve}))

    def test_a_status_check_whose_error_came_with_a_hosts_output(self):
        job_id = self.import_job()
        OPS.list_operations()
        row, texts = self.shown(job_id)
        self.assertEqual("Status temporarily unavailable: The updates on node-a did not start. "
                         "The host's output is in the job's Log.", row["message"])
        self.assertEqual(["E: dpkg was interrupted", "auth token=<hidden>"],
                         [step["m"] for step in OPS.log(job_id)["history"] if step.get("output")])
        for text in texts:
            self.assertNotIn(KEY, text)
        self.assertFalse([r for r in OPS.snapshot() if "dpkg" in r["message"]], "the job list carries no host line")

    def test_a_value_on_the_line_after_its_name_is_hidden_in_the_log(self):
        for output in (f"Password:\n{KEY}\ndone", f"api_key =\n   {KEY}", ["token:", KEY, "done"]):
            with self.subTest(output):
                item = {"history": []}
                lines = output if isinstance(output, list) else OPS.HostOutput("It stopped", output).output
                OPS.note_output(item, lines)
                steps = [step["m"] for step in item["history"]]
                self.assertTrue(steps)
                for step in steps:
                    self.assertNotIn(KEY, step)

    def test_the_host_checks_that_print_raise_homesteads_sentence_with_the_output_apart(self):
        said = ("Traceback (most recent call last)", "OSError: no such device")
        cases = (
            (lambda: RUNTIME._host("node-a", "1"), RUNTIME.HOST, "Runtime maintenance could not be verified on node-a, because its worker gave no result"),
            (lambda: LHV2_CLEANUP.host({"node": "node-a", "uid": "u1"}), LHV2_CLEANUP.HOST,
             "Host memory inspection did not finish on node-a"),
        )
        for call, host, sentence in cases:
            with self.subTest(sentence), mock.patch.object(host, "run", return_value=("no result", "\n".join(said))):
                with self.assertRaises(OPS.HostOutput) as raised:
                    call()
                self.assertEqual(sentence, str(raised.exception))
                self.assertEqual(list(said), raised.exception.output)

    def test_updates_that_did_not_start_say_so_and_keep_the_hosts_lines_apart(self):
        class Host:
            def hold(self, node, what, seconds=60, holder=""):
                return "lease/1"

            def let_go(self, node, holder):
                pass

            def run(self, node, script, timeout=60, change="", holder=""):
                return "", "E: Could not get lock /var/lib/dpkg/lock-frontend"
        keep_bound(self, HOST_OS)
        HOST_OS.bind(lambda path: {"items": []}, Host(), lambda force=False: {"distribution": "k3s"}, self.tmp)
        facts = {"package_manager": "apt", "updates": [{"name": "curl", "security": False}], "security": 0}
        with self.assertRaises(OPS.HostOutput) as raised:
            HOST_OS.upgrade_begin("node-a", facts, {})
        self.assertEqual("The updates on node-a did not start, because the host did not confirm the launch", str(raised.exception))
        self.assertEqual(["E: Could not get lock /var/lib/dpkg/lock-frontend"], raised.exception.output)


class CentralRoutingTests(Store):
    """A status check's error that came with a host's output, through the job
    store: the message is Homestead's sentence and the output is in the Log,
    with nothing asked of the check itself but to return or raise its error."""

    def setUp(self):
        super().setUp()
        self.kube = self.enterContext(Kube())
        self.bind()
        self.enterContext(mock.patch.dict(OPS.RESOLVERS, {"import": RUNTIME.progress}))
        self.enterContext(mock.patch.object(RUNTIME.V2, "_ready", lambda node: True))
        self.enterContext(mock.patch.object(RUNTIME.V2, "_get", lambda path: {}))

    def test_runtime_maintenance_that_could_not_be_verified_in_a_job(self):
        printed = f"worker refused: the store is busy\napi_key:\n  {KEY}"
        job_id = OPS.start("import", "Runtime maintenance on node-a", {"kind": "Node", "name": "node-a"}, "/disks",
                           {"phase": "staged", "node": "node-a", "request_id": "r1"})["id"]
        with mock.patch.object(RUNTIME.HOST, "run", return_value=("no result", printed)):
            OPS.list_operations()
        row, texts = self.shown(job_id)
        self.assertEqual("failed", row["status"])
        self.assertEqual("Runtime maintenance could not be verified on node-a, because its worker gave no result. The host's output is in the job's Log.",
                         row["message"])
        output = [step["m"] for step in OPS.log(job_id)["history"] if step.get("output")]
        self.assertEqual("worker refused: the store is busy", output[0])
        self.assertTrue(output[1].startswith("api_key:"), output)
        for text in texts:
            self.assertNotIn(KEY, text)


class AdminAnswerTests(unittest.TestCase):
    """A Longhorn runtime maintenance request an administrator makes, outside a
    job, answers with what the host said, as before."""

    def test_the_review_answer_keeps_what_the_host_said(self):
        import server
        handler = object.__new__(server.H)
        handler.path, handler.headers, handler.command = "/api/disks/runtime/review", {"X-Homestead-Auth": "1"}, "POST"
        handler._guard = lambda path: False
        handler._body = lambda: {"node": "node-a", "disk": "disk-1"}
        handler._client_ip = lambda: "127.0.0.1"
        handler.user, handler.role = "admin", "admin"
        handler._send = mock.Mock()
        no_jobs = SimpleNamespace(transaction=contextlib.nullcontext)   # no job: the request alone
        with mock.patch.object(RUNTIME, "ops", no_jobs), mock.patch.object(RUNTIME, "_pending", lambda node: None), \
                mock.patch.object(RUNTIME, "_conflicts", lambda node: None), \
                mock.patch.object(RUNTIME, "_plan", lambda node, disk, rid: RUNTIME._call(node, "inspect()")), \
                mock.patch.object(RUNTIME.HOST, "run", return_value=("no result", "refused: disk-1 is in use")):
            handler.do_POST()
        code, body = handler._send.call_args.args[:2]
        self.assertEqual(400, code, body)
        self.assertIn("Runtime maintenance could not be verified on node-a, because its worker gave no result", body["error"])
        self.assertIn("refused: disk-1 is in use", body["error"])


class PlainTextTests(Store):
    def setUp(self):
        super().setUp()
        self.kube = self.enterContext(Kube())
        self.bind()
        self.said = ["running", 40, ""]
        self.enterContext(mock.patch.dict(OPS.RESOLVERS, {"vm-disk-import": lambda item: tuple(self.said)}))

    def run_with(self, message):
        self.said[2] = message
        job_id = self.import_job()
        OPS.list_operations()
        return job_id

    def test_a_plain_message_is_kept_as_it_is_and_logged_once(self):
        message = ("Moving pvc-0b1c2d3e-1111-2222-3333-444455556666 of photos to node-b (192.0.2.11): "
                   "Waiting for pod nextcloud-7d9f8c6b5-x2k4z on node-a: ImagePullBackOff")
        job_id = self.run_with(message)
        for _ in range(3):
            OPS.list_operations()
        row, _ = self.shown(job_id)
        self.assertEqual(message, row["message"])
        self.assertEqual([message], [step["m"] for step in OPS.log(job_id)["history"] if step["s"] == "running"][-1:])
        self.assertEqual(1, sum(1 for step in OPS.log(job_id)["history"] if step["s"] == "running"),
                         "the same plain message is not a new step every round")

    def test_a_long_message_is_cut_and_made_plain_quickly(self):
        message = f"password={KEY} " + "a.b-" * 250_000
        started = time.perf_counter()
        job_id = self.run_with(message)
        for _ in range(20):
            OPS.snapshot()
        self.assertLess(time.perf_counter() - started, 2.0)
        row, _ = self.shown(job_id)
        self.assertLessEqual(len(row["message"]), 500)
        self.assertNotIn(KEY, row["message"])

    def test_the_sentences_that_point_to_the_log_keep_every_word_with_a_sign_in_apps_name(self):
        # The shapes written where a job's output goes to its Log (components, promote,
        # host OS, OS rollout, Unraid import, host console): no name is followed by ":" or "=".
        for name in ("authentik", "passbolt-1", "oauth2-proxy", "session-store"):
            for sentence in (f"The upgrade job on {name} failed (BackoffLimitExceeded). Its last output is in the job's Log.",
                             f"Reinstalling k3s on {name}. Its latest output is in the job's Log.",
                             f"The reinstall on {name} stopped with code 1. Its last output is in the job's Log.",
                             f"Installing updates on {name}. Its latest output is in the job's Log.",
                             f"Installing updates on {name}. Its latest output is in the job's Log, under Jobs.",
                             f"Installing updates on {name}", f"{name} said Setting up curl",
                             f"The rollout is waiting and tries again. The updates on {name} did not start, because the host did not "
                             "confirm the launch. "
                             "The host's output is in the job's Log.",
                             f"Runtime maintenance could not be verified on {name}, because its worker gave no result. "
                             "The host's output is in the job's Log.",
                             f"Host memory inspection did not finish on {name}. The host's output is in the job's Log",
                             f"Restarting {name} is waiting up to 15 minutes (it is busy)",
                             f"Draining and restarting {name}",
                             f"Copying {name} failed. The copy's last error is in the job's Log",
                             f"Installing the host console on {name} failed (step=install exit=1). "
                             "The host's output is in the job's Log."):
                with self.subTest(sentence):
                    self.assertEqual(sentence, OPS.plain_message(sentence))

    def test_a_value_after_a_name_and_several_spaces_is_hidden_too(self):
        for message in (f"password:   {KEY}", f"Token =\t {KEY}", f"api_key:\n  {KEY}"):
            with self.subTest(message):
                self.assertNotIn(KEY, OPS.plain_message(message))
        job_id = self.run_with(f"The host said Password:      {KEY}")
        _, texts = self.shown(job_id)
        for text in texts:
            self.assertNotIn(KEY, text)

    def test_making_a_message_plain_again_never_shows_more(self):
        for message in (f"Import failed: {SIGNED}", f"token: {KEY}", "Every node runs v1.32.8+k3s1",
                        f"{'x' * 480} https://h.example.com/a?b={KEY}"):
            once = OPS.plain_message(message)
            self.assertEqual(once, OPS.plain_message(once))
            self.assertNotIn(KEY, OPS.plain_message(once))


if __name__ == "__main__":
    unittest.main()
