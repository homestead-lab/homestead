"""A failed read never clears an alert: every alert source, on a cluster that
cannot be read.

An alert source that answers [] when its read fails tells ALERTS.observe that
everything it watches is fine, so after the hold (ALERTS.HOLD) the bell
resolves its open alerts and pushes "... cleared" while nothing was looked at
(#1002 fixed two of these). A source that could not look says so: it raises
(the loop takes None) or answers {"unknown_prefix"} / {"unknown_key"} facts,
and its alerts stay open.

The sources are the names the alerts loop's own _alert_sources() returns, so a
source added there is covered the day it is added. For each failure (every
Kubernetes read, or only Longhorn's or KubeVirt's, both Kubernetes APIs here):
1. the real _alerts_loop runs on the healthy fake cluster;
2. one look with the reads failing finds the alert keys each source says it
   cannot see (its unknown_* facts), or else takes "<source>:..." as its key;
3. an alert is opened and announced under each of those keys;
4. the reads fail and _alerts_loop runs for four holds on a fake clock.
No such alert may resolve, and no push may carry a resolved entry, for a
source that tried to read and failed. (A source whose own read worked has
answered for real, and may clear.)

ALLOWED names what clears anyway, by (source, failure) ("*": every failure),
each with its reason: a source whose alerts do not come from the cluster, a
read that fails but that its alerts do not use, or a source that still clears
on a failed read, with the issue or pull request that fixes it. It is a
ratchet: only remove entries. The test fails when an entry no longer applies,
so it comes off then."""
import contextlib
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_alerts as ALERTS
import homestead_kube as K
from test_page_snapshot import Cluster, clean_cache, clean_snapshot, setUpModule  # noqa: F401 (restart record off)

LH_PATH = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system"

ALLOWED = {
    # Not from the cluster: the data dir, memory or another loop's last
    # report. Empty here, so they answer [] and their test alerts clear,
    # correctly. (Unreadable data-dir files are another class: sweep
    # "unknown-as-fine", row 10.)
    ("agents", "*"): "AI agents' requests, from the data dir (SHARED.read_json, #658)",
    ("audit", "*"): "the audit log's size, from the data dir",
    ("forecast", "*"): "the forecast's history samples, from the data dir",
    ("hostos", "*"): "host OS reports in the data dir; the node read only drops a restart the host has "
                     "had since, and a failed one keeps the restart fact",
    ("housekeeping", "*"): "the data volume's free space (statvfs)",
    ("jobs", "*"): "Homestead's own jobs, in memory (OPS.snapshot)",
    ("outage", "*"): "the outage loop's state file in the data dir",
    ("rootguard", "*"): "the root-disk guard's state file in the data dir",
    ("schedules", "*"): "scheduled stop/start results, from the data dir",
    ("updates", "*"): "the last update report, in memory, which here lists no update. Its scan reads the "
                      "cluster's Deployments, but a failed read raises and the last report is kept, so "
                      "a failed read clears nothing",
    ("uptime", "*"): "the uptime monitors' state, in memory",
    # A read that fails but that the source's alerts do not use.
    ("platform", "Longhorn"): "the cluster inventory reads Longhorn, but only its Harvester version is "
                              "used, and the upgrades are read from Harvester, which still works",
    ("platform", "KubeVirt"): "as for Longhorn: the inventory's VM reads are not used",
    # Still clears on a failed read: what fixes it.
    ("platform", "every read"): "#TBD-upgrades-unread: homestead_upgrades.upgrades() and offered() answer [] "
                                "on any failed read, so upgrade_facts drops the platform:* events and they "
                                "fire again once the read works",
}

# Sources that answer None on the healthy fake cluster too, and why. Such a
# source holds its test alert whatever its code does, so it is not tested
# here. A ratchet: only remove entries.
NO_HEALTHY_ANSWER = {
    # Log alerts are read on their own thread (LOGALERTS.run, started by the
    # loop and stopped here: it reads pod logs on a real clock); the loop only
    # takes its last round, None until there is a recent one.
    "logs": "its last round, from its own thread, which this test does not run",
}

FAILURES = {
    "every read": lambda path: True,
    "Longhorn": lambda path: path.startswith("/apis/longhorn.io/"),
    "KubeVirt": lambda path: path.startswith("/apis/kubevirt.io/"),
}


class _Stop(Exception):
    pass


class FakeClock:
    """The alert loop's clock: sleep() moves it on, and stops the loop after
    a set number of rounds."""

    def __init__(self, start=1_000_000.0):
        self.now = start
        self.rounds = 0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        self.rounds -= 1
        if self.rounds <= 0:
            raise _Stop()

    def __getattr__(self, name):
        return getattr(time, name)


class UnreadNeverClearsTests(unittest.TestCase):
    def setUp(self):
        clean_cache(self)
        clean_snapshot(self)
        self.cluster = Cluster(self)
        self.OV = self.cluster.server.OVERVIEW_ROUTES
        # Other test files bind modules to fakes of their own and leave them
        # so: every module reads through the shared fake here.
        for name, module in list(sys.modules.items()):
            if not name.startswith("homestead_") or module is K:
                continue
            for attr, value in (("kget", K.kget), ("ksend", K.ksend)):
                if callable(getattr(module, attr, None)) and getattr(module, attr) is not value:
                    self.enterContext(mock.patch.object(module, attr, value))
        # Modules that keep their reader in a _bound table.
        for module in (self.cluster.server.HOUSEKEEPING, self.cluster.server.OUTAGE):
            self.enterContext(mock.patch.dict(module._bound, {"kget": K.kget}))
        # Nothing reaches a real API server, whatever goes round transport.
        self.enterContext(mock.patch.object(K, "_request", side_effect=OSError("no API server in the tests")))
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        for name, module in list(sys.modules.items()):
            if name.startswith("homestead_") and isinstance(getattr(module, "DATA_DIR", None), str):
                self.enterContext(mock.patch.object(module, "DATA_DIR", directory.name))
        self.clock = FakeClock()
        self.enterContext(mock.patch.object(ALERTS, "time", self.clock))
        self.enterContext(mock.patch.object(self.OV, "time", self.clock))
        self.enterContext(mock.patch.object(self.OV.LEADER, "is_leader", lambda: True))
        self.enterContext(mock.patch.object(self.OV.LOGALERTS, "start", lambda: None))
        self.pushed = []
        self.enterContext(mock.patch.object(self.OV, "push_alerts", lambda fresh: self.pushed.extend(fresh or [])))
        self.failing, self.failed = None, {}
        kube, inner = self.cluster.kube, K.transport

        def transport(method, path, *args, **kwargs):
            if method == "GET" and self.failing and self.failing(path.split("?", 1)[0]):
                self.failed.setdefault(self._source_reading(), set()).add(path.split("?", 1)[0])
                raise kube._error(500, "InternalError", "injected: the API server cannot answer", path)
            return inner(method, path, *args, **kwargs)
        self.enterContext(mock.patch.object(K, "transport", transport))

    def _source_reading(self):
        """The alert source whose take() this read is under.

        Limits: it looks at this thread's stack only, so a read on another
        thread counts for no source; a value another source already put in
        the cache under the same key (cached("network", ...)) is read once, by
        whichever source asks first; and the cache runs on real time, so
        within one loop run a cached answer is served again and only its
        first read is seen. break_reads() empties the caches for that."""
        frame = sys._getframe(2)
        while frame and not (frame.f_code.co_name == "take" and frame.f_globals is vars(self.OV)):
            frame = frame.f_back
        return frame.f_locals["name"] if frame else None

    def break_reads(self, which):
        self.failing = which
        clean_cache(self)
        clean_snapshot(self)

    def run_loop(self, rounds):
        self.clock.rounds = rounds
        with mock.patch("builtins.print"), contextlib.suppress(_Stop):
            self.OV._alerts_loop()

    def seed(self, source, key):
        return {"key": key, "category": "degraded", "severity": "degraded", "title": f"{source} test alert",
                "resolved": f"{source} test alert cleared", "body": "", "href": "/"}

    def add_problems(self):
        """Two things wrong that alerts are raised for: a faulted volume and
        a Longhorn disk that is not ready, so health and disks open alerts of
        their own (the others get a test alert each)."""
        kube = self.cluster.kube
        volume = kube.object(f"{LH_PATH}/volumes/pvc-1")
        volume["status"]["robustness"] = "faulted"
        kube.add(volume)
        node = kube.object(f"{LH_PATH}/nodes/node-a")
        node["status"]["diskStatus"]["d1"]["conditions"] = [{"type": "Ready", "status": "False", "message": "gone"}]
        kube.add(node)

    def check_healthy_answers(self):
        """Every source answers on the healthy cluster, or is named in
        NO_HEALTHY_ANSWER: one that answers None holds its alerts anyway."""
        with mock.patch("builtins.print"):
            answers = self.OV._alert_sources()
        self.assertEqual(sorted(NO_HEALTHY_ANSWER), sorted(s for s, facts in answers.items() if facts is None),
                         "sources that answer None on the healthy cluster (take one that answers now off "
                         "NO_HEALTHY_ANSWER)")

    def cleared_while_unread(self, which):
        """{source: open alerts that cleared}, and the sources that tried a read."""
        self.add_problems()
        self.run_loop(ALERTS.HOLD // 20 + 2)
        self.check_healthy_answers()
        self.break_reads(which)
        with mock.patch("builtins.print"):
            answers = self.OV._alert_sources()
        self.break_reads(None)
        seeds = {}
        for source, facts in answers.items():
            unknown = [f.get("unknown_key") or f["unknown_prefix"] + "test"
                       for f in facts or [] if "unknown_key" in f or "unknown_prefix" in f]
            seeds[source] = [self.seed(source, key) for key in unknown or [f"{source}:test"]]
        for _ in range(2):
            with mock.patch("builtins.print"):
                now = self.OV._alert_sources()
            # Without their unknown_* facts (SMART not read yet, say): an
            # alert is not raised while its source says it cannot see it.
            known = {s: [f for f in now[s] or [] if "key" in f] for s in now}
            ALERTS.observe({s: known[s] + seeds[s] for s in seeds}, now=self.clock.now)
            self.clock.now += ALERTS.HOLD
        opened = {k: ALERTS._source(k, row) for k, row in ALERTS._load()["active"].items() if row["announced"] > 0}
        self.assertEqual([], sorted(f["key"] for fs in seeds.values() for f in fs if f["key"] not in opened))
        self.assertTrue(any(k.startswith("health:Volume:") for k in opened), sorted(opened))
        self.assertTrue(any(k.startswith("disks:") for k in opened), sorted(opened))
        self.pushed.clear()
        self.failed.clear()
        self.break_reads(which)
        self.run_loop(4 * ALERTS.HOLD // 20 + 1)
        active = ALERTS._load()["active"]
        resolved = {e["key"] for e in self.pushed if e["phase"] == "resolved"}
        cleared = {}
        for key, source in opened.items():
            if key not in active or key in resolved:
                cleared.setdefault(source, []).append(key)
        return cleared, set(self.failed)

    def test_the_loop_has_its_sources(self):
        sources = set(self.OV._alert_sources())
        self.assertGreater(len(sources), 20)
        for name in (*(n for n, _ in ALLOWED), *NO_HEALTHY_ANSWER):
            self.assertIn(name, sources, f"{name} is no longer an alert source: take it off the list")

    def check(self, label):
        cleared, tried = self.cleared_while_unread(FAILURES[label])
        # A source that read and got its answer may clear. With every read
        # failing, all that read the cluster have tried, so all count.
        counted = {s: ks for s, ks in cleared.items() if s in tried or label == "every read"}
        allowed = {s for s, failing in ALLOWED if failing in ("*", label)}
        self.assertEqual({}, {s: ks for s, ks in counted.items() if s not in allowed},
                         f"with {label} failing, these alerts cleared although their source could not read")
        stale = sorted(s for s, failing in ALLOWED if failing == label or (failing == "*" and label == "every read")
                       if s not in counted)
        self.assertEqual([], stale, f"with {label} failing these hold their alerts now: take them off ALLOWED")

    def test_no_alert_clears_while_no_kubernetes_read_works(self):
        self.check("every read")

    def test_no_alert_clears_while_longhorn_cannot_be_read(self):
        self.check("Longhorn")

    def test_no_alert_clears_while_kubevirt_cannot_be_read(self):
        self.check("KubeVirt")
