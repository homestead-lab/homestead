"""The Events list shows the newest events, not the first by name (#988).

The API server lists events in key order (namespace, then name), so a
limited read keeps whichever namespaces sort first. The list is read whole,
sorted by time, cut to EVENT_ROWS, and says how many there were."""
import sys
import unittest
from pathlib import Path

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

    def test_a_failed_read_is_an_empty_list(self):
        CLUSTER.bind(lambda path: (_ for _ in ()).throw(OSError("down")), (), lambda: [])
        self.assertEqual({"events": [], "total": 0}, CLUSTER.events())


if __name__ == "__main__":
    unittest.main()
