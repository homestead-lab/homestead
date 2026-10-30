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
BEATS = "/api/v1/namespaces/lab/configmaps/homestead-shell-sessions"


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
                      mock.patch.object(SHELL, "_start_beater", lambda: None),
                      mock.patch.object(SHELL, "REPLICA", "replica-a"),
                      # Up for a while: the first minutes after a start are their own test.
                      mock.patch.object(SHELL, "_started", self.now - 10 * SHELL.LEGACY_SECONDS)):
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
        self.assertEqual(1, sum(1 for w in kube.writes if w[0] == "POST" and w[1].endswith("/pods")),
                         "one helper serves every session")

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
        self.assertEqual({}, self.records(kube), "its records go with it")

    def opened(self):
        """A helper that prepare() made, as the browser is about to connect."""
        kube = self.cluster()
        SHELL.prepare("node-1")
        self.assertIsNotNone(kube.object(HELPER))
        return kube

    def records(self, kube):
        return (kube.object(BEATS) or {}).get("data") or {}

    def as_replica(self, name, sessions):
        """Run as another Homestead process, with its own open sessions."""
        return mock.patch.multiple(SHELL, REPLICA=name, _sessions=dict(sessions))

    def test_a_helper_nobody_connects_to_ends_after_the_connect_deadline(self):
        kube = self.opened()
        self.now += SHELL.CONNECT_SECONDS + SHELL.SKEW_SECONDS - 1
        self.assertEqual([], SHELL.sweep())
        self.assertIsNotNone(kube.object(HELPER))
        self.now += 1
        self.assertEqual(["homestead-shell-node-1"], SHELL.sweep())
        self.assertIsNone(kube.object(HELPER))
        self.assertLessEqual(SHELL.CONNECT_SECONDS + SHELL.SKEW_SECONDS, 120, "a couple of minutes, not hours")

    def test_after_a_restart_a_helper_whose_sessions_stopped_beating_ends(self):
        kube = self.opened()
        SHELL.session_started("node-1")
        self.now += 600
        SHELL.beat()                      # a live session keeps it
        SHELL._sessions.clear()           # Homestead restarts: nobody counts it any more
        self.now += SHELL.IDLE_SECONDS + SHELL.SKEW_SECONDS - 1
        self.assertEqual([], SHELL.sweep())
        self.now += 1
        self.assertEqual(["homestead-shell-node-1"], SHELL.sweep())
        self.assertIsNone(kube.object(HELPER))
        self.assertEqual({}, self.records(kube))

    def test_the_beat_is_kept_beside_the_helper_never_on_it(self):
        kube = self.opened()
        SHELL.session_started("node-1")
        self.now += SHELL.BEAT_SECONDS
        SHELL.beat()
        self.assertEqual([], [w for w in kube.writes if w[0] == "PATCH" and "/pods/" in w[1]],
                         "Homestead's role does not patch pods")
        self.assertEqual({"homestead-shell-node-1.replica-a": str(int(self.now + SHELL.IDLE_SECONDS))},
                         self.records(kube))

    def test_a_session_on_another_replica_keeps_its_helper_while_it_beats(self):
        kube = self.opened()
        SHELL.session_started("node-1")
        for _ in range(10):
            self.now += SHELL.BEAT_SECONDS
            SHELL.beat()
            with self.as_replica("replica-b", {}):       # the leader is another process
                self.assertEqual([], SHELL.sweep())
        self.assertIsNotNone(kube.object(HELPER))

    def test_closing_the_last_session_here_keeps_a_helper_another_replica_serves(self):
        kube = self.opened()
        SHELL.session_started("node-1")
        with self.as_replica("replica-b", {}):
            SHELL.session_started("node-1")
            SHELL.session_ended("node-1")
        self.assertIsNotNone(kube.object(HELPER), "replica-a's terminal is still open")
        self.assertEqual(["homestead-shell-node-1.replica-a"], list(self.records(kube)))
        SHELL.session_ended("node-1")
        self.assertIsNone(kube.object(HELPER))

    def test_the_sweeping_process_never_removes_a_helper_it_is_serving(self):
        kube = self.opened()
        SHELL.session_started("node-1")
        self.now += SHELL.IDLE_SECONDS * 10
        self.assertEqual([], SHELL.sweep())
        self.assertIsNotNone(kube.object(HELPER))

    def test_a_clock_a_little_ahead_does_not_end_a_live_shell(self):
        kube = self.opened()
        SHELL.session_started("node-1")
        with self.as_replica("replica-b", {}):
            self.now += SHELL.IDLE_SECONDS + SHELL.SKEW_SECONDS - 1   # the leader's clock runs ahead
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

    def old_helper(self, kube, annotated=False):
        """A helper made by the release before, which neither stamps nor beats."""
        old = SHELL.body("node-1")
        if not annotated:
            old["metadata"].pop("annotations")
        old["metadata"]["creationTimestamp"] = "2026-01-01T00:00:00Z"
        old["status"] = {"phase": "Running"}
        kube.add(old)
        self.now = 1767225600.0             # 2026-01-01T00:00:00Z

    def test_during_a_rolling_upgrade_a_helper_from_the_old_release_is_given_time(self):
        kube = self.cluster()
        self.old_helper(kube)
        self.now += SHELL.LEGACY_SECONDS + SHELL.SKEW_SECONDS - 1
        self.assertEqual([], SHELL.sweep(), "an old replica may still serve a terminal on it")
        self.now += 1
        self.assertEqual(["homestead-shell-node-1"], SHELL.sweep())

    def test_just_after_a_start_a_helper_nobody_here_beats_is_given_time(self):
        # A new helper whose terminal an old replica serves: the old replica does not beat.
        kube = self.cluster()
        self.old_helper(kube, annotated=True)
        with mock.patch.object(SHELL, "_started", self.now):
            self.now += SHELL.CONNECT_SECONDS + SHELL.SKEW_SECONDS
            self.assertEqual([], SHELL.sweep())
            self.now = SHELL._started + SHELL.LEGACY_SECONDS + SHELL.SKEW_SECONDS
            self.assertEqual(["homestead-shell-node-1"], SHELL.sweep())

    def test_records_of_helpers_that_have_gone_are_dropped(self):
        kube = self.opened()
        SHELL.session_started("node-1")
        K.ksend("DELETE", HELPER)
        SHELL._sessions.clear()
        SHELL.sweep()
        self.assertEqual({}, self.records(kube))

    def test_reusing_a_running_helper_gives_the_browser_time_to_connect(self):
        kube = self.opened()
        self.now += SHELL.LIFETIME / 2
        SHELL.prepare("node-1")
        self.now += SHELL.CONNECT_SECONDS - 1
        self.assertEqual([], SHELL.sweep())
        self.assertIsNotNone(kube.object(HELPER))

    def test_a_beat_that_cannot_be_written_says_so(self):
        kube = self.opened()
        SHELL.session_started("node-1")
        kube.fail(BEATS, 503, times=5)
        with mock.patch("builtins.print") as said:
            SHELL.beat()
        self.assertIn("node-1", " ".join(str(c) for c in said.call_args_list))

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

    def test_only_an_admin_opens_one(self):
        import server
        self.assertEqual("admin", server.needed_role("/api/node/shell", "GET"))
        self.assertEqual("admin", server.needed_role("/api/node/shell/prepare", "POST"))


if __name__ == "__main__":
    unittest.main()
