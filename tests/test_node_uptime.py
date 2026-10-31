import sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_history as H

DAY = 86400


def overview(ready, boot="boot-1"):
    return {"cpu_pct": 1, "mem_pct": 1, "nodes_ready": int(ready), "nodes_total": 1,
            "nodes": [{"name": "node1", "status": "Ready" if ready else "NotReady", "cpu_pct": 1, "mem_pct": 1,
                       "boot_id": boot}]}


class UptimeCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = H.DATA_DIR
        H.bind(self.tmp.name)
        self.start = 1_700_000_000 - 1_700_000_000 % DAY

    def tearDown(self):
        H.DATA_DIR = self.saved
        self.tmp.cleanup()

    def run_for(self, minutes, down_between=(), boot_changes_at=None):
        for i in range(0, minutes, 5):
            t = self.start + i * 60
            boot = "boot-2" if boot_changes_at is not None and i >= boot_changes_at else "boot-1"
            H.record(overview(not (down_between and down_between[0] <= i < down_between[1]), boot), now=t)
        return self.start + minutes * 60


class NodeUptimeTests(UptimeCase):
    def test_an_outage_is_found_to_the_five_minutes(self):
        now = self.run_for(180, down_between=(60, 90))
        node = H.uptime(now)["nodes"]["node1"]
        outage = node["outages"][-1]
        self.assertEqual((self.start + 3600, 30 * 60, True), (outage["start"], outage["down_s"], outage["exact"]))
        self.assertFalse(outage["ongoing"])
        self.assertAlmostEqual(83.333, node["windows"]["24h"], places=2)

    def test_a_node_down_now_is_an_ongoing_outage(self):
        now = self.run_for(60, down_between=(40, 60))
        self.assertTrue(H.uptime(now)["nodes"]["node1"]["outages"][-1]["ongoing"])

    def test_a_new_boot_id_is_a_reboot(self):
        now = self.run_for(60, boot_changes_at=30)
        self.assertEqual([{"t": self.start + 30 * 60, "planned": False}], H.uptime(now)["nodes"]["node1"]["reboots"])

    def test_always_up_is_a_hundred(self):
        now = self.run_for(60)
        node = H.uptime(now)["nodes"]["node1"]
        self.assertEqual(100.0, node["windows"]["24h"])
        self.assertEqual([], node["outages"])
        self.assertEqual(90, len(node["days"]))


class MaintenanceTests(UptimeCase):
    """Downtime Homestead caused, recorded apart from the samples (#535)."""

    def at(self, minute):
        return self.start + minute * 60

    def test_open_close_and_idempotence(self):
        first = H.maintenance_open("node1", "OS update", "op-1", now=self.at(60))
        self.assertEqual(first, H.maintenance_open("node1", "OS update", "op-1", now=self.at(61)))
        self.assertEqual(1, len(H.maintenance_events("node1")))
        closed = H.maintenance_close("op-1", now=self.at(80), outcome="returned")
        self.assertEqual((self.at(80), "returned"), (closed["end"], closed["outcome"]))
        self.assertEqual(closed, H.maintenance_close("op-1", now=self.at(90), outcome="limit"))
        self.assertIsNone(H.maintenance_close("nope"))

    def test_excluding_maintenance_and_split_outage(self):
        H.maintenance_open("node1", "OS update", "op-1", now=self.at(60))
        H.maintenance_close("op-1", now=self.at(80), outcome="limit")
        now = self.run_for(180, down_between=(60, 90))
        node = H.uptime(now)["nodes"]["node1"]
        self.assertAlmostEqual(83.333, node["windows"]["24h"], places=2)
        self.assertAlmostEqual(93.75, node["windows_excluding_maintenance"]["24h"], places=2)
        self.assertEqual(1200, node["maintenance_s"]["24h"])
        first, second = node["outages"][-2:]
        self.assertEqual((self.at(60), 1200, "OS update"), (first["start"], first["down_s"], first["maintenance"]))
        self.assertEqual((self.at(80), 600), (second["start"], second["down_s"]))
        self.assertNotIn("maintenance", second)
        self.assertTrue(node["days"][-1]["maintenance"])
        self.assertFalse(node["days"][-2]["maintenance"])

    def test_planned_and_unexpected_reboots(self):
        H.maintenance_open("node1", "host reboot", "op-1", now=self.at(60))
        H.maintenance_close("op-1", now=self.at(65), outcome="returned")
        for minute in range(0, 180, 5):
            boot = "b1" if minute < 70 else "b2" if minute < 120 else "b3"
            H.record(overview(True, boot), now=self.at(minute))
        reboots = H.uptime(self.at(180))["nodes"]["node1"]["reboots"]
        self.assertEqual([{"t": self.at(70), "planned": True}, {"t": self.at(120), "planned": False}], reboots)

    def test_a_typical_os_update_leaves_no_outage(self):
        """A six-minute OS update whose host was seen down in two samples is
        maintenance through and through, in the samples and in the hourly rows
        (#535 review I1)."""
        H.maintenance_open("node1", "OS update", "op-1", now=self.at(61))
        H.maintenance_close("op-1", now=self.at(67), outcome="returned")
        now = self.run_for(180, down_between=(60, 70))
        node = H.uptime(now)["nodes"]["node1"]
        self.assertEqual(100.0, node["windows_excluding_maintenance"]["24h"])
        self.assertEqual([], [o for o in node["outages"] if not o.get("maintenance")])
        later = self.start + 3 * DAY
        H.record(overview(True), now=later)
        node = H.uptime(later)["nodes"]["node1"]
        self.assertEqual(100.0, node["windows_excluding_maintenance"]["90d"], "the hourly rows too")
        self.assertEqual([], [o for o in node["outages"] if not o.get("maintenance")])

    def test_a_day_with_maintenance_still_shows_its_outage(self):
        H.maintenance_open("node1", "OS update", "op-1", now=self.at(10))
        H.maintenance_close("op-1", now=self.at(16), outcome="returned")
        now = self.run_for(180, down_between=(60, 120))
        today = H.uptime(now)["nodes"]["node1"]["days"][-1]
        self.assertTrue(today["maintenance"])
        self.assertAlmostEqual(100 * 120 / 180, today["up_excluding_maintenance"], delta=4)

    def test_overlapping_events_count_once(self):
        H.maintenance_open("node1", "OS update", "op-1", now=self.at(60))
        H.maintenance_close("op-1", now=self.at(80))
        H.maintenance_open("node1", "Harvester maintenance", "harvester:node1:x", now=self.at(70))
        H.maintenance_close("harvester:node1:x", now=self.at(90))
        node = H.uptime(self.run_for(180, down_between=(60, 90)))["nodes"]["node1"]
        self.assertEqual(1800, node["maintenance_s"]["24h"])
        self.assertEqual(100.0, node["windows_excluding_maintenance"]["24h"])

    def test_a_forgotten_power_event_closes_at_the_ceiling(self):
        H.maintenance_open("node1", "host reboot", "op-1", now=self.start)
        H.maintenance_open("node2", "cluster shutdown", "cs:run:node2", now=self.start)
        later = self.start + H.MAX_OPEN + 1
        self.assertIsNone(H.maintenance_open_for("node1", now=later))
        H.record(overview(True), now=later)
        stored = H.maintenance_events("node1")[0]
        self.assertEqual((self.start + H.MAX_OPEN, "limit"), (stored["end"], stored["outcome"]))
        self.assertIsNotNone(H.maintenance_open_for("node2", now=later))

    def test_record_keeps_events_and_old_files_read_as_none(self):
        import json, os
        with open(os.path.join(self.tmp.name, H.FILE), "w") as handle:
            json.dump({"fine": [], "coarse": [], "boots": {}, "events": []}, handle)
        self.assertEqual([], H.maintenance_events("node1"))
        H.maintenance_open("node1", "OS update", "op-1", now=self.at(10))
        H.record(overview(True), now=self.at(15))
        self.assertEqual(1, len(H.maintenance_events("node1")))
        old = self.start - H.COARSE_KEEP - 10
        H.maintenance_open("node1", "OS update", "op-old", now=old - 60)
        H.maintenance_close("op-old", now=old)
        H.maintenance_open("node1", "cluster shutdown", "cs:old:node1", now=old - 60)
        H.record(overview(True), now=self.start)
        self.assertEqual({"op-1", "cs:old:node1"}, {e["job"] for e in H.maintenance_events("node1")})

    def test_end_is_never_before_start(self):
        H.maintenance_open("node1", "host reboot", "op-1", now=self.at(10))
        self.assertEqual(self.at(10), H.maintenance_close("op-1", now=self.at(0))["end"])

    def test_the_api_lists_events(self):
        H.maintenance_open("node1", "OS update", "op-1", now=self.at(60))
        H.maintenance_close("op-1", now=self.at(66), outcome="returned")
        node = H.uptime(self.run_for(90))["nodes"]["node1"]
        self.assertEqual([{"start": self.at(60), "end": self.at(66), "reason": "OS update", "job": "op-1", "ongoing": False}],
                         node["maintenance"])


    def test_a_malformed_entry_is_skipped_not_fatal(self):
        import json, os
        H.record(overview(True), now=self.at(0))
        path = os.path.join(self.tmp.name, H.FILE)
        data = json.load(open(path))
        good = {"id": "op-1", "node": "node1", "start": self.at(5), "end": self.at(10), "reason": "OS update", "job": "op-1", "outcome": "returned"}
        data["maintenance"] = [{"bad": 1}, "x", {"job": "op-2", "node": "node1", "start": "soon"}, good]
        json.dump(data, open(path, "w"))
        self.assertEqual([good], H.maintenance_events("node1"))
        H.record(overview(True), now=self.at(15))
        self.assertIsNone(H.maintenance_open_for("node1"))
        self.assertEqual(1, len(H.uptime(self.at(20))["nodes"]["node1"]["maintenance"]))

    def test_overlapping_events_name_both_reasons(self):
        H.maintenance_open("node1", "OS update", "op-1", now=self.at(60))
        H.maintenance_close("op-1", now=self.at(80))
        H.maintenance_open("node1", "Harvester maintenance", "harvester:node1:x", now=self.at(70))
        H.maintenance_close("harvester:node1:x", now=self.at(90))
        node = H.uptime(self.run_for(180, down_between=(60, 90)))["nodes"]["node1"]
        self.assertEqual({"OS update, Harvester maintenance"}, {o["maintenance"] for o in node["outages"] if o.get("maintenance")})


class HarvesterMaintenanceTests(UptimeCase):
    """Harvester's own maintenance mode counts as maintenance (#535)."""

    def test_harvester_maintenance_reads_the_annotation_and_the_condition(self):
        import homestead_nodes as NODES
        def node(annotations=None, conditions=()):
            return {"metadata": {"annotations": annotations or {}}, "status": {"conditions": list(conditions)}}
        # Harvester up to v1.6: an annotation; later releases: a node condition.
        for value in ("running", "completed"):
            self.assertTrue(NODES.harvester_maintenance(node({"harvesterhci.io/maintain-status": value})))
        self.assertFalse(NODES.harvester_maintenance(node({"harvesterhci.io/maintain-status": "other"})))
        self.assertFalse(NODES.harvester_maintenance(node()))
        self.assertTrue(NODES.harvester_maintenance(node(conditions=[{"type": "MaintenanceMode", "status": "True", "reason": "Draining"}])))
        self.assertFalse(NODES.harvester_maintenance(node(conditions=[{"type": "MaintenanceMode", "status": "False", "reason": "Error"}])))

    def test_harvester_mode_opens_and_closes_with_the_samples(self):
        def sample(minute, maintenance):
            o = overview(True)
            o["nodes"][0]["maintenance"] = maintenance
            H.record(o, now=self.start + minute * 60)
        sample(0, False)
        sample(5, True)
        sample(10, True)
        sample(15, False)
        events = H.maintenance_events("node1")
        self.assertEqual([("Harvester maintenance", f"harvester:node1:{self.start + 300}", self.start + 300, self.start + 900, "ended")],
                         [(e["reason"], e["job"], e["start"], e["end"], e["outcome"]) for e in events])

    def test_a_harvester_host_that_disappears_is_closed_when_last_seen(self):
        def sample(minute, nodes):
            H.record({"nodes": nodes}, now=self.start + minute * 60)
        host = lambda m: [{"name": "node1", "status": "Ready", "boot_id": "b1", "maintenance": m}]
        sample(0, host(True))
        sample(5, host(True))
        sample(10, [])
        self.assertIsNotNone(H.maintenance_open_for("node1", now=self.start + 600), "gone from one sample: still open")
        sample(5 + H.MISSING_CLOSE // 60 + 5, [])
        event = H.maintenance_events("node1")[0]
        self.assertEqual((self.start + 300, "ended"), (event["end"], event["outcome"]), "closed when it was last seen")


# uptime() as it was before #979, kept as the reference the faster one must
# match exactly: two whole passes over a node's rows for each of 90 days.
def _reference_ready_rows(data, name):
    fine = [(r["t"], H.STEP, r["nodes"][name][2]) for r in data["fine"] if name in (r.get("nodes") or {})]
    first_fine = fine[0][0] if fine else float("inf")
    coarse = [(r["t"], 3600, r["nodes"][name][2]) for r in data["coarse"]
              if name in (r.get("nodes") or {}) and r["t"] + 3600 <= first_fine]
    return coarse + fine


def _reference_share(rows, since):
    rows = [(row[0], row[1], row[2]) for row in rows if row[0] + row[1] > since]
    total = sum(span for _, span, _ in rows)
    return round(100 * sum(span * up for _, span, up in rows) / total, 3) if total else None


def _reference_pieces(rows, spans):
    out = []
    for t, span, up in rows:
        cuts = {t, t + span}
        for a, b, _ in spans:
            if a < t + span and b > t:
                cuts.update((max(a, t), min(b, t + span)))
        edges = sorted(cuts)
        cut = [(a, b - a, next((r for s, e, r in spans if s <= a and b <= e), None)) for a, b in zip(edges, edges[1:])]
        down = span * (1 - up)
        overlap = sum(length for _, length, reason in cut if reason)
        inside = min(down, overlap + 2 * H.STEP) if overlap else 0
        rest, outside_len = down - inside, span - overlap
        for a, length, reason in cut:
            share = (inside * length / overlap) if reason else (rest * length / outside_len if outside_len else 0)
            out.append((a, length, share, reason, span))
    return out


def _reference_uptime(data, now):
    names = sorted({n for r in data["fine"] + data["coarse"] for n in (r.get("nodes") or {})})
    out = {}
    maintenance = H._expire(data["maintenance"], now)
    for name in names:
        rows = _reference_ready_rows(data, name)
        events = [e for e in maintenance if e["node"] == name]
        spans = H._spans(events, now)
        pieces = _reference_pieces(rows, spans)
        outside = [(t, length, 1 - down / length) for t, length, down, reason, _ in pieces if reason is None and length]
        days = []
        for back in range(89, -1, -1):
            start = now - now % 86400 - back * 86400
            share = _reference_share([(t, span, up) for t, span, up in rows if t < start + 86400], start)
            days.append({"day": start, "up": share,
                         "up_excluding_maintenance": _reference_share([p for p in outside if p[0] < start + 86400], start),
                         "maintenance": any(a < start + 86400 and b > start for a, b, _ in spans)})
        planned = lambda t: any(a <= t <= b + H.STEP for a, b, _ in spans)
        out[name] = {"windows": {label: _reference_share(rows, now - span) for label, span in H.WINDOWS},
                     "windows_excluding_maintenance": {label: _reference_share(outside, now - span) for label, span in H.WINDOWS},
                     "maintenance_s": {label: H._covered(spans, now - span, now) for label, span in H.WINDOWS},
                     "days": days, "outages": H._outages(pieces, now)[-50:],
                     "reboots": [{"t": e["t"], "planned": planned(e["t"])} for e in data.get("events") or []
                                 if e.get("node") == name and e.get("kind") == "reboot"][-50:],
                     "maintenance": [{"start": e["start"], "end": e["end"], "reason": e["reason"], "job": e["job"],
                                      "ongoing": e["end"] is None} for e in sorted(events, key=lambda e: e["start"])][-50:],
                     "since": rows[0][0] if rows else None}
    return {"nodes": out, "step": H.STEP}


def _random_history(rnd, now):
    """Months of samples with gaps, odd timestamps, partial hours, nodes that
    come and go, and maintenance open, closed, overlapping and overrun."""
    names = ["node-a", "node-b", "node-c"]
    hour = now - now % 3600
    coarse = []
    for back in range(rnd.choice((30, 91, 95)) * 24, 0, -1):
        if rnd.random() < 0.3:
            continue
        t = hour - back * 3600 + (rnd.choice((0, 0, 0, 1800, 7)) if rnd.random() < 0.05 else 0)
        nodes = {n: [1.0, 2.0, rnd.choice((1, 1, 1, 0, 0.5, round(rnd.random(), 3)))] for n in names if rnd.random() < 0.9}
        coarse.append({"t": t, "n": 12, "nodes": nodes})
    fine = []
    for back in range(2 * 288, -1, -1):
        if rnd.random() < 0.2:
            continue
        nodes = {n: [1, 2, rnd.choice((1, 1, 1, 1, 0))] for n in names if rnd.random() < 0.9}
        fine.append({"t": now - now % 300 - back * 300, "nodes": nodes})
    if rnd.random() < 0.4:              # a hand-edited file: samples out of order
        i, j = rnd.randrange(len(fine)), rnd.randrange(len(fine))
        fine[i], fine[j] = fine[j], fine[i]
    maintenance = []
    for i in range(rnd.randint(0, 12)):
        start = now - rnd.randint(0, 92 * DAY)
        end = rnd.choice((None, start + rnd.randint(0, 3 * DAY), start + rnd.randint(0, 3600) + 0.5))
        maintenance.append({"id": f"j{i}", "job": f"j{i}", "node": rnd.choice(names), "start": start, "end": end,
                            "reason": rnd.choice(("OS update", "host reboot", "drain", "Harvester maintenance")),
                            "outcome": None})
    events = [{"t": now - rnd.randint(0, 90 * DAY), "node": rnd.choice(names), "kind": "reboot"} for _ in range(5)]
    return {"fine": fine, "coarse": coarse, "boots": {}, "events": events, "maintenance": maintenance}


class DayStripTests(UptimeCase):
    """#979: the 90-day strip comes from one pass over each node's rows, and the
    whole answer is exactly what two passes a day gave before."""

    def test_the_answer_matches_the_full_computation_on_varied_histories(self):
        import json, os, random
        rnd = random.Random(979)
        for case in range(16):
            now = self.start + rnd.randint(0, 3 * DAY) + rnd.choice((0, 1, 299, 3599, DAY - 1))
            data = _random_history(rnd, now)
            with open(os.path.join(self.tmp.name, H.FILE), "w", encoding="utf-8") as handle:
                json.dump(data, handle)
            want = _reference_uptime(H._read(), now)
            self.assertTrue(want["nodes"])
            self.assertEqual(want, H.uptime(now), f"case {case}")

    def test_it_is_several_times_quicker_than_the_full_computation(self):
        import json, os, time
        names = [f"node-{i}" for i in range(8)]
        now = self.start + 13 * 3600
        coarse = [{"t": now - now % 3600 - back * 3600, "n": 12, "nodes": {n: [1.0, 2.0, 1] for n in names}}
                  for back in range(90 * 24, 0, -1)]
        fine = [{"t": now - now % 300 - back * 300, "nodes": {n: [1, 2, 1] for n in names}} for back in range(576, -1, -1)]
        maintenance = [{"id": f"j{i}", "job": f"j{i}", "node": names[i % 8], "start": now - i * 7 * DAY, "end": now - i * 7 * DAY + 3600,
                        "reason": "OS update", "outcome": "ended"} for i in range(1, 13)]
        with open(os.path.join(self.tmp.name, H.FILE), "w", encoding="utf-8") as handle:
            json.dump({"fine": fine, "coarse": coarse, "maintenance": maintenance}, handle)
        started = time.process_time()
        want = _reference_uptime(H._read(), now)
        before = time.process_time() - started
        started = time.process_time()
        got = H.uptime(now)
        after = time.process_time() - started
        self.assertEqual(want, got)
        # Relative, so a busy machine slows both alike: 20 nodes took 0.8 s before #979.
        # A generous ratio: about 6 times on a quiet machine, never under 3 seen on a loaded one.
        self.assertLess(after * 2, before, f"{after:.3f} s against {before:.3f} s before #979")

if __name__ == "__main__":
    unittest.main()
