"""The host shell helper pod. Runs on the shared fake API (tests/fakes/kube.py)."""
import sys, unittest, urllib.parse
from unittest import mock
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_kube as K
import homestead_nodeshell as SHELL
import homestead_console as CONSOLE
from fakes.kube import Kube

HELPER = "/api/v1/namespaces/lab/pods/homestead-shell-node-1"


class NodeShellTests(unittest.TestCase):
    def cluster(self, ready=True):
        node = {"apiVersion": "v1", "kind": "Node", "metadata": {"name": "node-1"},
                "status": {"conditions": [{"type": "Ready", "status": "True" if ready else "Unknown"}]}}
        kube = self.enterContext(Kube(node))

        def kubelet(method, path, body):
            # The kubelet starts it straight away.
            body["status"] = {"phase": "Running"}
        kube.react("POST", "/api/v1/namespaces/lab/pods", kubelet)
        self.addCleanup(SHELL.bind, *(getattr(SHELL, name, None) for name in ("kget", "ksend", "NS")))
        SHELL.bind(K.kget, K.ksend, "lab")
        SHELL._sessions.clear()
        self.addCleanup(SHELL._sessions.clear)
        self.now = 1_000_000.0
        for patch in (mock.patch.object(SHELL, "clock", lambda: self.now),
                      mock.patch.object(SHELL, "_start_beater", lambda: None)):
            patch.start()
            self.addCleanup(patch.stop)
        return kube

    def test_the_helper_is_a_privileged_pod_on_that_node_that_does_not_outlive_its_day(self):
        spec = SHELL.body("node-1")["spec"]
        self.assertEqual(("node-1", True, True, True), (spec["nodeName"], spec["hostPID"], spec["hostNetwork"],
                                                      spec["containers"][0]["securityContext"]["privileged"]))
        self.assertEqual([{"operator": "Exists"}], spec["tolerations"], "a cordoned or tainted node still opens")
        self.assertEqual(8 * 3600, spec["activeDeadlineSeconds"])

    def test_opening_starts_the_helper_and_enters_the_host(self):
        kube = self.cluster()
        target = SHELL.open_shell("node-1", sleep=lambda s: None)
        self.assertEqual(("lab", "homestead-shell-node-1", "shell"), (target["namespace"], target["pod"], target["container"]))
        self.assertEqual(["nsenter", "-t", "1"], target["command"][:3])
        SHELL.open_shell("node-1", sleep=lambda s: None)
        self.assertEqual(1, sum(1 for w in kube.writes if w[0] == "POST"), "one helper serves every session")

    def test_a_node_that_is_not_ready_is_refused(self):
        self.cluster(ready=False)
        with self.assertRaisesRegex(ValueError, "not Ready"):
            SHELL.open_shell("node-1", sleep=lambda s: None)

    def test_the_helper_goes_when_the_last_session_closes(self):
        kube = self.cluster()
        SHELL.open_shell("node-1", sleep=lambda s: None)
        SHELL.session_started("node-1")
        SHELL.session_started("node-1")
        SHELL.session_ended("node-1")
        self.assertIsNotNone(kube.object(HELPER))
        SHELL.session_ended("node-1")
        self.assertIsNone(kube.object(HELPER))

    def opened(self):
        """A helper that prepare() made, as the browser is about to connect."""
        kube = self.cluster()
        SHELL.prepare("node-1")
        self.assertIsNotNone(kube.object(HELPER))
        return kube

    def test_a_helper_nobody_connects_to_ends_after_the_connect_deadline(self):
        kube = self.opened()
        self.now += SHELL.CONNECT_SECONDS - 1
        self.assertEqual([], SHELL.sweep())
        self.assertIsNotNone(kube.object(HELPER))
        self.now += 1
        self.assertEqual(["homestead-shell-node-1"], SHELL.sweep())
        self.assertIsNone(kube.object(HELPER))
        self.assertLessEqual(SHELL.CONNECT_SECONDS, 60, "a minute at most, not hours")

    def test_after_a_restart_a_helper_whose_sessions_stopped_beating_ends(self):
        kube = self.opened()
        SHELL.session_started("node-1")
        self.now += 600
        SHELL.beat()                      # a live session keeps it
        SHELL._sessions.clear()           # Homestead restarts: nobody counts it any more
        self.now += SHELL.IDLE_SECONDS - 1
        self.assertEqual([], SHELL.sweep())
        self.now += 1
        self.assertEqual(["homestead-shell-node-1"], SHELL.sweep())
        self.assertIsNone(kube.object(HELPER))

    def test_a_session_on_another_replica_keeps_its_helper_while_it_beats(self):
        kube = self.opened()
        SHELL.session_started("node-1")
        for _ in range(10):
            self.now += SHELL.BEAT_SECONDS
            SHELL.beat()
            with mock.patch.dict(SHELL._sessions, clear=True):   # the leader is another process
                self.assertEqual([], SHELL.sweep())
        self.assertIsNotNone(kube.object(HELPER))

    def test_the_sweeping_process_never_removes_a_helper_it_is_serving(self):
        kube = self.opened()
        SHELL.session_started("node-1")
        self.now += SHELL.IDLE_SECONDS * 10
        self.assertEqual([], SHELL.sweep())
        self.assertIsNotNone(kube.object(HELPER))

    def test_a_helper_that_ran_its_time_does_not_stay_behind(self):
        kube = self.opened()
        pod = kube.object(HELPER)
        pod["status"] = {"phase": "Failed", "reason": "DeadlineExceeded"}
        kube.add(pod)
        self.assertEqual(["homestead-shell-node-1"], SHELL.sweep())
        self.assertIsNone(kube.object(HELPER))

    def test_the_sweep_leaves_other_pods_alone(self):
        kube = self.opened()
        other = SHELL.body("node-1")
        other["metadata"].update(name="homestead-shell-copy", labels={"homestead.io/task": "files"})
        path = kube.add(other)
        self.now += SHELL.LIFETIME
        self.assertEqual(["homestead-shell-node-1"], SHELL.sweep())
        self.assertIsNotNone(kube.object(path))

    def test_a_helper_from_before_there_was_a_deadline_ends_too(self):
        kube = self.cluster()
        old = SHELL.body("node-1")
        old["metadata"].pop("annotations")
        old["metadata"]["creationTimestamp"] = "2026-01-01T00:00:00Z"
        old["status"] = {"phase": "Running"}
        kube.add(old)
        self.now = 1767225600.0 + SHELL.CONNECT_SECONDS - 1   # 2026-01-01T00:00:00Z
        self.assertEqual([], SHELL.sweep())
        self.now += 1
        self.assertEqual(["homestead-shell-node-1"], SHELL.sweep())

    def test_reusing_a_running_helper_gives_the_browser_time_to_connect(self):
        kube = self.opened()
        self.now += SHELL.LIFETIME / 2
        SHELL.prepare("node-1")
        self.now += SHELL.CONNECT_SECONDS - 1
        self.assertEqual([], SHELL.sweep())
        self.assertIsNotNone(kube.object(HELPER))

    def test_the_first_session_starts_the_beat_and_the_beat_stops_with_the_last(self):
        self.cluster()
        started = []
        with mock.patch.object(SHELL, "_start_beater", lambda: started.append(1)):
            SHELL.session_started("node-1")
            SHELL.session_started("node-1")
        self.assertEqual([1, 1], started, "asked each time; it starts only one")
        naps = []

        def nap(seconds):
            naps.append(seconds)
            SHELL._sessions.clear()
        SHELL._beat_loop(sleep=nap)       # returns once no session is open
        self.assertEqual([SHELL.BEAT_SECONDS], naps)
        self.assertLess(SHELL.BEAT_SECONDS * 2, SHELL.IDLE_SECONDS, "one missed beat never ends a live shell")

    def test_the_host_command_reaches_kubernetes_one_argument_at_a_time(self):
        path = CONSOLE.exec_path("lab", "homestead-shell-node-1", "shell", SHELL.HOST_SHELL)
        query = urllib.parse.parse_qs(urllib.parse.urlparse(path).query)
        self.assertEqual(SHELL.HOST_SHELL, query["command"])
        self.assertEqual(["/bin/sh"], urllib.parse.parse_qs(
            urllib.parse.urlparse(CONSOLE.exec_path("lab", "p", "c", "/bin/sh")).query)["command"])

    def test_homestead_may_mark_its_helper_in_use(self):
        # The beat is a pod patch; refused, a live shell would end at IDLE_SECONDS.
        lines = (Path(__file__).resolve().parents[1] / "deploy" / "rbac.yaml").read_text().splitlines()
        at = lines.index("    resources: [pods, pods/log, pods/status]")
        self.assertIn("patch", lines[at + 1].split("[", 1)[1])

    def test_only_an_admin_opens_one(self):
        import server
        self.assertEqual("admin", server.needed_role("/api/node/shell", "GET"))
        self.assertEqual("admin", server.needed_role("/api/node/shell/prepare", "POST"))


if __name__ == "__main__":
    unittest.main()
