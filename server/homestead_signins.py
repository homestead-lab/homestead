"""Who signed in to Homestead, from where, and when.

A line per sign-in, failed attempt, sign-out, password change and change to
an account, kept in Homestead's data directory beside its console audit.
Admins read it under Events › Sign-ins. It keeps the most recent entries and
drops the oldest, so it cannot grow without end. Every copy of Homestead
appends and trims it under one shared lock, so two copies on one volume lose
no line (#986). Passwords are never in it -
a failed attempt records the name tried, not what was typed for it.
"""
import json
import os
import re
import threading
import time

import homestead_shared as SHARED

DATA_DIR = "/data"
FILE = "signins.jsonl"
KEEP = 5000
TRIM_EVERY = 200         # records written by this process between trims
EVENTS = ("signin", "signin-failed", "signin-blocked", "signout", "signout-everywhere", "password",
          "password-failed", "setup", "user-added", "user-removed", "role",
          "key-added", "key-changed", "key-revoked", "key-refused")
LOCK_SECONDS = 2        # the longest a sign-in waits for another copy's write
# Strict, for its timeout: a copy stuck holding the shared lock (a hung NFS
# server) must not hold anyone's sign-in. Then this copy writes under its own
# lock, as before #986, and says so.
_lock = SHARED.SharedLock("signins", strict=True, timeout=LOCK_SECONDS, directory=lambda: DATA_DIR)
_local = threading.Lock()
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
            _append(line)
            _writes["n"] += 1
            if _writes["n"] % TRIM_EVERY == 0:
                _trim()
        return
    except TimeoutError:
        print("sign-ins: the shared lock is busy, so this copy writes under its own lock", flush=True)
    except Exception as error:      # full, unwritable, or held by a data move
        _lost(line, error)
        return
    try:
        with _local, SHARED.write_scope(_path()):
            _append(line)
    except Exception as error:
        _lost(line, error)


def _append(line):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(_path(), "a", encoding="utf-8") as handle:
        handle.write(line)


def _lost(line, error):
    # The pod log keeps the line the data volume did not take.
    print(f"sign-ins: not written to the data volume ({str(error)[:160]}): {line.strip()}", flush=True)


def _trim():
    """Keep the newest KEEP lines. Called under _lock."""
    try:
        with open(_path(), encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return
    if len(lines) <= KEEP:
        return
    temp = SHARED.temporary(_path())
    try:
        with open(temp, "w", encoding="utf-8") as handle:
            handle.writelines(lines[-KEEP:])
            handle.flush()
            os.fsync(handle.fileno())       # on disk before it replaces the log
        os.replace(temp, _path())
    except BaseException:
        try:
            os.remove(temp)
        except OSError:
            pass
        raise


def history(user="", failures=False, limit=500):
    """Newest first: everything, or one person's, or only what failed. No
    file yet is no sign-ins; one that cannot be read raises, so the page says
    so rather than "No sign-ins recorded yet"."""
    try:
        with open(_path(), encoding="utf-8") as handle:
            lines = handle.readlines()
    except FileNotFoundError:
        return []
    except OSError as error:
        raise OSError(error.errno, f"Homestead could not read the sign-in history on its data volume "
                                   f"({error.strerror or error})") from error
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


# Its routes and who may use them (homestead_routes.py).
ROUTES = {
    ("GET", "/api/auth/history"): ("admin", lambda request: history((request.query.get("user") or [""])[0], (request.query.get("failures") or [""])[0] == "1")),
}
