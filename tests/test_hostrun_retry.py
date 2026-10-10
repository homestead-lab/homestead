"""A host just back can show its helper Running before the API server reaches
its kubelet: the exec is refused and nothing ran, so it is tried again. Seen
on RKE2 in the release suite, after a host failed and came back."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_files as FILES
import homestead_hostrun as HOSTRUN


class Cluster:
    def __init__(self, answers):
        self.answers, self.calls, self.pods = list(answers), 0, {}

    def get(self, path):
        return self.pods.get(path)

    def send(self, method, path, body=None, **_):
        if method == "POST":
            name = body["metadata"]["name"]
            self.pods[f"{path}/{name}"] = {"status": {"phase": "Running"}}
        elif method == "DELETE":
            self.pods.pop(path, None)
        return {}

    def exec_in(self, *args, **kwargs):
        self.calls += 1
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer, ""


class RetryTests(unittest.TestCase):
    def run_with(self, *answers):
        cluster = Cluster(answers)
        HOSTRUN.bind(cluster.get, cluster.send, cluster.exec_in, "lab")
        paused = []
        try:
            return HOSTRUN.run("node-2", "echo ok", sleep=paused.append), cluster, paused
        except Exception as error:
            return error, cluster, paused

    def test_a_refused_exec_is_tried_again_until_the_kubelet_answers(self):
        refused = FILES.ExecRefused("Kubernetes refused to run a command in homestead-host-node-2 (HTTP/1.1 500)")
        result, cluster, paused = self.run_with(refused, refused, b"ok\n")
        self.assertEqual(("ok\n", ""), result)
        self.assertEqual(3, cluster.calls)
        self.assertEqual([HOSTRUN.EXEC_PAUSE] * 2, paused)
        self.assertEqual({}, cluster.pods, "the helper is removed after the run")

    def test_it_gives_up_with_the_api_servers_answer(self):
        refused = FILES.ExecRefused("Kubernetes refused to run a command in homestead-host-node-2 (HTTP/1.1 500)")
        result, cluster, _ = self.run_with(*[refused] * HOSTRUN.EXEC_TRIES)
        self.assertIsInstance(result, FILES.ExecRefused)
        self.assertIn("HTTP/1.1 500", str(result))
        self.assertEqual(HOSTRUN.EXEC_TRIES, cluster.calls)

    def test_other_failures_are_not_run_again(self):
        # Once the exec is open the script may have run: never twice.
        result, cluster, paused = self.run_with(ConnectionError("connection reset mid-run"))
        self.assertIsInstance(result, ConnectionError)
        self.assertEqual(1, cluster.calls)
        self.assertEqual([], paused)

    def test_a_refusal_is_still_a_connection_error_for_other_callers(self):
        self.assertTrue(issubclass(FILES.ExecRefused, ConnectionError))


if __name__ == "__main__":
    unittest.main()
