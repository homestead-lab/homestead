"""/metrics (#736): Homestead's own health for Prometheus, Grafana or an
uptime checker, in the Prometheus text format (0.0.4), written by hand.

Off until an admin turns it on (settings.metrics.enabled; Settings ->
Homestead). On, it needs what any other read needs: a signed-in session, or
an API key sent as `Authorization: Bearer <key>` (server.py's key guard lets
keys reach /metrics as well as /api/v1 and /mcp). /metrics needs the `read`
scope, which every key has: every scope includes it
(homestead_api_keys.IMPLIES; tests/test_metrics.py pins that). Off,
it answers 404 after that check, so someone without either learns nothing.

Each replica answers for itself (leader, its loops, its readiness); the
cluster figures are the same from any. The text is built at most every 10 s,
each source in its own try: one that fails is left out and
homestead_metrics_source_up says so, so /metrics never fails on a cluster
problem. Labels come only from fixed sets - loop names, states, severities,
check names - never an app, host, namespace or volume name.
"""
import datetime
import time

import homestead_alerts as ALERTS
import homestead_cache as CACHE
import homestead_heartbeats as HEARTBEATS
import homestead_leader as LEADER
import homestead_longhorn_backups as BACKUPS
import homestead_nodes as NODES
import homestead_operations as OPS
import homestead_readyz as READYZ
import homestead_routes as MODULE_ROUTES
import homestead_self_health as SELF_HEALTH
import homestead_settings_helpers as SETTINGS_HELPERS
import homestead_workloads as WORKLOADS

CTYPE = "text/plain; version=0.0.4; charset=utf-8"
EVERY = 10
OFF = "Metrics are off. An admin can turn them on in Settings → Homestead."
SOURCES = ("kube_api", "replicas", "loops", "jobs", "alerts", "nodes", "apps", "backups")
LOOP_STATES = ("ok", "late", "failing", "starting", "standby")
JOB_STATES = ("queued", "running", "cancelling", "succeeded", "failed", "cancelled")
SEVERITIES = ("warning", "critical")

HELP = {
    "homestead_build_info": "Homestead's version, as a label; always 1.",
    "homestead_ready": "1 when /readyz answers ready.",
    "homestead_ready_check": "Each /readyz check: 1 passed, 0 failed.",
    "homestead_kube_api_seconds": "How long the Kubernetes API last took to answer GET /version.",
    "homestead_leader": "1 when this replica leads.",
    "homestead_replicas_desired": "Homestead replicas asked for.",
    "homestead_replicas_ready": "Homestead replicas ready.",
    "homestead_loop_last_ok_timestamp_seconds": "When each background task last finished well (0: never).",
    "homestead_loop_state": "Each background task's state: 1 for the state it is in.",
    "homestead_jobs": "Jobs Homestead keeps, by state.",
    "homestead_alerts_open": "Open alerts, by severity.",
    "homestead_nodes": "Hosts, by state.",
    "homestead_apps": "Apps, by state.",
    "homestead_backup_last_success_timestamp_seconds": "When the newest backup of any volume finished.",
    "homestead_metrics_source_up": "1 when this source could be read for this answer.",
}


def enabled():
    return bool((SETTINGS_HELPERS.get_app_settings().get("metrics") or {}).get("enabled"))


def _nodes():
    return CACHE.cached("nodes", 5, NODES.get_nodes)


def _apps():
    return CACHE.cached("wl", 5, WORKLOADS.get_workloads)


def _label(value):
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _number(value):
    value = float(value)
    return str(int(value)) if value.is_integer() else repr(value)


def _kube_api():
    # The API check, and the last /data result from /readyz: /metrics never
    # writes to /data itself, so it is safe to read while the fence holds.
    # Before anything has asked /readyz, readiness is unknown and left out.
    state = READYZ.state(probe_data=False)
    known = {name: ok for name, ok in state["checks"].items() if ok is not None}
    out = [("homestead_ready", {}, int(state["ready"]))] if len(known) == len(state["checks"]) else []
    out += [("homestead_ready_check", {"check": name}, int(ok)) for name, ok in known.items()]
    if state["kube_api_seconds"] is not None:
        out.append(("homestead_kube_api_seconds", {}, round(state["kube_api_seconds"], 6)))
    return out


def _replicas():
    now = SELF_HEALTH.replicas_now()
    return [("homestead_replicas_desired", {}, int(now.get("desired") or 0)),
            ("homestead_replicas_ready", {}, sum(1 for pod in now.get("pods") or [] if pod.get("ready")))]


def _loops():
    leading = LEADER.is_leader()
    loops = SELF_HEALTH.loop_states(SELF_HEALTH.heartbeats(), {} if leading else HEARTBEATS.leader_beats(),
                                    leading, time.time())
    out = [("homestead_leader", {}, int(leading))]
    out += [("homestead_loop_last_ok_timestamp_seconds", {"loop": loop["name"]}, loop["last_ok"]) for loop in loops]
    out += [("homestead_loop_state", {"loop": loop["name"], "state": state}, int(loop["state"] == state))
            for loop in loops for state in LOOP_STATES]
    return out


def _count(name, label, states, values):
    values = list(values)
    return [(name, {label: state}, values.count(state)) for state in states]


def _jobs():
    return _count("homestead_jobs", "state", JOB_STATES, (job.get("status") for job in OPS.snapshot()))


def _alerts():
    # Raised alerts only, as the bell counts them (GET /api/alerts): a
    # condition still inside its hold has announced 0 and is not shown yet.
    return _count("homestead_alerts_open", "severity", SEVERITIES,
                  (row.get("severity") for row in ALERTS.active() if row.get("announced", 0) > 0))


def _node_counts():
    return _count("homestead_nodes", "state", ("ready", "not_ready"),
                  ("ready" if node.get("status") == "Ready" else "not_ready" for node in _nodes()))


def _app_state(app):
    if not app.get("desired"):
        return "stopped"
    return "ready" if (app.get("ready") or 0) >= app["desired"] else "not_ready"


def _app_counts():
    return _count("homestead_apps", "state", ("ready", "not_ready", "stopped"), map(_app_state, _apps()))


def _when(text):
    return datetime.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def _backups():
    times = [_when(row["last_backup_at"]) for row in BACKUPS.backup_volumes() if row.get("last_backup_at")]
    return [("homestead_backup_last_success_timestamp_seconds", {}, int(max(times)))] if times else []


READERS = {"kube_api": _kube_api, "replicas": _replicas, "loops": _loops, "jobs": _jobs,
           "alerts": _alerts, "nodes": _node_counts, "apps": _app_counts, "backups": _backups}


def build():
    """The whole answer, as text."""
    samples = [("homestead_build_info", {"version": SELF_HEALTH.VERSION}, 1)]
    up = []
    for source in SOURCES:
        try:
            samples += READERS[source]()
            up.append(("homestead_metrics_source_up", {"source": source}, 1))
        except Exception:
            up.append(("homestead_metrics_source_up", {"source": source}, 0))
    samples += up
    lines = []
    for family in HELP:
        rows = [row for row in samples if row[0] == family]
        if not rows:
            continue
        lines += [f"# HELP {family} {HELP[family]}", f"# TYPE {family} gauge"]
        for name, labels, value in rows:
            shown = ",".join(f'{key}="{_label(val)}"' for key, val in labels.items())
            lines.append(f"{name}{{{shown}}} {_number(value)}" if shown else f"{name} {_number(value)}")
    return "\n".join(lines) + "\n"


def answer(request):
    if not enabled():
        return MODULE_ROUTES.Reply(404, {"error": OFF})
    return MODULE_ROUTES.Raw(MODULE_ROUTES.cached("metrics", EVERY, lambda: build()), CTYPE)


ROUTES = {("GET", "/metrics"): ("viewer", answer)}
