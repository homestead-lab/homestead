import sys
import threading
import time
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_logsearch as LS


def app(name, pods, ns="lab", platform=""):
    return {"ns": ns, "name": name, "platform": platform, "pods": pods}


def pod(name, *containers, phase="Running"):
    return {"name": name, "phase": phase,
            "containers": [{"name": c, "kind": k} for c, k in (containers or ((name.split("-")[0], "app"),))]}


WORKLOADS = [
    app("frigate", [pod("frigate-1")]),
    app("paperless", [pod("paperless-1", ("paperless", "app"), ("redis", "app"), ("migrate", "init"))]),
    app("old", [pod("old-1", phase="Succeeded")]),
    app("homestead", [pod("homestead-1")], ns="homestead", platform="Homestead"),
]
LOGS = {
    ("frigate-1", "frigate"): "2026-10-07T09:00:01.5Z camera ok\n2026-10-07T09:05:00Z ERROR no frames\n",
    ("paperless-1", "paperless"): "2026-10-07T09:03:00Z consumed scan.pdf\n2026-10-07T09:06:00Z Error: OCR timeout\n",
    ("paperless-1", "redis"): "2026-10-07T09:01:00Z Ready to accept connections\n",
    ("homestead-1", "homestead"): "2026-10-07T09:07:00Z error in Homestead\n",
}


def reader(calls=None):
    def read(ns, pod_name, container, since, limit):
        if calls is not None:
            calls.append((ns, pod_name, container, since, limit))
        return LOGS[(pod_name, container)]
    return read


class SearchTests(unittest.TestCase):
    def test_matches_from_every_app_newest_first(self):
        calls = []
        d = LS.search(WORKLOADS, "error", reader(calls), window="6h")
        self.assertEqual([("paperless", "Error: OCR timeout"), ("frigate", "ERROR no frames")],
                         [(m["app"], m["line"]) for m in d["matches"]], "case does not matter by default")
        self.assertEqual("2026-10-07T09:06:00Z", d["matches"][0]["at"])
        self.assertEqual(3, d["asked"], "every app container of each running pod; not init containers, finished pods or the platform")
        self.assertTrue(all(c[3] == 21600 and c[4] == LS.MAX_BYTES for c in calls))

    def test_chosen_apps_only_and_platform_when_chosen(self):
        d = LS.search(WORKLOADS, "error", reader(), apps=["homestead/homestead"])
        self.assertEqual(["homestead"], [m["app"] for m in d["matches"]])
        self.assertEqual(1, d["asked"])

    def test_match_case_and_regular_expressions(self):
        self.assertEqual(["frigate"], [m["app"] for m in LS.search(WORKLOADS, "ERROR", reader(), case=True)["matches"]])
        d = LS.search(WORKLOADS, r"OCR|frames", reader(), regex=True)
        self.assertEqual(2, d["total"])

    def test_bad_and_slow_queries_are_refused(self):
        for query, regex in (("", False), ("   ", False), ("x" * 201, False), ("(a", True), ("(a+)+b", True), (r"(\w*)*", True), ("(a|aa)+", True)):
            with self.assertRaises(ValueError, msg=query):
                LS.search(WORKLOADS, query, reader(), regex=regex)
        with self.assertRaises(ValueError):
            LS.search(WORKLOADS, "x", reader(), window="7d")
        self.assertEqual(0, LS.search(WORKLOADS, "(a+)+b", reader())["total"], "as plain text it is just characters")

    def test_what_could_not_be_searched_is_said(self):
        def read(ns, pod_name, container, since, limit):
            if container == "redis":
                raise OSError("connection refused")
            if container == "frigate":
                return "x" * limit
            return LOGS[(pod_name, container)]
        d = LS.search(WORKLOADS, "error", read)
        self.assertEqual(["paperless (paperless-1): connection refused"], d["errors"])
        self.assertEqual(["frigate"], d["capped"])

    def test_caps_on_pods_and_matches(self):
        many = [app(f"a{i}", [pod(f"a{i}-1", (f"a{i}", "app"))]) for i in range(LS.MAX_PODS + 5)]
        lines = "".join(f"2026-10-07T09:{i // 60:02d}:{i % 60:02d}Z hit {i}\n" for i in range(20))
        lock, seen = threading.Lock(), []

        def read(ns, pod_name, container, since, limit):
            with lock:
                seen.append(pod_name)
            return lines
        d = LS.search(many, "hit", read)
        self.assertEqual((LS.MAX_PODS, 5), (d["asked"], d["skipped_pods"]))
        self.assertEqual(LS.MAX_PODS, len(seen))
        self.assertEqual(LS.MAX_MATCHES, len(d["matches"]))
        self.assertTrue(d["truncated"])
        self.assertEqual(LS.MAX_PODS * 20, d["total"])

    def test_long_lines_are_cut_and_unstamped_lines_kept(self):
        d = LS.search([app("x", [pod("x-1")])], "needle", lambda *a: "needle " + "y" * 5000 + "\n")
        self.assertEqual(("", LS.MAX_LINE), (d["matches"][0]["at"], len(d["matches"][0]["line"])))


SLOW = r"^(x?){30}x{30}$|ERROR"     # passes the shape guard, backtracks for minutes on 30 x's


class ExpressionTimeTests(unittest.TestCase):
    """#929: an expression the shape guard lets through still cannot hold the
    request thread: it is matched in a child process with a time limit."""

    def test_a_runaway_expression_is_cut_and_the_search_says_so(self):
        newest = "".join(f"2026-10-07T09:05:{i % 60:02d}Z ERROR newest {i}\n" for i in range(300))
        text = "2026-10-07T09:00:00Z " + "x" * 30 + "\n" + newest
        with mock.patch.object(LS, "REGEX_SECONDS", 1):
            began = time.monotonic()
            d = LS.search(WORKLOADS, SLOW, lambda *a: text, apps=["lab/frigate"], regex=True)
            took = time.monotonic() - began
        self.assertLess(took, 5, "the search ends soon after its time limit, not minutes later")
        self.assertEqual(["frigate"], d["cut"])
        self.assertEqual(1, d["cut_seconds"])
        lines = [m["line"] for m in d["matches"]]
        frigate = sorted(int(line.rsplit(" ", 1)[1]) for line in lines if line.startswith("ERROR newest"))
        self.assertGreaterEqual(len(frigate), 256,
                                "a container's newest lines are matched first, so what was matched before the cut is kept")
        self.assertEqual(list(range(300 - len(frigate), 300)), frigate, "the newest ones, without a gap")

    def test_once_the_time_is_spent_the_rest_is_not_matched(self):
        many = [app(f"a{i}", [pod(f"a{i}-1", (f"a{i}", "app"))]) for i in range(4)]
        with mock.patch.object(LS, "REGEX_SECONDS", 1):
            began = time.monotonic()
            d = LS.search(many, SLOW, lambda *a: "x" * 30 + "\n", regex=True)
            took = time.monotonic() - began
        self.assertLess(took, 4, "the limit is for the whole search, not per container")
        self.assertEqual([f"a{i}" for i in range(4)], d["cut"])

    def test_a_quick_expression_is_not_cut(self):
        d = LS.search(WORKLOADS, r"OCR|frames", reader(), regex=True)
        self.assertEqual((2, []), (d["total"], d["cut"]))
        self.assertEqual([], LS.search(WORKLOADS, "error", reader())["cut"], "plain text is never cut")

    def test_matching_in_order_and_what_was_checked(self):
        self.assertEqual(([0, 2], 3), LS.run_regex(["a 500 x", "a 200 x", "b 503"], r" 5\d\d", False, 5))
        self.assertEqual(([], 0), LS.run_regex([], "a", False, 5), "nothing to match spawns nothing")
        hits, checked = LS.run_regex(["ERROR"] * 300 + ["x" * 30], SLOW, False, 1)
        self.assertLess(checked, 301)
        self.assertEqual(list(range(checked)), hits, "every line before the cut that matched, nothing after it")
        self.assertEqual(([], 0), LS.run_regex(["ERROR"], "ERROR", False, 0), "no time left: not started")

    def test_a_row_cut_by_the_kill_is_not_read(self):
        # "c 1" cut from "c 12": only whole rows (ending in a newline) count.
        self.assertEqual(([0], 3), LS._child_rows("m 0\nc 3\nc 1"))
        self.assertEqual(([0, 2], 3), LS._child_rows("m 0\nm 2\nc 3\n"))

    def test_a_child_that_cannot_start_is_named_not_a_failed_search(self):
        with mock.patch.object(LS.subprocess, "Popen", side_effect=OSError("out of processes")):
            d = LS.search(WORKLOADS, "OCR|frames", reader(), regex=True)
        self.assertEqual(0, d["total"])
        self.assertEqual(3, len(d["errors"]))
        self.assertIn("the expression could not be run", d["errors"][0])

    def test_an_expression_search_reads_few_containers_at_once(self):
        # Matching is one child at a time; more readers would only hold
        # their logs in memory while they wait (256Mi pod).
        many = [app(f"a{i}", [pod(f"a{i}-1", (f"a{i}", "app"))]) for i in range(12)]
        lock, now, most = threading.Lock(), [0], [0]

        def read(*args):
            with lock:
                now[0] += 1
                most[0] = max(most[0], now[0])
            time.sleep(0.05)
            with lock:
                now[0] -= 1
            return "2026-10-07T09:00:00Z hit\n"
        LS.search(many, "h.t", read, regex=True)
        self.assertLessEqual(most[0], LS.REGEX_READERS)
        most[0] = 0
        LS.search(many, "hit", read)
        self.assertGreater(most[0], LS.REGEX_READERS, "plain text reads as many at once as before")


class OneAtATimeTests(unittest.TestCase):
    def test_a_second_search_waits_for_the_first(self):
        started, release = threading.Event(), threading.Event()

        def slow(*args):
            started.set()
            release.wait(5)
            return ""
        first = threading.Thread(target=LS.search, args=([app("x", [pod("x-1")])], "a", slow))
        first.start()
        started.wait(5)
        with self.assertRaises(ValueError):
            LS.search(WORKLOADS, "error", reader())
        release.set()
        first.join(5)
        self.assertEqual(2, LS.search(WORKLOADS, "error", reader())["total"], "and runs once it has finished")

    def test_exclusive_is_the_same_limit_other_readers_share(self):
        # A log alert's preview reads an hour of logs too: it takes this
        # limit, so it cannot be used to get round it.
        with LS.exclusive():
            with self.assertRaisesRegex(ValueError, "another log search is running"):
                LS.search(WORKLOADS, "error", reader())
            with self.assertRaisesRegex(ValueError, "another log search is running"):
                with LS.exclusive():
                    pass
        with LS.exclusive():
            pass
        self.assertEqual(2, LS.search(WORKLOADS, "error", reader())["total"])


class RouteTests(unittest.TestCase):
    def test_route_is_open_to_viewers(self):
        import homestead_route_policy as POLICY
        self.assertEqual("viewer", POLICY.role("/api/logs/search", "GET"))


if __name__ == "__main__":
    unittest.main()
