"""The overview, alerts, diagnostics and uptime code that lived in server.py
(track 1, parts 1 and 8): the dashboard sampler, the alert sources and their
loop, uptime settings, schedules (their settings, the scheduled items, a
schedule's action and the outage tick), protection issues, the history loop,
the change-undo plan and the portal's status, with the routes server.py's
handler answered for them.

server.py binds what it still owns through bind_routes(): each helper in
ROUTE_NEEDS is looked up in server.py when it is called, or, once moved
(server.MOVED), in the module it moved to, so a test that patches it there
reaches it here too; each name in VALUES is server.py's own value (the same
object, for _cache)."""
import copy
import json
import time
import urllib.request, urllib.parse, urllib.error
import homestead_alert_webhook as ALERT_WEBHOOK
import homestead_alerts as ALERTS
import homestead_app_copies as APP_COPIES
import homestead_api_v1 as API_V1
import homestead_audit as AUDIT_LOG
import homestead_auth as AUTH
import homestead_changes as CHANGES
import homestead_cluster as CLUSTER
import homestead_diagnostics as DIAGNOSTICS
import homestead_disks as DISKS
import homestead_expiry as EXPIRY
import homestead_forecast as FORECAST
import homestead_gitops as GITOPS
import homestead_history as HISTORY
import homestead_host_access as HOSTACCESS
import homestead_host_os as HOST_OS
import homestead_housekeeping as HOUSEKEEPING
import homestead_leader as LEADER
import homestead_longhorn_target as BACKUP_TARGET
import homestead_longhorn_jobs as JOBS
import homestead_lhcapacity as LHCAP
import homestead_lhrebuild as LHREBUILD
import homestead_logalerts as LOGALERTS
import homestead_mcp as MCP
import homestead_names as NAMES
import homestead_network_inventory as INV
import homestead_vip_registry as REG
import homestead_nodes as NODES
import homestead_operations as OPS
import homestead_outage as OUTAGE
import homestead_outage_carry as OUTAGE_CARRY
import homestead_outage_sweep as OUTAGE_SWEEP
import homestead_page_snapshot as PAGES
import homestead_place as PLACE
import homestead_portal as PORTAL
import homestead_ports as PORTS
import homestead_push as PUSH
import homestead_timeline_summary as SUMMARY
import homestead_recovery as RECOVERY
import homestead_resources as RESOURCES
import homestead_restarts as RESTARTS
import homestead_restore_swap as RESTORE_SWAP
import homestead_rollout_capacity as ROLLOUT_CAPACITY
import homestead_root_guard as ROOT_GUARD
import homestead_routes as MODULE_ROUTES
import homestead_schedules as SCHEDULES
import homestead_scheduled_jobs as CRONJOBS
import homestead_start_order as START_ORDER
import homestead_self as SELF
import homestead_storage as STORAGE
import homestead_update_checks as CHECKS
import homestead_weekly_trim as WEEKLY_TRIM
import homestead_upgrades as UPGRADES
import homestead_uptime as UPTIME
import homestead_vips as VIPS
import homestead_vm_list as VM_LIST
import homestead_vmusage as VMUSAGE
import homestead_workloads as WORKLOADS

ROUTE_NEEDS = ("age_secs", "api_path", "api_vm_power", "beat", "cached", "_dns_name",
               "get_app_settings", "is_managed_nfs", "is_managed_smb", "kget", "ksend",
               "_never_started", "require_workload_target", "rollout_review_context",
               "self_data_activity", "self_stop_warning")
VALUES = ("API", "CTX", "SYS_NS", "TOKEN", "_cache")


def bind_routes(helpers, **values):
    missing = set(ROUTE_NEEDS) - set(helpers)
    if missing:
        raise TypeError("homestead_overview_server needs " + ", ".join(sorted(missing)))
    if set(values) != set(VALUES):
        raise TypeError("homestead_overview_server is bound with " + ", ".join(sorted(set(values) ^ set(VALUES))))
    globals().update({name: helpers[name] for name in ROUTE_NEEDS})
    globals().update(values)


VMUSAGE_EVERY = 30


def _sampler_round(state, now=None):
    """One turn of the sampler: each VM's disk traffic every VMUSAGE_EVERY s
    on every replica, and on the leader the page snapshot's cycle (#705).
    Returns how long to wait before the next turn."""
    now = time.monotonic() if now is None else now
    # Each VM's disk traffic is a count, and a rate needs two readings.
    # Every replica keeps its own, in memory, for the pages it answers.
    if now - state.get("usage", now - VMUSAGE_EVERY) >= VMUSAGE_EVERY:
        state["usage"] = now
        try:
            VMUSAGE.sample()
        except Exception:
            pass
    # The pages' reads and the dashboard history are the leader's; a standby
    # replica's full scan of the cluster would be thrown away (#621).
    try:
        done = PAGES.tick(LEADER.is_leader, page_inputs(), page_builders(), now=now)
    except Exception as error:
        print(f"page snapshot: {str(error)[:160]}", flush=True)
        done = None
    if done is not None:
        try:
            if done.get("ov") is False:
                raise RuntimeError("the overview could not be read")
            part = PAGES.read("ov")
            step = int(time.time()) // HISTORY.LIVE_STEP
            # One live sample per step, not per cycle. None: a write overtook
            # the overview, and the cycle it woke records it.
            if part is not None and done.get("ov") and state.get("live") != step:
                HISTORY.record_live(part[1])
                state["live"] = step
            beat("sampler", 30, leader_only=True)
        except Exception as error:
            beat("sampler", 30, error, leader_only=True)
    return PAGES.LEADER_CHECK


def _sampler():
    state = {}
    while True:
        PAGES.sleep(_sampler_round(state))


def _uptime_loop():
    """Every minute on the leader: does each running app answer at its address."""
    while True:
        started = time.time()
        if LEADER.is_leader():
            try:
                try:
                    vms = cached("vms", 5, VM_LIST.list_vms)
                except Exception as error:
                    # Unread VMs sit this round out; their records are left as they were, not checked as gone.
                    vms = None
                    print(f"uptime: VMs not read: {str(error)[:160]}", flush=True)
                UPTIME.observe(cached("wl", 5, WORKLOADS.get_workloads), vms=vms)
                try:
                    outage_tick()
                except Exception as error:
                    print(f"outage actions: {str(error)[:160]}", flush=True)
                beat("uptime", UPTIME.CHECK_EVERY, leader_only=True)
            except Exception as error:
                beat("uptime", UPTIME.CHECK_EVERY, error, leader_only=True)
                print(f"uptime: {str(error)[:160]}", flush=True)
        time.sleep(max(5, UPTIME.CHECK_EVERY - (time.time() - started)))


def change_undo_plan(body):
    """Undo one change: the app's settings from before it, reviewed like any rollout."""
    ns, name = _dns_name(body.get("ns"), "namespace"), _dns_name(body.get("name"), "workload name")
    guard_managed_smb(ns, name)
    if is_self(ns, name):
        raise ValueError("Homestead's own settings are changed from Settings, not undone here")
    entry = CHANGES.kept_before(ns, name, str(body.get("id") or ""))
    current = kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")
    proposed, rows = CHANGES.undo_plan(current, entry)
    missing = sorted({(v.get("persistentVolumeClaim") or {}).get("claimName")
                      for v in proposed["spec"]["template"]["spec"].get("volumes") or []
                      if (v.get("persistentVolumeClaim") or {}).get("claimName")}
                     - {p["metadata"]["name"] for p in kget(f"/api/v1/namespaces/{ns}/persistentvolumeclaims").get("items", [])})
    if missing:
        raise ValueError(f"those settings used {', '.join(missing)}, which no longer exist{'s' if len(missing) == 1 else ''}")
    context = {"action": "change-undo", **rollout_review_context(current), "proposed_spec": proposed["spec"], "entry": entry["id"]}
    cache = {f"/apis/apps/v1/namespaces/{ns}/deployments/{name}": current}

    def read(path):
        if path not in cache:
            cache[path] = kget(path)
        return copy.deepcopy(cache[path])

    def planner(*args, **kwargs):
        return PLACE.manifest_plan(*args, **kwargs, read=read)
    capacity = ROLLOUT_CAPACITY.plan(current, proposed, ns, read, PLACE.get_nodes, planner,
                                     get_app_settings()["thresholds"]["memory"]["critical"])
    capacity["requires_confirmation"] = True
    return current, proposed, rows, capacity, context


def set_vm_monitoring(b):
    """How a VM is monitored: automatic, a port, a web page on a port, or off.
    An annotation on the VM, so it does not restart."""
    ns, name = _dns_name(b.get("ns"), "namespace"), _dns_name(b.get("name"), "VM name")
    mode = b.get("mode")
    # Automatic on a VM tries the usual ports; a port belongs to the other two.
    value = UPTIME.check_setting(mode, b.get("path"), b.get("port") if mode in ("tcp", "http") else None)
    if value in ("tcp",) or (value and value.startswith("/")):
        raise ValueError("choose the port to ask on this VM")
    ksend("PATCH", f"/apis/kubevirt.io/v1/namespaces/{ns}/virtualmachines/{name}",
          {"metadata": {"annotations": {NAMES.key("uptime"): value or None}}}, ctype="application/merge-patch+json")
    _cache.pop("vms", None)
    words = {"": "checked automatically", "off": "not monitored"}
    return {"ok": True, "detail": f"{name} is {words.get(value, f'checked at {value}')}"}


def _schedules_loop():
    """Every minute on the leader: any scheduled stop or start that is due.
    Its writes are noted as the schedule's, so change history leaves them out."""
    CHANGES.REQUEST.user = CHANGES.BY_SCHEDULE
    while True:
        started = time.time()
        if LEADER.is_leader():
            try:
                if SCHEDULES.tick(scheduled_items(), schedule_act, arrange=START_ORDER.arrange):
                    _cache.pop("wl", None)
                    _cache.pop("vms", None)
                beat("schedules", 60, leader_only=True)
            except Exception as error:
                beat("schedules", 60, error, leader_only=True)
                print(f"schedules: {str(error)[:160]}", flush=True)
        time.sleep(max(5, 60 - (time.time() - started)))


def set_uptime_setting(b):
    """An app's own check: auto, an HTTP path, TCP only, or off - on any of its
    published ports, or one chosen. An annotation, so nothing restarts."""
    ns, name = _dns_name(b.get("ns"), "namespace"), _dns_name(b.get("name"), "workload name")
    port = b.get("port") if b.get("mode") != "off" else None
    if port not in (None, ""):
        w = next((w for w in cached("wl", 5, WORKLOADS.get_workloads) if w["ns"] == ns and w["name"] == name), None)
        published = {int(p["port"]) for p in (w or {}).get("ports") or [] if p.get("port")}
        if w is not None and str(port).isdigit() and int(port) not in published:
            raise ValueError(f"{name} does not publish port {port}; choose one of "
                             + (", ".join(str(p) for p in sorted(published)) or "its ports"))
    value = UPTIME.check_setting(b.get("mode"), b.get("path"), port)
    ksend("PATCH", f"/apis/apps/v1/namespaces/{ns}/deployments/{name}",
          {"metadata": {"annotations": {NAMES.key("uptime"): value or None}}}, ctype="application/merge-patch+json")
    _cache.pop("wl", None)
    return {"ok": True, "value": value, "detail": f"{name} is {UPTIME.describe_setting(value)}"}


def portal_status(force=False):
    """Each portal link's dot. A link to an app takes the app's own Monitoring
    state - asked every minute, over HTTP, down only after three misses - so
    the portal and Containers agree; any other link is a TCP connection."""
    status = dict(PORTAL.status(force=force))
    try:
        answers, links = UPTIME.report(), PORTAL.stored()
    except Exception:
        return status
    for link in links:
        icon = str(link.get("icon") or "")
        if not icon.startswith("workload:"):
            continue
        a = answers.get(icon.split(":", 1)[1])
        if not a or a.get("state") not in ("up", "slow", "down"):
            continue
        last = a.get("last") or {}
        status[link["id"]] = {"up": a["state"] != "down", "ms": last.get("ms"), "state": a["state"], "source": "monitoring",
                              "since": a.get("since"), "error": last.get("error") or "", "code": last.get("code")}
    return status


def _diagnostic_read(path, timeout=5):
    req = urllib.request.Request(API + api_path(path), headers={"Authorization": f"Bearer {TOKEN}"})
    with urllib.request.urlopen(req, context=CTX, timeout=timeout) as response:
        raw = response.read(DIAGNOSTICS.MAX_SOURCE_BYTES + 1)
    if "/log?" in path:
        return raw.decode("utf-8", "replace")
    return json.loads(raw)


# ------------------------------------------------------------- alerts
UPDATE_SCAN_EVERY = CHECKS.FRESH_FOR
_last_update_scan = [0.0]


def _alert_sources():
    """What each source sees now; None where it could not look."""
    results = {}

    def take(name, fn):
        try:
            results[name] = fn()
        except Exception:
            results[name] = None

    take("health", lambda: ALERTS.health_facts(MODULE_ROUTES.page("ov", get_overview)))
    take("jobs", lambda: ALERTS.job_facts(OPS.snapshot()))
    take("joins", lambda: ALERTS.join_facts(kget("/api/v1/nodes").get("items", [])))
    take("addresses", lambda: VIPS.alert_facts(cached("network", 5, INV.inventory).get("addresses")))
    take("capacity", lambda: LHCAP.alert_facts(MODULE_ROUTES.page("lhcap", LHCAP.status, 15)))
    take("detached-copies", lambda: LHREBUILD.alert_facts(cached("lhrebuild", 30, LHREBUILD.status)))
    take("disks", lambda: DISKS.alert_facts(cached("disks", 15, DISKS.inventory)))
    take("hostos", HOST_OS.alert_facts)
    take("ports", lambda: PORTS.alert_facts(cached("ports", 20, NODES.ports_report)))
    take("uptime", lambda: UPTIME.alert_facts(UPTIME.report()))
    take("schedules", lambda: SCHEDULES.alert_facts())
    take("cronjobs", CRONJOBS.cronjob_alert_facts)
    take("outage", lambda: OUTAGE.alert_facts())
    take("outage-lan", OUTAGE_CARRY.alert_facts)
    take("starts-after", lambda: START_ORDER.alert_facts(cached("wl", 5, WORKLOADS.get_workloads)))
    take("housekeeping", lambda: HOUSEKEEPING.alert_facts(cached("housekeeping", 60, HOUSEKEEPING.report)))
    take("forecast", lambda: FORECAST.alert_facts(FORECAST.report()))
    take("gitops", GITOPS.alert_facts)
    take("expiry", EXPIRY.alert_facts)
    take("recovery", RECOVERY.alert_facts)
    # Log alerts read on their own thread; this only takes their last count.
    take("logs", LOGALERTS.alert_facts)
    take("previous-volumes", lambda: cached("previous-volumes", 60, RESTORE_SWAP.alert_facts))
    take("agents", MCP.alert_facts)
    take("rootguard", ROOT_GUARD.alert_facts)
    take("audit", AUDIT_LOG.alert_facts)
    take("platform", lambda: ALERTS.upgrade_facts(UPGRADES.report(
        ((cached("cluster", 15, CLUSTER.inventory) or {}).get("versions") or {}).get("harvester", ""))))
    # Twice a day whether or not anyone is looking - the Containers header and
    # the update notifications both read the result.
    if time.time() - _last_update_scan[0] > UPDATE_SCAN_EVERY:
        _last_update_scan[0] = time.time()
        try:
            CHECKS.report()
        except Exception:
            pass
    latest = CHECKS._LATEST.get("report")
    results["updates"] = ALERTS.update_facts(latest) if latest else None
    return results


def push_alerts(fresh):
    """Wakes every device that wants one of these alerts, and belongs to a user
    still here; and sends them to the alert webhook, if one is set (#741)."""
    if not fresh:
        return None
    try:
        ALERT_WEBHOOK.deliver(fresh)
    except Exception as error:
        print(f"alert webhook: {str(error)[:160]}", flush=True)
    users = {u["name"] for u in AUTH.list_users()}
    urgent = any(e["severity"] == "critical" and e["phase"] in ("raised", "worsened") for e in fresh)
    visible = {user: ALERTS.for_user(fresh, user) for user in users}
    return PUSH.send(lambda row: row["user"] in users and any(
                         e["category"] in row["categories"] for e in visible[row["user"]]),
                     urgency="high" if urgent else "normal")


def _alerts_loop():
    LOGALERTS.start()
    # The summary push (#921) has its own thread: building one reads the Timeline.
    SUMMARY.start()
    while True:
        # One replica raises alerts, or every notification arrives twice.
        if LEADER.is_leader():
            try:
                with self_data_activity():
                    push_alerts(ALERTS.observe(_alert_sources()))
                beat("alerts", 20, leader_only=True)
            except Exception as error:
                beat("alerts", 20, error, leader_only=True)
                print(f"alerts: {str(error)[:160]}", flush=True)
        time.sleep(20)


def set_schedule(b):
    """Stop and start an app or VM on a schedule, or no longer. An annotation,
    so nothing restarts."""
    kind = b.get("kind")
    ns = _dns_name(b.get("ns"), "namespace")
    if kind == "app":
        name = _dns_name(b.get("name"), "workload name")
        guard_managed_smb(ns, name)
        require_workload_target(ns, name)
        if is_self(ns, name):
            raise ValueError("Homestead cannot be put on a schedule: it is what runs the schedules")
        path = f"/apis/apps/v1/namespaces/{ns}/deployments/{name}"
    elif kind == "vm":
        name = _dns_name(b.get("name"), "VM name")
        path = f"/apis/kubevirt.io/v1/namespaces/{ns}/virtualmachines/{name}"
    else:
        raise ValueError("a schedule is for an app or a VM")
    schedule = SCHEDULES.clean(b.get("schedule"))
    ksend("PATCH", path, {"metadata": {"annotations": {
        NAMES.key(SCHEDULES.KEY): json.dumps(schedule, separators=(",", ":")) if schedule else None}}},
        ctype="application/merge-patch+json")
    _cache.pop("wl" if kind == "app" else "vms", None)
    return {"ok": True, "schedule": schedule,
            "detail": f"{name}: {SCHEDULES.describe(schedule).lower()}" if schedule else f"{name} is no longer on a schedule"}


def _busy_owners():
    """busy(ns, name): whether an app or VM of that name has a job not over:
    queued, running, cancelling, or ended but still to be recovered (not yet
    dismissible). When the jobs cannot be read, it raises: nothing is swept."""
    try:
        operations, error = OPS.snapshot(), None
    except Exception as failed:
        operations, error = [], failed
    working = {((o.get("resource") or {}).get("namespace"), (o.get("resource") or {}).get("name"))
               for o in operations if o.get("status") not in OPS.TERMINAL or not o.get("dismissible", True)}

    def busy(ns, name):
        if error is not None:
            raise error
        return (ns, name) in working
    return busy


def outage_tick():
    """After a monitoring round, on the leader: any outage action that is due,
    then the carry-over of webhooks saved before #741 - after, and guarded, so
    nothing in it can hold up or stop an outage action."""
    found = OUTAGE.items(cached("wl", 5, WORKLOADS.get_workloads), cached("vms", 5, VM_LIST.list_vms))
    did = []
    if found:
        operations = OPS.snapshot()
        working = {((o.get("resource") or {}).get("namespace"), (o.get("resource") or {}).get("name"))
                   for o in operations if o.get("status") in ("queued", "running")}

        def restart(item):
            if item["kind"] == "app":
                restart_workload(item["ns"], item["name"])
            else:
                api_vm_power(item["ns"], item["name"], "restart")
            _cache.pop("wl" if item["kind"] == "app" else "vms", None)

        try:
            did = OUTAGE.tick(found, UPTIME.report(), restart, lambda item: (item["ns"], item["name"]) in working, operations)
        except Exception as error:
            # Its own beat, so Homestead's health shows outage actions failing;
            # the carry-over below still runs.
            beat("outage-actions", UPTIME.CHECK_EVERY, error, leader_only=True)
            print(f"outage actions: {str(error)[:160]}", flush=True)
            did = None
    if did is not None:
        beat("outage-actions", UPTIME.CHECK_EVERY, leader_only=True)
    try:
        OUTAGE.migrate(found)       # webhook addresses still on an annotation go into their Secret (#931)
    except Exception as error:
        print(f"outage actions: move into Secrets: {type(error).__name__}", flush=True)
    try:
        OUTAGE_SWEEP.sweep(_busy_owners())  # Secrets of apps and VMs deleted outside Homestead (#931)
    except Exception as error:
        print(f"outage actions: sweep of webhook Secrets: {type(error).__name__}", flush=True)
    try:
        OUTAGE_CARRY.carry_lan()    # reads its own lists: these cached ones cannot tell a failed VM list from none
    except Exception as error:
        print(f"outage actions: carry-over: {type(error).__name__}", flush=True)
    return did or []


def scheduled_items():
    return SCHEDULES.items(cached("wl", 5, WORKLOADS.get_workloads), cached("vms", 5, VM_LIST.list_vms))


def schedule_act(item, action):
    """Do a scheduled stop or start as the buttons do, with their checks."""
    ns, name = item["ns"], item["name"]
    if item["kind"] == "app":
        dep = kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")
        current = int((dep.get("spec") or {}).get("replicas") or 0)
        if action == "stop":
            if not current:
                return f"{name} was already stopped"
            ksend("PATCH", f"/apis/apps/v1/namespaces/{ns}/deployments/{name}",
                  {"metadata": {"annotations": {NAMES.key(SCHEDULES.REPLICAS_KEY): str(current)}}},
                  ctype="application/merge-patch+json")
            api_scale(ns, name, 0)
            return f"stopped {name}"
        if current:
            return f"{name} was already running"
        try:
            copies = int(NAMES.read((dep.get("metadata") or {}).get("annotations") or {}, SCHEDULES.REPLICAS_KEY) or 1)
        except ValueError:
            copies = 1
        try:
            api_scale(ns, name, max(1, min(copies, 100)))
        except API_V1.ApiError as error:
            raise ValueError(error.message) from None
        return f"started {name}" + (f" with {copies} copies" if copies > 1 else "")
    if action == "stop":
        if not item["running"]:
            return f"{name} was already stopped"
        api_vm_power(ns, name, "stop")
        return f"shutting {name} down"
    if item["running"]:
        return f"{name} was already running"
    try:
        api_vm_power(ns, name, "start")
    except API_V1.ApiError as error:
        raise ValueError(error.message) from None
    return f"starting {name}"


def protection_issues(jobs, target, trim=None):
    """Protection that has quietly stopped working: a snapshot or backup job
    whose last run failed, or backup jobs with nowhere they can write to.
    Nothing else says so until the copy is needed. trim is the weekly trim's
    saved state: an attached volume it failed to trim is one too (#749)."""
    issues = []
    guarding = [j for j in jobs or [] if str(j.get("task", "")).split("-")[0] in ("snapshot", "backup")
                and j.get("task") not in ("snapshot-cleanup", "snapshot-delete") and j.get("covers")]
    for job in guarding:
        if job.get("last_failed"):
            issues.append({"severity": "degraded", "kind": "Backup", "name": job["name"],
                           "reason": f"The last {job['task'].split('-')[0]} run failed. Review its job log."})
    if any(j["task"].startswith("backup") for j in guarding):
        target = target or {}
        if not target.get("configured"):
            issues.append({"severity": "degraded", "kind": "Backup", "name": "backup target",
                           "reason": "Backup jobs have no backup target. Configure a destination."})
        elif not target.get("available"):
            issues.append({"severity": "degraded", "kind": "Backup", "name": "backup target",
                           "reason": f"{target.get('url', 'the backup target')} cannot be reached"
                                     + (f": {target['reason']}" if target.get("reason") else "")})
    return issues + WEEKLY_TRIM.issues(trim or {})


def _history_loop():
    """Long-term stats, on the leader, every five minutes, browser or not."""
    last = 0
    while True:
        # Checked often, recorded every STEP: a new leader starts at once.
        if LEADER.is_leader() and time.time() - last >= HISTORY.STEP:
            try:
                HISTORY.record(MODULE_ROUTES.page("ov", get_overview))
                last = time.time()
                beat("history", HISTORY.STEP, leader_only=True)
            except Exception as error:
                beat("history", HISTORY.STEP, error, leader_only=True)
                print(f"history: {str(error)[:160]}", flush=True)
        time.sleep(30)


# The routes server.py's handler answered for this code.
def _portal_status(request):
    q = request.query
    return portal_status(force=(q.get("force") or [""])[0] == "1")


def _resource(request):
    q, p = request.query, request.path
    arg = lambda key: (q.get(key) or [""])[0]
    # Secret-bearing fields (Helm values, cloud-init, any custom resource's
    # spec, an address's credentials) go only to the role that may reveal a
    # Secret; everyone else gets each field's name.
    need = ROUTES[("GET", "/api/resources/reveal")][0]
    if p == "/api/resources/list":
        return RESOURCES.list_objects(arg("group"), arg("version"), arg("resource"), arg("ns"),
                                      see_values=AUTH.allows(request.role, need))
    return RESOURCES.get_object(arg("group"), arg("version"), arg("resource"), arg("ns"), arg("name"),
                                reveal=p.endswith("reveal"), see_values=AUTH.allows(request.role, need),
                                reveal_role=need)


def _push_test(request):
    b = request.body
    endpoint = str(b.get("endpoint") or "")
    if not PUSH.mine(request.user, endpoint):
        return MODULE_ROUTES.Reply(404, {"error": "this device is not set up for notifications"})
    ALERTS.note({"key": f"test:{int(time.time())}", "category": "test", "severity": "info",
                 "title": "Homestead test notification",
                 "body": "Notifications are enabled for this device. Choose categories in Settings › This device.",
                 "href": "/settings", "to": PUSH.tag(endpoint)})
    result = PUSH.send(lambda row: row["endpoint"] == endpoint, urgency="high")
    status = (result["statuses"] or [0])[0]
    if not result["sent"]:
        return MODULE_ROUTES.Reply(502, {"error": f"the push service refused the push (HTTP {status})"
                                         if status else "the push service could not be reached"})
    return {"ok": True}


def _alerts_acknowledge(request):
    b = request.body
    try:
        return ALERTS.acknowledge(request.user, b.get("key"), b.get("version"), b.get("undo") is True)
    except ALERTS.AlertChanged as error:
        return MODULE_ROUTES.Reply(409, {"error": str(error)})


def _alerts_delivered(request):
    b = request.body
    endpoint, cursor = str(b.get("endpoint") or ""), b.get("latest")
    if not PUSH.mine(request.user, endpoint):
        return MODULE_ROUTES.Reply(404, {"error": "This notification device is not registered to your account."})
    if type(cursor) is not int or cursor < 0 or cursor > ALERTS.log(limit=0)["latest"]:
        return MODULE_ROUTES.Reply(400, {"error": "Invalid notification cursor."})
    PUSH.advance(request.user, endpoint, cursor)
    return {"ok": True}


ROUTES = {
    ("GET", "/api/portal/status"): ("viewer", _portal_status),
    ("GET", "/api/resources/list"): ("viewer", _resource),
    ("GET", "/api/resources/object"): ("viewer", _resource),
    ("GET", "/api/resources/reveal"): ("admin", _resource),
    ("POST", "/api/push/test"): ("viewer", _push_test),
    ("POST", "/api/alerts/acknowledge"): ("viewer", _alerts_acknowledge),
    ("POST", "/api/alerts/delivered"): ("viewer", _alerts_delivered),
}


def clear_unstarted_pods(ns, name, stopping):
    """Remove pods of a container that never started, so they cannot pile up.

    A pod stuck before its first start - an image that will not pull, a
    volume or network that will not attach - often never finishes stopping,
    and every Stop and Start left one more behind. Nothing ran in it, so
    removing it at once loses nothing. When stopping, every such pod goes;
    when starting, those still stuck stopping from before."""
    try:
        dep = kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")
        selector = ((dep.get("spec") or {}).get("selector") or {}).get("matchLabels") or {}
        if not selector:
            return []
        query = urllib.parse.quote(",".join(f"{k}={v}" for k, v in selector.items()), safe="")
        pods = kget(f"/api/v1/namespaces/{ns}/pods?labelSelector={query}").get("items", [])
    except Exception:
        return []
    removed = []
    for pod in pods:
        meta = pod.get("metadata") or {}
        stuck_stopping = meta.get("deletionTimestamp") and age_secs(meta["deletionTimestamp"]) > 20
        if not _never_started(pod) or not (stopping or stuck_stopping):
            continue
        try:
            ksend("DELETE", f"/api/v1/namespaces/{ns}/pods/{meta['name']}?gracePeriodSeconds=0")
            removed.append(meta["name"])
        except Exception:
            pass
    return removed


def is_self(ns, name):
    """The Deployment this Homestead runs as."""
    return (ns, name) == (SELF.NS, NAMES.BRAND)


def guard_managed_smb(ns, name):
    if is_managed_smb(ns, name):
        raise ValueError("Homestead manages SMB and its mounts from Network Shares. "
                         "Change shares or SMB settings there.")
    if is_managed_nfs(ns, name):
        raise ValueError("Homestead manages NFS and its mounts from Network Shares and Settings > Cluster > Add-ons.")


def workload_start_plan(ns, name, replicas=1):
    ns, name = _dns_name(ns, "namespace"), _dns_name(name, "workload name")
    threshold = get_app_settings()["thresholds"]["memory"]["critical"]
    plan = PLACE.start_plan(ns, name, replicas, threshold)
    # The apps it starts after that are stopped: it would wait for them (#746).
    try:
        plan["waits_for"] = START_ORDER.waits_for(ns, name, kget)
    except Exception as error:
        plan["waits_for"] = []
        # #620: Start says it could not check, rather than that nothing waits.
        plan["waits_for_error"] = f"could not check which apps it starts after: {str(error)[:160]}"
        print(f"start plan: starts-after not read for {ns}/{name}: {str(error)[:160]}", flush=True)
    return plan


def scale_workload(ns, name, n, *, confirm_capacity=False, confirm_self=False):
    """A Deployment to n replicas, after the checks the app makes: never the
    SMB server's, never Homestead's own unless confirmed, and a start only
    where the capacity check allows it."""
    if n < 0 or n > 100:
        raise ValueError("replicas must be between 0 and 100")
    guard_managed_smb(ns, name)
    require_workload_target(ns, name)
    guard_self(ns, name, stopping=n == 0, confirmed=confirm_self)
    plan = None
    if n > 0:
        plan = workload_start_plan(ns, name, n)
        if plan["blocked"]:
            raise WORKLOADS.WorkloadRefused("not enough eligible capacity for the requested replicas", plan)
        if plan["requires_confirmation"] and not confirm_capacity:
            raise WORKLOADS.WorkloadRefused("review node memory before starting", plan)
    if n:
        clear_unstarted_pods(ns, name, stopping=False)
    # The Deployment itself, not its /scale, so the write carries who made it
    # (#621). A policy that refuses it is the answer: no second try through /scale.
    ksend("PATCH", f"/apis/apps/v1/namespaces/{ns}/deployments/{name}",
          {"spec": {"replicas": n}}, ctype="application/merge-patch+json")
    if not n:
        clear_unstarted_pods(ns, name, stopping=True)
    _cache.pop("wl", None)
    return {"ok": True, "plan": plan}


def restart_workload(ns, name):
    guard_managed_smb(ns, name)
    require_workload_target(ns, name)
    ksend("PATCH", f"/apis/apps/v1/namespaces/{ns}/deployments/{name}",
          {"spec": {"template": {"metadata": {"annotations":
           {NAMES.key("restartedAt"): time.strftime("%Y-%m-%dT%H:%M:%SZ")}}}}},
          ctype="application/merge-patch+json")
    _cache.pop("wl", None)
    return {"ok": True}


def guard_self(ns, name, stopping=False, confirmed=False, renaming=False, deleting=False, moving=False):
    """Changes that would take Homestead down from its own page.

    Stopping is allowed once it has been said out loud and confirmed; deleting,
    renaming and moving its storage never are from here - each needs the page
    running to finish, and would leave it gone halfway.
    """
    if not is_self(ns, name):
        return
    if HOSTACCESS.role() not in (None, "admin"):
        raise PermissionError("only an admin can change Homestead itself")
    if deleting:
        raise ValueError("Homestead cannot delete itself from its own page - that removes this page for good. "
                         "Use helm uninstall, or kubectl, if that is what you want")
    if renaming:
        raise ValueError("Homestead cannot rename itself: its permissions, Service and data are tied to its name")
    if moving:
        raise ValueError("Homestead's own data moves from Settings > About > Redundancy, which restarts it safely; "
                         "a storage move here would stop the page doing the move")
    if stopping and not confirmed:
        raise ValueError(self_stop_warning())


def get_overview(nodes=None, wl=None, vols=None, pods=None):
    """The Dashboard's summary. The leader's page snapshot passes in what its
    cycle has read already; each one left out is read here."""
    nodes = NODES.get_nodes() if nodes is None else nodes
    wl = WORKLOADS.get_workloads() if wl is None else wl
    vols = STORAGE.get_volumes() if vols is None else vols
    pods = (kget("/api/v1/pods") if pods is None else pods).get("items", [])
    sysp = [p for p in pods if p["metadata"]["namespace"] in SYS_NS]
    usrp = [p for p in pods if p["metadata"]["namespace"] not in SYS_NS]
    deg = [v for v in vols if v["robustness"] == "degraded"]
    flt = [v for v in vols if v["robustness"] == "faulted"]
    try:
        protection = protection_issues(cached("lhjobs-health", 60, JOBS.list_jobs),
                                       cached("lhtarget-health", 60, BACKUP_TARGET.backup_target),
                                       cached("trim-health", 60, WEEKLY_TRIM.last_state))
    except Exception:
        protection = []
    # On one host, the degraded volumes Match copies to hosts offers to lower
    # (#841) say why; read only then, and a failed read says nothing new.
    short = set()
    if len(nodes) == 1 and deg:
        try:
            offer = APP_COPIES.cached_facts()
            short = {row["volume"] for row in offer["volumes"]} if offer.get("state") == "lower" else set()
        except Exception:
            short = set()
    try:
        in_maintenance, overran = HISTORY.maintenance_view()
    except Exception:
        in_maintenance, overran = set(), set()   # with no way to tell, every host counts, as in alerts
    # Routine things are activities, and an issue counts once it has lasted
    # the alerts' hold (#941).
    health = CLUSTER.HOLD.apply(CLUSTER.classify_cluster_health(
        nodes, wl, vols, protection=protection, maintenance=in_maintenance, overran=overran, short_of_hosts=short))
    tcap = sum(n["cpu_cap"] for n in nodes) or 1
    tuse = sum(n["cpu_used"] for n in nodes)
    mcap = sum(n["mem_cap_gb"] for n in nodes) or 1
    muse = sum(n["mem_used_gb"] for n in nodes)
    return {
        **health,
        "nodes": nodes,
        "nodes_ready": len([n for n in nodes if n["status"] == "Ready"]),
        "nodes_total": len(nodes),
        "workloads": len(wl),
        "workload_pods": len(usrp),
        "system_pods": len(sysp),
        "volumes": len(vols), "vol_degraded": len(deg), "vol_faulted": len(flt),
        "vol_one_host": sum(1 for issue in health["health_issues"] if issue.get("one_host")),
        "cpu_pct": round(tuse / tcap * 100, 1),
        "mem_pct": round(muse / mcap * 100, 1),
        "cpu_used": round(tuse, 2), "cpu_cap": tcap,
        "mem_used_gb": round(muse, 1), "mem_cap_gb": round(mcap, 1),
        "top_cpu": sorted(wl, key=lambda x: -x["cpu"])[:6],
        "top_mem": sorted(wl, key=lambda x: -x["mem_mb"])[:6],
        "lb_ip": REG.shared_vip(),
    }


def page_inputs():
    """What one snapshot cycle reads once and shares: the pod list (the
    Volumes page's bare pods read it too)."""
    return {"pods": _pods_seen}


def _pods_seen():
    """The cycle's pod list, which the leader also notes restarts from
    (#740), on a thread of its own: the pages never wait for it, and a
    failed note never costs them their pods."""
    pods = kget("/api/v1/pods")
    try:
        RESTARTS.observe_later(pods)
    except Exception as error:
        print(f"restart record: {str(error)[:160]}", flush=True)
    return pods


def page_builders():
    """The snapshot's parts, in the order a cycle builds them; Longhorn's
    volumes are read once, for the Volumes page, the hosts' duties, storage
    and the overview."""
    return [
        ("vol", lambda got: STORAGE.get_volumes(pods=got.get("pods"))),
        ("nodes", lambda got: NODES.get_nodes(pods=got["pods"], vols=got.get("vol"))),
        ("wl", lambda got: WORKLOADS.get_workloads(pods=got["pods"])),
        ("stor", lambda got: STORAGE.get_storage(vols=got["vol"])),
        ("lhcap", lambda got: LHCAP.status()),
        ("ov", lambda got: get_overview(nodes=got["nodes"], wl=got["wl"], vols=got["vol"], pods=got["pods"])),
    ]


def api_scale(ns, name, n):
    """Start or stop a container for /api/v1, under the app's own checks."""
    try:
        result = scale_workload(ns, name, n, confirm_capacity=True, confirm_self=False)
    except WORKLOADS.WorkloadRefused as refused:
        raise API_V1.ApiError(409, str(refused), blockers=list((refused.plan or {}).get("blockers") or [])) from None
    return {"warnings": list((result.get("plan") or {}).get("warnings") or []), "job": None}
