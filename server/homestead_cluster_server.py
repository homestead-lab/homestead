"""The cluster code that lived in server.py (track 1, parts 2 and 8): the
leader's background loops for hardware, housekeeping, power jobs, baselines,
host consoles and OS updates; image updates, their review and capacity plan,
and the auto-update tick; the Harvester upgrade status; the allocation
probe's capacity; the shutdown review; and the onboarding, clean-up,
node-removal and shutdown routes server.py's handler answered.

server.py binds what it still owns through bind_routes(): each helper in
ROUTE_NEEDS is looked up in server.py when it is called, or, once moved
(server.MOVED), in the module it moved to, so a test that patches it there
reaches it here too; each name in VALUES is server.py's own value (the same
object, for _cache and _lock)."""
import copy
import time
import urllib.request, urllib.parse, urllib.error
import homestead_allocation_capacity as ALLOCATION_CAPACITY
import homestead_allocation_probe as ALLOCATION_PROBE
import homestead_autoupdate as AUTOUPDATE
import homestead_baseline as BASELINE
import homestead_capacity_review as CAPACITY_REVIEW
import homestead_cluster_shutdown as CLUSTER_SHUTDOWN
import homestead_container_rebalance as CREBALANCE
import homestead_host_console as HOST_CONSOLE
import homestead_image_cache as IMAGES
import homestead_housekeeping as HOUSEKEEPING
import homestead_leader as LEADER
import homestead_lhrebuild as LHREBUILD
import homestead_lifecycle as LC
import homestead_move as MOVE
import homestead_move_source as MOVE_SOURCE
import homestead_names as NAMES
import homestead_nodes as NODES
import homestead_nodeshell as NODESHELL
import homestead_onboard as ONBOARD
import homestead_operations as OPS
import homestead_os_rollout as OS_ROLLOUT
import homestead_place as PLACE
import homestead_ports as PORTS
import homestead_probe as PROBE
import homestead_rollout_capacity as ROLLOUT_CAPACITY
import homestead_routes as MODULE_ROUTES
import homestead_self as SELF
import homestead_self_data_prepare as SELF_DATA_PREPARE
import homestead_storage_guard as STORAGE_GUARD
import homestead_updates as UPDATES
import homestead_update_rollouts as ROLLOUTS
import homestead_update_checks as CHECKS
import homestead_upgrades as UPGRADES
import homestead_uptime as UPTIME
import homestead_workloads as WORKLOADS

ROUTE_NEEDS = ("_dns_name", "_ksend", "_self_data_helper_image", "beat", "cached", "enforce_update_policy", "get_app_settings", "guard_managed_smb", "is_self", "kget", "ksend", "rollout_review_context", "self_data_activity", "update_policy_status")
VALUES = ("HOMESTEAD_VERSION", "_cache", "_lock")


def bind_routes(helpers, **values):
    missing = set(ROUTE_NEEDS) - set(helpers)
    if missing:
        raise TypeError("homestead_cluster_server needs " + ", ".join(sorted(missing)))
    if set(values) != set(VALUES):
        raise TypeError("homestead_cluster_server is bound with " + ", ".join(sorted(set(values) ^ set(VALUES))))
    globals().update({name: helpers[name] for name in ROUTE_NEEDS})
    globals().update(values)


# ---------------------------------------------------------------- collectors
def _hardware_loop():
    while True:
        if LEADER.is_leader():
            try:
                with self_data_activity():
                    NODES.reconcile_hardware()
                    PORTS.observe(NODES.node_temps())
                beat("hardware", 30, leader_only=True)
            except Exception as error:
                beat("hardware", 30, error, leader_only=True)
                print(f"hardware: {str(error)[:160]}", flush=True)
        time.sleep(30)


def _housekeeping_loop():
    """Once a day on the leader, ten minutes after start: keep the data volume in bounds."""
    time.sleep(600)
    while True:
        if LEADER.is_leader():
            try:
                HOUSEKEEPING.tidy_unused()
                beat("housekeeping", 86400, leader_only=True)
            except Exception as error:
                beat("housekeeping", 86400, error, leader_only=True)
                print(f"housekeeping: {str(error)[:160]}", flush=True)
        time.sleep(86400)


def pod_container_rows(pod):
    """Return an explicit, UI-safe view of init and app containers in a pod."""
    spec = pod.get("spec", {}) or {}
    status = pod.get("status", {}) or {}
    groups = [
        ("init", spec.get("initContainers", []) or [], status.get("initContainerStatuses", []) or []),
        ("app", spec.get("containers", []) or [], status.get("containerStatuses", []) or []),
    ]
    rows = []
    for kind, containers, statuses in groups:
        by_name = {x.get("name", ""): x for x in statuses}
        for container in containers:
            name = container.get("name", "")
            cs = by_name.get(name, {})
            state_obj = cs.get("state", {}) or {}
            waiting = state_obj.get("waiting") or {}
            terminated = state_obj.get("terminated") or {}
            if waiting:
                state = waiting.get("reason") or "waiting"
                message = waiting.get("message") or ""
            elif terminated:
                state = terminated.get("reason") or "terminated"
                message = terminated.get("message") or ""
            elif state_obj.get("running"):
                state, message = "running", ""
            else:
                state, message = "pending", ""
            rows.append({
                "name": name,
                "kind": kind,
                "image": container.get("image", ""),
                "ready": bool(cs.get("ready", False)),
                "state": state,
                # Why, in a sentence, where it is a pull that failed; the
                # whole message beside it - its cause is at the end.
                "message": (ROLLOUTS.explain_pull(message) or str(message))[:220],
                "detail": str(message)[:1200],
                "restarts": int(cs.get("restartCount", 0) or 0),
            })
    return rows


def image_update_capacity_plan(body, action):
    """Resolve immutable images and review the whole rollout without writes."""
    if action not in ("update", "rollback"):
        raise ValueError("image action must be update or rollback")
    ns = _dns_name(body.get("ns"), "namespace")
    name = _dns_name(body.get("name"), "workload name")
    guard_managed_smb(ns, name)
    if action == "update":
        enforce_update_policy(body)
    prepared = (ROLLOUTS.prepare_update if action == "update" else ROLLOUTS.prepare_rollback)(ns, name)
    current, proposed = prepared["current"], prepared["proposed"]
    if current["spec"].get("paused"):
        raise ValueError("resume this paused workload before reviewing an image rollout")
    context = {"action": "image-" + action, **rollout_review_context(current),
               "proposed_spec": proposed["spec"], "before": prepared["before"],
               "annotations": proposed["metadata"].get("annotations", {})}
    cache = {f"/apis/apps/v1/namespaces/{ns}/deployments/{name}": current}
    def read(path):
        if path not in cache:
            cache[path] = kget(path)
        return copy.deepcopy(cache[path])
    def planner(*args, **kwargs):
        return PLACE.manifest_plan(*args, **kwargs, read=read)
    capacity = ROLLOUT_CAPACITY.plan(
        current, proposed, ns, read, PLACE.get_nodes, planner,
        get_app_settings()["thresholds"]["memory"]["critical"])
    # Every image change needs a fresh review, even when it comfortably fits.
    capacity["requires_confirmation"] = True
    if int(current["spec"].get("replicas", 1) or 0) == 0:
        capacity["warnings"].append("Stopped workloads have no current running image. Recovery uses a recorded digest where known; otherwise the old template tag is resolved now, not claimed to be a previously running image.")
    return prepared, capacity, context


def release_tag(dep, container_name, image=""):
    """The release a container follows: the tag Homestead tracks for it, else
    the tag in its image. An update pins the image to its digest, which drops
    the tag, so the tracked one is what says 2.8.215 rather than sha256:..."""
    tracked = UPDATES._annotation_json(dep, UPDATES.TRACKED).get(container_name, "")
    for ref in (tracked, image):
        tail = str(ref or "").split("@", 1)[0].rsplit("/", 1)[-1]
        if ":" in tail:
            return tail.rsplit(":", 1)[1]
    return ""


def preview_image_update(body):
    action = body.get("action", "update")
    prepared, capacity, context = image_update_capacity_plan(body, action)
    old = {c["name"]: c.get("image", "") for c in ROLLOUTS._pod_containers(prepared["current"])}
    images = [{"container": c["name"], "before": old.get(c["name"], ""), "after": c.get("image", ""),
               "before_tag": release_tag(prepared["current"], c["name"], old.get(c["name"], "")),
               "after_tag": release_tag(prepared["proposed"], c["name"], c.get("image", "")),
               "rollback": prepared["before"].get(c["name"], "")}
              for c in ROLLOUTS._pod_containers(prepared["proposed"])
              if c.get("image", "") != old.get(c["name"], "")]
    return {"capacity": capacity, "capacity_token": CAPACITY_REVIEW.issue(body, context),
            "images": images, "action": action}


def _jobs_loop():
    """Only the leader advances jobs (#622): every 3 s while one is active, every
    20 s otherwise, or at once when something asks (OPS.advance_soon). Requests
    read the saved jobs (OPS.snapshot) and never run a resolver themselves.
    It beats each round, so a stalled loop or a hung resolver shows in health,
    and starts a round as soon as this replica becomes leader."""
    LEADER.when_leading(OPS.advance_soon)
    OPS.run_jobs(LEADER.is_leader,
                 beat=lambda error=None: beat("jobs", OPS.JOBS_BEAT_EVERY, error, leader_only=True))


def _detached_copies_loop():
    """Every minute on the leader, unless the cluster is shutting down:
    offline rebuilding on by default, and Homestead's stand-in for it on
    Longhorn without one; and the image scans and import clean-up that
    looking at those pages used to do (#622); and host shell helpers that
    nobody is connected to any more."""
    while True:
        time.sleep(60)
        try:
            if not (LEADER.is_leader() and (cluster_shutdown().state() or {}).get("phase") in (None, "released")):
                continue
        except Exception as error:
            print(f"leader or shutdown state unread; detached volume copies, image scans and import clean-up skipped: {str(error)[:160]}", flush=True)
            continue
        try:
            LHREBUILD.tick()
            _cache.pop("lhrebuild", None)
        except Exception as error:
            print(f"detached volume copies not checked: {str(error)[:160]}", flush=True)
        try:
            IMAGES.tick()
        except Exception as error:
            print(f"image scans and import clean-up not done: {str(error)[:160]}", flush=True)
        try:
            NODESHELL.sweep()
        except Exception as error:
            print(f"host shell helpers not checked: {str(error)[:160]}", flush=True)


def allocation_probe_capacity(obj, template):
    listing = kget("/api/v1/pods")
    if not isinstance(listing.get("items"), list) or (listing.get("metadata") or {}).get("continue"):
        raise ValueError("Complete workload inventory is unavailable; nothing was changed")
    return ALLOCATION_CAPACITY.plan(obj, template, PLACE.get_nodes(), listing["items"],
                                    get_app_settings()["thresholds"]["memory"]["critical"])


def _autoupdate_loop():
    while True:
        if LEADER.is_leader():
            try:
                autoupdate_tick()
                beat("auto-updates", 60, leader_only=True)
            except Exception as error:
                beat("auto-updates", 60, error, leader_only=True)
                print(f"auto-updates: {str(error)[:160]}", flush=True)
        time.sleep(60)


def set_update_mode(b):
    """Whether an app updates itself inside the maintenance window. An annotation, so nothing restarts."""
    ns, name = _dns_name(b.get("ns"), "namespace"), _dns_name(b.get("name"), "workload name")
    guard_managed_smb(ns, name)
    chosen = str(b.get("mode") or "")
    if chosen not in ("auto", "manual"):
        raise ValueError("choose auto or manual")
    dep = kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")
    if chosen == "auto" and UPDATES.managed_by(dep):
        raise ValueError(UPDATES.managed_message(ns, name, UPDATES.managed_by(dep)))
    if chosen == "auto" and is_self(ns, name):
        raise ValueError("Homestead updates itself from Settings › Updates")
    ksend("PATCH", f"/apis/apps/v1/namespaces/{ns}/deployments/{name}",
          {"metadata": {"annotations": {NAMES.key(AUTOUPDATE.MODE): "auto" if chosen == "auto" else None}}},
          ctype="application/merge-patch+json")
    _cache.pop("wl", None)
    return {"ok": True, "mode": chosen,
            "detail": f"{name} updates itself in the maintenance window" if chosen == "auto" else f"{name} waits for you to update it"}


def _baseline_loop():
    """What the installer asked Homestead to put under it - kube-vip and
    Multus on k3s and RKE2 - installed by the leader once the cluster can
    say what it has (homestead_baseline.py). Asked for once: nothing to do
    after that, so it checks rarely."""
    time.sleep(20)
    while True:
        if LEADER.is_leader():
            try:
                with self_data_activity():
                    for row in BASELINE.tick():
                        if row["ok"] and row.get("job"):
                            BASELINE.operation(row, "Install")
                        with _lock:
                            for key in ("helm", "platform", "baseline", "components"):
                                _cache.pop(key, None)
                beat("baseline", 300, leader_only=True)
            except Exception as error:
                beat("baseline", 300, error, leader_only=True)
                print(f"platform: {str(error)[:160]}", flush=True)
        time.sleep(60)


def _host_console_loop():
    """On the leader, the host console add-on brought to its setting, a host
    or two at a time. Its own loop: each host can take minutes, and the
    host fixes (the root filesystem's guard among them) do not wait for it."""
    time.sleep(90)
    while True:
        if LEADER.is_leader():
            try:
                for node, change in HOST_CONSOLE.tick():
                    print(f"platform: {node}: {change}", flush=True)
                beat("host-console", 600, leader_only=True)
            except Exception as error:
                beat("host-console", 600, error, leader_only=True)
        time.sleep(600)


def _os_updates_loop():
    """OS updates across the hosts (homestead_os_rollout.py), on the leader:
    Ubuntu's automatic updates held off or let go as the settings say, the
    weekly window opened, and a running update moved on a step. Its state is
    on disk, so a new leader carries on where the last one was."""
    time.sleep(45)
    while True:
        if LEADER.is_leader():
            try:
                with self_data_activity():
                    moved = OS_ROLLOUT.tick()
                if moved:
                    print(f"OS updates: {moved.get('message', '')[:200]}", flush=True)
                beat("os-updates", 20, leader_only=True)
            except Exception as error:
                beat("os-updates", 20, error, leader_only=True)
                print(f"OS updates: {str(error)[:200]}", flush=True)
        time.sleep(20)


def cluster_shutdown(review=False):
    image = _self_data_helper_image(kget, SELF.NS)[1] if review else ""
    def busy():
        with OPS.transaction():
            jobs = [j for j in SELF_DATA_PREPARE.blocking_jobs(OPS) if j["kind"] != "cluster-shutdown"]
            # The shutdown changes Homestead's replicas from a helper pod no
            # write guard reaches, so it waits for every move the storage guard
            # would refuse that for - a failed one not yet cleaned up too.
            listed = {j["id"] for j in jobs}
            moves = [m for m in STORAGE_GUARD.active_moves(OPS.stored()) if m["id"] not in listed]
        return (["Finish or recover job: " + j["title"] for j in jobs] +
                ["Recover the storage move first: " + (m.get("title") or m["id"]) for m in moves])
    return CLUSTER_SHUTDOWN.Shutdown(kget, _shutdown_send, SELF.NS, SELF.POD, image,
                                      enabled=LC.NODE_POWER_ENABLED, busy=busy)


def _shutdown_send(method, path, body=None, **kwargs):
    """The shutdown's own writes from here: past the shutdown fence and the
    other write guards - recovery gives Homestead its copies back even while
    a storage move waits, which the review kept from starting - but saying
    who made them, like every write to an app (#621)."""
    import homestead_changes as CHANGES
    CHANGES.note_write(method, path, body)
    return _ksend(method, path, body, **kwargs, shutdown_bypass=True)


def _upgrade_node_probe():
    """Finish the upgrade the image cannot finish by itself.

    Deliberately not fatal and deliberately quiet: a cluster where the probe
    is absent, or where Homestead lacks the rights to touch it, is a cluster
    that simply has no probe - not a reason to refuse to start.

    This replica's loop that reads the probes in the background starts
    here too, so the pages never wait on one (#705).
    """
    NODES.start_probes()
    try:
        with self_data_activity():
            result = PROBE.reconcile(HOMESTEAD_VERSION)
    except Exception as error:
        print(f"node probe: not updated ({str(error)[:120]})", flush=True)
        return
    if result["state"] in ("updated", "error"):
        print(f"node probe: {result['detail']}", flush=True)
    try:
        for attempt in range(7):
            with self_data_activity():
                result = ALLOCATION_PROBE.reconcile(HOMESTEAD_VERSION)
            if result["state"] != "waiting" or attempt == 6:
                break
            # Background read-only waiting, never retry an uncertain PATCH.
            time.sleep(10)
        if result["state"] in ("updated", "review", "waiting"):
            print(f"VM placement checks: {result['detail']}", flush=True)
    except Exception:
        print("VM placement helper not updated; review its settings in Cluster > Add-ons", flush=True)


def reviewed_image_update(body, action):
    if body.get("action", "update") != action:
        raise ValueError("image action changed; review again")
    prepared, capacity, context = image_update_capacity_plan(body, action)
    CAPACITY_REVIEW.enforce(body, capacity, context)
    if action == "update":
        enforce_update_policy(body)  # Registry/preflight reads may cross a window boundary.
    return ROLLOUTS.commit_prepared(prepared)


def _harvester_upgrade_status(item):
    """A Harvester upgrade Homestead started, followed as the Cluster page
    follows any: Harvester's own steps, then each node."""
    name = item["ref"].get("upgrade", "")
    row = next((row for row in UPGRADES.upgrades() if row["name"] == name), None)
    if not row:
        return "running", 1, "Waiting for Harvester to take the upgrade"
    step = next((s["label"] for s in row["steps"] if s["state"] in ("running", "failed")), "")
    message = row["message"] or (f"{step}" if step else f"Harvester {row['version']}")
    return row["state"], row["progress"], message


def autoupdate_tick():
    """On the leader: start the next automatic update if the window is open,
    and tidy old snapshots after ones that worked. Returns the app started."""
    status = update_policy_status()
    operations = OPS.snapshot()
    AUTOUPDATE.tidy(operations)
    report = CHECKS._LATEST.get("report") or {}
    chosen = AUTOUPDATE.pick(cached("wl", 5, WORKLOADS.get_workloads), report, UPTIME.report(), operations,
                             status["window_open"], status["policy"], (CREBALANCE.KIND,))
    if not chosen:
        return None
    try:
        # Refused under the job list's lock if a rebalance started since the look.
        OPS.start(AUTOUPDATE.KIND, f"Update {chosen['name']} automatically",
                  {"kind": "Deployment", "name": chosen["name"], "namespace": chosen["ns"]},
                  "/containers?" + urllib.parse.urlencode({"find": chosen["name"]}),
                  {"namespace": chosen["ns"], "name": chosen["name"], "phase": "snapshot"},
                  "Snapshotting its volumes before the update", unless=(CREBALANCE.KIND,))
    except ValueError:
        return None
    _cache.pop("wl", None)
    return chosen


# The routes server.py's handler answered for this code.
def _move_step(call):
    """Run a move step, answering a refusal with 409 rather than 500.

    The Homestead on the other end waits out a 5xx and stops on a 4xx, so
    "no, it is still running" has to arrive as the second kind.
    """
    try:
        return call()
    except MOVE_SOURCE.PendingRecovery as error:
        return MODULE_ROUTES.Reply(503, {"error": str(error)})
    except (ValueError, PermissionError) as error:
        return MODULE_ROUTES.Reply(409, {"error": str(error)})
    except MOVE.Unreachable as error:
        return MODULE_ROUTES.Reply(502, {"error": str(error)})


def _removal_plan(request):
    return _move_step(lambda: ONBOARD.removal_plan((request.query.get("node") or [""])[0]))


def _remove_node(request):
    b = request.body
    return _move_step(lambda: ONBOARD.remove_node(b.get("node"), bool(b.get("accept_loss")), bool(b.get("gone"))))


def _cleanup_run(request):
    b = request.body
    return _move_step(lambda: ONBOARD.cleanup(b.get("kind"), b.get("name") or "", bool(b.get("force"))))


def _shutdown(request):
    return MODULE_ROUTES.Reply(202, cluster_shutdown(review=True).start(request.body, OPS))


# What this cluster offers another one. Read-only.
ROUTES = {
    ("GET", "/api/onboard/guide"): ("admin", lambda request: _move_step(ONBOARD.guide)),
    ("GET", "/api/cluster/cleanup"): ("admin", lambda request: _move_step(ONBOARD.cleanup_report)),
    ("GET", "/api/cluster/removal"): ("admin", _removal_plan),
    ("POST", "/api/cluster/remove-node"): ("admin", _remove_node),
    ("POST", "/api/cluster/cleanup/run"): ("admin", _cleanup_run),
    ("POST", "/api/cluster/shutdown"): ("admin", _shutdown),
}


def register(ops):
    """The cluster jobs whose resolvers live here (#622), and two stand-ins:
    join plans are gone, so a job one left in Activity says so rather than
    erroring; and a node clean-up is recorded once it has happened, so it reads
    as done straight away."""
    ops.RESOLVERS["cluster-shutdown"] = lambda item: cluster_shutdown().progress(item)
    ops.RESOLVERS["harvester-upgrade"] = _harvester_upgrade_status
    ops.RESOLVERS["onboard"] = lambda item: ("cancelled", item.get("progress", 0),
                                             "Join plans were replaced by the install guide")
    ops.RESOLVERS["cluster-cleanup"] = lambda item: ("succeeded", 100, item.get("message", ""))
