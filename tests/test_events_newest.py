"""The Events list shows the newest events, not the first by name (#988).

The API server lists events in key order (namespace, then name), so a
limited read keeps whichever namespaces sort first. The list is read in
pages, the newest EVENT_ROWS kept as it goes, and the answer says how many
there were; a read cut short by its ceiling or an expired page says limited."""
import heapq
import sys
import types
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import homestead_cluster as CLUSTER
import homestead_kube as K
from fakes.kube import Kube


def event(ns, name, stamp=None, **fields):
    return {"apiVersion": "v1", "kind": "Event",
            "metadata": {"name": name, "namespace": ns, **fields.pop("metadata", {})},
            "involvedObject": {"kind": "Pod", "name": name}, "reason": "Pulled", "message": "x",
            "type": "Normal", "count": 1, "lastTimestamp": stamp, **fields}


def at(minute):
    return f"2026-10-30T{minute // 60:02d}:{minute % 60:02d}:00Z"


class NewestEvents(unittest.TestCase):
    def setUp(self):
        bound = (CLUSTER.kget, CLUSTER.SYS_NS, CLUSTER.nodes_provider)
        self.addCleanup(lambda: setattr(CLUSTER, "kget", bound[0]) or setattr(CLUSTER, "SYS_NS", bound[1])
                        or setattr(CLUSTER, "nodes_provider", bound[2]))

    def read(self, *objects):
        with Kube(*objects):
            CLUSTER.bind(K.kget, (), lambda: [])
            return CLUSTER.events()

    def test_a_busy_cluster_shows_the_newest_events_from_every_namespace(self):
        # 200 events over five namespaces; the newest sit in the one that sorts last.
        names = ["apps", "backup", "lab", "media", "zz-late"]
        rows = [event(names[i % 5], f"ev-{i:03d}", at(i if names[i % 5] != "zz-late" else 600 + i))
                for i in range(200)]
        answer = self.read(*rows)
        shown = answer["events"]
        self.assertEqual(CLUSTER.EVENT_ROWS, len(shown))
        self.assertEqual(200, answer["total"], "it says the list was cut, and from how many")
        self.assertEqual(("zz-late", "ev-199"), (shown[0]["ns"], shown[0]["obj"]))
        self.assertEqual(sorted((r["time"] for r in shown), reverse=True), [r["time"] for r in shown])
        self.assertEqual(40, sum(r["ns"] == "zz-late" for r in shown), "every late namespace's event is kept")

    def test_an_event_without_last_timestamp_sorts_by_its_series_event_time_or_creation(self):
        answer = self.read(
            event("a", "old", at(1)),
            event("b", "series", None, eventTime="2026-10-30T00:02:00.000000Z",
                  series={"count": 3, "lastObservedTime": "2026-10-30T00:09:00.500000Z"}),
            event("c", "new-style", None, eventTime="2026-10-30T00:05:00.250000Z"),
            event("d", "bare", None, metadata={"creationTimestamp": at(7)}),
        )
        self.assertEqual(["series", "bare", "new-style", "old"], [r["obj"] for r in answer["events"]])
        self.assertEqual("2026-10-30T00:09:00.500000Z", answer["events"][0]["time"])
        self.assertEqual(at(7), answer["events"][1]["time"])

    def test_an_event_with_no_time_it_can_read_sorts_last(self):
        # The fake stamps creation as the API server does; this one comes from a reader that did not.
        rows = [event("a", "undated", None), event("b", "garbled", "not a time"), event("c", "dated", at(1))]
        CLUSTER.bind(lambda path: {"items": rows}, (), lambda: [])
        answer = CLUSTER.events()
        self.assertEqual(["dated", "undated", "garbled"], [r["obj"] for r in answer["events"]])
        self.assertEqual(["", ""], [r["time"] for r in answer["events"][1:]])

    def test_a_small_cluster_shows_all_of_its_events(self):
        answer = self.read(*[event("lab", f"ev-{i}", at(i)) for i in range(3)])
        self.assertEqual(3, answer["total"])
        self.assertEqual(["ev-2", "ev-1", "ev-0"], [r["obj"] for r in answer["events"]])

    def test_a_failed_read_says_so_instead_of_an_empty_list(self):
        CLUSTER.bind(lambda path: (_ for _ in ()).throw(OSError("down")), (), lambda: [])
        answer = CLUSTER.events()
        self.assertEqual(([], 0), (answer["events"], answer["total"]))
        self.assertTrue(answer["error"])
        self.assertNotIn("down", answer["error"], "the reader's own words stay in the log")


def busy(count=200):
    """count events over five namespaces; the newest sit in the one that sorts last."""
    names = ["apps", "backup", "lab", "media", "zz-late"]
    return [event(names[i % 5], f"ev-{i:03d}", at(i if names[i % 5] != "zz-late" else 600 + i))
            for i in range(count)]


class PagedRead(unittest.TestCase):
    """The list is read a page at a time, never whole (#988 review)."""

    def setUp(self):
        bound = (CLUSTER.kget, CLUSTER.SYS_NS, CLUSTER.nodes_provider)
        self.addCleanup(lambda: setattr(CLUSTER, "kget", bound[0]) or setattr(CLUSTER, "SYS_NS", bound[1])
                        or setattr(CLUSTER, "nodes_provider", bound[2]))
        self.paths = []

    def reader(self, path, **options):
        self.paths.append(path)
        return K.kget(path, **options)

    def read(self, kube, **limits):
        CLUSTER.bind(self.reader, (), lambda: [])
        if not limits:
            return CLUSTER.events()
        with mock.patch.multiple(CLUSTER, **limits):
            return CLUSTER.events()

    def queries(self):
        return [dict(urllib.parse.parse_qsl(path.partition("?")[2])) for path in self.paths]

    def test_every_page_is_asked_for_with_the_page_size_and_followed_to_the_end(self):
        with Kube(*busy()) as kube:
            answer = self.read(kube, EVENT_PAGE=50)
        self.assertEqual(4, len(self.paths), "200 events in pages of 50")
        self.assertTrue(all(q.get("limit") == "50" for q in self.queries()), self.paths)
        self.assertEqual(["", "50", "100", "150"], [q.get("continue", "") for q in self.queries()])
        self.assertEqual((200, False), (answer["total"], answer["limited"]))
        self.assertEqual(40, sum(r["ns"] == "zz-late" for r in answer["events"]))
        self.assertEqual(("zz-late", "ev-199"), (answer["events"][0]["ns"], answer["events"][0]["obj"]))

    def test_no_request_cuts_the_list_before_it_is_sorted(self):
        with Kube(*busy()) as kube:
            self.read(kube)
        self.assertTrue(self.paths)
        self.assertTrue(all(q.get("limit") == str(CLUSTER.EVENT_PAGE) for q in self.queries()), self.paths)
        self.assertNotIn("limit=160", " ".join(self.paths))

    def test_it_holds_no_more_than_the_rows_it_shows_while_it_reads(self):
        sizes = []
        heap = types.SimpleNamespace(
            heappush=lambda h, x: (heapq.heappush(h, x), sizes.append(len(h)))[0],
            heappushpop=lambda h, x: (heapq.heappushpop(h, x), sizes.append(len(h)))[0])
        with Kube(*busy()) as kube, mock.patch.object(CLUSTER, "heapq", heap):
            answer = self.read(kube, EVENT_PAGE=50, EVENT_ROWS=30)
        self.assertEqual(200, len(sizes), "every event went through the keeper")
        self.assertEqual(30, max(sizes))
        self.assertEqual(30, len(answer["events"]))
        self.assertEqual("ev-199", answer["events"][0]["obj"])

    def test_a_read_past_its_ceiling_stops_and_says_limited(self):
        with Kube(*busy()) as kube:
            answer = self.read(kube, EVENT_PAGE=50, EVENT_CEILING=100)
        self.assertEqual(2, len(self.paths))
        self.assertEqual((100, True), (answer["total"], answer["limited"]))
        self.assertNotIn("error", answer)

    def test_a_read_past_its_time_stops_and_says_limited(self):
        clock = iter([0.0, 1.0, CLUSTER.EVENT_SECONDS + 1.0, 99.0, 99.0])
        with Kube(*busy()) as kube:
            answer = self.read(kube, EVENT_PAGE=50, _clock=lambda: next(clock))
        self.assertEqual(2, len(self.paths))
        self.assertEqual((100, True), (answer["total"], answer["limited"]))

    def test_an_expired_continue_token_keeps_what_was_read_and_says_limited(self):
        with Kube(*busy()) as kube:
            def expired(method, path, body):
                if "continue=" in path:
                    raise kube._error(410, "Expired", "the provided continue parameter is too old", path)
            kube.react("GET", "/api/v1/events", expired)
            answer = self.read(kube, EVENT_PAGE=50)
        self.assertEqual(2, len(self.paths))
        self.assertEqual((50, True), (answer["total"], answer["limited"]))
        self.assertEqual(50, len(answer["events"]))
        self.assertNotIn("error", answer)

    def test_a_page_that_fails_after_the_first_keeps_what_was_read(self):
        with Kube(*busy()) as kube:
            def broken(method, path, body):
                if "continue=" in path:
                    raise kube._error(500, "InternalError", "etcd went away", path)
            kube.react("GET", "/api/v1/events", broken)
            answer = self.read(kube, EVENT_PAGE=50)
        self.assertEqual((50, True), (answer["total"], answer["limited"]))

    def test_events_at_the_same_time_keep_the_order_kubernetes_listed_them_in(self):
        same = [event(ns, f"ev-{ns}", at(5)) for ns in ("b", "a", "d", "c")] + [event("z", "older", at(1))]
        with Kube(*same) as kube:
            answer = self.read(kube, EVENT_PAGE=2, EVENT_ROWS=3)
        self.assertEqual(["ev-a", "ev-b", "ev-c"], [r["obj"] for r in answer["events"]])


if __name__ == "__main__":
    unittest.main()
