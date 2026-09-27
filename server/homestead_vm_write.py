"""Request-local VM resource writes with intent/receipt journaling.

The caller owns the operation/dispatcher lock and durable record callback. This
layer never stores bodies, cloud-init, URLs, credentials or raw error text, and
never retries a request. A failed writer stays failed even if a caller catches
its exception to show a warning. Call check() before acknowledging completion.
"""
import copy
import re
import urllib.error

RESOURCES = {
    ("v1", "persistentvolumeclaims"): "PersistentVolumeClaim",
    ("v1", "secrets"): "Secret",
    ("kubevirt.io/v1", "virtualmachines"): "VirtualMachine",
    ("harvesterhci.io/v1beta1", "virtualmachineimages"): "VirtualMachineImage",
    ("cdi.kubevirt.io/v1beta1", "datavolumes"): "DataVolume",
}
PATH = re.compile(r"^/(?:api/(v1)|apis/([a-z0-9.]+/[a-z0-9]+))/namespaces/([a-z0-9.-]+)/([a-z]+)/?([a-z0-9.-]*)$")
NAME = re.compile(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?")


class WriteFailure(ValueError):
    """Safe public error; the original API body is deliberately not propagated."""


def operation_recorder(ops, ident):
    """Bind safe write events to one already-created operation.

    This does not create a job, authorize a write, consume an approval or finish
    the overall workflow. The reviewed API dispatcher must do those separately.
    Sequence checks prevent reusing a job's write stream after interruption.
    """
    def record(event):
        with ops._lock:
            items = ops._read()
            item = next((row for row in items if row.get("id") == ident), None)
            if not item or item.get("status") in ops.TERMINAL or item.get("status") == ops.CANCELLING:
                raise WriteFailure("VM mutation job has ended or is unavailable")
            if item.get("kind") not in ("vm-create", "vm-edit", "k3s-cluster"):
                raise WriteFailure("This job cannot record VM resource mutations")
            entries = copy.deepcopy(item["ref"].get("writes", []))
            if not isinstance(entries, list):
                raise WriteFailure("VM write history is malformed")
            phase = event["phase"]
            if phase == "intent":
                if event["sequence"] != len(entries) + 1 or (entries and entries[-1]["phase"] != "accepted"):
                    raise WriteFailure("VM write stream cannot be replayed or advanced past an unresolved write")
                entries.append(copy.deepcopy(event))
            else:
                if (phase not in ("accepted", "refused", "uncertain", "unverified") or not entries
                        or entries[-1]["phase"] != "intent" or any(
                            entries[-1].get(key) != event.get(key) for key in ("sequence", "method", "resource", "before"))):
                    raise WriteFailure("VM receipt does not match its recorded intent")
                entries[-1] = copy.deepcopy(event)
            failed = phase in ("refused", "uncertain", "unverified")
            item["ref"].update(writes=entries, retain_resources=True, phase="failed" if failed else "writing")
            target = event["resource"]
            message = f"Write {event['sequence']}: {event['method']} {target['kind']} {target['namespace']}/{target['name']} — {phase}"
            if failed:
                message += "; inspect retained resources; nothing was retried"
            # These are workflow milestones, not download or guest-health %.
            progress = max(int(item.get("progress") or 0), 10 if phase == "intent" else 20)
            ops._finish(item, "failed" if failed else "running", progress, message)
            ops._write(items)
    return record


def _identity(meta):
    uid, version = meta.get("uid"), meta.get("resourceVersion")
    # API identities are opaque: validate presence/bounded shape, never parse,
    # order, normalize or invent a resourceVersion from an old observation.
    if (not isinstance(uid, str) or not 0 < len(uid) <= 1024
            or not isinstance(version, str) or not 0 < len(version) <= 1024):
        raise WriteFailure("The resource identity/version could not be verified")
    return {"uid": uid, "resourceVersion": version}


def _target(method, path, body):
    match = PATH.fullmatch(path) if isinstance(path, str) else None
    if method not in ("POST", "PUT", "PATCH") or not match or not isinstance(body, dict):
        raise WriteFailure("Unsupported VM resource write; nothing was sent")
    core, grouped, namespace, plural, path_name = match.groups()
    version = core or grouped
    kind = RESOURCES.get((version, plural))
    meta = body.get("metadata") or {}
    if not isinstance(meta, dict):
        raise WriteFailure("VM resource metadata is malformed; nothing was sent")
    name = meta.get("name") if method == "POST" else path_name
    if (not kind or not isinstance(name, str) or len(name) > 253 or not NAME.fullmatch(name)
            or len(namespace) > 63 or not NAME.fullmatch(namespace)
            or (method == "POST" and path_name) or (method != "POST" and not path_name)
            or (meta.get("namespace") is not None and meta["namespace"] != namespace)
            or (meta.get("name") is not None and meta["name"] != name)
            or (body.get("kind") is not None and body["kind"] != kind)
            or (body.get("apiVersion") is not None and body["apiVersion"] != version)):
        raise WriteFailure("VM resource target is inconsistent; nothing was sent")
    target = {"apiVersion": version, "kind": kind, "namespace": namespace, "name": name}
    before = _identity(meta) if method != "POST" else None
    return target, before


class ResourceWriter:
    def __init__(self, send, record):
        self.send, self.record = send, record
        self.count = 0
        self.failed = False

    def check(self):
        if self.failed:
            raise WriteFailure("A VM resource write failed or is uncertain. Inspect its job; nothing was retried")

    def _record(self, event):
        try:
            self.record(copy.deepcopy(event))
        except Exception:
            self.failed = True
            raise WriteFailure("VM write progress could not be recorded. Inspect the retained intent; nothing was retried") from None

    def __call__(self, method, path, body=None, **kwargs):
        self.check()
        try:
            target, before = _target(method, path, body)
        except Exception:
            self.failed = True
            raise
        self.count += 1
        event = {"sequence": self.count, "method": method, "resource": target,
                 "before": before, "phase": "intent"}
        self._record(event)  # durable callback must succeed before transmission
        try:
            result = self.send(method, path, body, **kwargs)
        except Exception as error:
            self.failed = True
            refused = isinstance(error, urllib.error.HTTPError) and error.code in (400, 401, 403, 404, 405, 409, 422)
            outcome = {**event, "phase": "refused" if refused else "uncertain"}
            if isinstance(error, urllib.error.HTTPError):
                outcome["http_status"] = int(error.code)
            self._record(outcome)
            raise WriteFailure("The VM resource request was refused; earlier resources remain" if refused else
                               "The VM resource response is uncertain; inspect retained resources before a new review") from None
        try:
            meta = result.get("metadata") if isinstance(result, dict) else None
            if not isinstance(meta, dict) or (meta.get("namespace"), meta.get("name")) != (target["namespace"], target["name"]):
                raise WriteFailure("Response identity does not match the intended target")
            identity = _identity(meta)
            if before and before["uid"] != identity["uid"]:
                raise WriteFailure("Response belongs to a replacement resource")
            if result.get("kind") != target["kind"] or result.get("apiVersion") != target["apiVersion"]:
                raise WriteFailure("Response resource type is inconsistent")
        except Exception:
            self.failed = True
            self._record({**event, "phase": "unverified"})
            raise WriteFailure("VM write response identity could not be verified. The request may have applied; nothing was retried") from None
        self._record({**event, "phase": "accepted", "identity": identity})
        return result
