"""Alerts to a webhook: Generic JSON, ntfy or Gotify (#741, part 2).

Push reaches the browsers and phones that subscribed. An admin can also send
alerts - a host not ready, a drive failing, a backup that failed, an app out
of restarts - to one webhook of their own, from Settings > Connections. Off
until set, so nothing changes for anyone who does not.

Everything that makes a webhook safe is part 1's (homestead_outage), used
here rather than copied: the address check, the formats, where a webhook may
go (every resolved address checked, loopback, link-local and metadata never,
a LAN address only when an admin allowed it, no redirect), the address shown
only up to its host, and Send a test's limit. What is sent is what push
says: each alert once as it is raised, worsens or clears (homestead_alerts
already keeps an unchanged condition from repeating), from the leader only,
at or above the minimum severity chosen.

Kept in alert-webhook.json beside alerts.json, readable only by Homestead:
the address can carry a token anywhere after its host.
"""
import os
import queue
import threading
import time

import homestead_alerts as ALERTS
import homestead_leader_state as LEADER_STATE
import homestead_outage as OUTAGE
import homestead_routes as ROUTER
import homestead_shared as SHARED

SEVERITIES = ("info", "degraded", "critical")
DEFAULT_SEVERITY = "degraded"
# A burst - a host going down takes its apps with it - is cut short with one summary.
MAX_PER_ROUND = 10
# Alerts sent and not yet cleared, so a clear is sent for each even when the
# alert eased below the level chosen before it cleared (the leader's, shared).
SENT = "alert-webhook-sent"
MAX_SENT = 500
_lock = SHARED.SharedLock("alert-webhook")
_last = {}
# One sender, in the order the rounds came: a slow webhook never has a clear
# overtake its alert, nor do threads pile up behind it.
_queue = queue.Queue()
_worker = []


def _path():
    return os.path.join(ALERTS.DATA_DIR, "alert-webhook.json")


def load():
    """The webhook as kept, token and all - never for the browser - or None."""
    cfg = SHARED.read_json(_path(), {})
    return cfg if cfg.get("webhook") else None


def clean(value, before=None):
    """{"webhook", "format", "min_severity", "lan"?} as given, checked; None for off.
    An address shown hidden keeps the stored one, only on the same scheme, host and port."""
    value = value if isinstance(value, dict) else {}
    before = before or {}
    url = OUTAGE.webhook_url(OUTAGE.restore(OUTAGE.webhook_url(value.get("webhook")), before.get("webhook")))
    if not url:
        return None
    fmt = str(value.get("format") or "generic")
    if fmt not in OUTAGE.FORMATS:
        raise ValueError("the webhook format is generic, ntfy or gotify")
    least = str(value.get("min_severity") or DEFAULT_SEVERITY)
    if least not in SEVERITIES:
        raise ValueError("send alerts from info, degraded or critical up")
    out = {"webhook": url, "format": fmt, "min_severity": least}
    if value.get("lan"):
        out["lan"] = True
    return out


def save(value):
    """Admin-only (ROUTES): the webhook, checked as part 1 checks an outage webhook's."""
    given = value.get("webhook") if isinstance(value, dict) else ""
    with _lock:
        # Turning it off needs nothing kept, so it works even when the store cannot be read.
        cfg = clean(value, load()) if str(given or "").strip() else None
        if cfg:
            try:
                OUTAGE.destination(cfg["webhook"], bool(cfg.get("lan")))
            except OUTAGE.Refused as error:
                raise ValueError(str(error)) from None
            except OSError:
                pass    # not found now: checked again at each call
            SHARED.write_json(_path(), cfg, mode=0o600)
        else:
            try:
                os.remove(_path())
            except FileNotFoundError:
                pass
            LEADER_STATE.put(SENT, [])     # set again later, it starts afresh
    return {"ok": True, **public(),
            "detail": "Alerts go to the webhook" if cfg else "Alerts go to no webhook"}


def _last_call():
    shared, _ = LEADER_STATE.get("alert-webhook")
    mine = _last.get("call")
    if isinstance(shared, dict) and (not mine or shared.get("at", 0) > mine.get("at", 0)):
        return shared
    return mine


def public(cfg=None):
    """As anyone may read it: the address only up to its host."""
    cfg = cfg if cfg is not None else load()
    if not cfg:
        return {"webhook": "", "format": "generic", "min_severity": DEFAULT_SEVERITY, "lan": False, "last": None}
    return {"webhook": OUTAGE.hide(cfg["webhook"]), "format": cfg.get("format", "generic"),
            "min_severity": cfg.get("min_severity", DEFAULT_SEVERITY), "lan": bool(cfg.get("lan")), "last": _last_call()}


TAGS = {"critical": "rotating_light", "degraded": "warning", "info": "information_source"}
LEVEL = {"critical": "high", "degraded": "default", "info": "low"}


def notice(entry):
    """What ntfy and Gotify show for one alert: its title, its body, a level and a tag."""
    resolved = entry.get("phase") == "resolved"
    severity = entry.get("severity", "info")
    return {"agent": "Homestead alerts", "title": entry.get("title") or "Homestead",
            "message": entry.get("body") or entry.get("title") or "",
            "level": "default" if resolved else LEVEL.get(severity, "default"),
            "tags": "white_check_mark" if resolved else TAGS.get(severity, "house")}


def body(entry):
    """Generic JSON: the alert's fields, and a sentence as text, content and message for chat services."""
    title, detail = entry.get("title") or "", entry.get("body") or ""
    sentence = f"{title}: {detail}" if detail else title
    return {"event": "alert", "phase": entry.get("phase", "raised"), "severity": entry.get("severity", "info"),
            "category": entry.get("category", ""), "key": entry.get("key", ""), "title": title, "body": detail,
            "href": entry.get("href", "/"), "at": entry.get("at") or round(time.time()),
            "text": sentence, "content": sentence, "message": sentence}


def _wanted(entry, least):
    return entry.get("category") != "test" and \
        SEVERITIES.index(entry.get("severity") if entry.get("severity") in SEVERITIES else "info") >= SEVERITIES.index(least)


def _record(result):
    _last["call"] = result
    LEADER_STATE.put("alert-webhook", result)


def _sent():
    found, _ = LEADER_STATE.get(SENT)
    return [k for k in found if isinstance(k, str)] if isinstance(found, list) else []


def _pick(fresh, least):
    """The alerts to send: at or above the level, and the clear of any alert
    sent before, whatever its severity now (it may have eased first)."""
    sent = _sent()
    known = set(sent)
    picked = []
    for e in fresh or []:
        key = e.get("key")
        if e.get("phase") == "resolved" and key in known:
            picked.append(e)
            known.discard(key)
        elif e.get("phase") != "resolved" and _wanted(e, least):
            picked.append(e)
            if key and not e.get("event"):
                known.add(key)
    if known != set(sent):
        LEADER_STATE.put(SENT, [k for k in sent if k in known][-MAX_SENT:] +
                         sorted(known - set(sent))[:MAX_SENT])
    return picked


def _summary(left):
    """One call for what a burst left out: how many, and whether they cleared."""
    cleared = all(e.get("phase") == "resolved" for e in left)
    worst = max((e.get("severity", "info") for e in left), key=lambda s: SEVERITIES.index(s) if s in SEVERITIES else 0)
    return {"phase": "resolved" if cleared else "raised", "severity": worst, "category": "summary", "key": "summary",
            "title": "More alerts cleared" if cleared else "More alerts",
            "body": f"{len(left)} more alerts {'cleared' if cleared else 'too'}: open Homestead to see them all"}


def _enqueue(run):
    if not (_worker and _worker[0].is_alive()):
        def work():
            while True:
                job = _queue.get()
                try:
                    job()
                except Exception as error:
                    print(f"alert webhook: {str(error)[:160]}", flush=True)
                finally:
                    _queue.task_done()
        _worker[:] = [threading.Thread(target=work, name="alert-webhook", daemon=True)]
        _worker[0].start()
    _queue.put(run)


def deliver(fresh, start=None):
    """Sends the alerts just raised, worsened or cleared, one round after
    another on Homestead's own sender, so a slow webhook holds up nothing."""
    try:
        cfg = load()
    except Exception as error:
        _record({"at": round(time.time()), "ok": False, "error": str(error)[:200]})
        return 0
    if not cfg:
        return 0
    picked = _pick(fresh, cfg.get("min_severity", DEFAULT_SEVERITY))
    if not picked:
        return 0
    calls = [(body(e), notice(e)) for e in picked[:MAX_PER_ROUND]]
    if len(picked) > MAX_PER_ROUND:
        summary = _summary(picked[MAX_PER_ROUND:])
        calls.append((body(summary), notice(summary)))

    def run():
        result = {"at": round(time.time()), "ok": True, "sent": 0}
        for data, shown in calls:
            try:
                OUTAGE.post(cfg["webhook"], data, cfg.get("format", "generic"), bool(cfg.get("lan")), shown)
                result["sent"] += 1
            except Exception as error:
                result.update(ok=False, error=OUTAGE.scrub(str(error), cfg["webhook"])[:200])
                break
        result["at"] = round(time.time())
        _record(result)

    (start or _enqueue)(run)
    return len(calls)


def test_webhook(b):
    """Send a test now, in the format chosen in the dialog (saved or not); part 1's limit."""
    url = OUTAGE.webhook_url(b.get("webhook"))
    if not url:
        raise ValueError("type the webhook address to test")
    fmt = str(b.get("format") or "generic")
    if fmt not in OUTAGE.FORMATS:
        raise ValueError("the webhook format is generic, ntfy or gotify")
    stored = load() if OUTAGE.HIDDEN in url else None
    url = OUTAGE.webhook_url(OUTAGE.restore(url, (stored or {}).get("webhook")))
    if not OUTAGE._allow_test():
        return ROUTER.Reply(429, {"error": f"at most {OUTAGE.TEST_LIMIT} tests a minute; wait a little and send it again"})
    sample = {"phase": "raised", "severity": "info", "category": "test", "key": "test",
              "title": "Homestead", "body": "A test alert from Homestead"}
    shown = {**notice(sample), "level": "default", "tags": "test_tube"}
    what = {"ntfy": "ntfy", "gotify": "Gotify"}.get(fmt, "The webhook")
    try:
        OUTAGE.post(url, {**body(sample), "event": "test"}, fmt, True, shown)    # an admin's test may reach the LAN
    except Exception as error:
        return {"ok": False, "detail": f"{what} did not take it: {OUTAGE.scrub(str(error), url)[:200]}"}
    return {"ok": True, "detail": f"{what} took the test"}


ROUTES = {
    ("GET", "/api/alerts/webhook"): ("viewer", lambda request: public()),
    ("POST", "/api/alerts/webhook"): ("admin", lambda request: save(request.body)),
    # Admin-only: it sends a request from inside the cluster to any address typed.
    ("POST", "/api/alerts/webhook/test"): ("admin", lambda request: test_webhook(request.body)),
}
