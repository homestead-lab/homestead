"""Whether each app answers, checked every minute, with thirty days of history.

Running is not the same as answering: a pod can be Ready while its web page
returns 502, or while its address on the LAN is gone. The leading replica asks
each running app at its own address - its main port, the one its card links
to - as a person's browser would:

* HTTP first: any answer below 500 counts, so a login page, a redirect or a
  401 is an app that answers; a 5xx is one that does not. Certificates are not
  checked; a self-signed one is what most apps have.
* If the port takes the connection but does not answer in HTTP, the accepted
  connection counts: databases, MQTT and game servers close, reset or wait.

Which port: the app's main port when one is chosen; otherwise each of its
published TCP ports in turn, up to MAX_PORTS, and it answers if any does - so
CouchDB's Erlang port or a proxy's spare port beside the web UI is not taken
for the app. An app on the LAN through macvtap is asked at its pod's own
address instead: a host cannot reach a macvtap address on its own NIC, so
asking the LAN address from Homestead's pod on the same host would fail
while the app is fine.

An app is down after DOWN_AFTER misses in a row and up again on its first
answer, so one dropped packet raises nothing. An answer slower than SLOW_MS is
marked slow but is not an outage. Stopped apps (no replicas wanted) are not
checked. An app's own setting, the homestead.io/uptime annotation, can turn
the check off, make it TCP only, or give the HTTP path to ask for.

History is kept per hour: checks, misses and slow answers, for thirty days.
The leader writes it when an app goes down or comes back, and otherwise at
most every SAVE_EVERY seconds, so a quiet minute writes nothing to disk. The
other replica reads the file again when it changes.
"""
import http.client
import json
import os
import re
import socket
import ssl
import threading
import time

import homestead_shared as SHARED

DATA_DIR = "/data"
CHECK_EVERY = 60
DOWN_AFTER = 3
SLOW_MS = 2000
TIMEOUT = 5
KEEP_HOURS = 30 * 24
STRIP_HOURS = 24
SAVE_EVERY = 300
FORGET_AFTER = 7 * 86400     # an app gone this long is dropped from the history
HTTPS_PORTS = (443, 8443, 9443, 5001)
MAX_PORTS = 4
PATH = re.compile(r"^/[A-Za-z0-9._~!$&'()*+,;=:@%/?-]{0,199}$")
USER_AGENT = "Homestead-uptime/1"
_lock = threading.Lock()
_state = {"loaded": False, "apps": {}, "saved_at": 0.0, "observed_at": 0.0, "mtime": None}


def bind(data_dir="/data"):
    global DATA_DIR
    DATA_DIR = data_dir
    with _lock:
        _state.update(loaded=False, apps={}, saved_at=0.0, observed_at=0.0, mtime=None)


def _path():
    return os.path.join(DATA_DIR, "uptime.json")


def _mtime():
    try:
        return os.stat(_path()).st_mtime_ns
    except OSError:
        return None


def _load(fresh=False):
    """The apps' history. The replica that checks keeps it in memory; the
    other reads the file again whenever it has changed (fresh=True)."""
    if _state["loaded"] and not (fresh and _mtime() != _state["mtime"]):
        return _state["apps"]
    _state["mtime"] = _mtime()
    try:
        with open(_path(), encoding="utf-8") as handle:
            data = json.load(handle)
        _state["apps"] = data.get("apps", {}) if isinstance(data, dict) else {}
    except (OSError, ValueError):
        _state["apps"] = {}
    _state["loaded"] = True
    return _state["apps"]


def _save(now):
    SHARED.write_json(_path(), {"format": 1, "apps": _state["apps"]}, separators=(",", ":"), sort_keys=True)
    _state["saved_at"], _state["mtime"] = now, _mtime()


def setting(value):
    """An app's own uptime setting, from its annotation: auto, off, tcp or an HTTP path."""
    value = str(value or "").strip()
    if value in ("", "auto"):
        return {"mode": "auto"}
    if value in ("off", "tcp"):
        return {"mode": value}
    if PATH.match(value):
        return {"mode": "http", "path": value}
    return {"mode": "auto"}


def check_setting(mode, path=""):
    """What an operator asks for, as the annotation value; ValueError if it is not one."""
    mode = str(mode or "auto")
    if mode in ("auto", "off", "tcp"):
        return "" if mode == "auto" else mode
    if mode == "http":
        path = str(path or "/").strip() or "/"
        if not PATH.match(path):
            raise ValueError("the path starts with / and has no spaces, up to 200 characters")
        return path
    raise ValueError("choose auto, http, tcp or off")


def target(w):
    """Where and how to ask one app, or None with the reason it is not checked."""
    if w.get("platform") or w.get("self") or w.get("homestead") or w.get("site"):
        return None, "not an app of yours"
    chosen = setting(w.get("answer_check"))
    if chosen["mode"] == "off":
        return None, "checks are off for this app"
    if not w.get("desired"):
        return None, "stopped"
    ports = [p for p in w.get("ports") or [] if p.get("ip") and p.get("port") and (p.get("protocol") or "TCP") == "TCP"]
    if not ports:
        return None, "no address to ask"
    if any(p.get("primary") for p in ports):
        ports = [p for p in ports if p.get("primary")][:1]
    pod_ip = next((p.get("ip") for p in w.get("pods") or [] if p.get("ip") and p.get("ready")), "") or \
        next((p.get("ip") for p in w.get("pods") or [] if p.get("ip")), "")
    asks = []
    for p in ports[:MAX_PORTS]:
        port = int(p["port"])
        host = pod_ip if p.get("lan") and pod_ip else str(p["ip"])
        asks.append({"host": host, "port": port, "scheme": "https" if port in HTTPS_PORTS else "http", "lan": bool(p.get("lan"))})
    kind = "tcp" if chosen["mode"] == "tcp" else "http"
    first = asks[0]
    return {"kind": kind, "host": first["host"], "port": first["port"], "scheme": first["scheme"], "asks": asks,
            "path": chosen.get("path", "/"), "strict": chosen["mode"] == "http"}, ""


def ask_ports(t, ask=None):
    """Ask each of an app's ports in turn: the first that answers is the app
    answering. The result names the port asked last."""
    ask = ask or probe
    result = None
    for one in t.get("asks") or [t]:
        single = {**t, **one}
        result = {**ask(single), "host": single["host"], "port": single["port"], "scheme": single["scheme"]}
        if result["ok"]:
            break
    return result


def _describe(t, result=None):
    host, port, scheme = ((result or {}).get("host") or t["host"]), ((result or {}).get("port") or t["port"]), \
        ((result or {}).get("scheme") or t["scheme"])
    return f"{scheme}://{host}:{port}{t['path']}" if t["kind"] == "http" else f"tcp {host}:{port}"


def _tcp(host, port, timeout):
    started = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return {"ok": True, "ms": int((time.monotonic() - started) * 1000), "code": None, "error": ""}
    except OSError as e:
        return {"ok": False, "ms": None, "code": None, "error": _why(e)}


def _why(error):
    if isinstance(error, socket.timeout) or "timed out" in str(error):
        return "no answer in time"
    if isinstance(error, ConnectionRefusedError):
        return "connection refused"
    text = str(error) or type(error).__name__
    return text[:120]


def probe(t, timeout=TIMEOUT):
    """Ask once. {ok, ms, code, error}."""
    if t["kind"] == "tcp":
        return _tcp(t["host"], t["port"], timeout)
    started = time.monotonic()
    conn = None
    connected = None
    try:
        if t["scheme"] == "https":
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            conn = http.client.HTTPSConnection(t["host"], t["port"], timeout=timeout, context=context)
        else:
            conn = http.client.HTTPConnection(t["host"], t["port"], timeout=timeout)
        try:
            # The connection on its own first: refused or unreachable is down.
            sock = socket.create_connection((t["host"], t["port"]), timeout=timeout)
            sock.close()
        except OSError as e:
            return {"ok": False, "ms": None, "code": None, "error": _why(e)}
        connected = int((time.monotonic() - started) * 1000)
        conn.request("GET", t["path"], headers={"User-Agent": USER_AGENT, "Accept": "*/*", "Connection": "close"})
        response = conn.getresponse()
        code = response.status
        response.read(512)
        ms = int((time.monotonic() - started) * 1000)
        if code >= 500:
            return {"ok": False, "ms": ms, "code": code, "error": f"HTTP {code}"}
        return {"ok": True, "ms": ms, "code": code, "error": ""}
    except (http.client.HTTPException, ssl.SSLError, OSError) as e:
        # It took the connection but gave no HTTP answer: a database, MQTT, a
        # game server, which close, reset or wait. Unless HTTP was asked for,
        # an accepted connection is an app that answers.
        if connected is not None and not t.get("strict"):
            return {"ok": True, "ms": connected, "code": None, "error": ""}
        if connected is not None:
            return {"ok": False, "ms": None, "code": None, "error": f"no HTTP answer ({_why(e)})"}
        return {"ok": False, "ms": None, "code": None, "error": _why(e)}
    finally:
        if conn is not None:
            conn.close()


def _hour(now):
    return int(now // 3600 * 3600)


def _record(app, result, now):
    """Count one check into the app's hour; True when its state changed."""
    hour = str(_hour(now))
    hours = app.setdefault("hours", {})
    row = hours.setdefault(hour, [0, 0, 0])          # checks, misses, slow
    row[0] += 1
    if not result["ok"]:
        row[1] += 1
    elif result["ms"] is not None and result["ms"] > SLOW_MS:
        row[2] += 1
    app["last"] = {"at": round(now), **result}
    before = app.get("state", "unknown")
    if result["ok"]:
        app["misses"] = 0
        state = "up"
    else:
        app["misses"] = app.get("misses", 0) + 1
        state = "down" if app["misses"] >= DOWN_AFTER or before == "down" else (before if before != "unknown" else "unknown")
    if state != before:
        app["state"], app["since"] = state, round(now - (DOWN_AFTER - 1) * CHECK_EVERY if state == "down" and before != "down" else now)
        return True
    return False


def observe(workloads, now=None, ask=probe):
    """One round: ask every app that can be asked. The leader calls it each minute."""
    now = now or time.time()
    rows = []
    for w in workloads or []:
        t, why = target(w)
        rows.append((f"{w.get('ns')}/{w.get('name')}", t, why))
    results = {}

    def run(key, t):
        try:
            results[key] = ask_ports(t, ask)
        except Exception as e:      # a check never stops the round
            results[key] = {"ok": False, "ms": None, "code": None, "error": _why(e)}

    threads = [threading.Thread(target=run, args=(key, t), daemon=True) for key, t, _ in rows if t]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(TIMEOUT * 2 + 2)
    with _lock:
        apps = _load()
        _state["observed_at"] = now
        changed = False
        for key, t, why in rows:
            app = apps.setdefault(key, {"state": "unknown", "hours": {}})
            app["seen"] = round(now) if not app.get("seen") or now - app["seen"] > 3600 else app["seen"]
            if not t:
                if app.get("state") not in ("off", None) or app.get("why") != why:
                    changed = changed or app.get("state") in ("down",)
                    app.update({"state": "off", "why": why, "misses": 0, "since": round(now)})
                continue
            app.pop("why", None)
            result = results.get(key) or {"ok": False, "ms": None, "code": None, "error": "the check did not finish"}
            app["target"] = _describe(t, result)
            if len(t.get("asks") or []) > 1 and not result["ok"]:
                app["target"] = f"{len(t['asks'])} ports at {t['host']}"
            result = {k: v for k, v in result.items() if k not in ("host", "port", "scheme")}
            closing = str(_hour(now)) not in app.get("hours", {})
            changed = _record(app, result, now) or closing or changed
        oldest = _hour(now) - KEEP_HOURS * 3600
        for key in list(apps):
            apps[key]["hours"] = {h: v for h, v in apps[key].get("hours", {}).items() if int(h) >= oldest}
            if now - apps[key].get("seen", now) > FORGET_AFTER:
                apps.pop(key)
        if changed or now - _state["saved_at"] >= SAVE_EVERY:
            _save(now)
        return apps


def summary(app, now=None):
    """What a card shows: state, the last answer, a 24-hour strip, uptime over 24 h and 30 days."""
    now = now or time.time()
    hours = app.get("hours", {})
    this = _hour(now)
    strip = []
    for i in range(STRIP_HOURS - 1, -1, -1):
        row = hours.get(str(this - i * 3600))
        strip.append(None if not row or not row[0] else "down" if row[1] else "slow" if row[2] * 2 >= row[0] else "up")

    def share(rows):
        checks = sum(r[0] for r in rows)
        return None if not checks else round(100 * (checks - sum(r[1] for r in rows)) / checks, 2)

    recent = [v for h, v in hours.items() if int(h) > this - STRIP_HOURS * 3600]
    state = app.get("state", "unknown")
    last = app.get("last") or {}
    if state == "up" and last.get("ms") and last["ms"] > SLOW_MS:
        state = "slow"
    return {"state": state, "since": app.get("since"), "why": app.get("why", ""), "target": app.get("target", ""),
            "last": last, "strip": strip, "uptime_24h": share(recent), "uptime_30d": share(list(hours.values()))}


def report(now=None):
    with _lock:
        apps = _load(fresh=time.time() - _state["observed_at"] > 3 * CHECK_EVERY)
        return {key: summary(app, now) for key, app in apps.items()}


def alert_facts(rep):
    """An app that is down is an outage until it answers again."""
    facts = []
    for key, s in (rep or {}).items():
        if s.get("state") != "down":
            continue
        ns, name = key.split("/", 1)
        error = (s.get("last") or {}).get("error") or "no answer"
        facts.append({"key": f"uptime:{key}", "category": "outage", "severity": "critical",
                      "title": f"{name} is not answering",
                      "body": f"{s.get('target') or name}: {error}. Checked every minute; it has missed {DOWN_AFTER} or more in a row.",
                      "resolved": f"{name} is answering again",
                      "href": f"/containers?panel=answering&ns={ns}&workload={name}", "signals": {"error": error}})
    return facts
