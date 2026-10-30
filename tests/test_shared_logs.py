"""The agent audit and sign-in logs with two copies of Homestead on one data
volume (#986 §1): appends and trims from both lose no line, and two trims
never write into the same temporary file. Two real processes, as two
replicas would be, sharing a directory."""
import json
import multiprocessing
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_agent_audit as agent_audit
import homestead_signins as signins

LINES = 1000         # per process
KEEP = 50            # what a trim keeps
EVERY = 3            # a trim every few lines, so trims and appends overlap


def _audit_writer(folder, who, start):
    agent_audit.bind(folder)
    agent_audit.MAX_LINES, agent_audit.TRIM_EVERY = KEEP, EVERY
    agent_audit._written[0] = 0
    start.wait()
    for n in range(LINES):
        agent_audit.record({"id": who, "name": who}, "api", "read", detail=str(n))


def _signin_writer(folder, who, start):
    signins.bind(folder)
    signins.KEEP, signins.TRIM_EVERY = KEEP, EVERY
    signins._writes["n"] = 0
    start.wait()
    for n in range(LINES):
        signins.record("signin", who, detail=str(n))


def _run_two(target, folder):
    ctx = multiprocessing.get_context("fork")
    start = ctx.Barrier(2)
    procs = [ctx.Process(target=target, args=(folder, who, start)) for who in ("copy-a", "copy-b")]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(60)
        if proc.exitcode != 0:
            raise AssertionError(f"writer exited with {proc.exitcode}")


def _seqs(path, field):
    """Each writer's sequence numbers, in file order."""
    out = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        row = json.loads(line)              # a torn line fails here
        out.setdefault(row[field], []).append(int(row["detail"]))
    return out


@unittest.skipUnless(hasattr(os, "fork"), "two processes need fork")
class TwoCopiesTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def assertNothingLost(self, folder, seqs):
        # Appends and trims take turns under one lock, so the file is always
        # the newest lines in the order they were written: each writer's
        # lines in it are its own newest, with no gap. (A writer that
        # finished first can have been trimmed away whole.)
        self.assertTrue(seqs)
        self.assertGreaterEqual(sum(map(len, seqs.values())), KEEP)
        for who, numbers in seqs.items():
            self.assertEqual(list(range(numbers[0], LINES)), numbers, who)
        leftovers = [name for name in os.listdir(folder) if name.endswith(".tmp")]
        self.assertEqual([], leftovers)

    def rounds(self, writer, name, field):
        # A race: a few rounds, each in a fresh folder, so a lost line shows.
        for n in range(4):
            folder = os.path.join(self.dir.name, str(n))
            os.mkdir(folder)
            _run_two(writer, folder)
            self.assertNothingLost(folder, _seqs(os.path.join(folder, name), field))

    def test_agent_audit_appends_and_trims_lose_no_line(self):
        self.rounds(_audit_writer, agent_audit.FILE, "key")

    def test_sign_in_appends_and_trims_lose_no_line(self):
        self.rounds(_signin_writer, signins.FILE, "user")


class NewestFromTheEndTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        agent_audit.bind(self.dir.name)

    def test_the_newest_calls_are_read_from_the_end_of_a_large_log(self):
        row = json.dumps({"at": 1, "key": "k1", "action": "read", "args": {"x": "y" * 200}})
        Path(self.dir.name, agent_audit.FILE).write_text(
            "".join(row.replace('"at": 1', f'"at": {n}') + "\n" for n in range(20000)), encoding="utf-8")
        with mock.patch.object(agent_audit.json, "loads", wraps=json.loads) as parsed:
            rows = agent_audit.recent(limit=20)
        self.assertEqual(list(range(19999, 19979, -1)), [r["at"] for r in rows])
        self.assertLess(parsed.call_count, 2000)     # not all 20,000 lines of about 5 MB

    def test_one_keys_calls_and_a_cut_first_line(self):
        lines = [json.dumps({"at": n, "key": "k1" if n % 2 else "k2"}) for n in range(10)]
        Path(self.dir.name, agent_audit.FILE).write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.assertEqual([9, 7, 5, 3, 1], [r["at"] for r in agent_audit.recent("k1", 50)])
        self.assertEqual([8, 6, 4], [r["at"] for r in agent_audit.recent("k2", 3)])


if __name__ == "__main__":
    unittest.main()
