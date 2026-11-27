"""The page's big per-tick reads travel gzipped when the browser accepts it (#982).

Only the reads on homestead_static.COMPRESSED_ANSWERS are compressed: list and
summary answers every role may read, with no secret in them. Compressing an
answer that holds a secret next to text the requester chose is how BREACH
reads the secret back, so everything else (answers to actions, downloads,
streams, reveals, logs) is always sent as it is."""
import email.message
import gzip
import hashlib
import importlib
import io
import json
import sys
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import server
import homestead_history as HISTORY
import homestead_routes as ROUTES
import homestead_static as STATIC

BIG = {"nodes": [{"name": f"node-{n}", "days": [{"day": d, "up": 86400, "down": 0, "kind": "ok"}
                                                 for d in range(90)]} for n in range(3)]}
SMALL = {"ok": True}


def call(path, body, ctype="application/json", code=200, method="GET", gzip_ok=True, extra=()):
    """Send body through the handler's own _send, with no socket, and parse what it wrote."""
    handler = server.H.__new__(server.H)
    handler.path, handler.command = path, method
    handler.request_version, handler.requestline = "HTTP/1.1", f"{method} {path} HTTP/1.1"
    handler.headers = email.message.Message()
    if gzip_ok:
        handler.headers["Accept-Encoding"] = "br, gzip;q=0.9, deflate"
    handler.wfile, handler.connection = io.BytesIO(), None
    handler.close_connection, handler._extra_headers = False, list(extra)
    handler.client_address = ("192.0.2.10", 0)
    handler.log_message = lambda *args: None
    handler._send(code, body, ctype)
    head, _, payload = handler.wfile.getvalue().partition(b"\r\n\r\n")
    headers = {}
    for line in head.decode("latin-1").split("\r\n")[1:]:
        name, _, value = line.partition(":")
        headers.setdefault(name.strip().lower(), []).append(value.strip())
    return headers, payload


class AnswerCompressionTests(unittest.TestCase):
    def test_a_large_listed_read_is_gzipped_when_asked(self):
        headers, payload = call("/api/nodes/uptime", BIG)
        self.assertEqual(["gzip"], headers["content-encoding"])
        self.assertIn("Accept-Encoding", headers["vary"])
        self.assertEqual([str(len(payload))], headers["content-length"])
        self.assertEqual(BIG, json.loads(gzip.decompress(payload)))
        self.assertLess(len(payload), len(json.dumps(BIG)) / 5)

    def test_a_query_string_does_not_change_which_read_it_is(self):
        headers, _ = call("/api/history/long?range=7d", BIG)
        self.assertEqual(["gzip"], headers["content-encoding"])

    def test_the_same_read_is_plain_when_the_browser_did_not_ask(self):
        headers, payload = call("/api/nodes/uptime", BIG, gzip_ok=False)
        self.assertNotIn("content-encoding", headers)
        self.assertIn("Accept-Encoding", headers["vary"])
        self.assertEqual(BIG, json.loads(payload))

    def test_gzip_refused_with_q_zero_is_not_used(self):
        for refused in ("gzip;q=0, identity", "gzip; q=0.0", "identity"):
            with self.subTest(accept=refused):
                handler = type("H", (), {"headers": {"Accept-Encoding": refused}})()
                self.assertFalse(STATIC._wants_gzip(handler))

    def test_a_small_answer_is_never_compressed(self):
        headers, payload = call("/api/nodes/uptime", SMALL)
        self.assertNotIn("content-encoding", headers)
        self.assertEqual(SMALL, json.loads(payload))

    def test_a_read_not_on_the_list_is_never_compressed(self):
        for path in ("/api/resources/reveal?ns=a&name=b", "/api/auth/keys", "/api/logs?q=x", "/api/setup"):
            with self.subTest(path=path):
                headers, payload = call(path, BIG)
                self.assertNotIn("content-encoding", headers)
                self.assertNotIn("vary", headers)
                self.assertEqual(BIG, json.loads(payload))

    def test_an_answer_to_an_action_is_never_compressed(self):
        headers, _ = call("/api/overview", BIG, method="POST")
        self.assertNotIn("content-encoding", headers)

    def test_an_error_is_never_compressed(self):
        headers, _ = call("/api/overview", BIG, code=500)
        self.assertNotIn("content-encoding", headers)

    def test_a_download_or_stream_is_never_compressed(self):
        text = json.dumps(BIG)
        for ctype, extra in (("application/json", [("Content-Disposition", 'attachment; filename="a.json"')]),
                             ("text/event-stream", []), ("application/octet-stream", [])):
            with self.subTest(ctype=ctype):
                headers, payload = call("/api/overview", text, ctype=ctype, extra=extra)
                self.assertNotIn("content-encoding", headers)
                self.assertEqual(text.encode(), payload)

    def test_a_body_already_encoded_is_not_encoded_twice(self):
        packed = gzip.compress(json.dumps(BIG).encode())
        headers, payload = call("/api/overview", packed, extra=[("Content-Encoding", "gzip")])
        self.assertEqual(["gzip"], headers["content-encoding"])
        self.assertEqual(packed, payload)


def _digest(*parts):
    return "sha256:" + hashlib.sha256("/".join(map(str, parts)).encode()).hexdigest()


def image_updates(apps=60):
    """/api/image-updates for a cluster of `apps` apps, in the shape
    homestead_update_checks builds it: one or two images each, digests and all."""
    workloads = []
    for n in range(apps):
        ns, name = f"apps-{n % 7}", f"app-{n}"
        images = []
        for c in range(1 + n % 2):
            base = f"registry.example.com/team-{n % 5}/{name}-{c}"
            images.append({"container": f"{name}-{c}", "deployed": f"{base}:1.{n}.0", "source": f"{base}:1.{n}.0",
                           "available": n % 9 == 0, "current_digest": _digest(base, "now"),
                           "remote_digest": _digest(base, "remote" if n % 9 == 0 else "now"),
                           "candidate": f"{base}:1.{n + (n % 9 == 0)}.0", "candidate_tag": f"1.{n}.0",
                           "error": "registry authentication required" if n % 13 == 0 else "", "running": True})
        workloads.append({"ns": ns, "name": name, "images": images, "unchecked": False,
                          "available": any(i["available"] for i in images), "managed": {}, "yaml": "",
                          "can_rollback": n % 4 == 0, "last_action": "", "homestead": ""})
    return {"channel": "prod", "checked_at": "2026-11-23T10:00:00Z", "updates": 7, "rate_limited": 0, "errors": 5,
            "homestead": {"updates": 0, "errors": 0}, "workloads": workloads,
            "policy": {"policy": "approval_required", "allows_install": True, "requires_approval": True,
                       "reason": "Explicit operator approval is required before rollout.", "window_open": False,
                       "maintenance": {"start": "02:00", "duration_minutes": 120}}}


def storage_forecast(volumes=60, hosts=4):
    """/api/storage/forecast: a row per volume, per Longhorn disk and the pool (homestead_forecast.report)."""
    rows = [{"key": f"volume:pvc-{n:04d}", "kind": "volume", "label": f"apps-{n % 7}/data-app-{n}",
             "namespace": f"apps-{n % 7}", "volume": f"pvc-{n:04d}", "node": "", "used": 1_000_000_000 + n * 7919,
             "capacity": 10 * 1024**3, "pct": round(9.3 + n / 7, 1), "days": 30,
             "days_left": None if n % 3 else round(40.0 + n, 1), "per_day": 1048576.0 * n,
             "why": "not growing" if n % 3 else "", "full_on": "" if n % 3 else "2027-01-05"} for n in range(volumes)]
    rows += [{"key": f"disk:node-{h}:disk-{h}", "kind": "disk", "label": f"node-{h} /var/lib/longhorn",
              "namespace": "", "volume": "", "node": f"node-{h}", "used": 400 * 1024**3, "capacity": 1024**4,
              "pct": 39.1, "days": 30, "days_left": None, "per_day": 2.0e8, "why": "too irregular to forecast",
              "full_on": ""} for h in range(hosts)]
    rows.append({"key": "pool", "kind": "pool", "label": "Longhorn storage", "namespace": "", "volume": "", "node": "",
                 "used": 1600 * 1024**3, "capacity": 4 * 1024**4, "pct": 39.1, "days": 30, "days_left": None,
                 "per_day": 8.0e8, "why": "not growing", "full_on": ""})
    return {"rows": rows, "warn_days": 14, "show_days": 60, "min_days": 7}


def live_history(points=120):
    """/api/history from the real homestead_history.live_series, over a full live
    buffer: an hour of 30 s samples as record_live keeps them."""
    now = 1_763_903_580
    samples = [{"t": now - 30 * (points - n), "cpu": round(18 + (n * 37 % 50) / 7, 1),
                "mem": round(52 + (n * 11 % 30) / 10, 1), "rx": round(3.5 + (n * 29 % 97) / 13, 2),
                "tx": round(1.2 + (n * 31 % 89) / 17, 2), "pods": 140 + n % 3, "sys_pods": 61, "vol_bad": 0,
                "nodes_ready": 4, "nodes_total": 4} for n in range(points)]
    with mock.patch.object(HISTORY, "_read_live", return_value=samples):
        return HISTORY.live_series(now=now)


def longhorn_capacity(hosts=4, disks=2):
    """/api/longhorn/capacity: each host's Longhorn disks (homestead_lhcapacity.status)."""
    nodes = [{"name": f"node-{h}", "tags": [], "allocated_gb": 812.0, "limit_gb": 1862.6, "size_gb": 1862.6,
              "used_gb": 640.4, "pct": 43.6, "room_gb": 520.3, "physical_room_gb": 700.1, "level": "ok", "blocked": "",
              "disks": [{"id": f"disk-{h}-{d}", "path": f"/var/lib/longhorn-{d}", "type": "filesystem", "tags": ["ssd"],
                         "size_gb": 931.3, "allocated_gb": 406.0, "limit_gb": 931.3, "used_gb": 320.2,
                         "free_gb": 611.1, "room_gb": 525.3, "physical_room_gb": 378.3, "pct": 43.6, "blocked": ""}
                        for d in range(disks)]} for h in range(hosts)]
    return {"over_provisioning": 100, "minimal_available": 25, "nodes": nodes,
            "largest": {"1": 525.3, "2": 525.3, "3": 525.3}, "warn_pct": 80, "crit_pct": 90,
            "v2": {"enabled": False, "harvester_setting": None, "ready_nodes": 0, "total_nodes": hosts,
                   "distribution": "k3s", "longhorn_version": "v1.9.1", "longhorn_ok": True,
                   "nodes": [{"name": f"node-{h}", "block_disks": 0, "hugepages_mb": 0, "ready": False,
                              "missing": ["a V2 (block) disk", "2 GiB of hugepages (has 0 MiB)"],
                              "checks": {"cpu": True, "modules": None, "hugepages": False, "disk": False},
                              "missing_modules": []} for h in range(hosts)]},
            "node_down": "do-nothing", "rwx_fast_failover": False,
            "replenish_wait": 600, "replenish_choices": [600, 3600, 21600, 86400],
            "orphan_auto_removal": {"setting": "orphan-resource-auto-deletion", "said": "Longhorn removes records of replicas by itself."}}


class ListReadsAddedFor1211Tests(unittest.TestCase):
    """#1211: the steady list reads a page asks for on every tick, measured going
    over the wire at full size. Each holds names, numbers and fixed phrases, and
    no secret, log line or value masked for some roles."""
    ADDED = {"/api/image-updates": image_updates, "/api/storage/forecast": storage_forecast,
             "/api/history": live_history, "/api/longhorn/capacity": longhorn_capacity}

    def test_each_is_gzipped_for_a_browser_that_accepts_it(self):
        for path, shape in self.ADDED.items():
            body = shape()
            with self.subTest(path=path):
                headers, payload = call(path, body)
                self.assertEqual(["gzip"], headers.get("content-encoding"), f"{path} went out at full size")
                self.assertIn("Accept-Encoding", headers["vary"])
                self.assertEqual(body, json.loads(gzip.decompress(payload)))
                self.assertLess(len(payload), len(json.dumps(body)) / 3)

    def test_each_is_sent_unchanged_to_one_that_does_not(self):
        for path, shape in self.ADDED.items():
            body = shape()
            with self.subTest(path=path):
                headers, payload = call(path, body, gzip_ok=False)
                self.assertNotIn("content-encoding", headers)
                self.assertIn("Accept-Encoding", headers.get("vary", []), f"{path} is not on the list")
                self.assertEqual(json.dumps(body).encode(), payload)

    def test_alerts_and_gitops_stay_uncompressed(self):
        # /api/alerts: each person's own acknowledgements and push devices, a value
        # that differs by who asks. /api/gitops: Flux's own message,
        # which can name repositories and Secrets (homestead_gitops.alert_facts).
        for path in ("/api/alerts", "/api/gitops"):
            with self.subTest(path=path):
                headers, payload = call(path, BIG)
                self.assertNotIn("content-encoding", headers)
                self.assertNotIn("vary", headers)
                self.assertEqual(BIG, json.loads(payload))

    def test_every_listed_read_is_a_get_route_any_role_may_read(self):
        declared = {}
        for name in ROUTES.MODULES:
            for (method, path), (role, *_) in getattr(importlib.import_module(name), "ROUTES", {}).items():
                declared[(method, path)] = role
        for path in STATIC.COMPRESSED_ANSWERS:
            with self.subTest(path=path):
                self.assertEqual("viewer", declared.get(("GET", path)), f"{path} is not a viewer GET route")


if __name__ == "__main__":
    unittest.main()
