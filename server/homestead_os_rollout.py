"""Host OS updates across a k3s or RKE2 cluster: one host at a time, now or
in a weekly window, and who else may update the hosts meanwhile.

A rollout takes the Ready hosts in turn - the one Homestead's leader runs on
last - and for each: refreshes its package lists, installs what is waiting
(homestead_host_os, detached on the host), and, when an update asks for a
restart and the settings allow one, restarts it the way Host actions does:
the reviewed plan, cordon, drain through disruption budgets, reboot, and a
wait for the new boot and for the host's Longhorn volumes to be healthy
(homestead_power) - then uncordons it, unless it was cordoned before. Only
then is the next host touched.

A host whose restart the review refuses - running VMs, the cluster's only
etcd member, a volume with its only copy there (unless the settings accept
that) - keeps its updates and is reported as needing a restart; the rollout
goes on. A failed install stops it: something is wrong with that host's
packages, and the next host would likely fail the same way.

The engine is a state machine kept in /data and advanced by the leader, so
a rollout survives Homestead itself being drained off a host: a new leader
picks it up where it was. A restart interrupted that way - Homestead evicted
before it could send the reboot - is tried once more.

Ubuntu's own automatic updates (unattended-upgrades) run on their own timer,
and with Automatic-Reboot set restart hosts on their own. Homestead holds
them off - a file in /etc/apt/apt.conf.d switching them off, removed again
to let them go - on every host while a rollout runs, and for good when the
settings say Homestead manages updates.
"""
import datetime
import json
import os
import time
import urllib.error

import homestead_shared as SHARED

kget = platform = host_os = None
reboot = None           # (node, allow_data_risk) -> operation id; raises with the review's reason
operation = None        # operation id -> the job-tray item, or None
set_cordon = None       # (node, bool)
power_job = None        # (node, since epoch) -> id of a power job for it not failed, or ""
own_node = lambda: ""
announce = lambda rollout: None   # rollout -> its job-tray entry
DATA_DIR = "/data"
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DEFAULTS = {"schedule": {"enabled": False, "days": ["sun"], "hour": 3, "tz": "", "offset_min": 0},
            "reboot": "when-needed", "single_copy": False, "manage": "ubuntu"}
INSTALL_LIMIT = 2 * 3600
RESTART_LIMIT = 45 * 60
# The hosts restarted before this one leave their copies of volumes rebuilding
# for a while; meanwhile this host can hold a volume's only healthy copy.
COPIES_WAIT = 15 * 60
# An API server busy with a host rejoining answers 429 or 5xx for a while.
TRANSIENT_WAIT = 10 * 60
_lock = SHARED.SharedLock("os-rollout")


def bind(_kget, _platform, _host_os, _reboot, _operation, _set_cordon, _own_node, data_dir="/data", _announce=None,
         _power_job=None):
    global kget, platform, host_os, reboot, operation, set_cordon, own_node, DATA_DIR, announce, power_job
    kget, platform, host_os, reboot, operation, set_cordon = _kget, _platform, _host_os, _reboot, _operation, _set_cordon
    own_node, DATA_DIR = _own_node, data_dir
    announce = _announce or (lambda rollout: None)
    power_job = _power_job


def _transient(error):
    """The API server busy or briefly away - not a review's refusal."""
    if isinstance(error, urllib.error.HTTPError):
        return error.code == 429 or error.code >= 500
    return isinstance(error, (urllib.error.URLError, TimeoutError, ConnectionError))


def _path():
    return os.path.join(DATA_DIR, "os-rollout.json")


def _load():
    try:
        with open(_path(), encoding="utf-8") as handle:
            state = json.load(handle)
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(state):
    SHARED.write_json(_path(), state, indent=1, sort_keys=True)


def settings(state=None):
    saved = (state if state is not None else _load()).get("settings") or {}
    out = {**DEFAULTS, **{k: v for k, v in saved.items() if k in DEFAULTS}}
    out["schedule"] = {**DEFAULTS["schedule"], **(saved.get("schedule") or {})}
    return out


def save_settings(cfg):
    """Check and keep the settings. Returns them as kept."""
    cfg = cfg or {}
    schedule = cfg.get("schedule") or {}
    days = [d for d in schedule.get("days") or [] if d in DAYS]
    try:
        hour = int(schedule.get("hour", 3))
        offset = int(schedule.get("offset_min", 0))
    except (TypeError, ValueError):
        raise ValueError("the hour is 0 to 23") from None
    if not 0 <= hour <= 23:
        raise ValueError("the hour is 0 to 23")
    if schedule.get("enabled") and not days:
        raise ValueError("choose at least one day for the window")
    if cfg.get("reboot", "when-needed") not in ("when-needed", "never"):
        raise ValueError("restart is when-needed or never")
    if cfg.get("manage", "ubuntu") not in ("homestead", "ubuntu"):
        raise ValueError("updates are managed by homestead or ubuntu")
    kept = {"schedule": {"enabled": bool(schedule.get("enabled")), "days": days or ["sun"], "hour": hour,
                         "tz": str(schedule.get("tz") or "")[:64], "offset_min": max(-900, min(900, offset))},
            "reboot": cfg.get("reboot", "when-needed"), "single_copy": bool(cfg.get("single_copy")),
            "manage": cfg.get("manage", "ubuntu")}
    with _lock:
        state = _load()
        state["settings"] = kept
        _save(state)
    return kept


def local_now(schedule, now=None):
    """The window's own clock: its time zone where this Python knows it, else
    the offset the browser gave when the window was saved."""
    now = datetime.datetime.fromtimestamp(now or time.time(), datetime.timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        if schedule.get("tz"):
            return now.astimezone(ZoneInfo(schedule["tz"]))
    except Exception:
        pass
    return now + datetime.timedelta(minutes=int(schedule.get("offset_min") or 0))


def due(state, now=None):
    """Whether the weekly window is open now and has not run in it yet."""
    schedule = settings(state)["schedule"]
    if not schedule["enabled"]:
        return False
    local = local_now(schedule, now)
    if DAYS[local.weekday()] not in schedule["days"] or local.hour != schedule["hour"]:
        return False
    return (now or time.time()) - (state.get("last_scheduled") or 0) > 20 * 3600


def _ready(item):
    return any(c.get("type") == "Ready" and c.get("status") == "True"
               for c in (item.get("status") or {}).get("conditions") or [])


def _nodes():
    return {n["metadata"]["name"]: n for n in (kget("/api/v1/nodes") or {}).get("items", [])}


def start(reason="asked", now=None):
    """Begin a rollout over every Ready host. Returns it."""
    p = platform(True) or {}
    if p.get("harvester") or p.get("distribution") not in ("k3s", "rke2"):
        raise ValueError("Homestead updates hosts' OS on k3s and RKE2; Harvester updates its own")
    with _lock:
        state = _load()
        current = state.get("rollout") or {}
        if current.get("status") == "running":
            raise ValueError("an update of every host is running already")
        own = own_node()
        names = sorted(name for name, item in _nodes().items() if _ready(item))
        # The host Homestead's leader runs on goes last: draining it moves Homestead.
        names.sort(key=lambda name: name == own)
        if not names:
            raise ValueError("no host is Ready")
        now = now or time.time()
        rollout = {"id": f"os-{int(now)}", "reason": reason, "started": int(now), "nodes": names, "index": 0,
                   "phase": "hold", "status": "running", "results": [], "message": "Holding automatic updates off",
                   "stop": False, "node": {}}
        state["rollout"] = rollout
        if reason == "schedule":
            state["last_scheduled"] = int(now)
        _save(state)
    try:
        rollout["operation"] = announce(rollout)
    except Exception as error:
        print(f"OS updates: the job tray entry was not made: {str(error)[:120]}", flush=True)
    return rollout


def stop():
    """Stop after the host being worked on: nothing is cut off halfway."""
    with _lock:
        state = _load()
        rollout = state.get("rollout") or {}
        if rollout.get("status") != "running":
            raise ValueError("no update of every host is running")
        rollout["stop"] = True
        rollout["message"] = "Stopping after " + (rollout.get("node", {}).get("name") or "this host")
        _save(state)
    return rollout


def _holds(state, want_hold, names):
    """Ubuntu's automatic updates held off (or let go) on each host that has them."""
    done = []
    for name in names:
        auto = (host_os.stored(name) or {}).get("auto") or {}
        if auto.get("tool") != "unattended-upgrades" or bool(auto.get("held")) == want_hold:
            continue
        try:
            host_os.set_hold(name, want_hold)
            done.append(name)
        except Exception as error:
            print(f"OS updates: {name}: could not {'hold' if want_hold else 'release'} unattended-upgrades: "
                  f"{str(error)[:120]}", flush=True)
    return done


def _result(rollout, ok, note, **extra):
    node = rollout.get("node") or {}
    rollout["results"].append({"node": node.get("name", ""), "ok": ok, "note": note,
                               "updates": node.get("updates", 0), "security": node.get("security", 0),
                               "restarted": node.get("restarted", False), **extra})
    rollout["index"] += 1
    rollout["phase"], rollout["node"] = "next", {}


def _uncordon(rollout):
    node = rollout.get("node") or {}
    if node.get("cordoned_before") is False and node.get("name"):
        set_cordon(node["name"], False)


def step(state, now=None):
    """One step of the running rollout. Returns True when it moved."""
    now = now or time.time()
    rollout = state.get("rollout") or {}
    if rollout.get("status") != "running":
        return False
    config = settings(state)
    phase, node = rollout["phase"], rollout.get("node") or {}
    if phase == "hold":
        _holds(state, True, rollout["nodes"])
        rollout["phase"], rollout["message"] = "next", "Starting with " + rollout["nodes"][0]
        return True
    if phase == "next":
        if rollout.get("stop") or rollout["index"] >= len(rollout["nodes"]):
            return _finish(state, rollout, now)
        name = rollout["nodes"][rollout["index"]]
        item = _nodes().get(name)
        rollout["node"] = {"name": name, "attempts": 0}
        if not item or not _ready(item):
            _result(rollout, False, "not Ready, so left alone")
            return True
        rollout["node"]["cordoned_before"] = bool((item.get("spec") or {}).get("unschedulable"))
        rollout["phase"], rollout["message"] = "check", f"{name}: refreshing its package lists"
        return True
    name = node["name"]
    if phase == "check":
        facts = host_os.read(name, refresh=True)
        node.update(updates=len(facts.get("updates") or []), security=facts.get("security", 0))
        if node["updates"]:
            host_os.upgrade_begin(name, facts)
            rollout["phase"], node["since"] = "installing", now
            rollout["message"] = f"{name}: installing {node['updates']} update{'s' if node['updates'] != 1 else ''}"
        elif facts.get("reboot") and config["reboot"] == "when-needed":
            rollout["phase"], rollout["message"] = "restart", f"{name}: up to date, and needs a restart"
        else:
            _result(rollout, True, "up to date")
        return True
    if phase == "installing":
        state_now = host_os.upgrade_state(name)
        if state_now["running"]:
            if now - node.get("since", now) > INSTALL_LIMIT:
                _result(rollout, False, f"updates did not finish in two hours; see {host_os.LOG} there")
                rollout["stop"] = True
            else:
                rollout["message"] = f"{name}: {state_now.get('last') or 'installing updates'}"[:200]
            return True
        if state_now.get("code") not in ("0", 0):
            _result(rollout, False, f"the package manager stopped with code {state_now.get('code')}; "
                                    f"its output is in {host_os.LOG} there")
            rollout["stop"], rollout["failed"] = True, True
            return True
        facts = host_os.read(name)
        if facts.get("reboot") and config["reboot"] == "when-needed":
            rollout["phase"], rollout["message"] = "restart", f"{name}: updates installed; restarting it"
        else:
            _result(rollout, True, "updates installed" + ("; needs a restart" if facts.get("reboot") else ""))
        return True
    if phase == "restart":
        # The restart runs its drain from here, and Homestead itself may be
        # drained off the host mid-way. The job it started carries on under
        # another replica, so a new leader takes that job up rather than
        # asking for a second restart, which the cordoned host would refuse.
        existing = power_job(name, rollout.get("started", 0)) if power_job else ""
        if existing:
            node["op"] = existing
            rollout["phase"], node["since"] = "restarting", now
            rollout["message"] = f"{name}: draining and restarting"
            return True
        node["attempts"] = node.get("attempts", 0) + 1
        try:
            node["op"] = reboot(name, config["single_copy"])
        except Exception as error:
            if _transient(error) and now - node.setdefault("busy_since", now) < TRANSIENT_WAIT:
                node["attempts"] -= 1
                rollout["message"] = f"{name}: the Kubernetes API is busy ({str(error)[:60]}); trying again"
                return True
            if "only healthy copy" in str(error) and now - node.setdefault("copies_since", now) < COPIES_WAIT:
                # Copies on the hosts restarted before are still rebuilding:
                # wait for them rather than leave this host unrestarted.
                node["attempts"] -= 1
                rollout["message"] = f"{name}: waiting for volume copies to finish rebuilding before restarting it"
                return True
            # Refused by the review, or stopped before power was sent: the
            # host keeps its updates and waits for someone.
            _uncordon(rollout)
            _result(rollout, True, f"updates installed; restart not done: {str(error)[:220]}", restart_needed=True)
            return True
        rollout["phase"], node["since"] = "restarting", now
        rollout["message"] = f"{name}: draining and restarting"
        return True
    if phase == "restarting":
        item = operation(node.get("op")) or {}
        status = item.get("status")
        if status == "succeeded":
            _uncordon(rollout)
            node["restarted"] = True
            _result(rollout, True, "updates installed and restarted")
            return True
        if status in ("failed", "cancelled") or not item:
            message = item.get("message") or "its restart job is gone"
            if "power was not sent" in message.lower() or "stopped before power was sent" in message.lower():
                if node.get("attempts", 0) < 2:
                    # Homestead itself was likely drained off it mid-way.
                    rollout["phase"], rollout["message"] = "restart", f"{name}: restarting again ({message[:80]})"
                    return True
            _result(rollout, False, f"restart did not complete: {message[:220]}")
            rollout["stop"], rollout["failed"] = True, True
            return True
        if now - node.get("since", now) > RESTART_LIMIT:
            _result(rollout, False, "restart not confirmed in 45 minutes; it is left cordoned")
            rollout["stop"], rollout["failed"] = True, True
            return True
        rollout["message"] = f"{name}: {item.get('message') or 'restarting'}"[:200]
        return False
    return False


def _finish(state, rollout, now):
    if settings(state)["manage"] != "homestead":
        _holds(state, False, rollout["nodes"])
    results = rollout["results"]
    restarted = sum(1 for r in results if r.get("restarted"))
    waiting = [r for r in results if r.get("restart_needed")]
    rollout["status"] = "failed" if rollout.get("failed") else "stopped" if rollout.get("stop") and \
        rollout["index"] < len(rollout["nodes"]) else "succeeded"
    rollout["finished"] = int(now)
    words = [f"{len(results)} host{'s' if len(results) != 1 else ''} done", f"{restarted} restarted"]
    if waiting:
        # Why the first was not restarted - the review's reason - so the job
        # says what to fix, not only who waits.
        why = waiting[0]["note"].split("restart not done: ", 1)[-1]
        words.append("needing a restart: " + ", ".join(r["node"] for r in waiting) + f" ({waiting[0]['node']}: {why})")
    failed = [r for r in results if not r["ok"]]
    if failed:
        words.append(f"{failed[-1]['node']}: {failed[-1]['note']}")
    rollout["message"] = "; ".join(words)
    rollout["phase"] = "done"
    state["last_rollout"] = {k: rollout.get(k) for k in ("id", "status", "finished", "message", "reason")}
    return True


def tick(now=None):
    """Keep holds as the settings say, start a due window, move a rollout on."""
    p = platform() or {}
    if p.get("harvester") or p.get("distribution") not in ("k3s", "rke2"):
        return None
    with _lock:
        state = _load()
        running = (state.get("rollout") or {}).get("status") == "running"
        config = settings(state)
        if not running:
            ready = [name for name, item in _nodes().items() if _ready(item)]
            _holds(state, config["manage"] == "homestead", ready)
        _save(state)
    if not running and due(state, now):
        try:
            start("schedule", now)
        except ValueError as error:
            print(f"OS updates: the window opened but {error}", flush=True)
    moved = False
    for _ in range(4):
        with _lock:
            state = _load()
            try:
                advanced = step(state, now)
            except Exception as error:
                rollout = state.get("rollout") or {}
                rollout["message"] = f"waiting: {str(error)[:200]}"
                advanced = False
            _save(state)
        moved = moved or advanced
        if not advanced:
            break
    return (state.get("rollout") or {}) if moved else None


def status(item):
    """The job-tray line for a rollout: (status, progress, message)."""
    rollout = _load().get("rollout") or {}
    if rollout.get("id") != (item.get("ref") or {}).get("rollout"):
        last = _load().get("last_rollout") or {}
        if last.get("id") == (item.get("ref") or {}).get("rollout"):
            return last.get("status") or "succeeded", 100, last.get("message") or ""
        return "failed", item.get("progress", 0), "this rollout's record is gone"
    total = max(1, len(rollout.get("nodes") or []))
    progress = min(99, int(rollout.get("index", 0) * 100 / total)) if rollout.get("status") == "running" else 100
    return rollout.get("status", "running"), progress, rollout.get("message", "")


def report():
    state = _load()
    p = platform(True) or {}
    return {"applies": not p.get("harvester") and p.get("distribution") in ("k3s", "rke2"),
            "settings": settings(state), "rollout": state.get("rollout"), "last": state.get("last_rollout"),
            **{k: v for k, v in host_os.report().items() if k in ("hosts", "every_s", "checking")}}


# Its routes and who may use them (homestead_routes.py).
ROUTES = {
    ("GET", "/api/os-updates"): ("viewer", lambda request: report()),
}
