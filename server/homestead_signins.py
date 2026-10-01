"""Who signed in to Homestead, from where, and when.

A line per sign-in, failed attempt, sign-out, password change and change to
an account, kept in Homestead's data directory beside its console audit.
Admins read it under Events › Sign-ins. It keeps the most recent entries and
drops the oldest, so it cannot grow without end. Passwords are never in it -
a failed attempt records the name tried, not what was typed for it.
"""
import json
import os
import re
import threading
import time

DATA_DIR = "/data"
FILE = "signins.jsonl"
KEEP = 5000
EVENTS = ("signin", "signin-failed", "signin-blocked", "signout", "signout-everywhere", "password",
          "password-failed", "setup", "user-added", "user-removed", "role",
          "key-added", "key-revoked", "key-refused")
_lock = threading.Lock()
_writes = {"n": 0}


def bind(data_dir):
    global DATA_DIR
    DATA_DIR = data_dir


def _path():
    return os.path.join(DATA_DIR, FILE)


def device(agent):
    """A browser and system worth reading, from a User-Agent header."""
    agent = str(agent or "")
    if not agent:
        return ""
    system = next((name for pattern, name in (
        (r"iPhone|iPad", "iOS"), (r"Android", "Android"), (r"Windows", "Windows"),
        (r"Mac OS X|Macintosh", "macOS"), (r"CrOS", "ChromeOS"), (r"Linux", "Linux")) if re.search(pattern, agent)), "")
    browser = next((name for pattern, name in (
        (r"Edg/", "Edge"), (r"OPR/|Opera", "Opera"), (r"Firefox/", "Firefox"), (r"Chrome/", "Chrome"),
        (r"Safari/", "Safari"), (r"curl/", "curl"), (r"python", "a script")) if re.search(pattern, agent)), "")
    if browser and system:
        return f"{browser} on {system}"
    return browser or system or agent[:40]


def record(event, user, ip="", ok=True, detail="", agent="", via=""):
    """Write one entry. Never raises: a full disk must not stop anyone signing in."""
    if event not in EVENTS:
        return
    row = {"at": round(time.time(), 3), "event": event, "user": str(user or "")[:64], "ok": bool(ok),
           "ip": str(ip or "")[:64], "device": device(agent), "via": str(via or "")[:32],
           "detail": str(detail or "")[:200]}
    line = json.dumps(row, separators=(",", ":")) + "\n"
    try:
        with _lock:
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(_path(), "a", encoding="utf-8") as handle:
                handle.write(line)
            _writes["n"] += 1
            if _writes["n"] % 200 == 0:
                _trim()
    except OSError:
        pass


def _trim():
    try:
        with open(_path(), encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return
    if len(lines) <= KEEP:
        return
    temp = _path() + ".tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        handle.writelines(lines[-KEEP:])
    os.replace(temp, _path())


def history(user="", failures=False, limit=500):
    """Newest first: everything, or one person's, or only what failed."""
    try:
        with open(_path(), encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return []
    user = str(user or "").strip().lower()
    out = []
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if user and row.get("user", "").lower() != user:
            continue
        if failures and row.get("ok", True):
            continue
        out.append(row)
        if len(out) >= limit:
            break
    return out
