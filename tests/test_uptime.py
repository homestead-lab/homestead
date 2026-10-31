import http.server
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_uptime as UP


def app(name="sonarr", port=8989, desired=1, **extra):
    row = {"ns": "lab", "name": name, "desired": desired,
           "ports": [{"ip": "192.0.2.10", "port": port, "protocol": "TCP"}]}
    row.update(extra)
    return row


OK = {"ok": True, "ms": 40, "code": 200, "error": ""}
MISS = {"ok": False, "ms": None, "code": None, "error": "connection refused"}
SLOW = {"ok": True, "ms": 3500, "code": 200, "error": ""}


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        UP.bind(self.dir.name)

    def tearDown(self):
        self.dir.cleanup()


class TargetTests(unittest.TestCase):
    def test_the_main_tcp_port_over_http_and_https(self):
        t, _ = UP.target(app(port=8989))
        self.assertEqual(("http", "http", "192.0.2.10", 8989, "/"), (t["kind"], t["scheme"], t["host"], t["port"], t["path"]))
        self.assertEqual("https", UP.target(app(port=8443))[0]["scheme"])

    def test_what_is_not_checked_and_why(self):
        cases = [(app(desired=0), "stopped"), (app(ports=[]), "no address"),
                 (app(ports=[{"ip": "192.0.2.10", "port": 53, "protocol": "UDP"}]), "no address"),
                 (app(answer_check="off"), "off"), (app(platform=True), "not an app"), (app(self=True), "not an app")]
        for w, why in cases:
            with self.subTest(why=why):
                t, reason = UP.target(w)
                self.assertIsNone(t)
                self.assertIn(why, reason)

    def test_an_apps_own_setting(self):
        self.assertEqual("tcp", UP.target(app(answer_check="tcp"))[0]["kind"])
        t = UP.target(app(answer_check="/health?full=1"))[0]
        self.assertEqual(("/health?full=1", True), (t["path"], t["strict"]))
        self.assertEqual("/", UP.target(app(answer_check="bad path"))[0]["path"], "an unreadable setting is auto")

    def test_settings_are_checked(self):
        self.assertEqual("", UP.check_setting("auto"))
        self.assertEqual("/ping", UP.check_setting("http", "/ping"))
        for mode, path in (("http", "no-slash"), ("http", "/a b"), ("http", "/x\r\nHost: evil"), ("ftp", "")):
            with self.subTest(mode=mode, path=path), self.assertRaises(ValueError):
                UP.check_setting(mode, path)


class PortChoiceTests(Base):
    def couch(self, extra=None):
        extra = extra or {}
        return app(name="obsidian-livesync", ports=[{"ip": "192.0.2.10", "port": p, "protocol": "TCP", **extra.get(p, {})}
                                                    for p in (4369, 5984, 9100)])

    def test_without_a_main_port_any_published_port_answering_will_do(self):
        asked = []

        def ask(t):
            asked.append(t["port"])
            return OK if t["port"] == 5984 else MISS
        UP.observe([self.couch()], now=1000, ask=ask)
        self.assertEqual([4369, 5984], asked, "it stops at the first that answers")
        rep = UP.report(1000)["lab/obsidian-livesync"]
        self.assertEqual(("up", "http://192.0.2.10:5984/"), (rep["state"], rep["target"]))

    def test_a_chosen_main_port_is_the_only_one_asked(self):
        asked = []
        w = self.couch({4369: {"primary": True}})
        UP.observe([w], now=1000, ask=lambda t: asked.append(t["port"]) or MISS)
        self.assertEqual([4369], asked)

    def test_when_none_answer_it_says_how_many_were_asked(self):
        UP.observe([self.couch()], now=1000, ask=lambda t: MISS)
        self.assertEqual("3 ports at 192.0.2.10", UP.report(1000)["lab/obsidian-livesync"]["target"])

    def test_a_lan_app_is_asked_at_its_pod_not_its_macvtap_address(self):
        w = app(ports=[{"ip": "192.0.2.50", "port": 8123, "protocol": "TCP", "lan": True}],
                pods=[{"ip": "10.42.1.7", "ready": True}])
        t, _ = UP.target(w)
        self.assertEqual(("10.42.1.7", 8123), (t["host"], t["port"]))
        no_pod = app(ports=[{"ip": "192.0.2.50", "port": 8123, "protocol": "TCP", "lan": True}], pods=[])
        self.assertEqual("192.0.2.50", UP.target(no_pod)[0]["host"], "with no pod address it still tries the LAN one")


def vm(name="ha", running=True, ips=("192.0.2.60",), monitoring="", **extra):
    row = {"ns": "lab", "name": name, "running": running, "ips": list(ips), "monitoring": monitoring}
    row.update(extra)
    return row


class VmTests(Base):
    def test_what_a_vm_is_not_asked_and_why(self):
        for v, why in ((vm(running=False), "stopped"), (vm(ips=()), "guest agent"), (vm(monitoring="off"), "off"),
                       (vm(site={"id": "b"}), "another cluster")):
            with self.subTest(why=why):
                t, reason = UP.vm_target(v)
                self.assertIsNone(t)
                self.assertIn(why, reason)

    def test_a_chosen_port_or_page(self):
        t, _ = UP.vm_target(vm(monitoring="tcp:22"))
        self.assertEqual(("tcp", 22, True), (t["kind"], t["port"], t["chosen"]))
        t, _ = UP.vm_target(vm(monitoring="http:8123/api/"))
        self.assertEqual(("http", 8123, "/api/", True), (t["kind"], t["port"], t["path"], t["strict"]))

    def test_settings_with_a_port(self):
        self.assertEqual("tcp:22", UP.check_setting("tcp", "", 22))
        self.assertEqual("http:8123/", UP.check_setting("http", "/", "8123"))
        for bad in (0, 70000, "ssh"):
            with self.subTest(port=bad), self.assertRaises(ValueError):
                UP.check_setting("tcp", "", bad)
        self.assertEqual({"mode": "auto"}, UP.setting("tcp:99999"))
        self.assertEqual({"mode": "auto"}, UP.setting("tcp:22/x"))

    def test_the_first_usual_port_that_answers_is_learned_and_kept(self):
        asked = []

        def ask(t):
            asked.append(t["port"])
            return OK if t["port"] == 8123 else MISS
        UP.observe([], now=1000, ask=ask, vms=[vm()])
        self.assertEqual([22, 3389, 443, 80, 8006, 8123], asked, "the usual ports, quickly, until one answers")
        rep = UP.report(1000)["vm:lab/ha"]
        self.assertEqual(("up", 8123, "tcp 192.0.2.60:8123"), (rep["state"], rep["port"], rep["target"]))
        asked.clear()
        UP.observe([], now=1060, ask=ask, vms=[vm()])
        self.assertEqual([8123], asked, "after that only the port it answered on")

    def test_no_usual_port_answering_is_not_an_outage(self):
        for now in (1000, 1060, 1120, 1180):
            UP.observe([], now=now, ask=lambda t: MISS, vms=[vm()])
        rep = UP.report(1180)["vm:lab/ha"]
        self.assertEqual("off", rep["state"])
        self.assertIn("choose the port", rep["why"])
        self.assertEqual([], UP.alert_facts(UP.report(1180)))

    def test_a_learned_port_that_stops_answering_is_down_with_a_vm_alert(self):
        UP.observe([], now=1000, ask=lambda t: OK if t["port"] == 22 else MISS, vms=[vm()])
        for now in (1060, 1120, 1180):
            UP.observe([], now=now, ask=lambda t: MISS, vms=[vm()])
        fact = UP.alert_facts(UP.report(1180))[0]
        self.assertEqual(("uptime:vm:lab/ha", "VM ha is down"), (fact["key"], fact["title"]))
        self.assertIn("/vms?panel=monitoring&ns=lab&vm=ha", fact["href"])

    def test_a_chosen_port_replaces_a_learned_one(self):
        UP.observe([], now=1000, ask=lambda t: OK if t["port"] == 22 else MISS, vms=[vm()])
        UP.observe([], now=1060, ask=lambda t: OK, vms=[vm(monitoring="tcp:2222")])
        self.assertIsNone(UP.report(1060)["vm:lab/ha"]["port"])



def two_ports(**extra):
    return app(ports=[{"ip": "192.0.2.10", "port": 8080, "protocol": "TCP", "primary": True},
                      {"ip": "192.0.2.10", "port": 9090, "protocol": "TCP"},
                      {"ip": "192.0.2.11", "port": 9090, "protocol": "TCP", "lan": True}], **extra)


class ChosenPortTests(unittest.TestCase):
    def test_any_published_port_can_be_chosen(self):
        self.assertEqual(8080, UP.target(two_ports())[0]["port"], "by default its main port")
        t = UP.target(two_ports(answer_check="auto:9090"))[0]
        self.assertEqual(({9090}, False), ({a["port"] for a in t["asks"]}, t["strict"]))
        t = UP.target(two_ports(answer_check="http:9090/health"))[0]
        self.assertEqual((9090, "/health", True), (t["port"], t["path"], t["strict"]))
        self.assertEqual(("tcp", 9090), (UP.target(two_ports(answer_check="tcp:9090"))[0]["kind"],
                                         UP.target(two_ports(answer_check="tcp:9090"))[0]["port"]))

    def test_a_port_no_longer_published_says_so(self):
        t, why = UP.target(two_ports(answer_check="tcp:7000"))
        self.assertIsNone(t)
        self.assertIn("port 7000 is no longer published", why)

    def test_settings_with_a_port(self):
        self.assertEqual("auto:9090", UP.check_setting("auto", "", 9090))
        self.assertEqual("http:9090/x", UP.check_setting("http", "/x", "9090"))
        self.assertEqual("off", UP.check_setting("off", "", 9090))
        self.assertEqual({"mode": "auto"}, UP.setting("auto:9090/x"), "auto takes no path")
        self.assertEqual("checked automatically on port 9090", UP.describe_setting("auto:9090"))
        self.assertEqual("checked at /x on port 9090", UP.describe_setting("http:9090/x"))
        self.assertEqual("checked by a connection to its port", UP.describe_setting("tcp"))

    def test_the_route_takes_only_a_published_port(self):
        import server
        sent = []
        with mock.patch.object(server.KUBE_HELPERS, "ksend", side_effect=lambda *a, **k: sent.append(a)), \
             mock.patch.object(server.CACHE, "cached", return_value=[{"ns": "lab", "name": "sonarr", **two_ports()}]):
            result = server.OVERVIEW_ROUTES.set_uptime_setting({"ns": "lab", "name": "sonarr", "mode": "tcp", "port": 9090})
            with self.assertRaises(ValueError):
                server.OVERVIEW_ROUTES.set_uptime_setting({"ns": "lab", "name": "sonarr", "mode": "tcp", "port": 7000})
        self.assertEqual("tcp:9090", sent[0][2]["metadata"]["annotations"]["homestead.io/uptime"])
        self.assertEqual("tcp:9090", result["value"])

    def test_a_vms_automatic_check_takes_no_port(self):
        import server
        sent = []
        with mock.patch.object(server.KUBE_HELPERS, "ksend", side_effect=lambda *a, **k: sent.append(a)):
            server.OVERVIEW_ROUTES.set_vm_monitoring({"ns": "lab", "name": "ha", "mode": "auto", "port": 22})
        self.assertIsNone(sent[0][2]["metadata"]["annotations"]["homestead.io/uptime"])


class VmSettingRouteTests(unittest.TestCase):
    def test_only_the_vms_annotation_changes_and_a_port_is_needed(self):
        import server
        sent = []
        with mock.patch.object(server.KUBE_HELPERS, "ksend", side_effect=lambda *a, **k: sent.append(a)):
            server.OVERVIEW_ROUTES.set_vm_monitoring({"ns": "lab", "name": "ha", "mode": "http", "port": 8123, "path": "/api/"})
            server.OVERVIEW_ROUTES.set_vm_monitoring({"ns": "lab", "name": "ha", "mode": "auto"})
            for bad in ({"mode": "tcp"}, {"mode": "http", "path": "/x"}, {"mode": "tcp", "port": 99999}, {"name": "../x", "mode": "off"}):
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    server.OVERVIEW_ROUTES.set_vm_monitoring({"ns": "lab", "name": "ha", **bad})
        (method, path, body) = sent[0]
        self.assertEqual(("PATCH", "/apis/kubevirt.io/v1/namespaces/lab/virtualmachines/ha"), (method, path))
        self.assertEqual({"metadata": {"annotations": {"homestead.io/uptime": "http:8123/api/"}}}, body,
                         "the VM's spec is untouched, so it does not restart")
        self.assertIsNone(sent[1][2]["metadata"]["annotations"]["homestead.io/uptime"])
        self.assertEqual(2, len(sent))


class StateTests(Base):
    def run_round(self, result, now, apps=None):
        return UP.observe(apps or [app()], now=now, ask=lambda t: result)

    def test_down_after_three_misses_and_up_on_the_first_answer(self):
        self.run_round(OK, 1000)
        self.run_round(MISS, 1060)
        self.run_round(MISS, 1120)
        self.assertEqual("up", UP.report(1120)["lab/sonarr"]["state"], "two misses are not an outage")
        self.assertEqual([], UP.alert_facts(UP.report(1120)))
        self.run_round(MISS, 1180)
        rep = UP.report(1180)
        self.assertEqual("down", rep["lab/sonarr"]["state"])
        self.assertEqual(1060, rep["lab/sonarr"]["since"], "down since the first miss")
        fact = UP.alert_facts(rep)[0]
        self.assertEqual(("uptime:lab/sonarr", "critical", "sonarr is down"),
                         (fact["key"], fact["severity"], fact["title"]))
        self.assertIn("connection refused", fact["body"])
        self.run_round(OK, 1240)
        self.assertEqual("up", UP.report(1240)["lab/sonarr"]["state"])
        self.assertEqual([], UP.alert_facts(UP.report(1240)))

    def test_slow_is_shown_but_not_an_outage(self):
        self.run_round(SLOW, 1000)
        self.assertEqual("slow", UP.report(1000)["lab/sonarr"]["state"])
        self.assertEqual([], UP.alert_facts(UP.report(1000)))

    def test_hours_strip_and_uptime(self):
        hour = 3600 * 100
        for minute in range(60):
            self.run_round(MISS if minute < 6 else OK, hour + minute * 60)
        rep = UP.report(hour + 3599)["lab/sonarr"]
        self.assertEqual(90.0, rep["uptime_24h"])
        self.assertEqual("down", rep["strip"][-1])
        self.assertEqual([None] * 23, rep["strip"][:-1])

    def test_a_stopped_app_is_off_not_down(self):
        self.run_round(MISS, 1000)
        self.run_round(MISS, 1060, [app(desired=0)])
        rep = UP.report(1060)["lab/sonarr"]
        self.assertEqual(("off", "stopped"), (rep["state"], rep["why"]))

    def test_history_older_than_thirty_days_is_dropped(self):
        self.run_round(OK, 3600)
        self.run_round(OK, 3600 + 31 * 86400)
        self.assertEqual(1, len(UP._state["apps"]["lab/sonarr"]["hours"]))



def _reference_summary(app, now):
    """summary() as it was before #979: thirty passes over the hours, one a day."""
    hours = app.get("hours", {})
    this = UP._hour(now)
    strip = []
    for i in range(UP.STRIP_HOURS - 1, -1, -1):
        row = hours.get(str(this - i * 3600))
        strip.append(None if not row or not row[0] else "down" if row[1] else "slow" if row[2] * 2 >= row[0] else "up")

    def share(rows):
        checks = sum(r[0] for r in rows)
        return None if not checks else round(100 * (checks - sum(r[1] for r in rows)) / checks, 2)

    today = int(now // 86400 * 86400)
    days = []
    for i in range(29, -1, -1):
        start = today - i * 86400
        rows = [v for h, v in hours.items() if start <= int(h) < start + 86400]
        checks, misses, slow = (sum(r[k] for r in rows) for k in range(3))
        days.append(None if not checks else "down" if misses * 10 >= checks else "slow" if slow * 2 >= checks
                    else "dip" if misses else "up")
    recent = [v for h, v in hours.items() if int(h) > this - UP.STRIP_HOURS * 3600]
    state = app.get("state", "unknown")
    last = app.get("last") or {}
    if state == "up" and last.get("ms") and last["ms"] > UP.SLOW_MS:
        state = "slow"
    return {"state": state, "since": app.get("since"), "why": app.get("why", ""), "target": app.get("target", ""),
            "port": app.get("port"),
            "last": last, "strip": strip, "days": days, "uptime_24h": share(recent), "uptime_30d": share(list(hours.values()))}


def _random_hours(rnd, now):
    """A month and more of hours: gaps, misses, slow answers, empty rows, an
    hour from the future, and now and then a key or count no check writes."""
    this = UP._hour(now)
    hours = {}
    for back in range(rnd.choice((24, 400, 760)), -3, -1):
        if rnd.random() < 0.15 or (back < 0 and rnd.random() < 0.7):
            continue
        checks = rnd.choice((60, 60, 60, 12, 1, 0))
        hours[str(this - back * 3600)] = [checks, rnd.choice((0, 0, 0, 1, 6, checks)) if checks else 0,
                                          rnd.choice((0, 0, 1, 40)) if checks else 0]
    if rnd.random() < 0.2:
        hours[str(this - rnd.randint(0, 40 * 86400))] = [3, 1, 0]          # not on the hour
    if rnd.random() < 0.2:
        hours[str(this - 5 * 86400)] = [7.0, 0.5, 0]                        # counts as floats
    return dict(rnd.sample(sorted(hours.items()), len(hours)))


class ReportTests(Base):
    """#979: closed days are kept, so a report adds up only today; what it says
    is exactly what adding up every hour said."""

    def test_summary_matches_the_full_computation_on_varied_histories(self):
        import random
        rnd = random.Random(979)
        for case in range(300):
            now = 1_790_000_000 + rnd.randint(0, 86400 * 3) + rnd.random()
            app = {"state": rnd.choice(("up", "down", "off")), "hours": _random_hours(rnd, now),
                   "last": {"ms": rnd.choice((40, 3000))}}
            with self.subTest(case=case):
                self.assertEqual(_reference_summary(app, now), UP.summary(app, now))

    def test_report_matches_through_days_prunes_reloads_and_a_clock_set_back(self):
        import random
        rnd = random.Random(981)
        start = 1_790_000_000 - 1_790_000_000 % 86400 + 22 * 3600     # two hours before midnight
        names = [f"app-{i}" for i in range(4)]
        loaded = {f"lab/{n}": {"state": "up", "since": start - 86400, "hours": _random_hours(rnd, start)}
                  for n in names[:3]}
        Path(self.dir.name, "uptime.json").write_text(__import__("json").dumps({"format": 1, "apps": loaded}))
        now = peak = start
        # Past midnight, hour by hour to the next, then the clock set back over
        # midnight (a round writes into a closed day), then two days ahead.
        steps = [60] * 15 + [3600] * 28 + [-4 * 3600] + [60] * 5 + [86400 * 2] + [60] * 3
        for i, step in enumerate(steps):
            now += step
            peak = max(peak, now)
            UP.observe([app(n, port=8000 + k) for k, n in enumerate(names)], now=now,
                       ask=lambda t: rnd.choice((OK, OK, OK, MISS, SLOW)))
            if i == 10:
                saved = Path(self.dir.name, "uptime.json").read_text()
                UP.bind(self.dir.name)                     # another replica's view of the file
                Path(self.dir.name, "uptime.json").write_text(saved)
            for at in (now, now + 30, now - 4000, peak):
                got = UP.report(at)
                with self.subTest(step=i, at=at):
                    self.assertEqual({k: _reference_summary(a, at) for k, a in UP._state["apps"].items()}, got)

    def test_reports_between_rounds_are_several_times_quicker(self):
        import random, time
        rnd = random.Random(7)
        now = 1_790_000_000 + 13 * 3600
        this = UP._hour(now)
        apps = {f"lab/app-{i}": {"state": "up", "hours": {str(this - h * 3600): [60, 0 if rnd.random() > 0.03 else 5, 0]
                                                          for h in range(720)}} for i in range(100)}
        UP._state.update(loaded=True, apps=apps, observed_at=time.time())
        UP.report(now)
        started = time.process_time()
        want = {k: _reference_summary(a, now) for k, a in apps.items()}
        before = time.process_time() - started
        started = time.process_time()
        for _ in range(3):
            got = UP.report(now + 20)
        after = (time.process_time() - started) / 3
        self.assertEqual(want, got)
        # Relative, so a busy machine slows both alike: 200 apps took 481 ms before #979.
        # A generous ratio: about 100 times, so a loaded runner never trips it.
        self.assertLess(after * 5, before, f"{after:.4f} s a report against {before:.4f} s before #979")

class WriteTests(Base):
    def test_quiet_minutes_write_at_most_every_five_minutes(self):
        start = 7200 + 60
        with mock.patch.object(UP, "_save", wraps=UP._save) as save:
            for minute in range(10):      # within one hour, nothing changes
                UP.observe([app()], now=start + minute * 60, ask=lambda t: OK)
            self.assertEqual(2, save.call_count, "the first round and five minutes later")
            UP.observe([app()], now=start + 10 * 60, ask=lambda t: MISS)
            UP.observe([app()], now=start + 11 * 60, ask=lambda t: MISS)
            n = save.call_count
            UP.observe([app()], now=start + 12 * 60, ask=lambda t: MISS)
            self.assertEqual(n + 1, save.call_count, "going down is written at once")

    def test_the_other_replica_reads_the_file_again(self):
        UP.observe([app()], now=1000, ask=lambda t: OK)
        path = Path(self.dir.name, "uptime.json")
        saved = path.read_text()
        UP.bind(self.dir.name)               # a fresh process that does not check
        self.assertEqual("up", UP.report()["lab/sonarr"]["state"])
        path.write_text(saved.replace('"state":"up"', '"state":"down"'))
        os.utime(path, ns=(1, 2))
        self.assertEqual("down", UP.report()["lab/sonarr"]["state"])


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        code = {"/": 200, "/login": 401, "/broken": 502}.get(self.path, 404)
        self.send_response(code)
        self.end_headers()
        self.wfile.write(b"x")

    def log_message(self, *args):
        pass


class ProbeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.port = cls.server.server_address[1]
        cls.raw = socket.socket()
        cls.raw.bind(("127.0.0.1", 0))
        cls.raw.listen(5)

        def speak_nonsense():
            while True:
                try:
                    conn, _ = cls.raw.accept()
                except OSError:
                    return
                conn.sendall(b"\x00\x01 not http\r\n\r\n")
                conn.close()
        threading.Thread(target=speak_nonsense, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.raw.close()

    def t(self, path="/", port=None, strict=False):
        return {"kind": "http", "host": "127.0.0.1", "port": port or self.port, "scheme": "http",
                "path": path, "strict": strict}

    def test_answers_below_500_count_and_5xx_do_not(self):
        self.assertTrue(UP.probe(self.t("/"))["ok"])
        login = UP.probe(self.t("/login"))
        self.assertEqual((True, 401), (login["ok"], login["code"]))
        broken = UP.probe(self.t("/broken"))
        self.assertEqual((False, "HTTP 502"), (broken["ok"], broken["error"]))

    def test_a_port_that_does_not_speak_http_is_checked_by_connection(self):
        port = self.raw.getsockname()[1]
        self.assertTrue(UP.probe(self.t(port=port))["ok"])
        self.assertFalse(UP.probe(self.t(port=port, strict=True))["ok"], "unless HTTP was asked for")

    def test_nothing_listening(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        result = UP.probe(self.t(port=port), timeout=2)
        self.assertFalse(result["ok"])
        self.assertTrue(result["error"])

    def far_end(self, speak):
        """A port whose every connection is handed to speak(conn, stop)."""
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(5)
        stop = threading.Event()
        self.addCleanup(stop.set)
        self.addCleanup(listener.close)

        def run(conn):
            try:
                speak(conn, stop)
            except OSError:
                pass
            finally:
                conn.close()

        def accept():
            while not stop.is_set():
                try:
                    conn, _ = listener.accept()
                except OSError:
                    return
                threading.Thread(target=run, args=(conn,), daemon=True).start()
        threading.Thread(target=accept, daemon=True).start()
        return listener.getsockname()[1]

    def ends_by_the_deadline(self, speak, scheme="http"):
        """http.client's timeout is per wait: a byte every 0.1 s kept a probe
        going for as long as the far end liked (one thread per round)."""
        port = self.far_end(speak)
        for strict in (False, True):
            with self.subTest(strict=strict):
                results = []
                started = time.monotonic()
                worker = threading.Thread(target=lambda: results.append(
                    UP.probe({**self.t(port=port, strict=strict), "scheme": scheme}, timeout=0.5)), daemon=True)
                worker.start()
                worker.join(5)
                self.assertFalse(worker.is_alive(), "the probe is still reading")
                self.assertLess(time.monotonic() - started, 2.0, "the deadline is 2 x 0.5 s")
                if strict:
                    self.assertEqual({"ok": False, "error": "no HTTP answer (no answer in time)"},
                                     {k: results[0][k] for k in ("ok", "error")})
                else:
                    self.assertTrue(results[0]["ok"], "it took the connection: an app that answers")

    def test_a_target_that_accepts_and_never_answers_ends_by_the_deadline(self):
        self.ends_by_the_deadline(lambda conn, stop: stop.wait(10))

    def test_a_target_that_trickles_its_status_line_ends_by_the_deadline(self):
        def speak(conn, stop):
            conn.recv(4096)
            for byte in b"HTTP/1.1 200 OK\r\n" * 50:
                if stop.wait(0.1):
                    return
                conn.sendall(bytes([byte]))
        self.ends_by_the_deadline(speak)

    def test_a_target_that_trickles_its_headers_ends_by_the_deadline(self):
        def speak(conn, stop):
            conn.recv(4096)
            conn.sendall(b"HTTP/1.1 200 OK\r\n")
            while not stop.wait(0.1):
                conn.sendall(b"X")
        self.ends_by_the_deadline(speak)

    def test_a_tls_target_that_trickles_its_handshake_ends_by_the_deadline(self):
        def speak(conn, stop):
            conn.recv(4096)                       # the ClientHello
            # A 16 KiB handshake record opening a ServerHello, then a byte at a time.
            conn.sendall(b"\x16\x03\x03\x40\x00" + b"\x02\x00\x3f\xfc")
            while not stop.wait(0.1):
                conn.sendall(b"\x00")
        self.ends_by_the_deadline(speak, scheme="https")


class RoundDeadlineTests(Base):
    def test_a_round_waits_once_for_all_its_stuck_checks_not_once_each(self):
        release = threading.Event()
        self.addCleanup(release.set)

        def stuck(t):
            release.wait(30)
            return OK
        apps = [app(name=f"app{n}", port=8000 + n) for n in range(4)]
        started = time.monotonic()
        with mock.patch.object(UP, "TIMEOUT", 0.05):
            UP.observe(apps, now=1000, ask=stuck)
        took = time.monotonic() - started
        self.assertLess(took, 2 * (0.05 * 2 + 2), "joined one after another")
        self.assertEqual({"the check did not finish"},
                         {row["last"]["error"] for row in UP._load().values()})

    def test_a_target_with_several_ports_is_waited_for_to_its_own_bound(self):
        """The round waited 2 x TIMEOUT + 2 s whatever the target: an app whose
        first ports were slow landed after it and was recorded down."""
        w = app(ports=[{"ip": "192.0.2.10", "port": p, "protocol": "TCP"} for p in (8001, 8002, 8003)])

        def ask(t):
            if t["port"] != 8003:
                time.sleep(0.18)
                return MISS
            return OK
        with mock.patch.object(UP, "TIMEOUT", 0.1), mock.patch.object(UP, "ROUND_SLACK", 0.05):
            UP.observe([w], now=1000, ask=ask)
        rep = UP.report(1000)["lab/sonarr"]
        self.assertEqual(("up", "http://192.0.2.10:8003/"), (rep["state"], rep["target"]))

    def test_the_ports_of_one_target_share_one_deadline(self):
        asked = []

        def ask(t):
            asked.append((t["port"], t["timeout"]))
            time.sleep(0.35)
            return MISS
        t = {"kind": "tcp", "host": "192.0.2.10", "port": 1, "scheme": "http", "path": "/", "timeout": 0.1,
             "asks": [{"host": "192.0.2.10", "port": p, "scheme": "http"} for p in (1, 2, 3)]}
        self.assertAlmostEqual(0.6, UP.target_bound(t))
        result = UP.ask_ports(t, ask)
        self.assertEqual([1, 2], [port for port, _ in asked], "no port is asked once the time is spent")
        self.assertTrue(all(timeout <= 0.1 for _, timeout in asked))
        self.assertEqual((False, 2), (result["ok"], result["port"]))


class HostHeaderTests(unittest.TestCase):
    def test_the_host_header_names_the_port_only_when_it_is_not_the_schemes_own(self):
        self.assertEqual("192.0.2.10", UP._host("192.0.2.10", 443, "https"))
        self.assertEqual("192.0.2.10", UP._host("192.0.2.10", 80, "http"))
        self.assertEqual("192.0.2.10:8443", UP._host("192.0.2.10", 8443, "https"))
        self.assertEqual("192.0.2.10:443", UP._host("192.0.2.10", 443, "http"))

    def test_a_probe_sends_it(self):
        seen = []

        class Echo(Handler):
            def do_GET(self):
                seen.append(self.headers.get("Host"))
                super().do_GET()
        server = http.server.HTTPServer(("127.0.0.1", 0), Echo)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        port = server.server_address[1]
        self.assertTrue(UP.probe({"kind": "http", "host": "127.0.0.1", "port": port, "scheme": "http",
                                  "path": "/", "strict": True})["ok"])
        self.assertEqual([f"127.0.0.1:{port}"], seen)


if __name__ == "__main__":
    unittest.main()


class PortalTests(unittest.TestCase):
    def test_app_links_take_the_answering_state_and_others_keep_their_connection_check(self):
        import server
        links = [{"id": "a", "url": "http://192.0.2.10:8989", "icon": "workload:lab/sonarr"},
                 {"id": "b", "url": "http://192.0.2.1", "icon": "builtin:router"},
                 {"id": "c", "url": "http://192.0.2.11", "icon": "workload:lab/stopped"}]
        tcp = {"a": {"up": True, "ms": 2}, "b": {"up": False, "ms": None}, "c": {"up": True, "ms": 3}}
        answers = {"lab/sonarr": {"state": "down", "since": 1000, "last": {"error": "HTTP 502", "code": 502, "ms": 40}},
                   "lab/stopped": {"state": "off", "why": "stopped", "last": {}}}
        with mock.patch.object(server.PORTAL, "status", return_value=tcp), \
                mock.patch.object(server.PORTAL, "stored", return_value=links), \
                mock.patch.object(server.UPTIME, "report", return_value=answers):
            status = server.OVERVIEW_ROUTES.portal_status()
        self.assertEqual((False, "down", "monitoring", "HTTP 502"),
                         (status["a"]["up"], status["a"]["state"], status["a"]["source"], status["a"]["error"]),
                         "the port takes connections, but the app answers 502")
        self.assertEqual({"up": False, "ms": None}, status["b"], "a router is not an app: its connection check stays")
        self.assertEqual({"up": True, "ms": 3}, status["c"], "an app not being checked keeps the connection check")
