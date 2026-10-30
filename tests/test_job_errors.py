"""A job whose resolver keeps failing ends instead of waiting for ever.

Before, any exception from a resolver (or an HTTP error other than 404) left
the job "running · Status temporarily unavailable" every round with no
deadline. The job kept every start() guard, blocked a cluster shutdown and
every data move, and nobody was told. Now a job whose resolver has failed for
longer than its kind's limit either fails with the last error, which lets go
of its guards, or - for a kind that moves data or holds hosts or apps -
keeps its guards and says it needs attention and what to do."""
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "tests"))

import homestead_job_errors as ERRORS
import homestead_kube as K
import homestead_operations as OPS
from fakes.kube import Kube

SERVICE = {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "web", "namespace": "apps"},
           "spec": {"type": "LoadBalancer"}}   # no address yet: the job waits
SERVICE_PATH = "/api/v1/namespaces/apps/services/web"
CLAIM = {"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": {"name": "data", "namespace": "apps"},
         "spec": {}}
CLAIM_PATH = "/api/v1/namespaces/apps/persistentvolumeclaims/data"
MINUTE = 60


class Clock:
    def __init__(self, start=10_000.0):
        self.now = start

    def __call__(self):
        return self.now


class StoreTest(unittest.TestCase):
    """The real job store in a temporary folder, the shared Kubernetes fake,
    and a fake clock for how long a resolver has been failing."""

    def setUp(self):
        self.tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.kube = self.enterContext(Kube(SERVICE, CLAIM))
        OPS.bind(K.kget, self.tmp, lambda namespace, name: {})
        self.clock = Clock()
        self.enterContext(mock.patch.object(ERRORS, "clock", self.clock))
        # server.py (imported by another test) may wrap these; the store's own are tested.
        self.enterContext(mock.patch.dict(OPS.RESOLVERS, {"network-service": OPS._network_service,
                                                          "volume-delete": OPS._volume_delete}))

    def start(self, kind, ref, **extra):
        return OPS.start(kind, f"A {kind} job", {"kind": "Service", "name": "web", "namespace": "apps"},
                         "/", ref, **extra)["id"]

    def job(self, job_id):
        return next(row for row in OPS._read() if row["id"] == job_id)

    def rounds(self, minutes, every=3):
        """The jobs loop, every `every` s, for `minutes` of the fake clock."""
        end = self.clock.now + minutes * MINUTE
        while self.clock.now < end:
            OPS.list_operations()
            self.clock.now += every

    def fail_always(self, path, code=503):
        self.kube.fail(path, code, times=10 ** 9)


class GiveUpTests(StoreTest):
    def test_a_job_failing_within_its_limit_still_waits(self):
        job = self.start("network-service", {"namespace": "apps", "name": "web"})
        self.fail_always(SERVICE_PATH)
        self.rounds(ERRORS.DEFAULT_LIMIT / MINUTE - 1, every=30)
        item = self.job(job)
        self.assertEqual("running", item["status"])
        self.assertIn("Status temporarily unavailable (HTTP 503)", item["message"])

    def test_a_job_failing_past_its_limit_fails_with_the_last_error(self):
        job = self.start("network-service", {"namespace": "apps", "name": "web"})
        self.fail_always(SERVICE_PATH)
        self.rounds(ERRORS.DEFAULT_LIMIT / MINUTE + 1, every=30)
        item = self.job(job)
        self.assertEqual("failed", item["status"])
        self.assertIn("HTTP 503", item["message"])
        self.assertIn("30 min", item["message"])
        self.assertTrue(item["finished_at"])

    def test_a_failed_job_lets_go_of_its_guards(self):
        job = self.start("network-service", {"namespace": "apps", "name": "web"})
        with self.assertRaisesRegex(ValueError, "is running"):
            self.start("deployment", {}, unless=("network-service",))
        self.fail_always(SERVICE_PATH)
        self.rounds(ERRORS.DEFAULT_LIMIT / MINUTE + 1, every=30)
        self.assertEqual("failed", self.job(job)["status"])
        self.start("deployment", {}, unless=("network-service",))   # no longer refused

    def test_any_exception_counts_not_only_http_errors(self):
        job = self.start("network-service", {"namespace": "apps", "name": "web"})
        self.fail_always(SERVICE_PATH, code=TimeoutError("timed out"))
        self.rounds(ERRORS.DEFAULT_LIMIT / MINUTE + 1, every=30)
        item = self.job(job)
        self.assertEqual("failed", item["status"])
        self.assertIn("timed out", item["message"])

    def test_one_good_answer_starts_the_count_again(self):
        job = self.start("network-service", {"namespace": "apps", "name": "web"})
        self.kube.fail(SERVICE_PATH, 503, times=20)          # 20 rounds 60 s apart: 20 min
        self.rounds(21, every=MINUTE)                          # then one good answer
        self.fail_always(SERVICE_PATH)
        self.rounds(20, every=MINUTE)                          # 20 min more: 40 in all, never 30 in a row
        item = self.job(job)
        self.assertEqual("running", item["status"])
        self.assertIn("temporarily unavailable", item["message"])

    def test_a_404_still_fails_at_once(self):
        job = self.start("network-service", {"namespace": "apps", "name": "web"})
        self.kube.discard(SERVICE_PATH)
        OPS.list_operations()
        self.assertEqual("failed", self.job(job)["status"])

    def test_a_kind_can_have_a_longer_limit(self):
        # A host OS update may install for 2 h; it is not given up before its own deadline.
        self.assertGreater(ERRORS.limit("host-os"), 2 * 3600)
        self.assertEqual(ERRORS.DEFAULT_LIMIT, ERRORS.limit("network-service"))
        stub = mock.Mock(side_effect=RuntimeError("host not answering"))
        with mock.patch.dict(OPS.RESOLVERS, {"host-os": stub}):
            job = OPS.start("host-os", "Update node-a", {"kind": "Node", "name": "node-a"}, "/", {"node": "node-a"})["id"]
            self.rounds(ERRORS.limit("host-os") / MINUTE - 5, every=MINUTE)
            self.assertEqual("running", self.job(job)["status"])
            self.rounds(10, every=MINUTE)
        item = self.job(job)
        self.assertEqual("failed", item["status"], "resolved outside the store's lock, the count is kept too")
        self.assertIn("host not answering", item["message"])

    def test_the_count_is_not_shown_to_the_page(self):
        job = self.start("network-service", {"namespace": "apps", "name": "web"})
        self.fail_always(SERVICE_PATH)
        self.rounds(1)
        self.assertIn(ERRORS.KEY, self.job(job))
        shown = next(row for row in OPS.snapshot() if row["id"] == job)
        self.assertNotIn(ERRORS.KEY, shown)

    def test_a_count_from_a_clock_ahead_of_this_one_starts_again(self):
        # The leader moved to a pod whose clock is behind: the saved start is in its future.
        job = self.start("network-service", {"namespace": "apps", "name": "web"})
        self.fail_always(SERVICE_PATH)
        self.rounds(1)
        self.clock.now -= 3600
        self.rounds(ERRORS.DEFAULT_LIMIT / MINUTE - 1, every=30)
        self.assertEqual("running", self.job(job)["status"])

    def test_an_error_that_stays_the_same_is_not_written_every_round(self):
        job = self.start("network-service", {"namespace": "apps", "name": "web"})
        self.fail_always(SERVICE_PATH)
        self.rounds(1)
        saved = self.job(job)
        with mock.patch.object(OPS, "_write", wraps=OPS._write) as write:
            self.rounds(1)
        self.assertEqual(0, write.call_count)
        self.assertEqual(saved[ERRORS.KEY], self.job(job)[ERRORS.KEY])


class AttentionTests(StoreTest):
    """A kind that moves data, or holds a host or an app, keeps its guards."""

    def test_a_job_moving_data_keeps_its_guards_and_says_it_needs_attention(self):
        self.assertIn("volume-delete", ERRORS.KEEPS_GUARDS)
        job = self.start("volume-delete", {"namespace": "apps", "name": "data"})
        self.fail_always(CLAIM_PATH)
        self.rounds(ERRORS.DEFAULT_LIMIT / MINUTE + 1, every=30)
        item = self.job(job)
        self.assertEqual("running", item["status"], "still running: its guards stay")
        self.assertTrue(item["message"].startswith("Needs attention"), item["message"])
        self.assertIn("HTTP 503", item["message"])
        self.assertIn(ERRORS.KEEPS_GUARDS["volume-delete"], item["message"], "it says why it keeps them")
        self.assertIn("cancel", item["message"].lower(), "and what a person can do")
        with self.assertRaisesRegex(ValueError, "is running"):
            self.start("deployment", {}, unless=("volume-delete",))

    def broken_claim(self):
        """Every read of the claim fails while self.broken; each read is counted."""
        self.broken, self.reads = True, []

        def answer(method, path, body):
            self.reads.append(path)
            if self.broken:
                raise urllib.error.HTTPError(path, 503, "Service Unavailable", {}, None)
            return None
        self.kube.react("GET", CLAIM_PATH, answer)

    def test_a_job_that_needs_attention_is_asked_again_once_a_minute(self):
        job = self.start("volume-delete", {"namespace": "apps", "name": "data"})
        self.broken_claim()
        self.rounds(ERRORS.DEFAULT_LIMIT / MINUTE + 1, every=30)
        self.reads.clear()
        self.rounds(10)                                   # 200 rounds 3 s apart
        self.assertLessEqual(len(self.reads), 11)
        self.assertGreaterEqual(len(self.reads), 9)
        self.assertEqual("running", self.job(job)["status"])

    def test_a_job_that_needs_attention_carries_on_once_its_resolver_answers(self):
        job = self.start("volume-delete", {"namespace": "apps", "name": "data"})
        self.broken_claim()
        self.rounds(ERRORS.DEFAULT_LIMIT / MINUTE + 1, every=30)
        self.assertTrue(self.job(job)["message"].startswith("Needs attention"))
        self.broken = False
        self.rounds(2)
        item = self.job(job)
        self.assertEqual("Waiting for PVC deletion", item["message"])
        self.assertNotIn(ERRORS.KEY, item)

    def test_one_that_cannot_be_cancelled_says_why_instead(self):
        stub = mock.Mock(side_effect=ValueError("the host remains cordoned"))
        with mock.patch.dict(OPS.RESOLVERS, {"longhorn-v2-prepare": stub}):
            job = OPS.start("longhorn-v2-prepare", "Prepare V2", {"kind": "Node", "name": "node-a"}, "/", {})["id"]
            self.rounds(ERRORS.DEFAULT_LIMIT / MINUTE + 1, every=30)
        item = self.job(job)
        self.assertEqual("running", item["status"])
        self.assertIn("the host remains cordoned", item["message"])
        self.assertIn(OPS._block_longhorn_v2_prepare(item), item["message"])


class EveryKindTests(unittest.TestCase):
    """Each kind says whether giving up may let go of its guards (a ratchet)."""

    @classmethod
    def setUpClass(cls):
        import server  # noqa: F401  (fills the registries)
        import homestead_job_kinds as KINDS
        cls.kinds = set(KINDS.kinds())

    def test_every_kind_is_in_one_table(self):
        self.assertGreater(len(self.kinds), 50)
        both = set(ERRORS.FAILS) & set(ERRORS.KEEPS_GUARDS)
        self.assertEqual(set(), both)
        missing = self.kinds - set(ERRORS.FAILS) - set(ERRORS.KEEPS_GUARDS)
        self.assertEqual([], sorted(missing), "say in FAILS or KEEPS_GUARDS what giving up on it may do")

    def test_every_table_entry_is_a_kind(self):
        stale = (set(ERRORS.FAILS) | set(ERRORS.KEEPS_GUARDS) | set(ERRORS.LIMITS)) - self.kinds
        self.assertEqual([], sorted(stale))

    def test_every_entry_gives_a_reason(self):
        for kind, reason in {**ERRORS.FAILS, **ERRORS.KEEPS_GUARDS}.items():
            with self.subTest(kind=kind):
                self.assertGreater(len(reason), 10)

    def test_a_kind_no_table_names_keeps_its_guards(self):
        self.assertEqual("attention", ERRORS.outcome("a-new-kind"))
        self.assertEqual("failed", ERRORS.outcome("network-service"))


if __name__ == "__main__":
    unittest.main()
