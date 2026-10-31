"""Deploy: a new app from the form, the App Store or a Compose file, and the
preview that reviews it first.

server.py used to answer all of this in place (#363). The routes are declared
here now; the helpers they still share with server.py's other code are bound
by name (bind), so a test that replaces one in server.py replaces it here too.
The App Store catalogue and the Deployment builders live by feature in
homestead_deploy_appstore and homestead_deploy_manifests (#623): read and
replace their names there.
"""
import copy
import urllib.request

import homestead_batch_capacity as BATCH_CAPACITY
import homestead_capacity_review as CAPACITY_REVIEW
import homestead_env_secrets as ENVSEC
import homestead_lan as LAN
import homestead_operations as OPS
import homestead_place as PLACE
import homestead_priority as PRIORITY
import homestead_rollout_capacity as ROLLOUT_CAPACITY
import homestead_routes as ROUTER
import homestead_volowner as VOLOWNER

# server.py's helpers this module calls, bound by name at start.
NEEDS = ("kget", "ksend", "cached", "get_app_settings", "guard_managed_smb", "reviewed_deploy", "deploy_options",
         "compose_report", "compose_preview", "compose_apply", "_dns_name", "rollout_review_context",
         "deploy_capacity_plan", "prepare_deploy_network", "apply_deploy_bindings", "apply_generated_secrets",
         "redact_deployment_preview", "additional_container_configs", "deployment_volumes", "new_claims",
         "ensure_claim", "ensure_profile_compatible", "persist_icon_config", "prepare_lan", "record_lan")
# And its values: where apps go by default, and the release the App Store feed is asked for as.
DEFAULT_NS = "lab"
HOMESTEAD_VERSION = ""


def bind(helpers, *, default_ns, version):
    global DEFAULT_NS, HOMESTEAD_VERSION
    missing = set(NEEDS) - set(helpers)
    if missing:
        raise TypeError("homestead_deploy needs " + ", ".join(sorted(missing)))
    globals().update({name: helpers[name] for name in NEEDS})
    DEFAULT_NS, HOMESTEAD_VERSION = default_ns, version



def _arg(request, key, default=""):
    return (request.query.get(key) or [default])[0]


def _catalogue():
    """The App Store's apps, or the 502 that says its feed did not answer."""
    try:
        return APPSTORE.fetch_appstore(), None
    except APPSTORE.FeedLoading as e:
        return None, _loading(e)
    except Exception as e:
        return None, ROUTER.Reply(502, {"error": f"app feed unavailable: {e}"})


def _loading(error):
    """No catalogue yet: one is being read in the background (503, ask again)."""
    return ROUTER.Reply(503, {"error": str(error), "loading": True}, headers=(("Retry-After", "2"),))


def appstore(request):
    term = _arg(request, "q").lower().strip()
    cat = _arg(request, "cat").lower().strip()
    sort_mode = _arg(request, "sort", "home").lower().strip()
    if sort_mode not in {"home", "spotlight", "popular", "trending", "recent"}:
        sort_mode = "home"
    apps, refused = _catalogue()
    if refused:
        return refused
    source = {"url": APPSTORE.catalog_source(), "default": APPSTORE.catalog_source() == APPSTORE.CA_FEED}
    if sort_mode == "home" and not term and not cat:
        # The catalogue's front page, as Community Applications lays it out.
        return {"sort": "home", "total": len(apps), "source": source, "sections": {
            mode: [APPSTORE.appstore_summary(a) for a in APPSTORE.rank_appstore(apps, mode)[:count]]
            for mode, count in (("spotlight", 4), ("recent", 8), ("trending", 8), ("popular", 8))}}
    if sort_mode == "home":
        sort_mode = "popular"
    if term:
        apps = APPSTORE.search_appstore(apps, term)
    else:
        apps = APPSTORE.rank_appstore(apps, sort_mode)
    if cat:
        apps = [a for a in apps if any(cat in value.lower() for value in a.get("categories", []))]
    spotlight = None if term else APPSTORE.appstore_spotlight(apps)
    limit = 60 if term else 30
    return {"total": len(apps), "apps": [APPSTORE.appstore_summary(a) for a in apps[:limit]],
            "sort": "search" if term else sort_mode, "source": source,
            "spotlight": APPSTORE.appstore_summary(spotlight) if spotlight else None}


def appstore_app(request):
    key = _arg(request, "key")
    apps, refused = _catalogue()
    if refused:
        return refused
    app = next((a for a in apps if APPSTORE.appstore_key(a) == key), None)
    if not app:
        return ROUTER.Reply(404, {"error": "that app is no longer in the catalogue"})
    return APPSTORE.appstore_detail(app)


def appstore_install(request):
    b = request.body
    cfg = APPSTORE.template_to_cfg(b["app"])
    cfg.update(b.get("overrides") or {})
    # Older API clients must use the same review as the current UI.
    # The token is issued by /api/preview for the resolved template
    # plus overrides, not for the catalogue identifier alone.
    cfg.update(capacity_token=b.get("capacity_token"), confirm_capacity=b.get("confirm_capacity") is True)
    result = reviewed_deploy(cfg)
    reused = result.get("reused_volumes") or []
    if reused:
        result["detail"] = f"kept the existing {', '.join(reused)} - its data carries on"
    return result


def preview(request):
    """What Deploy would create, and its capacity review, without creating it."""
    b = MANIFESTS.analyze_deploy_intent(request.body)
    current = None
    context = None
    if b.get("target_mode") == "existing":
        ns = _dns_name(b.get("namespace") or DEFAULT_NS, "namespace")
        target = _dns_name(b.get("target_workload"), "existing workload")
        current = kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{target}")
        context = rollout_review_context(current)
    capacity = deploy_capacity_plan(b, existing=current)
    capacity_token = CAPACITY_REVIEW.issue(b, context)
    b = prepare_deploy_network(b)
    b = apply_deploy_bindings(b)
    b = apply_generated_secrets(b)
    if b.get("target_mode") == "existing":
        target = _dns_name(b.get("target_workload"), "existing workload")
        dep, svc = MANIFESTS.build_sidecar_deployment(b, current)
        return {"deployment": redact_deployment_preview(dep, b), "service": svc,
                "capacity": capacity, "capacity_token": capacity_token,
                "app_profile": b.get("app_profile"),
                "impact": {"mode": "existing", "workload": target,
                           "containers": [c.get("name") for c in current.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])],
                           "message": "Saving updates the Deployment template and restarts every container in its pods."}}
    dep, svc = MANIFESTS.build_deployment(b)
    return {"deployment": redact_deployment_preview(dep, b), "service": svc,
            "capacity": capacity, "capacity_token": capacity_token,
            "app_profile": b.get("app_profile"),
            "impact": {"mode": "new", "workload": dep["metadata"]["name"],
                       "message": "Creates a new independently managed Deployment."}}


def run_deploy(b, *, reviewed_current=None):
    """Create (or join) a workload. HTTP Deploy uses reviewed_deploy first."""
    b = MANIFESTS.analyze_deploy_intent(b)
    ns = b.get("namespace") or DEFAULT_NS
    target = b.get("target_workload") if b.get("target_mode") == "existing" else b.get("workload_name") or b.get("name")
    guard_managed_smb(ns, target)
    b = ensure_profile_compatible(b)
    persist_icon_config(b)
    b = prepare_deploy_network(b)
    b = apply_deploy_bindings(b)
    b = apply_generated_secrets(b)
    b = prepare_lan(b)
    ns = b.get("namespace") or DEFAULT_NS
    target_mode = b.get("target_mode", "new")
    if target_mode == "new":
        b = VOLOWNER.prepare(b)
        b["additional_containers"] = [VOLOWNER.prepare(item) for item in additional_container_configs(b)]
    if target_mode == "existing":
        target = _dns_name(b.get("target_workload"), "existing workload")
        current = copy.deepcopy(reviewed_current) if reviewed_current is not None else kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{target}")
        dep, svc = MANIFESTS.build_sidecar_deployment(b, current)
    elif target_mode == "new":
        dep, svc = MANIFESTS.build_deployment(b)
        target = dep["metadata"]["name"]
        # The class exists before the Deployment names it, as Edit does it.
        if MANIFESTS.deploy_tier(b):
            PRIORITY.ensure([MANIFESTS.deploy_tier(b)])
    else:
        raise ValueError("deployment target must be new or existing")
    reused = []
    for claim in new_claims(deployment_volumes(b)):
        if ensure_claim(ns, claim["name"], claim["size_gb"], claim["storage_class"], claim["access_mode"]):
            reused.append(claim["name"])
    b["_reused_claims"] = reused
    if b.get("network_mode") == "lan" and target_mode == "new":
        LAN.ensure_nad(ns, target, b["lan"])
    masked = {item.get("key") for item in b.get("env_meta") or [] if item.get("masked")}
    ENVSEC.externalize(ns, dep, kget, ksend, masked)
    if target_mode == "existing":
        ksend("PUT", f"/apis/apps/v1/namespaces/{ns}/deployments/{target}", dep)
    else:
        ksend("POST", f"/apis/apps/v1/namespaces/{ns}/deployments", dep)
    if b.get("network_mode") == "lan" and target_mode == "new":
        record_lan(ns, target, b["lan"])
    if svc:
        ksend("POST", f"/api/v1/namespaces/{ns}/services", svc)
    ROUTER.forget("wl", "ov", "network")
    container_name = _dns_name(b.get("container_name") or b.get("name"), "container name")
    action = f"Add {container_name} to {target}" if target_mode == "existing" else f"Deploy {target}"
    op = OPS.start("deployment", action,
                   {"kind": "Deployment", "name": target, "namespace": ns},
                   "/containers", {"namespace": ns, "name": target,
                                   # What cancelling it undoes: a new one is removed,
                                   # an existing one goes back to its last version.
                                   "undo": "rollout" if target_mode == "existing" else "delete"})
    return {"ok": True, "name": target, "container": container_name, "operation": op,
            "reused_volumes": b.get("_reused_claims") or []}


def compose_capacity(entries, claims, created=None):
    """Fresh joint review, including created controllers whose pods lag behind."""
    created = created or {}
    ns = entries[0]["deployment"]["metadata"]["namespace"]
    cache = {}
    def read(path):
        if path not in cache:
            try:
                cache[path] = kget(path)
            except urllib.error.HTTPError as error:
                if error.code != 404:
                    raise
                cache[path] = error
        value = cache[path]
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)
    pods = ROLLOUT_CAPACITY.items(read, "/api/v1/pods")
    planned = copy.deepcopy(entries)
    starting_headroom = {}
    for entry in planned:
        previous = created.get(entry["name"])
        if previous is None:
            continue
        current = read(f"/apis/apps/v1/namespaces/{ns}/deployments/{entry['name']}")
        if current.get("metadata", {}).get("uid") != previous.get("metadata", {}).get("uid") or current.get("spec") != previous.get("spec"):
            raise ValueError(entry["name"] + " changed during batch creation; review the remaining services again")
        owned, known = ROLLOUT_CAPACITY.owned_pods(current, pods, read, ns)
        if not known:
            raise ValueError(entry["name"] + " pod ownership is unavailable; remaining services were not created")
        scheduled = [p for p in owned if p.get("spec", {}).get("nodeName") and not p.get("metadata", {}).get("deletionTimestamp")]
        for pod in scheduled:
            spec = pod["spec"]
            host = spec["nodeName"]
            starting_headroom[host] = starting_headroom.get(host, 0) + max(0,
                PLACE._pod_memory(spec)[0] - PLACE._pod_request(spec, "memory")) / 1024**3
        # Keep all scheduled/terminating consumers reserved. Replace only this
        # controller's unassigned pods with synthetic placements of its deficit.
        pending_ids = {id(p) for p in owned if not p.get("spec", {}).get("nodeName") and not p.get("metadata", {}).get("deletionTimestamp")}
        pods = [p for p in pods if id(p) not in pending_ids]
        entry["deployment"] = current
        entry["replicas"] = max(0, int(current["spec"].get("replicas", 1)) - len(scheduled))
    nodes = copy.deepcopy(PLACE.get_nodes())
    for node in nodes:
        node["batch_starting_headroom_gb"] = starting_headroom.get(node["name"], 0)
    return BATCH_CAPACITY.plan(planned, ns, pods, nodes, claims,
                               get_app_settings()["thresholds"]["memory"]["critical"], read=read)


# Its routes and who may use them (homestead_routes.py).
ROUTES = {
    ("GET", "/api/deploy/options"): ("viewer", lambda request: deploy_options(_arg(request, "ns", DEFAULT_NS))),
    ("GET", "/api/appstore"): ("viewer", appstore),
    ("GET", "/api/appstore/app"): ("viewer", appstore_app),
    ("POST", "/api/appstore/install"): ("operator", appstore_install),
    ("POST", "/api/preview"): ("operator", preview),
    ("POST", "/api/deploy"): ("operator", lambda request: reviewed_deploy(request.body)),
    ("POST", "/api/compose/parse"): ("operator", lambda request: compose_report(request.body)),
    ("POST", "/api/compose/preview"): ("operator", lambda request: compose_preview(request.body)),
    ("POST", "/api/compose/apply"): ("operator", lambda request: compose_apply(request.body)),
}


# The parts (#623), imported last: each imports this module for what bind() sets.
import homestead_deploy_appstore as APPSTORE  # noqa: E402
import homestead_deploy_manifests as MANIFESTS  # noqa: E402
