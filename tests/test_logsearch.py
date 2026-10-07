import sys
import threading
import unittest
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


class RouteTests(unittest.TestCase):
    def test_route_is_open_to_viewers(self):
        import homestead_route_policy as POLICY
        self.assertEqual("viewer", POLICY.POLICY[("GET", "/api/logs/search")])


if __name__ == "__main__":
    unittest.main()
