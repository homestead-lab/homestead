import http.server
import os
import socket
import sys
import tempfile
import threading
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
        self.assertEqual(("uptime:lab/sonarr", "critical", "sonarr is not answering"),
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
            status = server.portal_status()
        self.assertEqual((False, "down", "answering", "HTTP 502"),
                         (status["a"]["up"], status["a"]["state"], status["a"]["source"], status["a"]["error"]),
                         "the port takes connections, but the app answers 502")
        self.assertEqual({"up": False, "ms": None}, status["b"], "a router is not an app: its connection check stays")
        self.assertEqual({"up": True, "ms": 3}, status["c"], "an app not being checked keeps the connection check")
