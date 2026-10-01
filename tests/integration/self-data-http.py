"""Disposable cluster only. Invoked by self_data_cluster.py, never with user credentials.

Two real data moves through Homestead's own HTTP API. Homestead stops and
starts again on its new volume in the middle of each, so while it does, a
refused connection, a reset, an empty reply or a 502/503 is the move working,
and is asked again - only an answer that says the move failed ends the test.
Every wait has its own limit and the whole run one more, well inside the CI
job's, so a stall fails here, saying where, rather than hanging the job.
"""
import http.client
import http.cookiejar
import json
import os
import secrets
import time
import urllib.error
import urllib.request

base = os.environ["FIXTURE_URL"]
BUDGET = float(os.environ.get("FIXTURE_BUDGET", "600"))     # seconds, for the whole run
POLL = 2
started = time.monotonic()
cookies = http.cookiejar.CookieJar()
client = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(cookies))
# What a Homestead between pods says: nothing, a reset, or a proxy's 502/503.
TRANSIENT = (urllib.error.URLError, http.client.HTTPException, ConnectionError, TimeoutError, json.JSONDecodeError)


class Refused(RuntimeError):
    """Homestead answered, and said no."""


def log(*words):
    print(f"[{time.monotonic() - started:6.1f}s]", *words, flush=True)


def request(path, body=None):
    req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Homestead-Auth": "1"})
    try:
        with client.open(req, timeout=20) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        raw = error.read().decode(errors="replace")
        if error.code == 503 and path.startswith("/api/self/data/handoff/"):
            try:
                result = json.loads(raw)
            except ValueError:
                result = {}
            if result.get("operation"):
                return result
        if error.code in (502, 503, 504):
            raise ConnectionError(f"{path}: {error.code}") from None
        raise Refused(f"{path}: {error.code}: {raw[:2000]}") from None


def until(what, seconds, attempt, retry_refusal=None):
    """Call attempt() until it returns something truthy: through Homestead
    restarting, and through a refusal retry_refusal names as temporary."""
    deadline = min(time.monotonic() + seconds, started + BUDGET)
    last = ""
    while True:
        try:
            result = attempt()
            if result:
                return result
        except TRANSIENT as error:
            last = f"{type(error).__name__}: {error}"
        except Refused as error:
            if not retry_refusal or retry_refusal not in str(error):
                raise
            last = str(error)[:200]
        if time.monotonic() >= deadline:
            over = "the whole run's budget" if deadline == started + BUDGET else f"{seconds:.0f}s"
            raise RuntimeError(f"{what}: still not done after {over}; last: {last or 'no answer yet'}")
        time.sleep(POLL)


until("Homestead answering", 120, lambda: request("/api/auth/state"))
request("/api/auth/setup", {"username": "release-test", "password": secrets.token_urlsafe(24)})
for attempt in range(2):
    operation = secrets.token_hex(12)
    cfg = {"operation": operation, "storage_class": "fixture-target" if attempt == 0 else "local-path", "node": "release-test"}
    preview = until("preparation review", 120, lambda: request("/api/self/data/prepare/preview", cfg),
                    retry_refusal=None if attempt == 0 else "earlier data move record exists")
    prepared = request("/api/self/data/prepare", {**cfg, "capacity_token": preview["capacity_token"], "confirm_capacity": True})
    log("PREPARATION", attempt + 1, prepared["destination"])

    def preparation_done():
        request("/api/operations")              # advances the job
        row = next(p for p in request("/api/self/data/prepare")["preparations"] if p["operation"] == operation)
        log("PREP", row["status"], row["message"])
        if row["status"] in ("failed", "cancelled"):
            raise RuntimeError(f"preparation {row['status']}: {row['message']}")
        return row["prepared"]
    until("preparation", 240, preparation_done)

    move = {"operation": operation, "destination": prepared["destination"], "worker_node": "release-test", "copy_node": "release-test"}
    preview = until("move review", 120, lambda: request("/api/self/data/move/preview", move),
                    retry_refusal="report current data-move support")
    request("/api/self/data/move", {**move, "capacity_token": preview["capacity_token"], "confirm_capacity": True, "confirm_move": True})
    log("MOVE CONFIRMED", attempt + 1)

    def moved():
        view = request("/api/self/data/handoff/" + operation)
        log("MOVE", view.get("phase"), view["status"], view["message"])
        if view["status"] in ("held", "failed", "cancelled"):
            raise RuntimeError(f"move {view['status']} at {view.get('phase')}: {view['message']}")
        return view["status"] == "done"
    until("move", 300, moved)
    # Done means the copy is in use; the page reports the new source once the
    # restarted Homestead has read its journal.
    until("new source reported", 60, lambda: request("/api/self/data/prepare")["source"] == prepared["destination"])
    log("VERIFIED new source; retained login and jobs")
log("PASS: two complete HTTP moves with real Kubernetes and retained account state")
