"""Kubernetes-resident journal for Homestead's own data-volume handoff.

Not wired to the legacy mover yet. The eventual independent worker must quiesce
writers and prove each phase before advancing. This record is deliberately NOT
on either data PVC. A stale copied journal cannot win a resourceVersion CAS.
No request bodies, configuration, credentials or raw API errors belong here.
Conditional PUT semantics: https://kubernetes.io/docs/reference/using-api/api-concepts/#resource-versions
"""
import copy
import json
import re

from homestead_storage_journal import Held, identity, target


PHASES = ("prepare", "quiesce", "copy", "verify", "switch", "start", "done")
LABEL = "homestead.io/self-data-handoff"
MAX_BYTES = 262144


def _keys(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= set(value) <= set(required) | set(optional):
        raise Held("The data handoff control record is invalid; inspect it before continuing")


def _name(value):
    if not isinstance(value, str) or len(value) > 253 or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", value):
        raise Held("The data handoff resource name is invalid")


def _identity(value, named=False):
    _keys(value, ("uid", "resourceVersion", "name") if named else ("uid", "resourceVersion"))
    identity({"metadata": value})
    if named:
        _name(value["name"])


def _hash(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise Held("The data handoff receipt fingerprint is invalid")


def _journal(job, operation, namespace):
    _keys(job, ("id", "ref"))
    _keys(job["ref"], ("namespace", "storage_writes"), ("retain_resources",))
    if job["id"] != operation or job["ref"]["namespace"] != namespace:
        raise Held("The data handoff journal belongs to another operation")
    if job["ref"].get("retain_resources", True) is not True:
        raise Held("Data handoff resources must remain retained")
    entries = job["ref"]["storage_writes"]
    if not isinstance(entries, list) or len(entries) > 128:
        raise Held("The data handoff journal is invalid or full")
    steps = set()
    for entry in entries:
        _keys(entry, ("step", "method", "target", "payload", "before", "state"), ("after", "shape"))
        step = entry["step"]
        if not isinstance(step, str) or not re.fullmatch(r"[a-zA-Z0-9_.:/-]{1,500}", step) or step in steps:
            raise Held("The data handoff journal step is invalid or repeated")
        steps.add(step)
        dest = entry["target"]
        _keys(dest, ("apiVersion", "kind", "namespace", "name", "path"))
        # Reuse the journal's strict target whitelist without accepting bodies.
        expected = target("DELETE", dest["path"], None, namespace)
        if dest != expected or entry["method"] not in ("POST", "PUT", "PATCH", "DELETE"):
            raise Held("The data handoff receipt target is invalid")
        if entry["method"] == "POST":
            if entry["before"] is not None:
                raise Held("A creation receipt cannot have an earlier resource identity")
        else:
            _identity(entry["before"])
        _hash(entry["payload"])
        if entry["state"] not in ("intent", "accepted", "refused", "uncertain", "unverified"):
            raise Held("The data handoff receipt state is invalid")
        if "after" in entry:
            _identity(entry["after"])
        if "shape" in entry:
            _hash(entry["shape"])
        if entry["state"] == "accepted" and entry["method"] != "DELETE" and not {"after", "shape"} <= set(entry):
            raise Held("The data handoff receipt is incomplete")


def _validate(state, namespace):
    _keys(state, ("protocol", "operation", "deployment", "source", "destination", "replicas", "phase", "journal"),
          ("plan", "copy_receipt"))
    if type(state["protocol"]) is not int or state["protocol"] != 1:
        raise Held("The data handoff protocol is unsupported")
    if not isinstance(state["operation"], str) or not re.fullmatch(r"[a-f0-9]{24}", state["operation"]):
        raise Held("The data handoff operation identity is invalid")
    _identity(state["deployment"], named=True)
    _identity(state["source"], named=True)
    _name(state["destination"])
    if state["destination"] == state["source"]["name"]:
        raise Held("The data handoff destination must be a different claim")
    if type(state["replicas"]) is not int or not 1 <= state["replicas"] <= 100:
        raise Held("The data handoff replica count is invalid")
    if state["phase"] not in PHASES:
        raise Held("The data handoff phase is invalid")
    _journal(state["journal"], state["operation"], namespace)
    if "plan" in state:
        plan = state["plan"]
        _keys(plan, ("deployment_shape", "source_pvc_shape", "source_pv", "destination_pvc", "destination_pv",
                     "worker", "nodes", "data_volume", "target_shareable"), ("copy_image", "copy_node"))
        for key in ("deployment_shape", "source_pvc_shape"):
            _hash(plan[key])
        for key in ("source_pv", "destination_pvc", "destination_pv", "worker"):
            fact = plan[key]
            _keys(fact, ("name", "uid", "shape"))
            _name(fact["name"])
            _identity({"uid": fact["uid"], "resourceVersion": "validated-separately"})
            _hash(fact["shape"])
        if plan["destination_pvc"]["name"] != state["destination"]:
            raise Held("The data handoff plan names a different destination")
        if (plan["destination_pvc"]["uid"] == state["source"]["uid"]
                or plan["destination_pv"]["uid"] == plan["source_pv"]["uid"]
                or plan["destination_pv"]["name"] == plan["source_pv"]["name"]):
            raise Held("The data handoff volumes must have distinct identities")
        if not isinstance(plan["nodes"], list) or not plan["nodes"]:
            raise Held("The data handoff node inventory is incomplete")
        seen = set()
        for node in plan["nodes"]:
            _keys(node, ("name", "uid", "boot_id"))
            _name(node["name"])
            _identity({"uid": node["uid"], "resourceVersion": node["boot_id"]})
            if node["name"] in seen:
                raise Held("The data handoff node inventory has duplicates")
            seen.add(node["name"])
        _name(plan["data_volume"])
        if type(plan["target_shareable"]) is not bool:
            raise Held("The data handoff access-mode plan is invalid")
        if "copy_image" in plan or "copy_node" in plan:
            if not isinstance(plan.get("copy_image"), str) or not re.fullmatch(r"[a-zA-Z0-9./:_-]+@sha256:[a-f0-9]{64}", plan["copy_image"]):
                raise Held("The data copy image is not pinned to a digest")
            if plan.get("copy_node") not in seen:
                raise Held("The data copy node was not included in the reviewed host inventory")
    if "copy_receipt" in state:
        receipt = state["copy_receipt"]
        _keys(receipt, ("state", "worker_uid"), ("manifest", "files", "bytes"))
        if not state.get("plan") or receipt["worker_uid"] != state["plan"]["worker"]["uid"]:
            raise Held("The data copy receipt belongs to another worker")
        if receipt["state"] not in ("intent", "verified", "uncertain"):
            raise Held("The data copy receipt state is invalid")
        if receipt["state"] == "verified":
            _keys(receipt, ("state", "worker_uid", "manifest", "files", "bytes"))
            _hash(receipt["manifest"])
            if any(type(receipt[k]) is not int or receipt[k] < 0 for k in ("files", "bytes")):
                raise Held("The data copy totals are invalid")
        elif set(receipt) != {"state", "worker_uid"}:
            raise Held("An unverified data copy cannot carry a completion receipt")
    encoded = json.dumps(state, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(encoded.encode()) > MAX_BYTES:
        raise Held("The data handoff control record is full; nothing more was sent")
    return encoded


class Anchor:
    def __init__(self, read, send, namespace, deployment):
        _name(namespace)
        _name(deployment)
        self.read, self.send, self.namespace = read, send, namespace
        self.name = deployment + "-data-handoff"
        _name(self.name)
        self.path = f"/api/v1/namespaces/{namespace}/configmaps/{self.name}"
        self.obj = self.state = None
        self.failed = False

    def _decode(self, obj, operation, uid=None):
        meta = obj.get("metadata", {}) if isinstance(obj, dict) else {}
        if (not isinstance(obj, dict) or obj.get("apiVersion") != "v1" or obj.get("kind") != "ConfigMap"
                or meta.get("namespace") != self.namespace or meta.get("name") != self.name
                or meta.get("labels", {}).get(LABEL) != operation
                or meta.get("deletionTimestamp") or meta.get("ownerReferences") or meta.get("finalizers")
                or obj.get("immutable") or obj.get("binaryData")
                or (uid is not None and meta.get("uid") != uid)):
            raise Held("The data handoff control resource changed or was replaced")
        identity(obj)
        _keys(obj.get("data"), ("state.json",))
        raw = obj["data"]["state.json"]
        if not isinstance(raw, str) or len(raw.encode()) > MAX_BYTES:
            raise Held("The data handoff control record is invalid")
        try:
            state = json.loads(raw)
            _validate(state, self.namespace)
        except (ValueError, TypeError, KeyError):
            raise Held("The data handoff control record cannot be verified") from None
        if state["operation"] != operation or state["deployment"]["name"] + "-data-handoff" != self.name:
            raise Held("The data handoff control record belongs to another operation")
        return state

    def create(self, *, operation, deployment, source, destination, replicas):
        if self.failed or self.obj is not None:
            raise Held("This data handoff handle cannot create another control record")
        state = {"protocol": 1, "operation": operation, "deployment": deployment, "source": source,
                 "destination": destination, "replicas": replicas, "phase": "prepare",
                 "journal": {"id": operation, "ref": {"namespace": self.namespace, "storage_writes": []}}}
        encoded = _validate(state, self.namespace)
        if deployment["name"] + "-data-handoff" != self.name:
            raise Held("The data handoff deployment does not match its control record")
        body = {"apiVersion": "v1", "kind": "ConfigMap",
                "metadata": {"namespace": self.namespace, "name": self.name, "labels": {LABEL: operation}},
                "data": {"state.json": encoded}}
        # POST is the singleton mutex; an existing name is NEVER adopted here.
        self._send("POST", self.path.rsplit("/", 1)[0], body, state)
        return self.handle()

    def load(self, *, operation, uid):
        if self.failed or self.obj is not None or not isinstance(uid, str) or not uid:
            raise Held("Load requires a fresh handle and the recorded control-resource UID")
        try:
            obj = self.read(self.path)
            state = self._decode(obj, operation, uid)
        except Exception:
            self.failed = True
            raise Held("The recorded data handoff cannot be read; no replacement was adopted") from None
        self.obj, self.state = copy.deepcopy(obj), state
        return self

    def handle(self):
        if self.failed or self.obj is None:
            raise Held("The data handoff control receipt is unavailable")
        return {"operation": self.state["operation"], "uid": identity(self.obj)["uid"]}

    def item(self):
        self.handle()
        return copy.deepcopy(self.state["journal"])

    def _send(self, method, path, body, state):
        try:
            result = self.send(method, path, body)
            decoded = self._decode(result, state["operation"], identity(self.obj)["uid"] if self.obj else None)
            if decoded != state:
                raise Held("Control receipt differs from the requested journal")
        except Exception:
            self.failed = True
            raise Held("The data handoff control write was not verified; inspect it before continuing. Nothing was retried") from None
        self.obj, self.state = copy.deepcopy(result), copy.deepcopy(decoded)

    def _replace(self, state):
        self.handle()
        encoded = _validate(state, self.namespace)
        body = copy.deepcopy(self.obj)
        body.pop("status", None)
        body["metadata"].pop("managedFields", None)
        body["data"] = {"state.json": encoded}
        # PUT carries the last accepted UID/resourceVersion. On conflict, even
        # an identical-looking journal cannot authorize this worker to proceed.
        self._send("PUT", self.path, body, state)

    def checkpoint(self, job):
        self.handle()
        _journal(job, self.state["operation"], self.namespace)
        old = self.state["journal"]["ref"]["storage_writes"]
        new = job["ref"]["storage_writes"]
        append = (len(new) == len(old) + 1 and new[:-1] == old
                  and all(e["state"] == "accepted" for e in old) and new[-1]["state"] == "intent")
        finish = (bool(old) and len(new) == len(old) and new[:-1] == old[:-1]
                  and old[-1]["state"] == "intent" and new[-1]["state"] in ("accepted", "refused", "uncertain", "unverified")
                  and all(new[-1].get(k) == v for k, v in old[-1].items() if k != "state"))
        if not (append or finish):
            raise Held("The data handoff journal cannot be rewritten or an uncertain request replayed")
        state = copy.deepcopy(self.state)
        state["journal"] = copy.deepcopy(job)
        self._replace(state)

    def advance(self, phase):
        self.handle()
        if (self.state["phase"] == "done" or phase != PHASES[PHASES.index(self.state["phase"]) + 1]
                or any(e["state"] != "accepted" for e in self.state["journal"]["ref"]["storage_writes"])):
            raise Held("The data handoff cannot skip a phase or advance past an unresolved write")
        if "plan" in self.state and self.state["phase"] == "copy" and self.state.get("copy_receipt", {}).get("state") != "verified":
            raise Held("The data handoff cannot advance without a verified copy receipt")
        state = copy.deepcopy(self.state)
        state["phase"] = phase
        self._replace(state)

    def configure(self, plan):
        """Pin reviewed, non-secret facts once, before this worker stops anything."""
        self.handle()
        if self.state["phase"] != "prepare" or "plan" in self.state:
            raise Held("The data handoff plan is already pinned and cannot be replaced")
        state = copy.deepcopy(self.state)
        state["plan"] = copy.deepcopy(plan)
        self._replace(state)

    def copy_started(self):
        self.handle()
        if (self.state["phase"] != "copy" or "copy_receipt" in self.state or "plan" not in self.state
                or any(e["state"] != "accepted" for e in self.state["journal"]["ref"]["storage_writes"])):
            raise Held("A data copy cannot start again or outside its reviewed phase")
        state = copy.deepcopy(self.state)
        state["copy_receipt"] = {"state": "intent", "worker_uid": state["plan"]["worker"]["uid"]}
        self._replace(state)

    def copy_finished(self, receipt=None):
        self.handle()
        if self.state.get("copy_receipt", {}).get("state") != "intent":
            raise Held("The data copy has no pending intent that this worker can finish")
        if receipt is not None:
            _keys(receipt, ("manifest", "files", "bytes"))
        state = copy.deepcopy(self.state)
        state["copy_receipt"] = {"state": "verified" if receipt is not None else "uncertain",
                                 "worker_uid": state["plan"]["worker"]["uid"], **(receipt or {})}
        self._replace(state)
