"""Host OS updates across a k3s or RKE2 cluster: one host at a time, now or
in a weekly window, and who else may update the hosts meanwhile.

A rollout takes the Ready hosts in turn, or the ones a person chose - the one
Homestead's leader runs on last - and for each: refreshes its package lists, installs what is waiting
(homestead_host_os, detached on the host), and, when an update asks for a
restart and the settings allow one, restarts it the way Host actions does:
the reviewed plan, cordon, drain through disruption budgets, reboot, and a
wait for the new boot and for the host's Longhorn volumes to be healthy
(homestead_power) - then uncordons it, unless it was cordoned before. Only
then is the next host touched.

A host whose restart the review refuses - running VMs, the cluster's only
etcd member, a volume with its only copy there (unless the settings accept
that), anything that would keep it from draining - keeps its updates and is reported as needing a restart; the rollout
goes on. A failed install stops it: something is wrong with that host's
packages, and the next host would likely fail the same way.

The engine is a state machine kept in /data and advanced by the leader, so
a rollout survives Homestead itself being drained off a host: a new leader
picks it up where it was. A restart interrupted that way - Homestead evicted
before it could send the reboot - is tried once more.

Ubuntu's own automatic updates (unattended-upgrades) run on their own timer,
and with Automatic-Reboot set restart hosts on their own. Homestead holds
them off - a file in /etc/apt/apt.conf.d switching them off, removed again
to let them go - on every Ready host while a rollout runs, the ones outside it
too, and for good when the settings say Homestead manages updates.
"""
import datetime
import hashlib
import json
import os
import time
import urllib.error

import homestead_alerts as ALERTS
import homestead_drain_blockers as DRAIN_BLOCKERS
import homestead_history as HISTORY
import homestead_operations as OPS
import homestead_routes as ROUTER
import homestead_shared as SHARED
import homestead_storage_conflicts as STORAGE_CONFLICTS

kget = platform = host_os = None
reboot = None           # (node, allow_data_risk) -> operation id; raises with the review's reason
operation = None        # operation id -> the job-tray item, or None
set_cordon = None       # (node, bool)
power_job = None        # (node, since epoch) -> id of a power job for it not failed, or ""
restart_check = None    # (node, allow_data_risk) -> (what the review would refuse with today or "", may it clear by itself)
own_node = lambda: ""
announce = lambda rollout: None   # rollout -> its job-tray entry
DATA_DIR = "/data"
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DEFAULTS = {"schedule": {"enabled": False, "days": ["sun"], "hour": 3, "tz": "", "offset_min": 0},
            "reboot": "when-needed", "single_copy": False, "manage": "ubuntu"}
INSTALL_LIMIT = 2 * 3600
RESTART_LIMIT = 45 * 60
# A step that keeps raising (a host gone or NotReady, its OS read cut short)
# ends the rollout after this long, as a job's resolver would (homestead_job_errors).
ERROR_LIMIT = 30 * 60
# An error this long after the one before starts a new streak (Homestead was down).
ERROR_GAP = 5 * 60
# The hosts restarted before this one leave their copies of volumes rebuilding
# for a while; meanwhile this host can hold a volume's only healthy copy. A drain
# check's failed read and what Longhorn clears by itself get the same wait.
COPIES_WAIT = 15 * 60
# An API server busy with a host rejoining answers 429 or 5xx for a while.
TRANSIENT_WAIT = 10 * 60
# A hold that failed is tried again this long after that try ended. A try can take
# well over a minute (hostrun: up to 60 s for its helper, 45 s for the script, up to
# 90 s more on a refused exec), as can reading a host not read yet, so tries run
# outside the rollout's lock and at most one host fails per step (_hold_round).
HOLD_RETRY = 60
# Failing this long, a hold is news: the dialog says so and an alert goes out.
HOLD_NOTICE = 3 * 60
# A weekly window something is in the way of looks again this often while its
# hour lasts. Five minutes: such jobs take minutes or more, and each look is a
# read of the job store, a save and a line in the log.
WINDOW_RETRY = 5 * 60
# A refused restart's note names what refused it, every drain blocker included.
NOTE_LIMIT = 2000
clock = time.monotonic
_lock = SHARED.SharedLock("os-rollout")


def bind(_kget, _platform, _host_os, _reboot, _operation, _set_cordon, _own_node, data_dir="/data", _announce=None,
         _power_job=None, _restart_check=None):
    global kget, platform, host_os, reboot, operation, set_cordon, own_node, DATA_DIR, announce, power_job, restart_check
    kget, platform, host_os, reboot, operation, set_cordon = _kget, _platform, _host_os, _reboot, _operation, _set_cordon
    own_node, DATA_DIR = _own_node, data_dir
    announce = _announce or (lambda rollout: None)
    power_job, restart_check = _power_job, _restart_check


def _transient(error):
    """The API server busy or briefly away - not a review's refusal."""
    if isinstance(error, urllib.error.HTTPError):
        return error.code == 429 or error.code >= 500
    return isinstance(error, (urllib.error.URLError, TimeoutError, ConnectionError))


def _path():
    return os.path.join(DATA_DIR, "os-rollout.json")


def _load():
    return SHARED.read_json(_path(), {})


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


RUNNING = "An OS update is already running. It must end first."
# Jobs across the cluster that a smaller run waits for, as they restart or stop hosts.
CLUSTER_JOBS = ("cluster-shutdown", "platform-upgrade", "harvester-upgrade", "k3s-cluster")


def _rebalancing():
    """Whether Balance hosts is moving containers: an update window's
    rollout drains and reboots the hosts it is moving them to, so the
    scheduled one waits for it. An unreadable job list does not hold it up."""
    try:
        return any(i.get("kind") == "container-rebalance" and i.get("status") not in OPS.TERMINAL for i in OPS.stored())
    except Exception:
        return False


def _busy_cluster():
    """Why no chosen host can start: a job across the cluster, or Balance hosts
    moving containers onto the hosts this would drain. "" when none. An
    unreadable job list raises: it is no proof that nothing runs."""
    for item in OPS.stored():
        if item.get("kind") in CLUSTER_JOBS and item.get("status") not in OPS.TERMINAL:
            return f"{item.get('title') or item.get('kind')} is running. It must end first."
    return "Containers are being rebalanced. That must end first." if _rebalancing() else ""


def _in_rows(rows, name):
    """Whether a job's list of hosts names this one: names, or rows with a node."""
    return isinstance(rows, list) and any(row == name or isinstance(row, dict) and row.get("node") == name for row in rows)


def _busy_host(name):
    """Why this host can't start: a job not ended that names it, of any kind, so
    that a kind added later is refused rather than run into. "" when none. The
    install would otherwise fail inside the run and wait out its error limit."""
    for item in OPS.stored():
        ref = item.get("ref") or {}
        resource = item.get("resource") or {}
        if (item.get("kind") != "os-rollout" and item.get("status") not in OPS.TERMINAL
                and (ref.get("node") == name or _in_rows(ref.get("nodes"), name)
                     or (resource.get("kind") == "Node" and resource.get("name") == name))):
            return f"{name} has a job running: {item.get('title') or item.get('kind')}"
    return ""


def start(reason="asked", now=None, only=None, restart=None):
    """Begin a rollout over every Ready host, or the hosts in only, all or
    none. restart ("when-needed" or "never") is this run's own answer, over the
    settings. Returns it. A run whose job can't be made does not start: what was
    saved goes back, and as the lock is held all the while nothing has moved."""
    p = platform(True) or {}
    if p.get("harvester") or p.get("distribution") not in ("k3s", "rke2"):
        raise ValueError("Homestead updates the OS of k3s and RKE2 hosts. Harvester updates its own.")
    if restart not in (None, "when-needed", "never"):
        raise ValueError("restart is when-needed or never")
    with _lock:
        state = _load()
        current = state.get("rollout") or {}
        if current.get("status") == "running":
            raise ValueError(RUNNING)
        own = own_node()
        ready = sorted(name for name, item in _nodes().items() if _ready(item))
        names = ready
        if only is not None:
            names = [name for name in ready if name in only]
            missing = [name for name in only if name not in ready]
            if missing:
                raise ValueError("Not Ready, so left alone: " + ", ".join(missing))
        # The window too: _in_the_way does not cover host jobs, and _open_window turns this refusal into a skip and a retry.
        why = _busy_cluster() or next(filter(None, (_busy_host(name) for name in names)), "")
        if why:
            raise ValueError(why)
        # The host Homestead's leader runs on goes last: draining it moves Homestead.
        names.sort(key=lambda name: name == own)
        if not names:
            raise ValueError("no host is Ready")
        now = now or time.time()
        rollout = {"id": f"os-{int(now)}", "reason": reason, "started": int(now), "nodes": names, "index": 0,
                   "phase": "hold", "status": "running", "results": [], "message": "Holding automatic updates off",
                   "stop": False, "node": {},
                   # Not only the run's hosts: another host's own automatic update
                   # must not restart it while this one is drained.
                   "held_hosts": ready}
        if restart:
            rollout["restart"] = restart
        before = {key: state[key] for key in ("rollout", "last_scheduled", "window_skip") if key in state}
        state["rollout"] = rollout
        if reason != "asked-hosts":
            state.pop("window_skip", None)    # every host is getting what the window would have done
        if reason == "schedule":
            state["last_scheduled"] = int(now)
        _save(state)
        try:
            rollout["operation"] = announce(rollout)
        except Exception as error:
            for key in ("rollout", "last_scheduled", "window_skip"):
                state.pop(key, None)
            state.update(before)
            _save(state)
            raise ValueError(str(error)[:300]) from error
    return rollout


def stop():
    """Stop after the host being worked on: nothing is cut off halfway. A host
    waiting for its restart ends at once, not restarted (step)."""
    with _lock:
        state = _load()
        rollout = state.get("rollout") or {}
        if rollout.get("status") != "running":
            raise ValueError("No OS update is running")
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


def _hold_round(now):
    """Before every step of a run: automatic updates held on every Ready host, so
    one that comes back at any point is caught, and a hold that failed or a host
    not read yet is tried again. A host already held costs no command. A run goes
    on without a hold: a hold keeps a host from restarting itself, which is a risk
    to name, not a reason to stop the host in hand.

    The tries are outside the lock, so Stop, a start and Save settings don't wait
    on a host that doesn't answer. That is safe because tick is the one place that
    moves a run, one step after another on the leader, and what a try found is
    written back to a fresh read of the state, for the same run only. A hold that
    lands after its run ended (a leader that moved meanwhile) is let go by the next
    tick, as with any hold a run leaves. The first host to fail ends the round."""
    with _lock:
        rollout = _load().get("rollout") or {}
    if rollout.get("status") != "running":
        return
    try:
        ready = sorted(name for name, item in _nodes().items() if _ready(item))
    except Exception:
        return    # the node list is read again next step
    base = now or time.time()
    failed = {name: row for name, row in (rollout.get("hold_failed") or {}).items() if name in ready}
    # Those not failing first, so that a host that keeps failing doesn't keep them waiting.
    todo = [name for name in ready if name not in failed] + [name for name in ready if name in failed and base >= failed[name].get("retry_at", 0)]
    started, held, failure = clock(), [], None
    for name in todo:
        try:
            auto = (host_os.stored(name) or host_os.read(name) or {}).get("auto") or {}
            if auto.get("tool") == "unattended-upgrades" and not auto.get("held"):
                host_os.set_hold(name, True)
            held.append(name)
        except Exception as error:
            failure = (name, " ".join(str(error).split())[:200])
            print(f"OS updates: {name}: could not hold unattended-upgrades: {failure[1]}", flush=True)
            break
    spent = clock() - started
    with _lock:
        state = _load()
        current = state.get("rollout") or {}
        if current.get("id") != rollout.get("id") or current.get("status") != "running":
            return
        before = json.dumps([current.get("held_hosts"), current.get("hold_failed") or {}, current.get("error_last")], sort_keys=True)
        current["held_hosts"] = _held_hosts(current) + [name for name in ready if name not in _held_hosts(current)]
        failed = {name: row for name, row in (current.get("hold_failed") or {}).items() if name in ready and name not in held}
        if failure:
            row = failed.setdefault(failure[0], {"since": base})
            row.update(error=failure[1], retry_at=base + int(spent) + HOLD_RETRY)
        if isinstance(current.get("error_last"), (int, float)):
            # Time here is not time the step's errors stopped: an error after a slow
            # try is the same streak, not "Homestead was down" (ERROR_GAP).
            current["error_last"] += spent
        for name, row in failed.items():
            if base + spent - row["since"] >= HOLD_NOTICE and not row.get("noted"):
                row["noted"] = True
                _alert({"key": f"os-update-hold:{current.get('id')}:{name}", "category": "degraded", "severity": "degraded", "event": True,
                        "title": f"Automatic updates could not be held on {name} during an OS update",
                        "body": f"{row['error']}. Its automatic updates could restart it while another host is drained. Homestead keeps trying.",
                        "href": "/nodes"})
        current["hold_failed"] = failed
        if json.dumps([current.get("held_hosts"), failed, current.get("error_last")], sort_keys=True) != before:
            _save(state)


def _alert(fact):
    """An alert for a person. It never stops what raised it."""
    try:
        ALERTS.note(fact)
    except Exception as error:
        print(f"OS updates: the alert was not raised: {str(error)[:120]}", flush=True)


# A host that needs a restart, with restarts off (the setting, or this run's
# own answer): its note, and the reason the summary gives.
NEEDS_RESTART = "It needs a restart, which this run does not do."
NOT_RESTARTING = "this run does not restart hosts"


def _result(rollout, ok, note, **extra):
    node = rollout.get("node") or {}
    rollout["results"].append({"node": node.get("name", ""), "ok": ok, "note": note,
                               "updates": node.get("updates", 0), "security": node.get("security", 0),
                               "restarted": node.get("restarted", False), **extra})
    rollout["index"] += 1
    rollout["phase"], rollout["node"] = "next", {}


def _uncordon(rollout):
    node = rollout.get("node") or {}
    # A cordon a person's maintenance put on since isn't the rollout's to take
    # off, and one it can't tell about stays.
    if node.get("cordoned_before") is False and node.get("name") and _in_maintenance(node["name"]) is False:
        set_cordon(node["name"], False)


def _in_maintenance(name):
    """Whether a person has this host in maintenance; None when that can't be
    read, which is not the same as no."""
    try:
        return bool(HISTORY.declared_for(name, strict=True))
    except Exception as error:
        print(f"OS updates: {name}: its maintenance could not be read: {str(error)[:120]}", flush=True)
        return None


def current_host():
    """The host a running rollout is working on now, or "". """
    rollout = _load().get("rollout") or {}
    return (rollout.get("node") or {}).get("name") or "" if rollout.get("status") == "running" else ""


def _not_in_maintenance(name):
    """Whether this host's turn may go on. An unread history raises: _failing
    waits it out for ERROR_LIMIT, then ends the rollout and lets go of the holds."""
    declared = _in_maintenance(name)
    if declared is None:
        raise ValueError(f"{name}: whether it is in maintenance could not be read")
    return not declared


def step(state, now=None):
    """One step of the running rollout. Returns True when it moved."""
    now = now or time.time()
    rollout = state.get("rollout") or {}
    if rollout.get("status") != "running":
        return False
    config = settings(state)
    if rollout.get("restart"):
        config["reboot"] = rollout["restart"]
    phase, node = rollout["phase"], rollout.get("node") or {}
    if phase == "hold":
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
        if not _not_in_maintenance(name):
            _result(rollout, False, "in maintenance, so left alone")
            return True
        rollout["node"]["cordoned_before"] = bool((item.get("spec") or {}).get("unschedulable"))
        rollout["phase"], rollout["message"] = "check", f"{name}: refreshing its package lists"
        return True
    name = node["name"]
    if phase == "check":
        facts = host_os.read(name, refresh=True)
        node.update(updates=len(facts.get("updates") or []), security=facts.get("security", 0))
        if node["updates"]:
            host_os.upgrade_begin(name, facts, node)
            rollout["phase"], node["since"] = "installing", now
            rollout["message"] = f"{name}: installing {node['updates']} update{'s' if node['updates'] != 1 else ''}"
        elif facts.get("reboot") and config["reboot"] == "when-needed":
            rollout["phase"], rollout["message"] = "restart", f"{name}: up to date, and needs a restart"
        elif facts.get("reboot"):
            _result(rollout, True, "Up to date. " + NEEDS_RESTART, restart_needed=True)
        else:
            _result(rollout, True, "up to date")
        return True
    if phase == "installing":
        # A launch that timed out is read only by its own id: an older result
        # on the host must not move the rollout on to a restart.
        state_now = host_os.upgrade_state(name, **({"run": node.get("run", "")} if node.get("unconfirmed") else {}))
        if not state_now["running"]:
            host_os.upgrade_done(name, node)
        if state_now["running"]:
            if now - node.get("since", now) > INSTALL_LIMIT:
                _result(rollout, False, f"updates did not finish within two hours. See {host_os.LOG} there")
                rollout["stop"] = True
            elif node.get("unconfirmed") and not state_now.get("active"):
                # The log's last line may be an earlier install's.
                rollout["message"] = (f"{name} needs attention: the update launch did not finish within "
                                      f"{node['unconfirmed']:g} seconds, so Homestead cannot tell whether it started. "
                                      f"{name} stays held while Homestead looks for its result. If there is none within two "
                                      f"hours, the rollout stops. Check {host_os.LOG} on {name}.")
            else:
                rollout["message"] = f"{name}: {state_now.get('last') or 'installing updates'}"[:200]
            return True
        if state_now.get("code") not in ("0", 0):
            _result(rollout, False, f"the package manager stopped with code {state_now.get('code')}. "
                                    f"Its output is in {host_os.LOG} there")
            rollout["stop"], rollout["failed"] = True, True
            return True
        facts = host_os.read(name)
        if facts.get("reboot") and config["reboot"] == "when-needed":
            rollout["phase"], rollout["message"] = "restart", f"{name}: updates installed. Restarting it."
        elif facts.get("reboot"):
            _result(rollout, True, "Updates installed. " + NEEDS_RESTART, restart_needed=True)
        else:
            _result(rollout, True, "updates installed")
        return True
    if phase == "restart":
        # The restart runs its drain from here, and Homestead itself may be
        # drained off the host mid-way. The job it started carries on under
        # another replica, so a new leader takes that job up rather than
        # asking for a second restart, which the cordoned host would refuse.
        existing = power_job(name, rollout.get("started", 0)) if power_job else ""
        if existing:
            node["op"] = existing
            node.pop("waiting", None)
            node.pop("wait_why", None)
            rollout["phase"], node["since"] = "restarting", now
            rollout["message"] = f"{name}: draining and restarting"
            return True
        if rollout.get("stop") and node.get("waiting"):
            # Stop during a wait ends the run at once (Andreas, 5 Nov): nobody asked
            # for this host to restart later by itself, even if what held it clears.
            return _stopped_waiting(rollout, node.get("wait_why") or "")
        # A person may have declared maintenance since this host's turn began.
        if not _not_in_maintenance(name):
            _result(rollout, True, "in maintenance, so not restarted", restart_needed=True)
            return True
        node["attempts"] = node.get("attempts", 0) + 1
        try:
            node["op"] = reboot(name, config["single_copy"])
        except Exception as error:
            wait_why = str(error)[:NOTE_LIMIT]
            if _transient(error) and now - node.setdefault("busy_since", now) < TRANSIENT_WAIT:
                if rollout.get("stop"):
                    return _stopped_waiting(rollout, wait_why)
                node["attempts"] -= 1
                node["waiting"], node["wait_why"] = True, wait_why
                rollout["message"] = f"{name}: the Kubernetes API is busy ({str(error)[:60]}). Trying again."
                return True
            copies = "only healthy copy" in str(error)
            if isinstance(error, DRAIN_BLOCKERS.MayClear) and now - node.setdefault("copies_since", now) < COPIES_WAIT:
                if rollout.get("stop"):
                    return _stopped_waiting(rollout, wait_why)
                node["waiting"], node["wait_why"] = True, wait_why
                # Copies on the hosts restarted before are still rebuilding, or a
                # read or Longhorn may answer soon: wait rather than leave this
                # host for a person. Once a tick: the review is many reads.
                node["attempts"] -= 1
                rollout["message"] = (f"{name}: waiting for volume copies to finish rebuilding before restarting it" if copies
                                      else f"{name}: waiting up to 15 minutes before restarting it: {str(error)[:160]}")
                return False
            # Refused by the review, or stopped before power was sent: the
            # host keeps its updates and waits for someone.
            _uncordon(rollout)
            _result(rollout, True, f"updates installed, but the restart was not done: {str(error)[:NOTE_LIMIT]}", restart_needed=True)
            return True
        node.pop("waiting", None)
        node.pop("wait_why", None)
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
            # The power job's words for "nothing reached the host", old and new.
            if any(words in message.lower() for words in ("nothing was sent", "stopped before the restart or shutdown was sent",
                                                          "power was not sent", "stopped before power was sent")):
                if node.get("attempts", 0) < 2:
                    # Homestead itself was likely drained off it mid-way.
                    rollout["phase"], rollout["message"] = "restart", f"{name}: restarting again ({message[:80]})"
                    return True
            _result(rollout, False, f"restart did not complete: {message[:220]}")
            rollout["stop"], rollout["failed"] = True, True
            return True
        if now - node.get("since", now) > RESTART_LIMIT:
            _result(rollout, False, "the restart was not confirmed within 45 minutes. It is left cordoned")
            rollout["stop"], rollout["failed"] = True, True
            return True
        rollout["message"] = f"{name}: {item.get('message') or 'restarting'}"[:200]
        return False
    return False


def _stopped_waiting(rollout, why):
    """Stop arrived while a host waited for its restart: it keeps what it installed
    and waits for a person, and the run ends without restarting it."""
    _uncordon(rollout)
    rollout["stopped_waiting"] = True
    _result(rollout, True, f"updates installed, but the restart was not done. It stopped while waiting: {why}", restart_needed=True)
    return True


def _held_hosts(rollout):
    """The hosts this run holds. A run saved before it held them all has its own."""
    return rollout.get("held_hosts") or rollout["nodes"]


def _finish(state, rollout, now):
    if settings(state)["manage"] != "homestead":
        _holds(state, False, _held_hosts(rollout))
    results = rollout["results"]
    restarted = sum(1 for r in results if r.get("restarted"))
    waiting = [r for r in results if r.get("restart_needed")]
    rollout["status"] = "failed" if rollout.get("failed") else "stopped" if rollout.get("stop") and \
        (rollout["index"] < len(rollout["nodes"]) or rollout.get("stopped_waiting")) else "succeeded"
    rollout["finished"] = int(now)
    words = [f"{len(results)} host{'s' if len(results) != 1 else ''} done", f"{restarted} restarted"]
    if waiting:
        # Why the first was not restarted - the review's reason - so the job
        # says what to fix, not only who waits.
        note = waiting[0]["note"]
        why = NOT_RESTARTING if note.endswith(NEEDS_RESTART) else next(
            (note.split(marker, 1)[1] for marker in ("restart was not done: ", "restart was not done. ", "restart not done: ")
             if marker in note), note)
        named = ", ".join(r["node"] for r in waiting)
        words.append(f"needing a restart: {named} ({why if len(waiting) == 1 else waiting[0]['node'] + ': ' + why})")
    failed = [r for r in results if not r["ok"]]
    if failed:
        words.append(f"{failed[-1]['node']}: {failed[-1]['note']}")
    for name, row in sorted((rollout.get("hold_failed") or {}).items()):
        words.append(f"automatic updates could not be held on {name} ({row.get('error')})")
    rollout["message"] = "; ".join(words)
    rollout["phase"] = "done"
    state["last_rollout"] = {k: rollout.get(k) for k in ("id", "status", "finished", "message", "reason", "nodes")}
    return True


def _failing(state, error, now=None):
    """A step raised. Waited out for ERROR_LIMIT (INSTALL_LIMIT while a host
    installs); after that the host being
    updated is marked failed with the error and the rollout ends, which lets go
    of the hosts' automatic-update holds and lets a new rollout start. What
    may still be going on is left alone: a host still installing keeps its own
    hold, which lapses by itself, and a host being restarted stays cordoned."""
    now = now or time.time()
    rollout = state.get("rollout") or {}
    text = " ".join(str(error).split())[:200]
    since, last = rollout.get("error_since"), rollout.get("error_last", rollout.get("error_since"))
    if (not isinstance(since, (int, float)) or not isinstance(last, (int, float))
            or since > now or last > now or now - last > ERROR_GAP):
        since = now   # the first error, a clock behind, or Homestead was down: a new streak
    rollout["error_since"], rollout["error_last"] = since, now
    phase = rollout.get("phase")
    # A host installing gets its install time, as a lone host-os job does.
    limit = INSTALL_LIMIT if phase == "installing" else ERROR_LIMIT
    if now - since <= limit or rollout.get("status") != "running":
        rollout["message"] = f"waiting: {text}"
        return False
    left = (". Its updates may still be installing" if phase == "installing" else
            ". It is left cordoned" if phase in ("restart", "restarting") else "")
    _result(rollout, False, f"no answer for {limit // 60} min: {text}{left}")
    rollout["stop"], rollout["failed"] = True, True
    rollout.pop("error_since", None)
    rollout.pop("error_last", None)
    return _finish(state, rollout, now)


def _in_the_way():
    """The job that would refuse a rollout's job, named, or "". Asked without
    making anything, one job at a time so that the answer can name it, so a
    window can look every few minutes at the cost of one read."""
    conflicts = OPS.CONFLICTS.get("os-rollout")
    ref = {"rollout": "window"}
    for item in OPS.stored():
        if item.get("kind") == "container-rebalance" and item.get("status") not in OPS.TERMINAL:
            return f"{item.get('title') or item.get('kind')} is running"
        try:
            why = conflicts([item], "os-rollout", ref, None) if conflicts else ""
            if not why:
                STORAGE_CONFLICTS.require_clear([item], "os-rollout", ref)
        except ValueError as error:
            why = str(error)
        if why:
            return f"{item.get('title') or item.get('kind')} is in the way ({why})"
    return ""


def _open_window(state, now):
    """The weekly window is open: start, or say why not. A skip is kept beside
    Last run until a window or an update of every host runs, into later weeks
    too, and each window that is skipped raises one alert."""
    skip = state.get("window_skip") or {}
    if now - skip.get("last_try", 0) < WINDOW_RETRY:
        return
    blip = False
    try:
        why = _in_the_way()
    except Exception as error:
        why, blip = f"the job list could not be read: {str(error)[:200]}", True
    if not why:
        try:
            start("schedule", now)
            return
        except ValueError as error:
            why = str(error)
        except Exception as error:    # a read that failed, say: the next look may run
            why, blip = f"Homestead could not start it: {str(error)[:200]}", True
    print(f"OS updates: the window opened but {why}", flush=True)
    new_window = now - skip.get("first", 0) > 20 * 3600
    alerted = skip.get("alerted", False) and not new_window
    with _lock:
        fresh = _load()
        fresh["window_skip"] = {"why": why[:400], "first": int(now if new_window else skip["first"]), "last_try": int(now),
                                "blip": blip, "alerted": alerted or not blip}
        _save(fresh)
    if not alerted and not blip:
        _window_alert(fresh["window_skip"])


def _window_alert(skip):
    _alert({"key": f"os-update-window:{skip['first']}", "category": "degraded", "severity": "degraded", "event": True,
            "title": "The weekly OS update window is being skipped",
            "body": f"{skip['why']}. It looks again every {WINDOW_RETRY // 60} minutes until its hour ends, then in its next window.",
            "href": "/nodes"})


def _window_lost(state, now):
    """The window's hour ended on a read that kept failing: that was no blip after all."""
    skip = state.get("window_skip") or {}
    if not skip.get("blip") or due(state, now):
        return
    with _lock:
        fresh = _load()
        current = fresh.get("window_skip") or {}
        if not current.get("blip"):
            return
        current["blip"], alert, current["alerted"] = False, not current.get("alerted"), True
        _save(fresh)
    if alert:
        _window_alert(current)


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
        _open_window(state, now or time.time())
    elif not running:
        _window_lost(state, now)
    moved = False
    for _ in range(4):
        _hold_round(now)
        with _lock:
            state = _load()
            try:
                advanced = step(state, now)
                for key in ("error_since", "error_last"):
                    (state.get("rollout") or {}).pop(key, None)
            except Exception as error:
                advanced = _failing(state, error, now)
            _save(state)
        moved = moved or advanced
        if not advanced:
            break
    return (state.get("rollout") or {}) if moved else None


# A stopped run is over, so its job has to end: "stopped" is no job status (OPS.TERMINAL),
# and a job that doesn't end holds up what waits for os-rollout jobs. It stays "stopped"
# on the run itself, which the OS updates dialog shows.
JOB_STATUS = {"stopped": "cancelled"}


def status(item):
    """The job-tray line for a rollout: (status, progress, message)."""
    rollout = _load().get("rollout") or {}
    if rollout.get("id") != (item.get("ref") or {}).get("rollout"):
        last = _load().get("last_rollout") or {}
        if last.get("id") == (item.get("ref") or {}).get("rollout"):
            return JOB_STATUS.get(last.get("status"), last.get("status") or "succeeded"), 100, last.get("message") or ""
        return "failed", item.get("progress", 0), "this rollout's record is gone"
    total = max(1, len(rollout.get("nodes") or []))
    progress = min(99, int(rollout.get("index", 0) * 100 / total)) if rollout.get("status") == "running" else 100
    return JOB_STATUS.get(rollout.get("status"), rollout.get("status", "running")), progress, rollout.get("message", "")


def report():
    state = _load()
    p = platform(30) or {}
    return {"applies": not p.get("harvester") and p.get("distribution") in ("k3s", "rke2"),
            "settings": settings(state), "rollout": state.get("rollout"), "last": state.get("last_rollout"),
            "window_skip": state.get("window_skip") if settings(state)["schedule"]["enabled"] else None,
            **{k: v for k, v in host_os.report().items() if k in ("hosts", "every_s", "checking")}}


def title(rollout):
    """The job tray's name for a rollout."""
    names = rollout["nodes"]
    if rollout.get("reason") != "asked-hosts":
        return f"Update every host's OS ({len(names)} hosts)"
    return f"Update {names[0]}'s OS" if len(names) == 1 else f"Update the OS of {len(names)} hosts"


def stop_asked():
    rollout = stop()
    node = rollout.get("node") or {}
    return {"ok": True, "rollout": rollout,
            "detail": (f"Stopping now. {node.get('name')} does not restart by itself. It keeps its updates and waits to be restarted."
                       if node.get("waiting") else "Stopping once the host being updated is done")}


def _chosen(names):
    """A list of hosts as asked for: at least one, each named. Only an absent
    list means every host, never an empty or a broken one."""
    if not isinstance(names, list) or not names or not all(isinstance(n, str) and n for n in names):
        raise ValueError("Which hosts? Name at least one")
    return list(dict.fromkeys(names))


def preview(names):
    """What an update of these hosts would do, for its review, and what the
    restart checks would say today. Read only. Hosts in the order they would
    run. blocked says why it can't start now; restart_refusal only means that
    host would install and then wait for a person. The token follows what the
    person would be agreeing to, not the time anything was read."""
    names = _chosen(names)
    p = platform(30) or {}
    if p.get("harvester") or p.get("distribution") not in ("k3s", "rke2"):
        raise ValueError("Homestead updates the OS of k3s and RKE2 hosts. Harvester updates its own.")
    state = _load()
    config = settings(state)
    nodes, own = _nodes(), own_node()
    hosts = []
    for name in sorted(names, key=lambda name: (name == own, name)):
        facts = host_os.stored(name) or {}
        ready = name in nodes and _ready(nodes[name])
        refusal, may_clear, unread = "", False, ""
        if ready:
            try:
                if restart_check is None:
                    raise ValueError("the restart checks are not available")
                refusal, may_clear = restart_check(name, config["single_copy"])
            except Exception as error:
                unread = str(error)[:200]
        hosts.append({"node": name, "ready": ready, "updates": facts.get("updates") or [], "security": facts.get("security", 0),
                      "reboot": bool(facts.get("reboot")), "reboot_for": facts.get("reboot_for") or "", "at": facts.get("at"),
                      "homestead_here": name == own, "restart_refusal": refusal, "restart_may_clear": may_clear, "restart_unread": unread,
                      "busy": _busy_host(name)})
    missing = [h["node"] for h in hosts if not h["ready"]]
    blocked = (RUNNING if (state.get("rollout") or {}).get("status") == "running" else "") or _busy_cluster() \
        or ("Not Ready, so left alone: " + ", ".join(missing) if missing else "") \
        or next((h["busy"] for h in hosts if h["busy"]), "")
    # Not who answered: with two replicas, that and the order differ between requests.
    agreed = [[h["node"], sorted(u.get("name", "") for u in h["updates"]), h["security"], h["reboot"],
               h["restart_refusal"], h["restart_may_clear"], bool(h["restart_unread"]), h["busy"]] for h in sorted(hosts, key=lambda h: h["node"])]
    token = hashlib.sha256(json.dumps([agreed, config["reboot"], config["single_copy"], blocked]).encode()).hexdigest()
    return {"hosts": hosts, "restart_policy": config["reboot"], "single_copy": config["single_copy"], "blocked": blocked,
            "review_token": token}


def start_asked(body=None):
    """Every host when no list is named; otherwise the hosts a person reviewed:
    a review that no longer holds is refused (409) with a fresh one."""
    body = body or {}
    if "nodes" not in body:
        rollout = start("asked", restart=body.get("restart"))
        return {"ok": True, "rollout": rollout, "operation": rollout.get("operation"),
                "detail": f"Updating {len(rollout['nodes'])} hosts one at a time. Follow it under Jobs."}
    names = _chosen(body["nodes"])
    review = preview(names)
    if review["blocked"]:
        raise ValueError(review["blocked"])
    if body.get("review_token") != review["review_token"]:
        return ROUTER.Reply(409, {"error": "Something changed since this was reviewed. Review it again.", "preview": review})
    rollout = start("asked-hosts", only=names, restart=body.get("restart"))
    which = rollout["nodes"][0] if len(rollout["nodes"]) == 1 else f"{len(rollout['nodes'])} hosts"
    return {"ok": True, "rollout": rollout, "operation": rollout.get("operation"),
            "detail": f"Updating {which}. Follow it under Jobs."}


# Its routes and who may use them (homestead_routes.py).
ROUTES = {
    ("POST", "/api/os-updates/start"): ("admin", lambda request: start_asked(request.body)),
    ("GET", "/api/os-updates/preview"): ("admin", lambda request: preview(request.query.get("node"))),
    ("GET", "/api/os-updates"): ("viewer", lambda request: report()),
    ("POST", "/api/os-updates/settings"): ("admin", lambda request: {"ok": True, "settings": save_settings(request.body)}),
    ("POST", "/api/os-updates/stop"): ("admin", lambda request: stop_asked()),
}


def register(ops):
    """The job store's entry for an OS update rolled across the hosts (#622)."""
    ops.RESOLVERS["os-rollout"] = status
