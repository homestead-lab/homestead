"""Containers: the list, an app's edit form, its logs, and what is done to one
from its card - scale, restart, edit, move to another host.

server.py used to answer all of this in place (#363). The routes are declared
here now; the helpers they still share with server.py's other code are bound
by name (bind), so a test that replaces one in server.py replaces it here too.
"""
import copy
import json
import re
import urllib.error
import urllib.parse
import urllib.request

import homestead_affinity as AFFINITY
import homestead_auth as AUTH
import homestead_autoupdate as AUTOUPDATE
import homestead_capacity_review as CAPACITY_REVIEW
import homestead_copy_job as COPY_JOB
import homestead_env_secrets as ENVSEC
import homestead_failover as FAILOVER
import homestead_gitops as GITOPS
import homestead_group_tiers as GROUPS
import homestead_hardware as HW
import homestead_kube_metrics as KUBE_METRICS
import homestead_host_access as HOSTACCESS
import homestead_icons as ICONS
import homestead_lan as LAN
import homestead_kube as KUBE
import homestead_lifecycle as LC
import homestead_workload_edit as WORKLOAD_EDIT
import homestead_logsearch as LOGSEARCH
import homestead_names as NAMES
import homestead_network_services as SVC
import homestead_network_inventory as INV
import homestead_operations as OPS
import homestead_outage as OUTAGE
import homestead_value_masks as MASKS
import homestead_place as PLACE
import homestead_priority as PRIORITY
import homestead_privileges as PRIV
import homestead_rename as RENAME
import homestead_restore_test as RESTORE_TEST
import homestead_restructure as RESTRUCTURE
import homestead_rollout_capacity as ROLLOUT_CAPACITY
import homestead_routes as ROUTER
import homestead_schedules as SCHEDULES
import homestead_start_order as START_ORDER
import homestead_updates as UPDATES
import homestead_update_rollouts as ROLLOUTS
import homestead_usage as USAGE

# server.py's helpers this module calls, bound by name at start.
NEEDS = ("kget", "ksend", "workload_start_plan", "scale_workload", "restart_workload",
         "guard_managed_smb", "guard_self", "_dns_name", "get_app_settings", "apply_reviewed_edit",
         "logsearch_read", "rollout_review_context", "hardware_device_paths", "is_self", "is_managed_smb",
         "is_managed_nfs", "homestead_part", "own_group", "pod_container_rows", "parse_cpu", "parse_mem",
         "age_secs", "display_icon", "_pull_progress", "_service_listeners")
# And its values: the API server, where apps go by default, where
# logos are kept, and which namespaces are the platform's own.
API = TOKEN = CTX = None
DEFAULT_NS = "lab"
DATA_DIR = "/data"
SYS_NS = PLATFORM_NS = frozenset()


def bind(helpers, *, api, token, ssl_context, default_ns, data_dir, system_namespaces, platform_namespaces):
    global API, TOKEN, CTX, DEFAULT_NS, DATA_DIR, SYS_NS, PLATFORM_NS
    missing = set(NEEDS) - set(helpers)
    if missing:
        raise TypeError("homestead_workloads needs " + ", ".join(sorted(missing)))
    globals().update({name: helpers[name] for name in NEEDS})
    API, TOKEN, CTX, DEFAULT_NS = api, token, ssl_context, default_ns
    DATA_DIR, SYS_NS, PLATFORM_NS = data_dir, system_namespaces, platform_namespaces


class WorkloadRefused(Exception):
    """A start the capacity check will not allow as asked: 409, with its plan."""
    def __init__(self, message, plan):
        super().__init__(message)
        self.plan = plan


def _arg(request, key, default=""):
    return (request.query.get(key) or [default])[0]


# A namespace, pod, job or container name as Kubernetes allows it (DNS
# subdomain characters), so it cannot add a path or query to a request.
LOG_NAME = re.compile(r"[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?")


def logs_refusal(error, pod, container):
    """What Kubernetes' refusal to give logs means, in words rather than its JSON."""
    try:
        message = json.loads(error.read().decode("utf-8", "replace")).get("message", "")
    except Exception:
        message = ""
    who = f"{container} in {pod}" if container else pod
    if "waiting to start" in message or "ContainerCreating" in message or "PodInitializing" in message:
        return f"{who} has not started yet, so there are no logs. It shows here once it is running."
    if "not found" in message and "pods" in message:
        return f"{pod} no longer exists; the workload has probably replaced it. Reopen the logs."
    if "terminated" in message:
        return f"{who} has stopped and Kubernetes kept no logs from it."
    return f"Kubernetes could not return the logs for {who}" + (f": {message}" if message else ".")


# The most a log read asks Kubernetes for (#986): the kubelet splits lines at
# 16 KB, so 1,000 lines can be 16 MB. Log search reads 2 MB too.
LOG_BYTES = 2 * 1024 * 1024
TAIL_RETRIES = 4      # halvings of the lines asked for when an answer reaches LOG_BYTES


def _tail(ns, pod, container, lines):
    """The pod's last lines, at most LOG_BYTES. limitBytes keeps the oldest
    of the lines asked for, so a live Logs view (Follow) would sit on old
    lines: an answer that reaches it is asked for again with half the lines,
    a few times at most, so the newest are kept. A cut says so."""
    wanted = lines
    for _ in range(TAIL_RETRIES + 1):
        query = f"tailLines={lines}&timestamps=true&limitBytes={LOG_BYTES}" + (
            f"&container={urllib.parse.quote(container)}" if container else "")
        req = urllib.request.Request(f"{API}/api/v1/namespaces/{ns}/pods/{pod}/log?{query}",
                                     headers={"Authorization": f"Bearer {TOKEN}"})
        with urllib.request.urlopen(req, context=CTX, timeout=15) as r:
            raw = r.read(LOG_BYTES + 1)
        text = raw.decode("utf-8", "replace")
        if len(raw) < LOG_BYTES:
            if lines == wanted:
                return text
            return text + f"\n(Log cut: its lines are long, so Homestead is showing the last {lines} lines.)\n"
        if lines <= 1:
            break
        lines = max(1, lines // 2)
    return text + (f"\n(Log cut at {LOG_BYTES // (1024 * 1024)} MiB of its oldest lines asked for: "
                   "the newest lines may be missing.)\n")


def logs(request):
    """A pod's last lines, as text. A job's logs are its pod's."""
    q = request.query
    ns = q["ns"][0]
    pod = _arg(request, "pod")
    job = _arg(request, "job")
    container = _arg(request, "container")
    # Each goes into an API path as it is: "kube-system/secrets?x=" as a
    # namespace would read another list as Homestead.
    if any(value and not LOG_NAME.fullmatch(value) for value in (ns, pod, job, container)) or not ns:
        return ROUTER.Reply(400, {"error": "That is not a valid namespace, pod, job or container name."})
    if job and not pod:
        matches = kget(f"/api/v1/namespaces/{ns}/pods?labelSelector=job-name%3D{urllib.parse.quote(job)}").get("items", [])
        pod = matches[0]["metadata"]["name"] if matches else ""
    if not pod:
        return ROUTER.Reply(404, {"error": "Logs are not available yet because no pod exists."})
    tail = max(20, min(1000, int((q.get("tail") or [300])[0])))
    try:
        found = kget(f"/api/v1/namespaces/{ns}/pods/{pod}")
    except Exception:
        found = None
    # The Helm controller's pods run helm with the chart's values, and a
    # chart can print them: those logs need the role that may reveal a
    # Secret. A pod that cannot be read cannot be told apart, so it is
    # refused too below that role.
    if not MASKS.sees_values(request.role) and (found is None or MASKS.helm_job_pod(found)):
        if found is None:
            return ROUTER.Reply(409, {"error": f"{pod} could not be read (it may have gone), so its log is not shown."})
        need = MASKS.reveal_role()
        return ROUTER.Reply(403, {"error": f"Helm's install logs can show a chart's values, so they need the {need} role.",
                                  "role": need})
    if not container:
        # A pod with more than one container has to be told which;
        # asking without a name is a 400 from Kubernetes.
        spec = (found or {}).get("spec", {}) or {}
        names = [c.get("name") for c in spec.get("containers", []) or []]
        container = names[0] if names else ""
    try:
        return ROUTER.Reply(200, _tail(ns, pod, container, tail), "text/plain; charset=utf-8")
    except urllib.error.HTTPError as error:
        return ROUTER.Reply(409 if error.code in (400, 404) else error.code,
                            {"error": logs_refusal(error, pod, container)})


def search_logs(request):
    apps = [a for a in _arg(request, "apps").split(",") if a]
    return LOGSEARCH.search(
        ROUTER.cached("wl", 5, get_workloads), _arg(request, "q"), logsearch_read,
        window=_arg(request, "window", "1h"), apps=apps,
        regex=_arg(request, "regex") == "1", case=_arg(request, "case") == "1")


def _path_names(ns, name):
    """The namespace and workload a request names, checked before either
    goes into an API path."""
    return KUBE.request_name(ns, "namespace", namespace=True), KUBE.request_name(name, "workload name")


def edit_form(request):
    ns, name = _path_names(request.query["ns"][0], request.query["name"][0])
    deployment = kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")
    payload = workload_edit_payload(ns, name, deployment)
    return payload if may_see_secret_values(request.role, deployment, ns) else ENVSEC.mask(payload)


def may_see_secret_values(role, deployment, ns):
    """Values the app keeps in its own Secret go only to someone who could
    save the form (POST /api/edit) for this workload. Revealing a Secret
    anywhere else is admin-only; everyone else gets the names, masked."""
    if not AUTH.allows(role, ROUTES[("POST", "/api/edit")][0]):
        return False
    try:
        HOSTACCESS.require_target_as(deployment, ns, role)
    except PermissionError:
        return False
    return True


def scale(request):
    b = request.body
    try:
        scale_workload(b["ns"], b["name"], int(b["replicas"]),
                       confirm_capacity=b.get("confirm_capacity") is True,
                       confirm_self=b.get("confirm_self") is True)
    except WorkloadRefused as refused:
        return ROUTER.Reply(409, {"error": str(refused), "plan": refused.plan})
    return {"ok": True}


def set_failover(request):
    b = request.body
    for item in b.get("items") or []:
        _path_names(item.get("ns"), item.get("name"))
        guard_managed_smb(item.get("ns"), item.get("name"))
    result = FAILOVER.set_many(b.get("items") or [])
    ROUTER.forget("wl")
    return result


def set_primary_port(request):
    b = request.body
    ns = KUBE.request_name(b.get("ns") or DEFAULT_NS, "namespace", namespace=True)
    name = _dns_name(b.get("name"), "workload name")
    guard_managed_smb(ns, name)
    port = int(b.get("port") or 0)
    ksend("PATCH", f"/apis/apps/v1/namespaces/{ns}/deployments/{name}",
          {"metadata": {"annotations": {NAMES.key("primary-port"): str(port) if port else None}}},
          ctype="application/merge-patch+json")
    ROUTER.forget("wl")
    return {"ok": True, "detail": f"{name} opens on port {port} first" if port
            else f"{name} shows its ports in their own order"}


def preview_edit(request):
    b = request.body
    _path_names(b.get("ns"), b.get("name"))
    guard_managed_smb(b.get("ns", ""), b.get("name", ""))
    prepared, context, plan = edit_capacity_plan(b)
    current = kget(f"/apis/apps/v1/namespaces/{b['ns']}/deployments/{b['name']}")
    # The edit as YAML for git (#735); a rename is made in git as a rename.
    # Values kept in the app's own Secret - and any value with a credential
    # in it - are shown as references to that Secret, not as values.
    change, secret = {}, {}
    if context.get("action") != RENAME.KIND:
        proposed = copy.deepcopy(prepared["deployment"])
        secret = ENVSEC.as_saved(proposed, current, b.get("masked_env"), ENVSEC.held(b["ns"], current, kget))
        change = GITOPS.patch(current, proposed)
    return {"capacity": plan, "capacity_token": CAPACITY_REVIEW.issue(b, context),
            "managed": GITOPS.notice(current), "yaml": GITOPS.document(current, change) if change else "",
            "secret": secret if change else {}}


def edit(request):
    b = request.body
    _path_names(b.get("ns"), b.get("name"))
    guard_managed_smb(b.get("ns", ""), b.get("name", ""))
    # Homestead's manifest sets its own priority on every upgrade.
    if is_self(b.get("ns", ""), b.get("name", "")):
        b.pop("priority", None)
    # An app Flux or Argo CD applies keeps its tier where it is applied
    # (#429): a change here would be put back at their next sync.
    if "priority" in b:
        dep_now = kget(f"/apis/apps/v1/namespaces/{b['ns']}/deployments/{b['name']}")
        owner = UPDATES.managed_by(dep_now)
        if owner:
            if PRIORITY.tier_of(((dep_now.get("spec") or {}).get("template") or {}).get("spec") or {})[0] != b["priority"]:
                raise ValueError(f"{b['name']}'s tier is set where {owner['by']} applies it; set priorityClassName there")
            b.pop("priority", None)
    # The same for its start order (#746): Homestead shows the lines to commit.
    first_gate = None
    if "starts_after" in b:
        dep_now = kget(f"/apis/apps/v1/namespaces/{b['ns']}/deployments/{b['name']}")
        owner = UPDATES.managed_by(dep_now)
        if START_ORDER.unchanged(dep_now, b["starts_after"]):
            b.pop("starts_after")     # the form sent back as it was shown: no change, nothing checked
        elif owner:
            if START_ORDER.changes(dep_now, b["starts_after"], kget):
                raise ValueError(START_ORDER.managed_message(b["name"], owner))
            b.pop("starts_after")
        elif isinstance(b["starts_after"], dict) and b["starts_after"].get("after"):
            first_gate = START_ORDER.lists(kget)[0]   # as they are before this save
    prepared, context, plan = edit_capacity_plan(b)
    CAPACITY_REVIEW.enforce(b, plan, context)
    # The class is made only when an app first moves into its tier:
    # every save sends the tier, and most keep it. A class someone made with
    # other values is kept, and the save says so.
    notes = []
    if b.get("priority") in ("critical", "low", "group") and context.get("action") != RENAME.KIND:
        dep_now = kget(f"/apis/apps/v1/namespaces/{b['ns']}/deployments/{b['name']}")
        try:
            wanted = GROUPS.edit_tier(dep_now, b["priority"], kget)
        except ValueError:
            if b["priority"] != "group" or not GROUPS.follows(dep_now):
                raise
            wanted = None   # an app already following keeps its class while the tiers can't be read
        if wanted in ("critical", "low") and PRIORITY.tier_of(
                ((dep_now.get("spec") or {}).get("template") or {}).get("spec") or {})[0] != wanted:
            notes = PRIORITY.ensure((wanted,), PRIORITY.ladder())
    if context.get("action") == RENAME.KIND:
        GITOPS.require_rename_ack(kget(f"/apis/apps/v1/namespaces/{b['ns']}/deployments/{b['name']}"), b)
        def admission(dep):
            return PLACE.manifest_plan(dep, b["ns"], dep["metadata"]["name"], dep["spec"]["replicas"],
                get_app_settings()["thresholds"]["memory"]["critical"], read=kget,
                nodes_snapshot=PLACE.get_nodes(), pod_snapshot=ROLLOUT_CAPACITY.items(kget, "/api/v1/pods"))
        result = RENAME.dispatch(b, context, prepared["deployment"], kget, ksend, OPS, admission)
        ROUTER.forget("wl", "ov", "network")
        return result
    moves = RESTRUCTURE.copies(b)
    guard_self(b.get("ns", ""), b.get("name", ""),
               stopping=("autostart" in b and not b["autostart"]) or
                        ("replicas" in b and int(b.get("replicas") or 0) == 0),
               confirmed=b.get("confirm_self") is True, moving=bool(moves))
    if moves:
        return COPY_JOB.start(b, context, prepared, moves, kget, OPS,
                              lambda: apply_reviewed_edit(b, prepared, hold=True))
    result = apply_reviewed_edit(b, prepared)
    if first_gate is not None and isinstance(result, dict):
        note = START_ORDER.ensure_image(first_gate, kget)
        if note:
            notes.append(note)
    if notes and isinstance(result, dict):
        result["detail"] = " ".join(filter(None, [str(result.get("detail") or ""), "; ".join(notes) + "."]))
    return result


def move_to_host(request):
    """Another host for a container, after its capacity review: its image
    pulled onto that host first, and where it landed checked
    (homestead_host_move.py)."""
    import homestead_host_move as HOST_MOVE
    b = request.body
    proposed, capacity, context = move_capacity_plan(b)
    CAPACITY_REVIEW.enforce(b, capacity, context)
    meta = proposed["metadata"]

    def apply():
        # Preserve resourceVersion: a concurrent controller edit must conflict,
        # never be silently overwritten by a second, unreviewed fetch-and-move.
        ksend("PUT", f"/apis/apps/v1/namespaces/{meta['namespace']}/deployments/{meta['name']}", proposed)
        PLACE._bust("wl", "ov", "flow", "nodes", "impact:")
    operation = HOST_MOVE.start(b, proposed, capacity, apply)
    return {"ok": True, "moved": meta["name"], "to": capacity["move"]["node"] or "any node",
            "mode": capacity["move"]["mode"], "operation": operation}


def get_workloads(pods=None):
    """Every app for the Apps page; pods: the pod list, when the caller has
    read it already (the leader's page snapshot)."""
    deps = kget("/apis/apps/v1/deployments").get("items", [])
    pods = (kget("/api/v1/pods") if pods is None else pods).get("items", [])
    # The loop's last reading, with its state: never waits on metrics-server (#906).
    usage, metrics = KUBE_METRICS.read("pods", kget)
    pm = {(m["metadata"]["namespace"], m["metadata"]["name"]): m for m in usage}
    try:
        svcs = kget("/api/v1/services").get("items", [])
    except Exception:
        svcs = []
    tiers = GROUPS.stored(kget)    # each group's tier (#430); None when it can't be read
    hardware_definitions = HW.features()    # one read for every app, not one each (#977)

    out = []
    for d in deps:
        ns, name = d["metadata"]["namespace"], d["metadata"]["name"]
        if ns in SYS_NS and ns not in PLATFORM_NS:
            continue
        sel = d["spec"].get("selector", {}).get("matchLabels", {})
        mine = [p for p in pods if p["metadata"]["namespace"] == ns and
                all(p["metadata"].get("labels", {}).get(k) == v for k, v in sel.items())]
        cpu = mem = 0.0
        for p in mine:
            m = pm.get((ns, p["metadata"]["name"]))
            if m:
                for c in m.get("containers", []):
                    cpu += parse_cpu(c.get("usage", {}).get("cpu"))
                    mem += parse_mem(c.get("usage", {}).get("memory"))
        ports = []
        for s in svcs:
            if s["metadata"]["namespace"] != ns: continue
            ssel = s["spec"].get("selector") or {}
            if ssel and all(sel.get(k) == v for k, v in ssel.items()):
                ip = ""
                ing = s.get("status", {}).get("loadBalancer", {}).get("ingress", [])
                if ing: ip = ing[0].get("ip", "")
                for pt in s["spec"].get("ports", []):
                    ports.append({"port": pt.get("port"), "ip": ip, "name": pt.get("name", ""),
                                  "protocol": str(pt.get("protocol") or "TCP").upper()})
        starts = [p["status"].get("startTime") for p in mine if p["status"].get("startTime")]
        uptime = max([age_secs(x) for x in starts], default=0) if starts else 0
        st = d.get("status", {})
        pspec = d["spec"]["template"]["spec"]
        annotations = d["metadata"].get("annotations", {}) or {}
        # On its own LAN address it answers on its container ports there,
        # with no Service in between.
        lan_ip = (LAN.read(d) or {}).get("address", "")
        if lan_ip and not ports:
            ports = [{"port": cp.get("containerPort"), "ip": lan_ip, "name": cp.get("name", ""), "lan": True,
                      "protocol": str(cp.get("protocol") or "TCP").upper()}
                     for c in pspec.get("containers", []) or [] for cp in c.get("ports", []) or []
                     if cp.get("containerPort")]
        # The port chosen as the app's own - its web UI, usually - comes
        # first, so the card's first link is the one people want.
        try:
            primary = int(NAMES.read(annotations, "primary-port") or 0)
        except ValueError:
            primary = 0
        if primary:
            ports.sort(key=lambda row: row.get("port") != primary)
            for row in ports:
                row["primary"] = row.get("port") == primary
        hardware = HW.workload_features(pspec, annotations, hardware_definitions)
        pod_rows = []
        transition_ages = []
        fatal_waits = []
        fatal_reasons = {"ImagePullBackOff", "ErrImagePull", "CrashLoopBackOff",
                         "CreateContainerConfigError", "CreateContainerError"}
        for p in mine:
            conditions = {c.get("type"): c for c in p.get("status", {}).get("conditions", []) or []}
            ready = conditions.get("Ready", {}).get("status") == "True"
            waits = []
            for cs in p.get("status", {}).get("containerStatuses", []) or []:
                waiting = (cs.get("state", {}).get("waiting") or {})
                if waiting:
                    reason = waiting.get("reason", "Waiting")
                    waits.append({"container": cs.get("name", ""), "reason": reason,
                                  "message": (ROLLOUTS.explain_pull(waiting.get("message")) or waiting.get("message") or "")[:400]})
                    if reason in fatal_reasons:
                        why = ROLLOUTS.explain_pull(waiting.get("message"))
                        fatal_waits.append(f"{p['metadata']['name']}: {reason}" + (f" - {why}" if why else ""))
            if not ready:
                # Not ready since its Ready condition last changed: a restart
                # in a days-old pod is seconds old, not days (#941).
                transition_ages.append(age_secs(conditions.get("Ready", {}).get("lastTransitionTime")
                                                or p["metadata"].get("creationTimestamp")))
            # Pending and unscheduled says nothing; the scheduler said why.
            unplaced = ""
            placed = conditions.get("PodScheduled") or {}
            if placed.get("status") == "False" and not p["metadata"].get("deletionTimestamp"):
                own = []
                if pspec.get("hostNetwork") and INV.servicelb_present():
                    wanted = {cp.get("containerPort") for c in pspec.get("containers", []) or []
                              for cp in c.get("ports", []) or []}
                    own = sorted({row["port"] for row in ports if row.get("port") in wanted})
                unplaced = ROLLOUTS.explain_unplaced(placed.get("message", ""), own)
                if age_secs(p["metadata"].get("creationTimestamp")) > 60:
                    fatal_waits.append(f"{p['metadata']['name']} cannot be placed: {unplaced}")
            containers = pod_container_rows(p)
            # A pod still fetching its image says how far it has got: events
            # say Pulling, containerd says how many bytes.
            pull = {}
            if not ready and not p["metadata"].get("deletionTimestamp") and any(
                    w["reason"] in ("ContainerCreating", "PodInitializing") for w in waits):
                try:
                    pull = ROLLOUTS.pull_state(ns, p["metadata"]["name"]) or {}
                    if pull.get("state") == "pulling":
                        pull.update(_pull_progress(p["spec"].get("nodeName", ""), pull.get("image", "")))
                    else:
                        pull = {}
                except Exception:
                    pull = {}
            pod_rows.append({"name": p["metadata"]["name"], "hostname": p["spec"].get("hostname", ""),
                             "ip": (p.get("status") or {}).get("podIP", ""),
                             "phase": p["status"].get("phase"),
                             "node": p["spec"].get("nodeName", ""), "ready": ready,
                             # Told to stop, and not stopped yet.
                             "terminating": bool(p["metadata"].get("deletionTimestamp")),
                             "unplaced": unplaced,
                             "pull": pull,
                             "waiting": waits,
                             "uptime": age_secs(p["status"].get("startTime")),
                             "containers": containers,
                             "container_count": len([c for c in containers if c["kind"] == "app"]),
                             "restarts": sum(c.get("restartCount", 0) for c in
                                             p["status"].get("containerStatuses", []) or [])})
        progress_errors = [c.get("message") or c.get("reason") or "rollout failed"
                           for c in st.get("conditions", []) or []
                           if c.get("type") == "Progressing" and c.get("status") == "False"]
        template_annotations = d["spec"].get("template", {}).get("metadata", {}).get("annotations", {}) or {}
        rollout_at = (NAMES.read(template_annotations, "update-rollout-at") or
                      NAMES.read(template_annotations, "restartedAt"))
        if rollout_at:
            transition_ages.append(age_secs(rollout_at))
        if not transition_ages:
            transition_ages.append(age_secs(d["metadata"].get("creationTimestamp")))
        out.append({
            "ns": ns, "name": name, "kind": "Deployment", "uptime": uptime,
            "ready": st.get("readyReplicas", 0) or 0,
            "desired": d["spec"].get("replicas", 0) or 0,
            "available": st.get("availableReplicas", 0) or 0,
            "updated": st.get("updatedReplicas", 0) or 0,
            "unavailable": st.get("unavailableReplicas", 0) or 0,
            "generation": d["metadata"].get("generation", 0) or 0,
            "observed_generation": st.get("observedGeneration", 0) or 0,
            "transition_age": min(transition_ages),
            "problems": fatal_waits + progress_errors,
            "images": [c["image"] for c in pspec.get("containers", [])],
            # Its volumes, by claim, so the row can lead to them.
            "claims": sorted({(v.get("persistentVolumeClaim") or {}).get("claimName") for v in pspec.get("volumes") or []
                              if (v.get("persistentVolumeClaim") or {}).get("claimName")}),
            "nodes": sorted({p["spec"].get("nodeName", "") for p in mine if p["spec"].get("nodeName")}),
            "pods": pod_rows,
            "pod_count": len(pod_rows),
            "container_count": sum(p["container_count"] for p in pod_rows),
            "cpu": round(cpu, 3), "mem_mb": round(mem / 1024**2, 1), "metrics": metrics,
            "ports": ports,
            "gpu": "igpu" in hardware,
            "hardware": hardware,
            "icon": display_icon(annotations),
            # A logo whose cached copy is being fetched again still counts as one.
            "has_logo": bool(NAMES.read(annotations, "icon")),
            "logo_skipped": NAMES.read(annotations, "logo-skipped") == "true",
            # The Helm release it belongs to, whose chart may name a logo.
            "helm_release": annotations.get("meta.helm.sh/release-name", ""),
            "answer_check": NAMES.read(annotations, "uptime"),
            "schedule": SCHEDULES.read(annotations, NAMES),
            "outage_actions": OUTAGE.read(annotations, NAMES),
            "update_mode": "auto" if NAMES.read(annotations, AUTOUPDATE.MODE) == "auto" else "manual",
            "restore_tested": RESTORE_TEST.last_result(annotations),
            "group": NAMES.read(annotations, "group") or own_group(ns, name),
            "group_tier": None if tiers is None else tiers.get(NAMES.read(annotations, "group"), ""),
            "self": is_self(ns, name),
            "managed_smb": is_managed_smb(ns, name),
            "managed_nfs": is_managed_nfs(ns, name),
            "platform": "Homestead" if homestead_part(ns, name) else PLATFORM_NS.get(ns, ""),
            "homestead": homestead_part(ns, name),
            "failover": FAILOVER.mode_of(pspec),
            "lan": (LAN.read(d) or {}).get("address", ""),
            # Starts after (#746): its setting, its gate, and whether a pod waits.
            **_start_fields(d, mine, deps),
        })
    return sorted(out, key=lambda x: (x["ns"], x["name"]))


def _start_fields(dep, pods, deps):
    """Starts after's row fields; a setting that breaks them marks that app
    and never the whole list."""
    try:
        return START_ORDER.row_fields(dep, pods, deps)
    except Exception as error:
        print(f"workloads: starts-after of {dep['metadata'].get('namespace')}/{dep['metadata'].get('name')} "
              f"not read: {str(error)[:160]}", flush=True)
        return START_ORDER.row_unread()


def set_workload_logos(b):
    """Gives workloads a logo, or takes it away with a blank one.

    Only the Deployment's own annotations change, never its pod template, so
    nothing restarts. Each logo is fetched and kept like one typed in Edit."""
    items = b.get("items") or []
    if not items:
        raise ValueError("choose at least one workload")
    if len(items) > 200:
        raise ValueError("at most 200 workloads at a time")
    plans = []
    for item in items:
        ns, name = _dns_name(item.get("ns"), "namespace"), _dns_name(item.get("name"), "workload name")
        guard_managed_smb(ns, name)
        source = str(item.get("icon") or "").strip()
        if source and not source.startswith(("https://", "http://")):
            raise ValueError("a logo is a public http:// or https:// image URL")
        plans.append((ns, name, source, item.get("skip") is True))
    done, saved, skipped, failed = 0, 0, 0, []
    for ns, name, source, skip in plans:
        try:
            if skip:      # no logo, on purpose: it is not counted as missing again
                patch = {"metadata": {"annotations": {NAMES.key("logo-skipped"): "true"}}}
            else:
                cached_ref = ICONS.persist(source, DATA_DIR) if source else ""
                patch = {"metadata": {"annotations": {NAMES.key("icon"): cached_ref or None,
                                                      NAMES.key("icon-source"): source or None,
                                                      NAMES.key("logo-skipped"): None}}}
            ksend("PATCH", f"/apis/apps/v1/namespaces/{ns}/deployments/{name}", patch,
                  ctype="application/merge-patch+json")
            done += 1
            saved += bool(source and not skip)
            skipped += skip
        except Exception as e:
            failed.append(f"{name}: {e}")
    ROUTER.forget("wl")
    detail = ", ".join(part for part in (saved and f"{saved} logo{'s' if saved != 1 else ''} saved",
                                         skipped and f"{skipped} left without one",
                                         done and not saved and not skipped and "logo removed") if part)
    return {"ok": not failed, "done": done, "failed": failed,
            "detail": detail + (f"; {len(failed)} could not be: " + "; ".join(failed[:3]) if failed else "")}


def move_capacity_plan(body):
    """Review the exact host-placement edit without stopping or writing anything."""
    ns, name = _dns_name(body.get("ns"), "namespace"), _dns_name(body.get("name"), "workload name")
    guard_managed_smb(ns, name)
    if body.get("auto"):
        raise ValueError("select an explicit suggested host and review it before moving")
    node = body.get("node") or None
    if node and (not isinstance(node, str) or len(node) > 253 or
                 not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in node.split("."))):
        raise ValueError("host must be a valid Kubernetes node name")
    if not isinstance(body.get("pin", False), bool):
        raise ValueError("pin must be true or false")
    cache = {}
    def read(path):
        if path not in cache:
            cache[path] = kget(path)
        return copy.deepcopy(cache[path])
    current = read(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")
    context = {"action": "host-move", **rollout_review_context(current)}
    if current["spec"].get("paused"):
        raise ValueError("paused workloads cannot be moved; resume and review placement again")
    if current["spec"]["template"]["spec"].get("nodeName"):
        raise ValueError("this pod template uses a direct nodeName binding; remove it before using reviewed host placement")
    # A move stops the old pods. Never interpret incomplete ownership as free
    # capacity, nor invite consent to stopping a workload on that assumption.
    pods = ROLLOUT_CAPACITY.items(read, "/api/v1/pods")
    _, known = ROLLOUT_CAPACITY.owned_pods(current, pods, read, ns)
    if not known:
        raise ValueError("pod ownership is unavailable; no move can be reviewed until inventory is complete")
    nodes = copy.deepcopy(PLACE.get_nodes())
    if node and not any(n["name"] == node for n in nodes):
        raise ValueError("selected host no longer exists; review placement again")
    proposed = copy.deepcopy(current)
    mode = PLACE.apply_node_placement(proposed, node, body.get("pin", False))
    threshold = get_app_settings()["thresholds"]["memory"]["critical"]
    def planner(*args, **kwargs):
        return PLACE.manifest_plan(*args, **kwargs, read=read)
    def review(dep):
        return ROLLOUT_CAPACITY.plan(current, dep, ns, read, lambda: nodes, planner, threshold)
    capacity = review(proposed)
    # Check the chosen destination even for a soft preference, where Kubernetes
    # might otherwise hide its shortage by finding room on the current host.
    target = None
    if node:
        pinned = copy.deepcopy(proposed)
        pinned["spec"]["template"]["spec"].setdefault("nodeSelector", {})["kubernetes.io/hostname"] = node
        target = review(pinned)
        capacity["blocked"] = capacity["blocked"] or target["blocked"]
    capacity["move"] = {"node": node, "mode": mode, "target": target}
    capacity["warnings"].append("A preferred host is not guaranteed; Kubernetes may choose another eligible host, including the current host. Hard pinning prevents failover.")
    spec = proposed["spec"]["template"]["spec"]
    if any("hostPath" in v for v in spec.get("volumes", [])):
        capacity["warnings"].append("Host paths are not copied: files and devices may differ on the destination. Verify required host-local data before moving.")
    if any("emptyDir" in v for v in spec.get("volumes", [])):
        capacity["warnings"].append("Pod-local emptyDir data is lost when old pods are replaced.")
    capacity["requires_confirmation"] = True
    return proposed, capacity, context


def preview_host_move(body):
    current, capacity, context = move_capacity_plan(body)
    return {"capacity": capacity, "capacity_token": CAPACITY_REVIEW.issue(body, context),
            "managed": GITOPS.notice(current)}


def reviewed_host_move(body):
    proposed, capacity, context = move_capacity_plan(body)
    CAPACITY_REVIEW.enforce(body, capacity, context)
    meta = proposed["metadata"]
    # Preserve resourceVersion: a concurrent controller edit must conflict,
    # never be silently overwritten by a second, unreviewed fetch-and-move.
    ksend("PUT", f"/apis/apps/v1/namespaces/{meta['namespace']}/deployments/{meta['name']}", proposed)
    PLACE._bust("wl", "ov", "flow", "nodes", "impact:")
    return {"ok": True, "moved": meta["name"], "to": capacity["move"]["node"] or "any node",
            "mode": capacity["move"]["mode"]}


def edit_capacity_plan(config):
    """Build the exact edit before seed/PVC/icon or workload writes."""
    ns, name = LC.dns_label(config["ns"], "namespace"), LC.dns_label(config["name"], "workload name")
    current = kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")
    HOSTACCESS.require_target(current, ns)
    if config.get("workload_name") and config["workload_name"] != name:
        new_name = LC.dns_label(config["workload_name"], "new workload name")
        if (config["ns"], config["name"], config["workload_name"]) != (ns, name, new_name):
            raise ValueError("Use exact lowercase Kubernetes names for the rename")
        guard_self(ns, name, renaming=True)
        proposed, context = RENAME.prepare(config, current, WORKLOAD_EDIT._renamed_deployment, kget)
        post_stop = copy.deepcopy(proposed)
        post_stop["spec"]["strategy"] = {"type": "Recreate"}  # rename orchestration, not a saved strategy change
        plan = ROLLOUT_CAPACITY.plan(current, post_stop, ns, kget, PLACE.get_nodes, PLACE.manifest_plan,
                                     get_app_settings()["thresholds"]["memory"]["critical"])
        plan["requires_confirmation"] = True
        plan["rename"] = {"from": name, "to": new_name}
        plan["warnings"].append("Rename stops the old pods before starting the new workload. Volumes and service addresses are kept. Failed or uncertain steps retain resources for inspection, not automatic rollback.")
        return {"deployment": proposed, "name": new_name, "claims": [], "seeds": []}, context, plan
    if config.get("remove_containers") and is_self(ns, name):
        raise ValueError("Homestead cannot remove its own containers from this page")
    prepared = WORKLOAD_EDIT.prepare_edit(config, current=current)
    context = {"action": "edit", **rollout_review_context(current),
               "seeds": [(path, cm.get("metadata", {}).get("resourceVersion")) for path, cm in prepared["seeds"]]}
    proposed = copy.deepcopy(prepared["deployment"])
    HOSTACCESS.require_edit(current, proposed, hardware_device_paths())
    if proposed["spec"].get("paused") and proposed["spec"].get("replicas", 1) > current["spec"].get("replicas", 1):
        raise ValueError("Increasing replicas of a paused Deployment needs a separate capacity review; resume it before editing replicas")
    if prepared["name"] != name:
        count = proposed["spec"].get("replicas", 1)
        proposed = WORKLOAD_EDIT._renamed_deployment(proposed, ns, prepared["name"])
        proposed["spec"].update(replicas=count, strategy={"type": "Recreate"})
    moves = RESTRUCTURE.copies(config)
    if moves:
        if prepared["name"] != name:
            raise ValueError("rename the workload and move its data in separate saves")
        if "lan" in config and (LAN.clean(config["lan"]) if config.get("lan") else None) != (LAN.read(current) or None):
            raise ValueError("Change the LAN address and move storage in separate saves")
        proposed["spec"]["strategy"] = {"type": "Recreate"}
        COPY_JOB.guards(current, kget)
        COPY_JOB.validate_sources(config, current)
    threshold = get_app_settings()["thresholds"]["memory"]["critical"]
    plan = ROLLOUT_CAPACITY.plan(current, proposed, ns, kget, PLACE.get_nodes, PLACE.manifest_plan,
                                 threshold, planned_claims={row["name"]: row for row in prepared["claims"]})
    if moves:
        helper = COPY_JOB.helper_manifest(ns, name, moves)
        helper_plan = ROLLOUT_CAPACITY.plan(current, helper, ns, kget, PLACE.get_nodes, PLACE.manifest_plan,
            threshold, planned_claims={row["name"]: row for row in prepared["claims"]})
        plan["copy_helper"] = helper_plan
        plan["blocked"] = plan["blocked"] or helper_plan["blocked"]
        plan["requires_confirmation"] = True
        plan["warnings"] += ["Copy helper: " + warning for warning in helper_plan["warnings"]]
        plan["warnings"].append("Storage copy stops all containers. Placement is checked again before copying and restarting. Failed or uncertain steps keep the data and require inspection; no automatic rollback or deletion.")
        plan["warnings"].append("Destination files may be merged or overwritten. This is not a backup or filesystem free-space/consistency guarantee; avoid external writers during the copy.")
    if moves or prepared["name"] != name or proposed["spec"].get("replicas", 1):
        import_blocker = PLACE.IMPORT_GUARD.pending(current, ns, kget)
        if import_blocker:
            plan["blocked"] = True
            plan["warnings"].append(import_blocker)
    additions = [row.get("name") for row in config.get("containers") or [] if row.get("new") is True]
    removals = config.get("remove_containers") or []
    if additions or removals:
        exposed = [port for container in config.get("containers") or []
                   for port in container.get("ports") or [] if port.get("expose")]
        if exposed and config.get("manage_ports"):
            SVC._ports({"ports": [{"name": port.get("name"), "port": port.get("host") or port.get("container"),
                                      "target_port": port.get("container"), "protocol": port.get("protocol") or "TCP"}
                                     for port in exposed]})
        plan["container_changes"] = {"added": additions, "removed": removals}
        plan["requires_confirmation"] = True
        plan["warnings"].append("Changing the containers rolls every pod in this workload. Removed containers stop; persistent volumes and their data are retained.")
    if config.get("lan"):
        plan["warnings"].append("LAN network attachment availability is not guaranteed by the memory and placement review")
    return prepared, context, plan


def workload_edit_payload(ns, name, deployment, hardware_definitions=None, services=None):
    """Return pod-level settings plus an editable record for every app container."""
    pspec = deployment["spec"]["template"]["spec"]
    if services is None:
        try:
            services = kget(f"/api/v1/namespaces/{ns}/services").get("items", [])
        except Exception:
            services = []
    listeners = _service_listeners(deployment, services)
    annotations = deployment["metadata"].get("annotations", {}) or {}
    definitions = hardware_definitions if hardware_definitions is not None else HW.features()
    # Variables kept in the workload's own Secret are edited like any other.
    kept, own_secret = ENVSEC.values(ns, deployment, kget), ENVSEC.own(deployment)
    volumes = {volume.get("name"): volume for volume in pspec.get("volumes", []) or []}

    def source_label(volume):
        if volume.get("persistentVolumeClaim"):
            return volume["persistentVolumeClaim"].get("claimName", "")
        for field, label in (("configMap", "ConfigMap"), ("secret", "Secret")):
            if volume.get(field):
                return f"{label} {volume[field].get('name', '')}".strip()
        if volume.get("hostPath"):
            return volume["hostPath"].get("path", "host path")
        if "emptyDir" in volume:
            return "temporary storage"
        return "Kubernetes volume"

    def storage_kind(volume):
        """Map a pod volume onto the storage picker's vocabulary.

        Only claims, host paths and emptyDir are editable as storage. ConfigMap
        and Secret volumes are Kubernetes wiring that the editor shows but does
        not offer to repoint.
        """
        if volume.get("persistentVolumeClaim"):
            return "existing", volume["persistentVolumeClaim"].get("claimName", "")
        if volume.get("hostPath"):
            return "host", volume["hostPath"].get("path", "")
        if "emptyDir" in volume:
            empty = volume.get("emptyDir") or {}
            if str(empty.get("medium", "")).lower() == "memory":
                return "memory", str(empty.get("sizeLimit", "") or "")
            return "ephemeral", ""
        for field in ("configMap", "secret"):
            if volume.get(field):
                return field, volume[field].get("name", volume[field].get("secretName", ""))
        return "other", ""

    def env_reference(item):
        ref = item.get("valueFrom", {}) or {}
        for field, label in (("secretKeyRef", "Secret"), ("configMapKeyRef", "ConfigMap")):
            if ref.get(field):
                value = ref[field]
                return f"{label} {value.get('name', '')} · {value.get('key', '')}".strip(" ·")
        if ref.get("fieldRef"):
            return f"Pod field · {ref['fieldRef'].get('fieldPath', '')}".strip(" ·")
        if ref.get("resourceFieldRef"):
            return f"Resource field · {ref['resourceFieldRef'].get('resource', '')}".strip(" ·")
        return "Managed Kubernetes reference"

    containers = []
    for container in pspec.get("containers", []) or []:
        mounts, hardware = [], []
        for mount in container.get("volumeMounts", []) or []:
            volume = volumes.get(mount.get("name"), {})
            kind, value = storage_kind(volume)
            host_path = (volume.get("hostPath") or {}).get("path", "").rstrip("/")
            mount_path = str(mount.get("mountPath") or "").rstrip("/")
            device = False
            for feature in definitions:
                expected_host = feature["host_path"].rstrip("/")
                if (host_path == expected_host or host_path.startswith(expected_host + "/")) and mount_path == feature["container_path"].rstrip("/"):
                    device = True
                    if feature["id"] not in hardware:
                        hardware.append(feature["id"])
            mounts.append({"name": mount.get("name", ""), "path": mount.get("mountPath", ""),
                           "source": source_label(volume), "read_only": bool(mount.get("readOnly", False)),
                           "sub_path": mount.get("subPath", ""),
                           "kind": kind, "value": value,
                           "managed": device or kind in ("configMap", "secret", "other")})
        own_values = kept.get(container.get("name"), {})
        literals = {item["name"]: item.get("value", "") if "valueFrom" not in item else own_values[item["name"]]
                    for item in container.get("env", []) or []
                    if item.get("name") and ("valueFrom" not in item or item["name"] in own_values)}
        refs = [{"name": item["name"], "source": env_reference(item)}
                for item in container.get("env", []) or []
                if item.get("name") and item.get("valueFrom") and item["name"] not in own_values]
        requests = (container.get("resources", {}) or {}).get("requests", {}) or {}
        limits = (container.get("resources", {}) or {}).get("limits", {}) or {}
        containers.append({
            "original_name": container.get("name", ""), "name": container.get("name", ""),
            "image": container.get("image", ""), "cpu": requests.get("cpu", ""), "memory": requests.get("memory", ""),
            "memory_limit": limits.get("memory", ""),
            "usage": USAGE.summary(ns, name, container.get("name", ""), requests, limits, tier=PRIORITY.tier_of(pspec)[0]),
            "env": literals, "env_refs": refs, "secret_env": sorted(own_values),
            "ports": [{"container": port.get("containerPort"), "name": port.get("name", ""),
                       "protocol": port.get("protocol", "TCP"),
                       "host": listeners.get((str(port.get("protocol") or "TCP").upper(),
                                              port.get("containerPort")), port.get("containerPort")),
                       "expose": (str(port.get("protocol") or "TCP").upper(),
                                  port.get("containerPort")) in listeners}
                      for port in container.get("ports", []) or []],
            "hardware": hardware,
            "volumes": [m for m in mounts if m["name"] != PRIV.TUN_VOLUME],
            "privileges": PRIV.read(container, pspec),
        })
    detected = HW.workload_features(pspec, annotations, definitions)
    assigned = {feature for container in containers for feature in container["hardware"]}
    if containers:
        containers[0]["hardware"].extend(feature for feature in detected if feature not in assigned)
    first = containers[0] if containers else {"name": "", "image": "", "cpu": "", "memory": "", "memory_limit": "", "env": {}, "ports": [], "volumes": []}
    reusable = []
    device_paths = {feature["host_path"].rstrip("/") for feature in definitions}
    for volume in pspec.get("volumes", []) or []:
        kind, value = storage_kind(volume)
        if kind not in ("existing", "host", "ephemeral", "memory"):
            continue
        if kind == "host" and any(value.rstrip("/") == device or value.rstrip("/").startswith(device + "/")
                                  for device in device_paths):
            continue
        reusable.append({"name": volume.get("name", ""),
                         "kind": {"existing": "pvc", "ephemeral": "emptyDir"}.get(kind, kind),
                         "source": value})
    replicas = deployment["spec"].get("replicas", 1) or 0
    try:
        parked = int(annotations.get(LC.AUTOSTART_REPLICAS, "") or 0)
    except ValueError:
        parked = 0
    return {
        "ns": ns, "name": name, "pod_hostname": pspec.get("hostname", ""),
        "service_links": pspec.get("enableServiceLinks") is not False,
        "replicas": replicas, "autostart": replicas > 0,
        "start_replicas": replicas or parked or 1, "containers": containers,
        "pod_volumes": reusable,
        "hardware": detected, "icon": NAMES.read(annotations, "icon-source") or NAMES.read(annotations, "icon"),
        # How it is monitored, so Edit shows it rather than Automatic - and
        # saving an edit keeps it.
        "answer_check": NAMES.read(annotations, "uptime"),
        "node": pspec.get("nodeSelector", {}).get("kubernetes.io/hostname", ""),
        "placement": AFFINITY.public(deployment),
        "failover": FAILOVER.mode_of(pspec),
        # Homestead's own follows the apps': critical once any is (PRIORITY.protect).
        "priority": "self" if is_self(ns, name) else PRIORITY.tier_of(pspec)[0], "priority_class": PRIORITY.tier_of(pspec)[1],
        # Flux or Argo CD when they apply it: Edit then shows its tier read-only (#429).
        "priority_owner": "" if is_self(ns, name) else (UPDATES.managed_by(deployment) or {}).get("by", ""),
        # #430: "group" for an app that follows its group, its group, and that group's tier.
        **({} if is_self(ns, name) else GROUPS.edit_fields(deployment, PRIORITY.tier_of(pspec)[0], kget)),
        "lan": LAN.read(deployment),
        "network_mode": "host" if pspec.get("hostNetwork") else "",
        # Starts after (#746): the setting, and whether its gate is in the template.
        "starts_after": START_ORDER.read(deployment), "start_gate": START_ORDER.gate_state(deployment),
        "has_service": bool(listeners),
        "seed_configs": WORKLOAD_EDIT.seed_configs(ns, deployment),
        "container_name": first["name"], "image": first["image"], "cpu": first["cpu"], "memory": first["memory"],
        "memory_limit": first["memory_limit"],
        "env": first["env"], "ports": first["ports"], "volumes": first["volumes"], "gpu": "igpu" in detected,
    }


# Its routes and who may use them (homestead_routes.py). server.py checks
# /api/move's target is an app before it gets here.
ROUTES = {
    ("GET", "/api/workloads"): ("viewer", lambda request: ROUTER.page("wl", get_workloads)),
    ("GET", "/api/workloads/start-plan"): ("viewer", lambda request: workload_start_plan(
        _arg(request, "ns"), _arg(request, "name"), int((request.query.get("replicas") or [1])[0]))),
    ("GET", "/api/workload"): ("viewer", edit_form),
    ("GET", "/api/logs"): ("viewer", logs),
    ("GET", "/api/logs/search"): ("viewer", search_logs),
    ("POST", "/api/workloads/logo"): ("operator", lambda request: set_workload_logos(request.body)),
    ("POST", "/api/workloads/failover"): ("operator", set_failover),
    ("POST", "/api/workload/primary-port"): ("operator", set_primary_port),
    ("POST", "/api/scale"): ("operator", scale),
    ("POST", "/api/restart"): ("operator", lambda request: restart_workload(request.body["ns"], request.body["name"])),
    ("POST", "/api/edit/preview"): ("operator", preview_edit),
    ("POST", "/api/edit"): ("operator", edit),
    ("POST", "/api/move/preview"): ("operator", lambda request: preview_host_move(request.body)),
    ("POST", "/api/move"): ("operator", move_to_host),
}
