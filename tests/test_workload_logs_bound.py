"""GET /api/workloads/logs asks Kubernetes for at most LOG_BYTES (#986 §7):
the kubelet splits lines at 16 KB, so 1,000 lines can be 16 MB."""
import io
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_workloads as WL


class Answer(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class LogBoundTests(unittest.TestCase):
    def read(self, body=b"line\n"):
        """body: bytes, or a function of the lines asked for."""
        asked = self.asked = []

        def urlopen(request, **kw):
            asked.append(request.full_url)
            lines = int(request.full_url.split("tailLines=")[1].split("&")[0])
            return Answer(body(lines) if callable(body) else body)
        request = SimpleNamespace(query={"ns": ["lab"], "pod": ["web-1"], "container": ["web"], "tail": ["1000"]},
                                  role="admin", user="alice")
        with mock.patch.object(WL.urllib.request, "urlopen", urlopen), \
             mock.patch.object(WL, "kget", lambda path: {"metadata": {"name": "web-1"}, "spec": {"containers": [{"name": "web"}]}},
                               create=True), \
             mock.patch.object(WL, "API", "https://192.0.2.1", create=True), \
             mock.patch.object(WL, "TOKEN", "t", create=True), mock.patch.object(WL, "CTX", None, create=True):
            reply = WL.logs(request)
        return asked[0], reply

    def test_a_log_read_sends_limit_bytes(self):
        url, _ = self.read()
        self.assertIn("tailLines=1000", url)
        self.assertIn(f"limitBytes={WL.LOG_BYTES}", url)
        self.assertEqual(2 * 1024 * 1024, WL.LOG_BYTES)

    def test_a_log_that_reaches_the_limit_is_asked_again_with_fewer_lines_so_the_newest_are_kept(self):
        """limitBytes keeps the oldest of the lines asked for: a live Logs view
        (Follow) would sit on old lines. Half the lines, until they fit."""
        with mock.patch.object(WL, "LOG_BYTES", 10):
            _, reply = self.read(lambda lines: b"0123456789" if lines > 250 else b"newest\n")
        self.assertEqual(["1000", "500", "250"], [u.split("tailLines=")[1].split("&")[0] for u in self.asked])
        self.assertTrue(reply.body.startswith("newest\n"), reply.body)
        self.assertIn("last 250 lines", reply.body.splitlines()[-1])

    def test_a_log_that_never_fits_is_asked_a_few_times_at_most_and_says_it_was_cut(self):
        with mock.patch.object(WL, "LOG_BYTES", 10):
            _, reply = self.read(b"0123456789")
        self.assertEqual(WL.TAIL_RETRIES + 1, len(self.asked))
        self.assertTrue(reply.body.startswith("0123456789"))
        self.assertIn("newest lines may be missing", reply.body.splitlines()[-1])

    def test_a_short_log_is_returned_as_it_is(self):
        _, reply = self.read(b"one\ntwo\n")
        self.assertEqual("one\ntwo\n", reply.body)
        self.assertEqual(1, len(self.asked))


if __name__ == "__main__":
    unittest.main()
