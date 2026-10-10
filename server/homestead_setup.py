"""The setup guide's own memory: what was skipped, whether it was hidden,
whether it has opened itself yet, and the facts only the guide records (an
HTTPS address seen to reach Homestead, when Homestead's settings were last
exported).

Whether a step is done is never stored here: Homestead looks at the cluster
each time, so a step done once and undone since (a VIP removed, a backup
target gone) shows again. Only a person's choice to skip is kept. Cluster
steps are skipped for everyone; a person's own steps (appearance, their
phone, their notifications) for that person alone.
"""
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request

import homestead_routes
import homestead_shared as SHARED

DATA_DIR = "/data"
# Steps that are each person's own; the rest are the cluster's, for admins.
PERSONAL = ("appearance", "phone", "notifications")
STEPS = ("health", "quorum", "clocks", "address", "lan", "https", "hostname", "disks", "storage", "smb",
         "backups", "config", "osupdates", "appearance", "phone", "notifications", "people",
         "unifi", "unraid", "homeassistant", "linked", "starter", "console")
_lock = threading.Lock()


def lan_state(networks):
    """Configured LAN attachments, not a test of bridges or connectivity."""
    lan = [n for n in networks if n.get("lan")]
    vms = sorted(n["name"] for n in lan if n.get("vms"))
    containers = sorted(n["name"] for n in lan if n.get("containers"))
    return {"done": bool(vms and containers), "applies": True,
            "vms": vms, "containers": containers}


# What the guide looks at that server.py collects (bind below).
_bound = {"overview": None, "nodes": None, "workloads": None, "vm_networks": None,
          "storage_classes": None, "samba": None}


def bind(data_dir, **collectors):
    """Where the guide keeps its memory, and server.py's collectors it reads:
    overview, nodes, workloads, vm_networks, storage_classes and samba."""
    global DATA_DIR
    DATA_DIR = data_dir
    unknown = set(collectors) - set(_bound)
    if unknown:
        raise TypeError(f"no such collector: {', '.join(sorted(unknown))}")
    _bound.update(collectors)


def _path():
    return os.path.join(DATA_DIR, "setup.json")


def load():
    try:
        with open(_path(), encoding="utf-8") as handle:
            found = json.load(handle)
        return found if isinstance(found, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(state):
    SHARED.write_json(_path(), state, durable=True)


def _change(apply):
    with _lock:
        state = load()
        apply(state)
        _save(state)
        return state


def skips(user):
    state = load()
    mine = ((state.get("users") or {}).get(user) or {}).get("skips") or []
    return sorted(set(state.get("skips") or []) | set(mine))


def skip(step, skipped, user, admin):
    """Skip a step, or bring it back. A cluster step needs an admin."""
    if step not in STEPS:
        raise ValueError("no such step")
    personal = step in PERSONAL
    if not personal and not admin:
        raise PermissionError("only an administrator can skip a step for the whole cluster")

    def apply(state):
        if personal:
            mine = state.setdefault("users", {}).setdefault(user, {})
            target = set(mine.get("skips") or [])
        else:
            target = set(state.get("skips") or [])
        (target.add if skipped else target.discard)(step)
        if personal:
            state["users"][user]["skips"] = sorted(target)
        else:
            state["skips"] = sorted(target)
    _change(apply)
    return {"ok": True, "skips": skips(user)}


def hidden(user):
    return bool(((load().get("users") or {}).get(user) or {}).get("hidden"))


def hide(user, value):
    _change(lambda state: state.setdefault("users", {}).setdefault(user, {}).update(hidden=bool(value)))
    return {"ok": True, "hidden": bool(value)}


def completed(user):
    return bool(((load().get("users") or {}).get(user) or {}).get("completed"))


def complete(user, value=True):
    """Record a person's guide completion, without changing cluster checks."""
    _change(lambda state: state.setdefault("users", {}).setdefault(user, {}).update(completed=bool(value)))
    return {"ok": True, "completed": bool(value)}


def opened():
    """Whether the guide has opened itself for an admin yet (once, ever).
    A cluster that had the old welcome checklist closed counts as opened."""
    if load().get("opened"):
        return True
    try:
        with open(os.path.join(DATA_DIR, "welcome.json"), encoding="utf-8") as handle:
            return bool(json.load(handle).get("done"))
    except (OSError, ValueError):
        return False


def mark_opened():
    _change(lambda state: state.update(opened=True))
    return {"ok": True}


def note(key, value):
    if key not in ("config_backup_at", "https_url"):
        raise ValueError("not a setup fact")
    _change(lambda state: state.update({key: value}))


def https_check(url, fetch=None):
    """Whether an HTTPS address reaches this Homestead: its /healthz answers.
    Asked of an address an administrator typed, never one a page offered."""
    url = str(url or "").strip().rstrip("/")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("Enter an HTTPS address without credentials, a query or a fragment, for example https://homestead.example.com")
    if not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", parts.hostname):
        raise ValueError("Enter a valid hostname")
    fetch = fetch or (lambda target: urllib.request.urlopen(urllib.request.Request(target, headers={"Accept": "application/json"}), timeout=8))
    try:
        with fetch(f"{parts.scheme}://{parts.netloc}{parts.path}/healthz") as answer:
            raw = answer.read(4096).decode("utf-8", "replace")
    except Exception as error:
        raise ValueError(f"Could not reach {url}: {str(error)[:160]}") from None
    try:
        body = json.loads(raw or "{}")
    except ValueError:
        body = None      # a login page, a parked domain, someone else's site
    if not isinstance(body, dict) or not body.get("ok"):
        raise ValueError(f"{url} did not return a Homestead health response. If Cloudflare Access protects this address, open it in your browser, sign in and reopen the setup guide there.")
    note("https_url", url)
    return {"ok": True, "url": url, "at": int(time.time())}


TUNNEL_IMAGES = ("cloudflare/cloudflared", "tailscale/tailscale")


def welcome(role="admin"):
    """The first-run checklist: the few settings a new cluster wants, each
    with whether it is done. Shown to an admin until one says it is done."""
    import homestead_longhorn as LH, homestead_networking as NETWORK, homestead_os_rollout as OS_ROLLOUT
    import homestead_platform as PLATFORM, homestead_probe as PROBE, homestead_self_address as SELF_ADDRESS
    try:
        with open(os.path.join(DATA_DIR, "welcome.json"), encoding="utf-8") as handle:
            done = bool(json.load(handle).get("done"))
    except (OSError, ValueError):
        done = False
    p = PLATFORM.detect() or {}
    steps = {}
    try:
        address = SELF_ADDRESS.report(homestead_routes.cached("network", 5, NETWORK.inventory))
        steps["address"] = {"done": address["on_vip"], "url": address["url"], "shared_vip": address["shared_vip"],
                            "vips": len(NETWORK.registered())}
    except Exception as error:
        steps["address"] = {"done": False, "error": str(error)[:160]}
    steps["probe"] = {"done": bool(PROBE.installed())}
    try:
        steps["backups"] = {"done": bool(LH.backup_target().get("configured"))}
    except Exception:
        steps["backups"] = {"done": False}
    steps["updates"] = {"applies": not p.get("harvester") and p.get("distribution") in ("k3s", "rke2"),
                        "done": bool((OS_ROLLOUT.settings().get("schedule") or {}).get("enabled"))}
    return {"show": role == "admin" and not done, "done": done, "harvester": bool(p.get("harvester")),
            "load_balancer": p.get("load_balancer", ""), "steps": steps}




def state(user, role):
    """The setup guide: each step and whether the cluster shows it done.
    Looked at fresh each time; only skips are remembered. A person who is not
    an admin gets their own steps alone."""
    import homestead_api_keys as API_KEYS, homestead_auth as AUTH, homestead_disks as DISKS
    import homestead_fleet as FLEET, homestead_host_console as HOST_CONSOLE, homestead_host_os as HOST_OS
    import homestead_imports as IMP, homestead_ipam as IPAM, homestead_lifecycle as LC, homestead_longhorn as LH
    import homestead_networking as NETWORK, homestead_os_rollout as OS_ROLLOUT, homestead_platform as PLATFORM
    import homestead_push as PUSH, homestead_self_address as SELF_ADDRESS
    cached = homestead_routes.cached
    p = PLATFORM.detect() or {}
    kube = not p.get("harvester") and p.get("distribution") in ("k3s", "rke2")
    store = load()
    steps = {}

    def step(name, compute):
        try:
            steps[name] = compute()
        except Exception as error:
            steps[name] = {"done": False, "applies": True, "error": str(error)[:160]}

    if role == "admin":
        def health():
            ov = cached("ov", 5, _bound["overview"])
            issues = ov.get("health_issues") or []
            return {"done": not issues, "applies": True, "summary": ov.get("health_summary", ""),
                    "issues": [{k: x.get(k, "") for k in ("severity", "kind", "name", "reason")} for x in issues[:6]]}
        step("health", health)

        def quorum():
            q = LC.quorum_report()
            nodes = [{"name": n["name"], "ready": n.get("status") == "Ready", "roles": n.get("roles") or []}
                     for n in cached("nodes", 5, _bound["nodes"])]
            servers = q["total"] or sum(1 for n in nodes if any(r in ("control-plane", "master", "etcd") for r in n["roles"])) or 1
            return {"done": servers != 2, "applies": True, "servers": servers, "members": q["members"],
                    "ready": q["ready"], "can_lose": q["can_lose"], "nodes": nodes}
        step("quorum", quorum)

        def clocks():
            known = {n["name"]: (HOST_OS.stored(n["name"]) or {}).get("ntp") for n in cached("nodes", 5, _bound["nodes"])}
            told = {k: v for k, v in known.items() if v is not None}
            return {"done": bool(told) and all(told.values()), "applies": kube and bool(told),
                    "unsynced": sorted(k for k, v in told.items() if v is False)}
        step("clocks", clocks)

        def address():
            report = SELF_ADDRESS.report(cached("network", 5, NETWORK.inventory))
            return {"done": bool(report["on_vip"]), "applies": True, "url": report["url"], "service_url": report.get("service_url", ""),
                    "shared_vip": report.get("shared_vip"), "vips": len(NETWORK.registered()),
                    "load_balancer": p.get("load_balancer", ""), "harvester": bool(p.get("harvester"))}
        step("address", address)

        def lan():
            networks = [row for row in _bound["vm_networks"]() if row["lan"]]
            return {**lan_state(networks),
                    "networks": [{key: row[key] for key in ("name", "type", "vms", "containers")} for row in networks]}
        step("lan", lan)

        def https():
            tunnels = sorted({w["name"] for w in cached("wl", 5, _bound["workloads"])
                              if any(t in image for image in w.get("images") or [] for t in TUNNEL_IMAGES)})
            return {"done": bool(store.get("https_url")), "applies": True, "url": store.get("https_url", ""), "tunnels": tunnels}
        step("https", https)
        step("hostname", lambda: {"done": False, "applies": True})       # the browser can tell; see the page

        def disks():
            unused = [{"node": node, "device": r["device"], "size_gb": r.get("size_gb"), "kind": r.get("kind", "")}
                      for node, rows in DISKS.inventory()["nodes"].items() for r in rows
                      if r.get("role") == "unused" and not r.get("system")]
            return {"done": not unused, "applies": bool(p.get("longhorn", True)) and not p.get("harvester"), "unused": unused[:12]}
        step("disks", disks)

        def storage():
            classes = _bound["storage_classes"]()
            default = next((c for c in classes if c.get("default")), None)
            ready = sum(1 for n in cached("nodes", 5, _bound["nodes"]) if n.get("status") == "Ready")
            target = max(1, min(3, ready))
            copies = int(default["replicas"]) if default and str(default.get("replicas") or "").isdigit() else None
            fits = bool(default) and default.get("provisioner") == "driver.longhorn.io" and copies is not None and copies == target
            return {"done": fits, "applies": True, "default": (default or {}).get("name", ""), "copies": copies,
                    "provisioner": (default or {}).get("provisioner", ""), "nodes": ready, "target": target,
                    "candidates": [c["name"] for c in classes if c.get("provisioner") == "driver.longhorn.io"
                                   and str(c.get("replicas")) == str(target) and not c.get("made_for") and not c.get("internal")]}
        step("storage", storage)

        def smb():
            report = _bound["samba"]()
            return {"done": bool(report.get("installed") and report.get("enabled") and not report.get("error")),
                    "applies": True, "installed": bool(report.get("installed")), "enabled": bool(report.get("enabled")),
                    "address": report.get("address", ""), "shares": report.get("shares", 0),
                    **({"error": report["error"]} if report.get("error") else {})}
        step("smb", smb)
        step("backups", lambda: {"done": bool(LH.backup_target().get("configured")), "applies": True})
        step("config", lambda: {"done": bool(store.get("config_backup_at")), "applies": True, "at": store.get("config_backup_at")})
        step("osupdates", lambda: {"done": bool((OS_ROLLOUT.settings().get("schedule") or {}).get("enabled")), "applies": kube})
        step("people", lambda: {"done": sum(1 for u in AUTH.list_users() if u["role"] == "admin") >= 2, "applies": True,
                                "users": len(AUTH.list_users())})
        ipam_read = {}

        def ipam_data():
            # One read of the IP-address record for both its steps.
            if "data" not in ipam_read:
                ipam_read["data"] = IPAM.load()[0]
            return ipam_read["data"]
        step("unifi", lambda: {"done": bool((ipam_data().get("unifi") or {}).get("url")), "applies": True})

        def ipam():
            # IP addresses: subnets known, each scanned, and - with UniFi
            # connected - its devices and reservations brought in.
            data = ipam_data()
            unifi = data.get("unifi") or {}
            subnets = [{"id": s.get("id"), "cidr": s.get("cidr"), "name": s.get("name", ""),
                        "scanned": int((data.get("scans", {}).get(s.get("cidr")) or {}).get("at") or 0)}
                       for s in data.get("subnets") or []]
            connected = bool(unifi.get("url") and unifi.get("has_key"))
            synced = int(unifi.get("last_sync") or 0)
            return {"done": bool(subnets) and all(s["scanned"] for s in subnets) and (bool(synced) or not connected),
                    "applies": True, "subnets": subnets, "unifi": connected, "synced": synced}
        step("ipam", ipam)
        step("unraid", lambda: {"done": bool(IMP.list_sources()), "applies": True})
        step("homeassistant", lambda: {"done": any(not k["expired"] for k in API_KEYS.list_keys()), "applies": True})
        step("linked", lambda: {"done": bool(FLEET.summary().get("linked")), "applies": True})
        step("starter", lambda: {"done": any(not w.get("homestead") for w in cached("wl", 5, _bound["workloads"])), "applies": True})
        step("console", lambda: {"done": bool(HOST_CONSOLE.inventory().get("enabled")), "applies": kube})
    step("notifications", lambda: {"done": bool(PUSH.devices(user)), "applies": True})
    return {"steps": steps, "skips": skips(user), "hidden": hidden(user), "completed": completed(user),
            "opened": opened(), "admin": role == "admin", "personal": list(PERSONAL)}


def welcomed(user):
    """The first-run checklist closed for everyone."""
    SHARED.write_json(os.path.join(DATA_DIR, "welcome.json"),
                      {"done": True, "by": str(user or ""), "at": int(time.time())})
    return {"ok": True}


def _changed(answer):
    """A change to the guide: each person's kept view of it is out of date.
    Dropped before the change, as server.py did, so a refusal drops it too."""
    def route(request):
        homestead_routes.forget_prefix("setup:")
        return answer(request)
    return route


ROUTES = {
    # Many checks: kept for each person half a minute.
    ("GET", "/api/setup"): ("viewer", lambda request: homestead_routes.cached(
        f"setup:{request.role}:{request.user}", 30, lambda: state(request.user, request.role))),
    ("POST", "/api/setup/skip"): ("viewer", _changed(lambda request: skip(
        str(request.body.get("step") or ""), bool(request.body.get("skip", True)), request.user, request.role == "admin"))),
    ("POST", "/api/setup/hide"): ("viewer", _changed(lambda request: hide(request.user, request.body.get("hidden", True)))),
    ("POST", "/api/setup/complete"): ("viewer", _changed(lambda request: complete(request.user, request.body.get("completed", True)))),
    ("POST", "/api/setup/opened"): ("admin", _changed(lambda request: mark_opened())),
    ("POST", "/api/setup/https-check"): ("admin", _changed(lambda request: https_check(request.body.get("url")))),
    ("GET", "/api/welcome"): ("viewer", lambda request: welcome(request.role)),
    ("POST", "/api/welcome/done"): ("admin", lambda request: welcomed(request.user)),
}
