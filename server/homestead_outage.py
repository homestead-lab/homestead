"""What to do when an app or VM is down: nothing, unless asked.

Each app and VM may have actions for an outage, kept as an annotation on it
(homestead.io/outage-actions), so setting them restarts nothing:

* restart it once it has been down for after_min minutes, then again every
  after_min minutes it stays down, at most max times. The count starts again
  once it has answered for UP_RESET seconds. Out of tries, it is left alone and
  an alert says so. A VM is rebooted cleanly, and only when it is monitored on
  a port of its own choosing: automatically, Homestead cannot tell a VM that is
  down from one that does not answer on the usual ports.
* call a webhook - an HTTP POST of JSON - when it goes down, when it comes
  back, and when an automatic update of it finishes or is rolled back.

Nothing is restarted while a job is working on the app (an update, a
rollback, a move, a restore test, a scheduled stop or start): an automatic
update watches the app itself and rolls the image back if it stays down, and a
restart in the middle would read as the update failing.

The leader runs tick() after each monitoring round. Its state - restarts done,
what each webhook was last told - is a small file, written when it changes.
"""
import json
import os
import threading
import time
import urllib.parse
import urllib.request

import homestead_shared as SHARED

KEY = "outage-actions"
UP_RESET = 30 * 60
MAX_RESTARTS = 10
WEBHOOK_TIMEOUT = 10
DATA_DIR = "/data"
_lock = threading.Lock()


# server.py's, which read and write the app or VM (bind).
_bound = {"set_actions": None, "test_webhook": None}


def bind(data_dir="/data", set_actions=None, test_webhook=None):
    global DATA_DIR
    DATA_DIR = data_dir
    if set_actions:
        _bound.update(set_actions=set_actions, test_webhook=test_webhook)


def _path():
    return os.path.join(DATA_DIR, "outage-actions.json")


def _load():
    try:
        with open(_path(), encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def webhook_url(value):
    """A webhook address, checked; "" for none."""
    url = str(value or "").strip()
    if not url:
        return ""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or len(url) > 500 or any(c in url for c in "\r\n "):
        raise ValueError("the webhook is an http:// or https:// address, up to 500 characters")
    return url


def clean(value, kind="app", monitored_on_port=True):
    """Actions as given, checked: {"restart": {...}?, "webhook": url?}, or None for none."""
    value = value or {}
    if not isinstance(value, dict):
        raise ValueError("outage actions are a restart and a webhook")
    out = {}
    restart = value.get("restart")
    if restart:
        try:
            after, most = int(restart.get("after_min")), int(restart.get("max"))
        except (TypeError, ValueError, AttributeError):
            raise ValueError("say after how many minutes down to restart, and at most how many times") from None
        if not 1 <= after <= 1440:
            raise ValueError("restart after 1 to 1440 minutes down")
        if not 1 <= most <= MAX_RESTARTS:
            raise ValueError(f"restart at most 1 to {MAX_RESTARTS} times")
        if kind == "vm" and not monitored_on_port:
            raise ValueError("a VM is restarted only when it is monitored on a port you chose: "
                             "automatically, Homestead cannot tell it is down")
        out["restart"] = {"after_min": after, "max": most}
    url = webhook_url(value.get("webhook"))
    if url:
        out["webhook"] = url
    return out or None


def read(annotations, names):
    raw = names.read(annotations or {}, KEY)
    if not raw:
        return None
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) and (value.get("restart") or value.get("webhook")) else None
    except ValueError:
        return None


def items(workloads, vms):
    """Every app and VM with actions: kind, ns, name, key (as monitoring keys it), actions."""
    out = []
    for w in workloads or []:
        if w.get("outage_actions"):
            out.append({"kind": "app", "ns": w["ns"], "name": w["name"], "key": f"{w['ns']}/{w['name']}",
                        "actions": w["outage_actions"]})
    for v in vms or []:
        if v.get("outage_actions"):
            out.append({"kind": "vm", "ns": v["ns"], "name": v["name"], "key": f"vm:{v['ns']}/{v['name']}",
                        "actions": v["outage_actions"]})
    return out


def payload(item, event, state=None, detail="", now=None):
    """What a webhook is sent: plain fields, and a sentence as text/content/message
    so chat services (Slack, Discord, ntfy, Home Assistant) show it as it is."""
    state = state or {}
    error = (state.get("last") or {}).get("error") or ""
    what = "VM " if item["kind"] == "vm" else ""
    sentence = {"down": f"{what}{item['name']} is down" + (f": {error}" if error else ""),
                "up": f"{what}{item['name']} is up again",
                "restarted": f"Restarted {what}{item['name']}: {detail}",
                "gave-up": f"{what}{item['name']} is still down after {detail}",
                "update": f"{item['name']}: {detail}",
                "test": f"A test from Homestead for {what}{item['name']}"}.get(event, f"{item['name']}: {event}")
    return {"event": event, "kind": item["kind"], "namespace": item["ns"], "name": item["name"],
            "state": state.get("state", ""), "since": state.get("since"), "error": error, "detail": detail,
            "at": round(now or time.time()), "text": sentence, "content": sentence, "message": sentence}


def post(url, body):
    """Send one webhook; raises on failure."""
    data = json.dumps(body).encode()
    request = urllib.request.Request(url, data=data, method="POST", headers={
        "Content-Type": "application/json", "User-Agent": "Homestead outage actions"})
    with urllib.request.urlopen(request, timeout=WEBHOOK_TIMEOUT) as response:
        if response.status >= 400:
            raise OSError(f"HTTP {response.status}")


def tick(scheduled, report, restart, busy, operations=(), send=None, now=None):
    """One look after a monitoring round. restart(item) restarts it (raises
    to say why it could not); busy(item) is whether a job is working on it;
    send(url, body) sends a webhook. Returns what was done."""
    now = now or time.time()
    send = send or (lambda url, body: _send_later(url, body))
    did = []
    with _lock:
        data = _load()
        state_by_key = data.setdefault("items", {})
        changed = False
        keys = set()
        for item in scheduled:
            key = f"{item['kind']}:{item['ns']}/{item['name']}"
            keys.add(key)
            mine = state_by_key.setdefault(key, {"restarts": 0})
            seen = report.get(item["key"]) or {}
            now_state = seen.get("state")
            hook = (item["actions"] or {}).get("webhook")
            plan = (item["actions"] or {}).get("restart")

            def tell(event, detail=""):
                if hook:
                    send(hook, payload(item, event, seen, detail, now))

            if now_state == "down":
                if mine.get("told") != "down":
                    mine["told"], changed = "down", True
                    tell("down")
                if mine.pop("up_since", None) is not None:
                    changed = True
                if plan and not busy(item):
                    since = seen.get("since") or now
                    wait = plan["after_min"] * 60
                    due = now - since >= wait and now - (mine.get("last_restart") or 0) >= wait
                    if due and mine.get("restarts", 0) < plan["max"]:
                        try:
                            restart(item)
                            mine["restarts"] = mine.get("restarts", 0) + 1
                            mine["last_restart"], mine["last_error"] = round(now), ""
                            detail = f"try {mine['restarts']} of {plan['max']}"
                            did.append({"key": key, "action": "restart", "detail": detail})
                            tell("restarted", detail)
                        except Exception as error:
                            mine["last_restart"], mine["last_error"] = round(now), str(error)[:200]
                            did.append({"key": key, "action": "restart", "error": mine["last_error"]})
                        changed = True
                    elif due and not mine.get("gave_up") and mine.get("restarts", 0) >= plan["max"]:
                        mine["gave_up"], changed = True, True
                        tell("gave-up", f"{plan['max']} restart{'s' if plan['max'] != 1 else ''}")
            elif now_state in ("up", "slow"):
                if mine.get("told") == "down":
                    mine["told"], changed = "up", True
                    tell("up")
                if mine.get("gave_up") or mine.get("last_error"):
                    # Answering again: its alert resolves; the count waits for UP_RESET.
                    mine.update(gave_up=False, last_error="")
                    changed = True
                if mine.get("up_since") is None:
                    mine["up_since"], changed = round(now), True
                elif mine.get("restarts") and now - mine["up_since"] >= UP_RESET:
                    mine["restarts"], changed = 0, True
            # An automatic update of it finished: say how, once.
            if hook:
                told = set(mine.get("told_jobs") or [])
                for op in operations or []:
                    res = op.get("resource") or {}
                    if op.get("kind") != "auto-update" or op.get("status") not in ("succeeded", "failed") \
                            or (res.get("namespace"), res.get("name")) != (item["ns"], item["name"]) or op.get("id") in told:
                        continue
                    told.add(op.get("id"))
                    tell("update", op.get("message") or op.get("status"))
                    changed = True
                if told != set(mine.get("told_jobs") or []):
                    mine["told_jobs"] = sorted(told)[-20:]
        for key in [k for k in state_by_key if k not in keys]:
            state_by_key.pop(key)
            changed = True
        if changed:
            SHARED.write_json(_path(), data, separators=(",", ":"))
    return did


def _send_later(url, body):
    """Webhooks go out on their own thread, so a slow one holds nothing up;
    how the last one went is kept for the dialog."""
    def run():
        try:
            post(url, body)
            result = {"at": round(time.time()), "ok": True, "event": body.get("event")}
        except Exception as error:
            result = {"at": round(time.time()), "ok": False, "event": body.get("event"), "error": str(error)[:200]}
        _last_webhook[f"{body['kind']}:{body['namespace']}/{body['name']}"] = result
    threading.Thread(target=run, name="outage-webhook", daemon=True).start()


_last_webhook = {}


def report():
    """Each item's restarts and last webhook, for the Monitoring dialogs."""
    with _lock:
        data = _load().get("items") or {}
    return {key: {"restarts": s.get("restarts", 0), "gave_up": bool(s.get("gave_up")), "last_restart": s.get("last_restart"),
                  "last_error": s.get("last_error", ""), "webhook": _last_webhook.get(key)}
            for key, s in data.items()} | {key: {"restarts": 0, "webhook": hook}
                                            for key, hook in _last_webhook.items() if key not in data}


def alert_facts(scheduled_report=None):
    """Out of restarts and still down, or a restart refused."""
    facts = []
    with _lock:
        data = _load().get("items") or {}
    for key, s in sorted(data.items()):
        kind, ref = key.split(":", 1)
        ns, name = ref.split("/", 1)
        href = f"/vms?panel=monitoring&ns={ns}&vm={name}" if kind == "vm" else f"/containers?panel=monitoring&ns={ns}&workload={name}"
        what = "VM " if kind == "vm" else ""
        if s.get("gave_up"):
            facts.append({"key": f"outage:{key}", "category": "outage", "severity": "critical",
                          "title": f"{what}{name} is still down after {s.get('restarts')} restart{'s' if s.get('restarts') != 1 else ''}",
                          "body": f"Homestead restarted {what}{name} as many times as it was asked to, and it still does not answer. "
                                  "It is left alone now; look at its logs.",
                          "resolved": f"{what}{name} is answering again", "href": href, "signals": {"restarts": s.get("restarts")}})
        elif s.get("last_error"):
            facts.append({"key": f"outage:{key}", "category": "degraded", "severity": "degraded",
                          "title": f"{what}{name} is down and could not be restarted",
                          "body": s["last_error"], "resolved": f"{what}{name} could be restarted", "href": href,
                          "signals": {"error": s["last_error"]}})
    return facts


# Its routes and who may use them (homestead_routes.py).
ROUTES = {
    ("POST", "/api/monitoring/actions"): ("operator", lambda request: _bound["set_actions"](request.body)),
    ("POST", "/api/monitoring/actions/test"): ("operator", lambda request: _bound["test_webhook"](request.body)),
}
