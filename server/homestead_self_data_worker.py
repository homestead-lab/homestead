"""Independent self-data worker loop and read-only progress HTTP surface.

No application imports, data-directory access, account secrets or file journal.
The deployment wrapper must supply the pinned API handle, narrow Kubernetes
client and current admission adapter. It is not wired to the legacy mover yet.
"""
import hashlib
import hmac
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import homestead_self_data_anchor as A
import homestead_self_data_coordinator as C
from homestead_storage_journal import Held


STAGES = (
    ("prepare", "Prepare"), ("quiesce", "Stop Homestead"), ("copy", "Copy and check data"),
    ("verify", "Release copy mounts"), ("switch", "Select new volume"),
    ("start", "Start and check Homestead"), ("done", "Complete"),
)
DESCRIPTIONS = {
    "prepare": "Preparing the move. Homestead has not been stopped yet.",
    "quiesce": "Waiting for Homestead's pods and volume mounts to stop.",
    "copy": "Copying and checking the data. A byte percentage is not available.",
    "verify": "Data verified. Waiting for the copy pod to release both volumes.",
    "switch": "Selecting the new volume while Homestead remains stopped.",
    "start": "Waiting for every Homestead replica to start on the new volume.",
    "done": "Homestead is ready on the new volume. The original volume is retained.",
}


def unavailable_status(operation):
    return {"operation": operation, "phase": None, "status": "unknown", "stale": True,
            "message": "The move record cannot be verified right now. No completion is assumed.",
            "checked_at": None, "stages": [], "copy_percent": None,
            "retention_policy": "keep_both_volumes", "requires_review": True, "can_cancel": False}


def progress(anchor, now):
    """Project known facts, never expose the internal journal or invent copy %.

    checked_at is the last verified worker checkpoint, not the time a browser
    requested this view. Missing/stale heartbeats are not 'still running'.
    """
    state = anchor.state
    phase = state["phase"]
    runtime = state.get("runtime", {})
    stamp = runtime.get("checked_at")
    fresh = stamp is not None and -5 <= now - stamp <= 60
    writes = state["journal"]["ref"]["storage_writes"]
    pending = any(w["state"] == "intent" for w in writes)
    uncertain = any(w["state"] not in ("accepted", "intent") for w in writes)
    completion_receipts = {w["step"] for w in writes if w["state"] == "accepted"}
    incomplete_done = phase == "done" and (pending or "pointer_receipt" not in state
        or state.get("copy_receipt", {}).get("state") != "verified"
        or not {"stop", "copy-job", "release-copy", "switch", "start"} <= completion_receipts)
    held = runtime.get("state") == "held" or uncertain or incomplete_done or state.get("copy_receipt", {}).get("state") == "uncertain"
    preparing = "pointer_receipt" not in state
    status = "held" if held else "done" if phase == "done" else "preparing" if preparing else "checking" if pending and fresh else "running" if fresh else "unknown"
    message = (runtime.get("message") if runtime.get("state") == "held" else
               "Completion evidence is incomplete. Review the retained move record before continuing." if incomplete_done else
               "A request has no verified outcome. Nothing will be retried automatically." if held else
               "Waiting for the durable move record. Homestead has not been stopped." if preparing else
               "The worker has not reported recently. The last verified stage is shown; do not assume the move is running." if status == "unknown" else
               "Waiting for confirmation of the current request. It will not be sent again." if pending else
               DESCRIPTIONS[phase])
    current = A.PHASES.index(phase)
    stages = [{"id": key, "label": label,
               "state": "complete" if i < current or status == "done" else "current" if i == current else "pending"}
              for i, (key, label) in enumerate(STAGES)]
    return {"operation": state["operation"], "phase": phase, "status": status,
            "message": message, "checked_at": stamp, "stale": not fresh and phase != "done",
            "stages": stages, "copy_percent": None, "retention_policy": "keep_both_volumes",
            "requires_review": held, "can_cancel": False}


class Runner:
    def __init__(self, read, send, logs, admit=None, *, namespace, deployment, operation, anchor_uid,
                 worker_uid, clock=time.time):
        A._name(namespace); A._name(deployment)
        if not re.fullmatch(r"[a-f0-9]{24}", operation or "") or not anchor_uid or not worker_uid:
            raise Held("The data move worker needs its exact reviewed identities")
        self.read, self.send, self.logs, self.admit = read, send, logs, admit
        self.namespace, self.deployment = namespace, deployment
        self.operation, self.anchor_uid, self.worker_uid = operation, anchor_uid, worker_uid
        self.clock = clock
        self.lock = threading.Lock()
        self.hold = None

    def _load(self):
        return A.Anchor(self.read, self.send, self.namespace, self.deployment).load(
            operation=self.operation, uid=self.anchor_uid)

    def snapshot(self):
        """Status polling is read-only, including when the API cannot be reached."""
        try:
            view = progress(self._load(), self.clock())
            if self.hold:
                view.update(status="held", message=self.hold, requires_review=True)
            return view
        except Exception:
            return unavailable_status(self.operation)

    def _save_hold(self):
        try:
            anchor = self._load()
            if (anchor.state.get("plan", {}).get("worker", {}).get("uid") == self.worker_uid
                    and "pointer_receipt" in anchor.state and anchor.state.get("runtime", {}).get("state") != "held"):
                anchor.report(self.worker_uid, int(self.clock()), "held", self.hold)
        except Exception:
            # Hold is latched in this process too. A later poll may checkpoint
            # the hold, but cannot continue the move or replay a resource write.
            pass

    def tick(self):
        if not self.lock.acquire(blocking=False):
            return self.snapshot()
        try:
            if self.hold:
                self._save_hold()
                return self.snapshot()
            try:
                anchor = self._load()
                if anchor.state.get("runtime", {}).get("state") == "held":
                    self.hold = anchor.state["runtime"]["message"]
                    return self.snapshot()
                if anchor.state["phase"] == "done":
                    return self.snapshot()
                if "pointer_receipt" not in anchor.state:
                    # Setup still owns this record; heartbeat writes would race
                    # its publication CAS. Do not mutate it or stop the app.
                    return self.snapshot()
                admit = self.admit
                if admit is None:
                    from homestead_self_data_admission import Admitter
                    plan = anchor.state.get("plan", {})
                    admit = Admitter(self.read, self.namespace, plan.get("nodes", []), plan.get("admission"),
                                     handoff=anchor.state, clock=self.clock)
                engine = C.Coordinator(anchor, self.read, self.send, self.logs, admit,
                                       worker_uid=self.worker_uid, clock=self.clock)
                result = engine.step()
                anchor.report(self.worker_uid, int(self.clock()),
                              "done" if result["phase"] == "done" else "running", result["message"])
            except Held as error:
                self.hold = str(error) if 0 < len(str(error)) <= 512 and all(ord(c) >= 32 for c in str(error)) else "The data move needs a recovery review before continuing."
                self._save_hold()
            except Exception:
                self.hold = "The data move worker could not verify its next step. Both volumes are retained; review before continuing."
                self._save_hold()
            return self.snapshot()
        finally:
            self.lock.release()

    def run(self, stop, interval=5):
        """Bounded ticks; Event.wait allows immediate worker shutdown."""
        if not 1 <= interval <= 30:
            raise ValueError("Worker poll interval must be between one and 30 seconds")
        while not stop.is_set():
            self.tick()
            stop.wait(interval)


def status_server(address, runner, token_digest):
    """Read-only, operation-scoped capability, independent of login/account data.

    Setup returns the random status token once to the authorized browser and
    mounts only its SHA-256 digest in this helper. Tokens must be sent in an
    Authorization header, never a URL. This grants no console or mutation rights.
    """
    A._hash(token_digest)

    class Handler(BaseHTTPRequestHandler):
        server_version = "Homestead data move"
        sys_version = ""

        def log_message(self, *_):
            pass  # no request URLs or credentials in helper logs

        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def answer(self, code, value):
            body = json.dumps(value, separators=(",", ":")).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/healthz":
                # Only means the progress server answers, never app readiness.
                return self.answer(200, {"service": "data-move-progress"})
            if self.path != "/api/self/data/handoff/" + runner.operation:
                return self.answer(404, {"error": "Not found"})
            values = self.headers.get_all("Authorization") or []
            token = values[0][7:] if len(values) == 1 and values[0].startswith("Bearer ") else ""
            if not re.fullmatch(r"[a-f0-9]{64}", token) or not hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(), token_digest):
                return self.answer(401, {"error": "This move's status token is required"})
            view = runner.snapshot()
            return self.answer(503 if view["status"] == "unknown" else 200, view)

        def do_POST(self):
            return self.answer(405, {"error": "This endpoint only reports progress"})

        do_PUT = do_PATCH = do_DELETE = do_POST

    class Server(ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = False

    return Server(address, Handler)
