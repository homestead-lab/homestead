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

import homestead_shared as SHARED

DATA_DIR = "/data"
# Steps that are each person's own; the rest are the cluster's, for admins.
PERSONAL = ("appearance", "phone", "notifications")
STEPS = ("health", "quorum", "probe", "clocks", "address", "https", "hostname", "disks", "storage",
         "backups", "config", "osupdates", "appearance", "phone", "notifications", "people",
         "unifi", "unraid", "homeassistant", "linked", "starter", "console")
_lock = threading.Lock()


def bind(data_dir):
    global DATA_DIR
    DATA_DIR = data_dir


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
