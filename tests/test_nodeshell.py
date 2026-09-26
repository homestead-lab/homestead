import sys, unittest, urllib.error, urllib.parse
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_nodeshell as SHELL
import homestead_console as CONSOLE


class Cluster:
    def __init__(self, ready=True):
        self.pods, self.sent = {}, []
        self.node = {"metadata": {"name": "node-1"},
                     "status": {"conditions": [{"type": "Ready", "status": "True" if ready else "Unknown"}]}}
        SHELL.bind(self.get, self.send, "lab")
        SHELL._sessions.clear()

    def get(self, path):
        if path == "/api/v1/nodes/node-1":
            return self.node
        name = path.rsplit("/", 1)[1]
        if "/pods/" in path and name in self.pods:
            return self.pods[name]
        raise urllib.error.HTTPError(path, 404, "missing", {}, None)

    def send(self, method, path, body=None, **kw):
        self.sent.append((method, path))
        if method == "POST":
            # The kubelet starts it straight away.
            self.pods[body["metadata"]["name"]] = dict(body, status={"phase": "Running"})
        elif method == "DELETE":
            self.pods.pop(path.rsplit("/", 1)[1], None)


class NodeShellTests(unittest.TestCase):
    def test_the_helper_is_a_privileged_pod_on_that_node_that_does_not_outlive_its_day(self):
        spec = SHELL.body("node-1")["spec"]
        self.assertEqual(("node-1", True, True, True), (spec["nodeName"], spec["hostPID"], spec["hostNetwork"],
                                                      spec["containers"][0]["securityContext"]["privileged"]))
        self.assertEqual([{"operator": "Exists"}], spec["tolerations"], "a cordoned or tainted node still opens")
        self.assertEqual(8 * 3600, spec["activeDeadlineSeconds"])

    def test_opening_starts_the_helper_and_enters_the_host(self):
        c = Cluster()
        target = SHELL.open_shell("node-1", sleep=lambda s: None)
        self.assertEqual(("lab", "homestead-shell-node-1", "shell"), (target["namespace"], target["pod"], target["container"]))
        self.assertEqual(["nsenter", "-t", "1"], target["command"][:3])
        SHELL.open_shell("node-1", sleep=lambda s: None)
        self.assertEqual(1, sum(1 for m, p in c.sent if m == "POST"), "one helper serves every session")

    def test_a_node_that_is_not_ready_is_refused(self):
        Cluster(ready=False)
        with self.assertRaisesRegex(ValueError, "not Ready"):
            SHELL.open_shell("node-1", sleep=lambda s: None)

    def test_the_helper_goes_when_the_last_session_closes(self):
        c = Cluster()
        SHELL.open_shell("node-1", sleep=lambda s: None)
        SHELL.session_started("node-1")
        SHELL.session_started("node-1")
        SHELL.session_ended("node-1")
        self.assertIn("homestead-shell-node-1", c.pods)
        SHELL.session_ended("node-1")
        self.assertNotIn("homestead-shell-node-1", c.pods)

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
