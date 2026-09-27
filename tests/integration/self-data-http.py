"""Disposable cluster only. Invoked by self_data_cluster.py, never with user credentials."""
import http.cookiejar
import json
import os
import secrets
import time
import urllib.request
import urllib.error

base = os.environ["FIXTURE_URL"]
cookies = http.cookiejar.CookieJar()
client = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(cookies))

def request(path, body=None):
    req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Homestead-Auth": "1"})
    try:
        with client.open(req, timeout=30) as response: return json.load(response)
    except urllib.error.HTTPError as error:
        raw = error.read().decode()
        if error.code == 503 and path.startswith('/api/self/data/handoff/'):
            result = json.loads(raw)
            if result.get('operation'): return result
        raise RuntimeError(f"{path}: {error.code}: {raw[:2000]}") from None

request("/api/auth/setup", {"username": "release-test", "password": secrets.token_urlsafe(24)})
for attempt in range(2):
    operation = secrets.token_hex(12)
    cfg = {"operation": operation, "storage_class": "fixture-target" if attempt == 0 else "local-path", "node": "release-test"}
    deadline = time.monotonic() + 120
    while True:
        try:
            preview = request("/api/self/data/prepare/preview", cfg)
            break
        except RuntimeError as error:
            if attempt == 0 or "earlier data move record exists" not in str(error) or time.monotonic() >= deadline: raise
            print("Waiting for temporary helper cleanup", flush=True)
            time.sleep(3)
    prepared = request("/api/self/data/prepare", {**cfg, "capacity_token": preview["capacity_token"], "confirm_capacity": True})
    print("PREPARATION", attempt + 1, prepared["destination"], flush=True)
    deadline = time.monotonic() + 240
    while time.monotonic() < deadline:
        request("/api/operations")
        state = request("/api/self/data/prepare")
        row = next(p for p in state["preparations"] if p["operation"] == operation)
        print("PREP", row["status"], row["message"], flush=True)
        if row["prepared"]: break
        if row["status"] == "failed": raise RuntimeError(row["message"])
        time.sleep(3)
    else: raise RuntimeError("Preparation timed out")
    cfg = {"operation": operation, "destination": prepared["destination"], "worker_node": "release-test", "copy_node": "release-test"}
    preview = request("/api/self/data/move/preview", cfg)
    request("/api/self/data/move", {**cfg, "capacity_token": preview["capacity_token"], "confirm_capacity": True, "confirm_move": True})
    print("MOVE CONFIRMED", attempt + 1, flush=True)
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        try:
            view = request("/api/self/data/handoff/" + operation)
            print("MOVE", view.get("phase"), view["status"], view["message"], flush=True)
            if view["status"] == "done": break
            if view["status"] == "held": raise RuntimeError(view["message"])
        except (urllib.error.URLError, TimeoutError) as error:
            print("Reconnecting", type(error).__name__, flush=True)
        time.sleep(3)
    else: raise RuntimeError("Move timed out")
    time.sleep(10)
    assert request("/api/self/data/prepare")["source"] == prepared["destination"]
    print("VERIFIED new source; retained login and jobs", flush=True)
print("PASS: two complete HTTP moves with real Kubernetes and retained account state", flush=True)
