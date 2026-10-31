"""Settings code that lived in server.py (track 1, part 8): saving the app
settings, forgetting what a configuration restore made stale, and the
configuration backup and restore routes server.py's handler answered.

server.py binds what it still owns through bind_routes(): each helper in
ROUTE_NEEDS is looked up in server.py when it is called, or, once moved
(server.MOVED), in the module it moved to, so a test that patches it there
reaches it here too; each name in VALUES is server.py's own value."""
import json
import threading
import time
import urllib.request, urllib.parse, urllib.error
import homestead_app_settings as APP_SETTINGS
import homestead_auth as AUTH
import homestead_config_backup as CONFIG
import homestead_fleet as FLEET
import homestead_hardware as HW
import homestead_imports as IMP
import homestead_names as NAMES
import homestead_placement_mode as PLACEMODE
import homestead_recovery as RECOVERY
import homestead_routes as MODULE_ROUTES
import homestead_setup as SETUP
import homestead_update_checks as CHECKS

ROUTE_NEEDS = ("kget", "ksend", "_settings_map")
VALUES = ("DEFAULT_NS", "_cache")
DEFAULT_APP_SETTINGS = APP_SETTINGS.DEFAULTS
# The settings page's read is cached apart from the "settings" the rest of the
# server caches, since those carry no revision; _forget() drops both.
REVISION_KEY = "settings-revision"


# The API server's version for Settings: it changes only with an upgrade, so
# it is asked at most every KUBE_VERSION_TTL seconds; a failed read is not kept.
KUBE_VERSION_TTL = 600
version_clock = time.monotonic
_kube_version = {"until": float("-inf"), "value": ""}


def kube_version(read):
    if version_clock() < _kube_version["until"]:
        return _kube_version["value"]
    try:
        value = read("/version").get("gitVersion", "")
    except Exception:
        return ""
    _kube_version.update(until=version_clock() + KUBE_VERSION_TTL, value=value)
    return value


def forget_kube_version():
    _kube_version.update(until=float("-inf"), value="")


def bind_routes(helpers, **values):
    missing = set(ROUTE_NEEDS) - set(helpers)
    if missing:
        raise TypeError("homestead_settings_helpers needs " + ", ".join(sorted(missing)))
    if set(values) != set(VALUES):
        raise TypeError("homestead_settings_helpers is bound with " + ", ".join(sorted(set(values) ^ set(VALUES))))
    globals().update({name: helpers[name] for name in ROUTE_NEEDS})
    globals().update(values)


def save_app_settings(value):
    """Save the sections value sends over the stored settings; any section, or
    part of one, it leaves out is kept as it is. A value naming the revision
    it was read at (GET /api/settings) is refused if the settings have changed
    since: APP_SETTINGS.Stale, and nothing is saved (#601)."""
    value = value if isinstance(value, dict) else {}
    stored, revision = _read_settings()
    if value.get("revision") is not None and str(value["revision"]) != revision:
        _stale()
    settings = APP_SETTINGS.validate(_merged(stored, value))
    name = _settings_map()
    body = {"apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": name, "namespace": DEFAULT_NS,
                         "labels": {NAMES.key("managed"): "true"}},
            "data": {"settings.json": json.dumps(settings, indent=2)}}
    try:
        if revision:
            # Kubernetes refuses the write if anything saved in between.
            body["metadata"]["resourceVersion"] = revision
            ksend("PUT", f"/api/v1/namespaces/{DEFAULT_NS}/configmaps/{name}", body)
        else:
            ksend("POST", f"/api/v1/namespaces/{DEFAULT_NS}/configmaps", body)
    except urllib.error.HTTPError as e:
        if e.code != 409:
            raise
        _stale()
    _forget()
    CHECKS.invalidate()
    PLACEMODE.saved(settings["placement"])
    # A different catalogue address is a different catalogue: the App Store's
    # next read sees the new one (homestead_deploy_appstore._start_refresh).
    # A renamed site is carried to the linked clusters, for their switches.
    threading.Thread(target=_fleet_rename, daemon=True).start()
    return settings


def _config_restored(parts):
    """A restored part is read afresh everywhere it is cached."""
    _cache.clear()
    AUTH._store_cache.update(at=0)
    HW._cache.clear()
    IMP._cache.clear()


# The routes server.py's handler answered for this code.
def _config(request):
    b = request.body
    try:
        if request.path == "/api/config/backup":
            made = CONFIG.backup(b.get("parts"), str(b.get("passphrase") or ""))
            SETUP.note("config_backup_at", int(time.time()))
            return made
        return CONFIG.restore(b.get("file"), str(b.get("passphrase") or ""), b.get("parts") or [])
    except PermissionError as error:
        # A wrong passphrase is not a missing role: 422, not 403.
        return MODULE_ROUTES.Reply(422, {"error": str(error)})
    except ValueError as error:
        return MODULE_ROUTES.Reply(400, {"error": str(error)})


ROUTES = {
    ("POST", "/api/config/backup"): ("admin", _config),
    ("POST", "/api/config/restore"): ("admin", _config),
}


def update_policy_status(settings=None, now=None):
    """Return whether a manual managed update may start at the current UTC time."""
    update = (settings or get_app_settings()).get("updates") or DEFAULT_APP_SETTINGS["updates"]
    policy = update.get("policy", "approval_required")
    maintenance = update.get("maintenance") or DEFAULT_APP_SETTINGS["updates"]["maintenance"]
    duration = int(maintenance["duration_minutes"])
    window_open = RECOVERY.window(maintenance, time.time() if now is None else now)[0]
    if policy == "notify_only":
        allowed, reason = False, "Cluster policy is notify only; an admin must change it before installing."
    elif policy == "maintenance_window" and not window_open:
        allowed, reason = False, (f"Updates are limited to the {maintenance['start']} UTC "
                                  f"maintenance window ({duration} minutes).")
    else:
        allowed, reason = True, "Explicit operator approval is required before rollout."
    return {"policy": policy, "allows_install": allowed, "requires_approval": True,
            "reason": reason, "window_open": window_open,
            "maintenance": json.loads(json.dumps(maintenance))}


def enforce_update_policy(body, settings=None, now=None):
    status = update_policy_status(settings, now)
    if not status["allows_install"]:
        raise PermissionError(status["reason"])
    if body.get("approved") is not True:
        raise PermissionError("explicit approval is required before installing an image update")
    return status


def get_app_settings():
    return _read_settings()[0]


def app_settings_with_revision():
    """The settings, with the revision a save based on them names."""
    settings, revision = _read_settings()
    return {**settings, "revision": revision}


def _read_settings():
    """The stored settings, and their revision: the ConfigMap's
    resourceVersion, "" before they are first saved."""
    try:
        cm = kget(f"/api/v1/namespaces/{DEFAULT_NS}/configmaps/{_settings_map()}")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return APP_SETTINGS.validate({}), ""
        raise
    revision = str((cm.get("metadata") or {}).get("resourceVersion") or "")
    try:
        raw = json.loads((cm.get("data") or {}).get("settings.json", "{}"))
        return APP_SETTINGS.validate(raw), revision
    except (ValueError, TypeError, json.JSONDecodeError):
        return APP_SETTINGS.validate({}), revision


def _merged(stored, change):
    """change over stored, section by section and key by key; anything
    else it sends (a list, a value) replaces what was there."""
    out = dict(stored)
    for key, value in change.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            value = _merged(out[key], value)
        out[key] = value
    return out


def _forget():
    for key in ("settings", REVISION_KEY):
        _cache.pop(key, None)


def _stale():
    _forget()    # the 409 answers with the settings as they are now
    raise APP_SETTINGS.Stale("The settings were changed since they were read here, "
                             "so nothing was saved. Check them and save again.")


def _fleet_rename():
    try:
        FLEET.refresh_name()
    except Exception:
        pass            # the next change, or their next look, carries it
