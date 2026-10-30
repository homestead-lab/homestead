"""Registry update discovery and deterministic Kubernetes rollouts: the bound
state, the "updated by something else" marks, and the routes. The work lives
by feature in homestead_update_registry, homestead_update_checks and
homestead_update_rollouts (#623), re-exported here.


Only the Python standard library is used. Registry credentials are read from
the workload's Kubernetes imagePullSecrets and are never returned by the API or
written to the update history. Applied images are pinned by manifest digest so
rollback cannot accidentally re-pull a broken mutable tag.
"""
import homestead_names as NAMES
import json
import time
import urllib.parse
import homestead_operations as OPS
import homestead_routes as ROUTER
import homestead_forced as FORCED

# The forced checks' answers (homestead_forced): forgotten when an image or the channel changes.
FORCED_KEYS = ("image-updates", "image-updates:homestead")


kget = ksend = None
DEFAULT_NS = "lab"
DATA_DIR = "/data"
SYSTEM_NAMESPACES = set()
SMB_NAMESPACE = "lab"
TRACKED = NAMES.key("update-sources")
PREVIOUS = NAMES.key("update-previous")
LAST_ACTION = NAMES.key("update-action")
# The digest each container last ran on, so a stopped workload can still be
# compared with the registry: without a pod there is nothing else to ask.
RAN = NAMES.key("ran-digests")
ROLLOUT_AT = NAMES.key("update-rollout-at")
# An administrator's mark that something else updates this app - plain CI,
# Renovate - where no GitOps tool leaves a marker of its own (#296).
MANAGED = NAMES.key("updates-managed")


def bind(_kget, _ksend, default_ns="lab", data_dir="/data", system_namespaces=None, smb_namespace="lab", channel=None):
    global kget, ksend, DEFAULT_NS, DATA_DIR, SYSTEM_NAMESPACES, SMB_NAMESPACE, CHANNEL
    kget, ksend, DEFAULT_NS, DATA_DIR = _kget, _ksend, default_ns, data_dir
    SYSTEM_NAMESPACES = set(system_namespaces or ())
    SMB_NAMESPACE = smb_namespace
    CHANNEL = channel or (lambda: "prod")


# Which part of Homestead a Deployment is ("self", "nfs", ...), or "" for an
# app: set by the server. Homestead's own updates are counted apart from
# apps' - they have their own place on the top bar and under Settings › About.
PART = lambda ns, name: ""
CHANNEL = lambda: "prod"
VERSION = lambda: ""


def _managed_smb(ns, name):
    return ns == SMB_NAMESPACE and name in (NAMES.object_name("smb"), "samba")


def _suffix(key):
    return key.split("/", 1)[-1]


def _annotation(dep, key, default=""):
    return NAMES.annotation_of(dep.get("metadata", {}), _suffix(key), default)


def _write(annotations, key, value):
    annotations[key] = value


def managed_by(dep):
    """Who updates this app instead of Homestead, if anyone (#296): Flux and
    Argo CD by the marks they leave on what they apply, or an administrator's
    mark. {} when Homestead does. An update made here would be undone at
    the owner's next sync, so Homestead keeps the notice and not the action."""
    meta = dep.get("metadata") or {}
    labels, annotations = meta.get("labels") or {}, meta.get("annotations") or {}
    own = str(_annotation(dep, MANAGED) or "").strip()
    if own:
        return {"by": own, "source": "", "detected": False}
    for kind, group in (("Kustomization", "kustomize.toolkit.fluxcd.io"), ("HelmRelease", "helm.toolkit.fluxcd.io")):
        if labels.get(f"{group}/name"):
            where = "/".join(x for x in (labels.get(f"{group}/namespace", ""), labels[f"{group}/name"]) if x)
            return {"by": "Flux", "source": f"{kind} {where}", "detected": True}
    # Argo CD: its tracking annotation (<app>:<group>/<kind>:<ns>/<name>), or
    # the label it sets when told to track by label.
    tracking = str(annotations.get("argocd.argoproj.io/tracking-id") or "")
    app = tracking.split(":", 1)[0] if tracking else str(labels.get("argocd.argoproj.io/instance") or "")
    if app:
        return {"by": "Argo CD", "source": f"Application {app}", "detected": True}
    return {}


def managed_message(ns, name, managed):
    where = f" ({managed['source']})" if managed.get("source") else ""
    if managed.get("detected"):
        return (f"{ns}/{name} is updated by {managed['by']}{where}: a change made here would be undone at its next "
                f"sync. Change its image where {managed['by']} reads it")
    return (f"{ns}/{name} is marked as updated elsewhere ({managed['by']}): change its image there, or clear "
            "the mark under Image updates to update it here")


def set_managed(ns, name, by):
    """Mark an app as updated elsewhere (by: who, in a few words), or clear
    the mark with an empty by. A GitOps tool's own marks are not changed."""
    by = str(by or "").strip()
    if len(by) > 60 or any(ord(ch) < 32 for ch in by):
        raise ValueError("say who updates it in 60 characters or fewer")
    dep = kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")
    if not by and managed_by(dep).get("detected") and not str(_annotation(dep, MANAGED) or "").strip():
        raise ValueError(f"{ns}/{name} is managed by {managed_by(dep)['by']}, which Homestead detects; there is no mark to clear")
    ksend("PATCH", f"/apis/apps/v1/namespaces/{ns}/deployments/{name}",
          {"metadata": {"annotations": {MANAGED: by or None}}}, ctype="application/merge-patch+json")
    invalidate()
    return {"ok": True, "ns": ns, "name": name,
            "managed": managed_by(kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}"))}


def _annotation_json(dep, key):
    try:
        parsed = json.loads(_annotation(dep, key, "{}") or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


# node, image -> {"percent", "done_bytes", "total_bytes"}; bound by server.
pull_progress = None


# server.py's helpers the routes below call, bound by name (bind_routes):
# the update policy and the capacity review live there still.
ROUTE_NEEDS = ("guard_managed_smb", "_dns_name", "get_app_settings", "save_app_settings", "set_update_mode",
               "update_policy_status", "enforce_update_policy", "preview_image_update", "reviewed_image_update")


def bind_routes(helpers):
    missing = set(ROUTE_NEEDS) - set(helpers)
    if missing:
        raise TypeError("homestead_updates needs " + ", ".join(sorted(missing)))
    globals().update({name: helpers[name] for name in ROUTE_NEEDS})


def _report(request):
    force = (request.query.get("force") or ["0"])[0].lower() in ("1", "true", "yes")
    # Settings › Homestead checks Homestead's own parts, not every app.
    only = (request.query.get("only") or [""])[0] == "homestead"
    if not force:
        answer = report(False)
    elif only:
        answer = FORCED.run(FORCED_KEYS[1], homestead_report)
    else:
        answer = FORCED.run(FORCED_KEYS[0], lambda: report(True))
    answer = json.loads(json.dumps(answer))      # a copy: the forced answer is shared
    answer["policy"] = update_policy_status()
    return answer


def _set_channel(request):
    settings = get_app_settings()
    settings["updates"]["channel"] = request.body.get("channel")
    saved = save_app_settings(settings)
    FORCED.forget(*FORCED_KEYS)
    return saved


def _set_managed(request):
    b = request.body
    ns, name = _dns_name(b.get("ns"), "namespace"), _dns_name(b.get("name"), "workload name")
    guard_managed_smb(ns, name)
    result = set_managed(ns, name, b.get("by"))
    ROUTER.forget("wl")
    return result


def _apply(request, action):
    """An update or a rollback, after its capacity review, followed as a job."""
    b = request.body
    guard_managed_smb(b.get("ns"), b.get("name"))
    if action == "update":
        enforce_update_policy(b)
    result = reviewed_image_update(b, action)
    result["operation"] = OPS.start(
        "image-" + action, ("Update " if action == "update" else "Roll back ") + b["name"],
        {"kind": "Deployment", "name": b["name"], "namespace": b["ns"]},
        "/containers?" + urllib.parse.urlencode({"find": b["name"]}),
        {"namespace": b["ns"], "name": b["name"]})
    ROUTER.forget("wl", "ov"); refresh_soon(b["ns"], b["name"])
    FORCED.forget(*FORCED_KEYS)
    return result


# Its routes and who may use them (homestead_routes.py). server.py checks the
# target is an app before apply, rollback and preview get here.
ROUTES = {
    ("GET", "/api/image-updates"): ("viewer", _report),
    ("GET", "/api/image-updates/progress"): ("viewer", lambda request: progress(request.query["ns"][0], request.query["name"][0])),
    # Read while a scan is in flight, so it needs no session cache
    # and must not be served from one.
    ("GET", "/api/image-updates/scan-progress"): ("viewer", lambda request: scan_progress()),
    ("POST", "/api/image-updates/mode"): ("operator", lambda request: set_update_mode(request.body)),
    ("POST", "/api/image-updates/channel"): ("admin", _set_channel),
    ("POST", "/api/image-updates/preview"): ("operator", lambda request: preview_image_update(request.body)),
    ("POST", "/api/image-updates/managed"): ("operator", _set_managed),
    ("POST", "/api/image-updates/apply"): ("operator", lambda request: _apply(request, "update")),
    ("POST", "/api/image-updates/rollback"): ("operator", lambda request: _apply(request, "rollback")),
}


# Moved by feature (#623). Re-exported for one release, so other modules,
# server.py and tests that use or replace these names here keep working.
from homestead_update_registry import (  # noqa: E402,F401
    MANIFEST_ACCEPT, _CACHE, _CACHE_LOCK, parse_image, with_tag, _secret_credentials, _credential,
    _NoAuthRedirect, _auth_realm, _open, RATE_LIMIT_WAITS, _retry_after, _read_capped, _registry_json, manifest_info,
    image_layers, manifest_digest, registry_tags, semver, newer_semver, RELEASE, release_key, channel_newest,
    channel_release, channel_behind,
)
from homestead_update_checks import (  # noqa: E402,F401
    _pod_digest, _matching_pods, _last_known, _names_tag, followed, _check_deployment,
    SCAN, _SCAN_LOCK, scan_progress, _scan_note,
    scan, _summary, _LATEST, _STARTED, _RUN_LOCK, FRESH_FOR, RETRY_RATE_LIMITED, _fresh_for, invalidate,
    refresh_one, RECONCILE_EVERY, _RECONCILED, reconcile, refresh_soon, homestead_report, report,
)
from homestead_update_rollouts import (  # noqa: E402,F401
    PINNED_VERSION_ENV, _pod_containers, _same_repository, _drop_pinned_version, _immutable, _history,
    _review_pods, _previous_image, prepare_update, prepare_rollback, commit_prepared, apply_update, rollback,
    PULL_IMAGE, PULL_TOOK, _event_age, pull_state, STUCK_REASONS, STUCK_AFTER, PULL_CAUSES, explain_pull,
    explain_unplaced, _why_waiting, _age, progress,
)
