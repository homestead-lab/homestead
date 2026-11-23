"""/metrics (#736): who may read it, and what it says. The walk that every
module route outside the public paths asks for credentials sits beside the
route rule, in tests/test_module_routes.py (ModuleRoutesNeedCredentials)."""
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.environ.get("HOMESTEAD_TEST_SERVER_DIR", str(Path(__file__).resolve().parents[1] / "server")))
import server
import homestead_routes as MODULE_ROUTES
import homestead_metrics as METRICS

REAL_ACTIVE = METRICS.ALERTS.active


VIEWER = {"user": "viewer1", "role": "viewer", "remember": False, "started": 0}
SAMPLE = re.compile(r'^[a-zA-Z_:][a-zA-Z0-9_:]*'
                    r'(\{[a-zA-Z_][a-zA-Z0-9_]*="(?:[^"\\\n]|\\.)*"(?:,[a-zA-Z_][a-zA-Z0-9_]*="(?:[^"\\\n]|\\.)*")*\})?'
                    r' -?[0-9][0-9.e+-]*$')
LABEL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"')


def get(path, who=None, headers=None):
    """GET path through server.H as a request with this session or headers."""
    h = object.__new__(server.H)
    h.command, h.path, h.headers = "GET", path, dict(headers or {})
    h._send = mock.Mock(); h._who = mock.Mock(return_value=who); h._signin = mock.Mock()
    h.client_address = ("192.0.2.10", 40000)
    with mock.patch.object(server.CFACCESS, "enabled", return_value=False), \
         mock.patch.object(server, "_self_data_boot_pending", False), \
         mock.patch.object(server, "require_self_data_write", return_value=None):
        h.do_GET()
    return h._send.call_args.args


class Fixture(unittest.TestCase):
    """A cluster with two apps, two hosts and alerts that all carry names
    which must never reach /metrics."""

    def setUp(self):
        MODULE_ROUTES.forget("metrics"); self.addCleanup(MODULE_ROUTES.forget, "metrics")
        self.settings = {"metrics": {"enabled": True}}
        pods = [{"name": "homestead-secretpod", "node": "secret-node-1", "ready": True},
                {"name": "homestead-otherpod", "node": "secret-node-2", "ready": False}]
        for patch in (
                mock.patch.object(METRICS.SETTINGS_HELPERS, "get_app_settings", lambda: self.settings),
                mock.patch.object(METRICS.READYZ, "state", return_value={
                    "ready": True, "checks": {"kube_api": True, "data_writable": True}, "kube_api_seconds": 0.012}),
                mock.patch.object(METRICS.SELF_HEALTH, "replicas_now", return_value={"desired": 2, "pods": pods}),
                mock.patch.object(METRICS.SELF_HEALTH, "heartbeats", return_value={
                    "alerts": {"last_ok": 1700000000.0, "every": 30, "error": "", "error_at": 0, "leader_only": False}},
                    create=True),
                mock.patch.object(METRICS.LEADER, "is_leader", return_value=True),
                mock.patch.object(METRICS.OPS, "snapshot", return_value=[
                    {"status": "running", "kind": "move", "ref": {"app": "secret-app"}},
                    {"status": "failed", "kind": "restore", "message": "secret-app failed"}]),
                mock.patch.object(METRICS.ALERTS, "active", return_value=[
                    {"severity": "critical", "subject": "secret-app", "announced": 7},
                    {"severity": "warning", "subject": "secret-node-1", "announced": 8},
                    {"severity": "info", "subject": "secret-vol", "announced": 9},
                    # Still in its hold: the bell does not show it, so neither does the count.
                    {"severity": "critical", "subject": "secret-held", "announced": 0}]),
                mock.patch.object(METRICS, "_nodes", return_value=[
                    {"name": "secret-node-1", "status": "Ready"}, {"name": "secret-node-2", "status": "NotReady"}]),
                mock.patch.object(METRICS, "_apps", return_value=[
                    {"ns": "secret-ns", "name": "secret-app", "ready": 1, "desired": 1},
                    {"ns": "secret-ns", "name": "secret-db", "ready": 0, "desired": 1},
                    {"ns": "secret-ns", "name": "secret-off", "ready": 0, "desired": 0}]),
                mock.patch.object(METRICS.BACKUPS, "backup_volumes", return_value=[
                    {"name": "pvc-secret", "last_backup_at": "2026-10-20T03:00:00Z"},
                    {"name": "pvc-secret-2", "last_backup_at": "2026-10-19T03:00:00Z"},
                    {"name": "pvc-never", "last_backup_at": ""}])):
            patch.start(); self.addCleanup(patch.stop)

    def samples(self, body):
        out = {}
        for line in body.splitlines():
            if line.startswith("#"):
                continue
            series, value = line.rsplit(" ", 1)
            out[series] = float(value)
        return out


class WhoMayRead(Fixture):
    def test_off_answers_404_after_sign_in(self):
        self.settings = {"metrics": {"enabled": False}}
        code, body = get("/metrics", VIEWER)[:2]
        self.assertEqual(404, code)
        self.assertIn("Settings", body["error"])

    def test_off_still_asks_for_credentials_first(self):
        self.settings = {"metrics": {"enabled": False}}
        self.assertEqual(401, get("/metrics")[0])

    def test_on_without_credentials_is_401(self):
        self.assertEqual(401, get("/metrics")[0])

    def test_on_a_viewer_session_reads_it(self):
        code, body, ctype = get("/metrics", VIEWER)
        self.assertEqual(200, code)
        self.assertEqual(METRICS.CTYPE, ctype)
        self.assertIn("homestead_ready 1", body)

    def test_on_a_valid_key_reads_it_as_prometheus_text(self):
        key = {"name": "prometheus", "scopes": ["read"]}
        with mock.patch.object(server.API_KEYS, "verify", return_value=key) as verify:
            code, body, ctype = get("/metrics", headers={"Authorization": "Bearer hsk_example"})
        self.assertEqual(200, code)
        self.assertEqual("text/plain; version=0.0.4; charset=utf-8", ctype)
        verify.assert_called_once()

    def test_a_bad_key_is_refused(self):
        with mock.patch.object(server.API_KEYS, "verify", side_effect=PermissionError("unknown API key")):
            self.assertEqual(401, get("/metrics", headers={"Authorization": "Bearer hsk_wrong"})[0])

    def test_a_key_still_works_nowhere_else_outside_v1(self):
        key = {"name": "prometheus", "scopes": ["read"]}
        with mock.patch.object(server.API_KEYS, "verify", return_value=key):
            for path in ("/api/nodes", "/metrics.txt", "/api/settings"):
                code, body = get(path, headers={"Authorization": "Bearer hsk_example"})[:2]
                self.assertEqual(401, code, path)
                self.assertIn("API keys work only", body["error"])


class KeysCanRead(unittest.TestCase):
    def test_every_key_scope_includes_read(self):
        """/metrics lets any valid key read it because every scope includes
        read. A scope added without read must not get /metrics silently."""
        import homestead_api_keys as KEYS
        for scope in KEYS.SCOPES:
            with self.subTest(scope=scope):
                self.assertIn("read", KEYS.effective([scope]))


class WhatItSays(Fixture):
    def body(self):
        return METRICS.build()

    def test_every_line_is_help_type_or_a_sample(self):
        body = self.body()
        self.assertTrue(body.endswith("\n"))
        helps, types = [], []
        for line in body.splitlines():
            if line.startswith("# HELP "):
                helps.append(line.split()[2])
            elif line.startswith("# TYPE "):
                types.append(line.split()[2])
                self.assertIn(line.split()[3], ("gauge", "counter"))
            else:
                self.assertRegex(line, SAMPLE)
        self.assertEqual(len(helps), len(set(helps)), "one HELP per family")
        self.assertEqual(helps, types)

    def test_the_numbers(self):
        s = self.samples(self.body())
        self.assertEqual(1, s["homestead_ready"])
        self.assertEqual(1, s['homestead_ready_check{check="kube_api"}'])
        self.assertEqual(0.012, s["homestead_kube_api_seconds"])
        self.assertEqual(1, s["homestead_leader"])
        self.assertEqual(2, s["homestead_replicas_desired"])
        self.assertEqual(1, s["homestead_replicas_ready"])
        self.assertEqual(1700000000, s['homestead_loop_last_ok_timestamp_seconds{loop="alerts"}'])
        self.assertEqual(0, s['homestead_loop_last_ok_timestamp_seconds{loop="history"}'])
        self.assertEqual(1, s['homestead_jobs{state="running"}'])
        self.assertEqual(1, s['homestead_jobs{state="failed"}'])
        self.assertEqual(0, s['homestead_jobs{state="queued"}'])
        self.assertEqual(1, s['homestead_alerts_open{severity="critical"}'])
        self.assertEqual(1, s['homestead_alerts_open{severity="warning"}'])
        self.assertEqual(1, s['homestead_nodes{state="ready"}'])
        self.assertEqual(1, s['homestead_nodes{state="not_ready"}'])
        self.assertEqual((1, 1, 1), tuple(s[f'homestead_apps{{state="{x}"}}'] for x in ("ready", "not_ready", "stopped")))
        self.assertEqual(1792465200, s["homestead_backup_last_success_timestamp_seconds"])
        self.assertTrue(all(s[f'homestead_metrics_source_up{{source="{x}"}}'] == 1 for x in METRICS.SOURCES))

    def test_one_series_per_loop_state(self):
        s = self.samples(self.body())
        states = {k: v for k, v in s.items() if k.startswith('homestead_loop_state{loop="alerts"')}
        self.assertEqual(len(METRICS.LOOP_STATES), len(states))
        self.assertEqual(1, sum(states.values()))

    def test_no_label_value_outside_the_fixed_sets(self):
        body = self.body()
        self.assertNotIn("secret", body)
        allowed = (set(METRICS.SELF_HEALTH.LOOP_WORDS) | set(METRICS.LOOP_STATES) | set(METRICS.JOB_STATES)
                   | set(METRICS.SOURCES) | {"kube_api", "data_writable", "warning", "critical", "ready", "not_ready",
                                             "stopped", METRICS.SELF_HEALTH.VERSION})
        for name, value in LABEL.findall(body):
            self.assertIn(value, allowed, f"{name}={value}")

    def test_a_source_that_fails_is_left_out_and_says_so(self):
        with mock.patch.object(METRICS.SELF_HEALTH, "replicas_now", side_effect=RuntimeError("secret-node-1 down")):
            s = self.samples(self.body())
        self.assertEqual(0, s['homestead_metrics_source_up{source="replicas"}'])
        self.assertNotIn("homestead_replicas_desired", s)
        self.assertEqual(1, s['homestead_metrics_source_up{source="jobs"}'])
        self.assertEqual(1, s["homestead_ready"])

    def test_metrics_never_asks_readyz_to_write(self):
        with mock.patch.object(METRICS.READYZ, "state", return_value={
                "ready": False, "checks": {"kube_api": True, "data_writable": None}, "kube_api_seconds": 0.01}) as state:
            s = self.samples(self.body())
        state.assert_called_once_with(probe_data=False)
        self.assertNotIn("homestead_ready", s, "readiness is unknown until /readyz has probed /data")
        self.assertEqual(1, s['homestead_ready_check{check="kube_api"}'])
        self.assertNotIn('homestead_ready_check{check="data_writable"}', s)

    def test_no_backups_leaves_the_timestamp_out(self):
        with mock.patch.object(METRICS.BACKUPS, "backup_volumes", return_value=[]):
            body = self.body()
        self.assertNotIn("homestead_backup_last_success_timestamp_seconds ", body)
        self.assertIn('homestead_metrics_source_up{source="backups"} 1', body)

    def test_built_once_per_10_seconds(self):
        with mock.patch.object(METRICS, "build", wraps=METRICS.build) as build:
            for _ in range(5):
                METRICS.answer(None)
        self.assertEqual(1, build.call_count)


class AlertsOpenCountsWhatTheBellShows(Fixture):
    """homestead_alerts_open counts the alerts the bell lists: raised ones.
    A condition still inside its hold is not raised yet; the real alert store
    is used here, not a list of rows."""

    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        for patch in (mock.patch.object(METRICS.ALERTS, "active", REAL_ACTIVE),
                      mock.patch.object(METRICS.ALERTS, "DATA_DIR", directory.name)):
            patch.start(); self.addCleanup(patch.stop)

    def look(self, at):
        METRICS.ALERTS.observe({"nodes": [{"key": "nodes:node-a", "category": "nodes", "severity": "critical",
                                           "title": "Host node-a is not ready", "body": ""}]}, now=at)

    def bell(self):
        return [a for a in METRICS.ALERTS.active() if a.get("announced", 0) > 0]

    def critical(self):
        return self.samples(METRICS.build())['homestead_alerts_open{severity="critical"}']

    def test_a_condition_in_its_hold_is_not_counted(self):
        self.look(1000)
        self.assertEqual([], self.bell())
        self.assertEqual(0, self.critical())

    def test_it_is_counted_once_raised(self):
        self.look(1000)
        self.look(1000 + METRICS.ALERTS.HOLD)
        self.assertEqual(1, len(self.bell()))
        self.assertEqual(1, self.critical())


class Setting(unittest.TestCase):
    def test_off_by_default(self):
        self.assertEqual({"enabled": False}, server.APP_SETTINGS.validate({})["metrics"])

    def test_on_or_off_only(self):
        self.assertEqual({"enabled": True}, server.APP_SETTINGS.validate({"metrics": {"enabled": True}})["metrics"])
        with self.assertRaisesRegex(ValueError, "metrics"):
            server.APP_SETTINGS.validate({"metrics": {"enabled": "yes"}})


if __name__ == "__main__":
    unittest.main()
