"""Durable, one-shot writes for a storage handoff under the operations lock.

The workflow chooses each step, supplies a reviewed identity and owns admission,
quiescence and recovery. This module does not authorize a workflow or infer that a
lost request failed. Receipts contain hashes/identities, never manifest bodies.
"""
import copy
import hashlib
import json
import re
import urllib.error


class Held(ValueError):
    """Public, credential-free reason to inspect the retained storage operation."""


class NextWrite(Held):
    """Read-only inspection reached a verified next write; nothing was sent."""
    def __init__(self, step, method, target, before, payload):
        super().__init__("Next storage step is ready for review")
        self.plan = {"step": step, "method": method, "target": target, "before": before, "payload": payload}


NAME = r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?"
PATH = re.compile(rf"^/(?:api/(v1)|apis/([a-z0-9.]+/[a-z0-9]+))/(?:namespaces/({NAME})/)?([a-z]+)(?:/({NAME}))?$")
KINDS = {
    ("v1", "persistentvolumeclaims"): "PersistentVolumeClaim",
    ("v1", "persistentvolumes"): "PersistentVolume",
    ("v1", "pods"): "Pod",
    ("apps/v1", "deployments"): "Deployment",
    ("apps/v1", "statefulsets"): "StatefulSet",
    ("batch/v1", "cronjobs"): "CronJob",
    ("batch/v1", "jobs"): "Job",
    ("kubevirt.io/v1", "virtualmachines"): "VirtualMachine",
    ("cdi.kubevirt.io/v1beta1", "datavolumes"): "DataVolume",
}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def identity(obj):
    meta = obj.get("metadata", {}) if isinstance(obj, dict) else {}
    result = {key: meta.get(key) for key in ("uid", "resourceVersion")}
    if any(not isinstance(value, str) or not 0 < len(value) <= 1024 for value in result.values()):
        raise Held("Storage resource identity/version is unavailable")
    return result


# Written onto a Pod by the network plugin once its sandbox is up - Multus's
# attachment status, Calico's address (RKE2's Canal). They report what was
# set up; they neither schedule nor grant anything, so they are not edits.
CNI_ANNOTATIONS = frozenset({
    "k8s.v1.cni.cncf.io/network-status", "k8s.v1.cni.cncf.io/networks-status",
    "cni.projectcalico.org/podIP", "cni.projectcalico.org/podIPs", "cni.projectcalico.org/containerID",
})


def pod_annotations(obj):
    """A Pod's annotations without what its network plugin reported."""
    annotations = dict(((obj.get("metadata") or {}).get("annotations")) or {})
    # A Pod from a list has no kind of its own (the list endpoint drops each
    # item's TypeMeta): with Multus writing network-status on every pod, the
    # same pod read two ways looked changed, and the data move review refused
    # with "This Homestead pod is not one of the reviewed Deployment's replicas".
    kind = obj.get("kind")
    if kind == "Pod" or (kind is None and isinstance((obj.get("spec") or {}).get("containers"), list)):
        for key in CNI_ANNOTATIONS:
            annotations.pop(key, None)
    return annotations


def shape(obj):
    """Ignore status/managedFields, but do not overlook edits to storage or holds."""
    meta = obj.get("metadata") or {}
    annotations = pod_annotations(obj)
    if obj.get("kind") in ("Deployment", "ReplicaSet") and obj.get("apiVersion") == "apps/v1":
        # The Deployment controller increments this asynchronously after our
        # acknowledged template update. It is status bookkeeping, not a new
        # user edit. All spec, labels and other annotations remain pinned.
        annotations.pop("deployment.kubernetes.io/revision", None)
        # Observational image bookkeeping is refreshed asynchronously too,
        # then copied onto ReplicaSets by the Deployment controller.
        # The image in spec (and self-data's live runtime identity) is still
        # checked; this annotation neither schedules nor changes the workload.
        annotations.pop("homestead.io/ran-digests", None)
    return digest({"spec": obj.get("spec"), "labels": meta.get("labels", {}),
                   "annotations": annotations, "owners": meta.get("ownerReferences", [])})


def target(method, path, body, namespace):
    match = PATH.fullmatch(path) if isinstance(path, str) else None
    if not match or method not in ("POST", "PUT", "PATCH", "DELETE"):
        raise Held("Unsupported storage mutation target")
    core, grouped, ns, plural, name = match.groups()
    version = core or grouped
    kind = KINDS.get((version, plural))
    if not kind or (ns != namespace if plural != "persistentvolumes" else ns is not None):
        raise Held("Storage mutation is outside the reviewed namespace")
    if method == "POST":
        if name or not isinstance(body, dict):
            raise Held("Storage creation target is invalid")
        name = (body.get("metadata") or {}).get("name")
        if any((body.get("metadata") or {}).get(key) for key in ("uid", "resourceVersion", "generateName")):
            raise Held("Storage creation cannot reuse an existing identity or generated name")
    if not isinstance(name, str) or len(name) > 253 or not re.fullmatch(NAME, name):
        raise Held("Storage resource name is invalid")
    if method != "DELETE":
        if not isinstance(body, dict):
            raise Held("Storage mutation body is invalid")
        meta = body.get("metadata") or {}
        if any(value is not None and value != wanted for value, wanted in (
                (meta.get("name"), name), (meta.get("namespace"), ns),
                (body.get("kind"), kind), (body.get("apiVersion"), version))):
            raise Held("Storage mutation body does not match its target")
    return {"apiVersion": version, "kind": kind, "namespace": ns, "name": name,
            "path": path + "/" + name if method == "POST" else path}


def _payload(body):
    result = copy.deepcopy(body)
    if isinstance(result, dict):
        result.pop("status", None)
        for key in ("resourceVersion", "managedFields"):
            if isinstance(result.get("metadata"), dict):
                result["metadata"].pop(key, None)
    return digest(result)


class Journal:
    def __init__(self, item, read, send, checkpoint, *, preview=False, before_write=None):
        self.item, self.read, self.send, self.checkpoint = item, read, send, checkpoint
        self.ref = item["ref"]
        self.failed = False
        self.preview = preview
        self.before_write = before_write
        self.entries = self.ref.setdefault("storage_writes", [])
        if (not isinstance(self.entries, list) or any(not isinstance(e, dict) or not isinstance(e.get("step"), str) for e in self.entries)
                or len({e["step"] for e in self.entries}) != len(self.entries)):
            raise Held("Storage write history is invalid; inspect the job")

    def _get(self, path):
        try:
            return self.read(path)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            raise Held("Storage resource state could not be read") from None
        except Exception:
            raise Held("Storage resource state could not be read") from None

    def check(self):
        if self.failed or any(e.get("state") != "accepted" for e in self.entries):
            raise Held("A storage request is unresolved; inspect the job before continuing")

    def _save(self):
        self.ref["retain_resources"] = True
        try:
            self.checkpoint(self.item)
        except Exception:
            self.failed = True
            raise Held("Storage write progress could not be saved; inspect its retained intent before continuing") from None

    def observe(self, entry):
        """Observe a confirmed receipt, never adopt or resend an uncertain one."""
        if entry.get("state") != "accepted":
            raise Held("A storage request is unresolved; nothing will be repeated automatically")
        current = self._get(entry["target"]["path"])
        expected = entry["before"] if entry["method"] == "DELETE" else entry["after"]
        if current is None and entry["method"] == "DELETE":
            return None
        self._check_target(current, entry["target"])
        if identity(current)["uid"] != expected["uid"]:
            raise Held("A storage resource was replaced; the receipt cannot apply to its replacement")
        if entry["method"] != "DELETE":
            if current["metadata"].get("deletionTimestamp") or shape(current) != entry["shape"]:
                raise Held("A confirmed storage resource changed; review the handoff before continuing")
        return current  # accepted deletion may still be waiting for finalizers

    @staticmethod
    def _check_target(obj, dest):
        meta = obj.get("metadata", {}) if isinstance(obj, dict) else {}
        if not isinstance(obj, dict) or any(value != wanted for value, wanted in (
                (meta.get("name"), dest["name"]), (meta.get("namespace"), dest["namespace"]),
                (obj.get("kind"), dest["kind"]), (obj.get("apiVersion"), dest["apiVersion"]))):
            raise Held("Storage API receipt does not match the requested object")
        identity(obj)

    def write(self, step, method, path, body=None, *, expected=None, ctype=None):
        """Send once, with optimistic concurrency and a durable receipt.

        Calling an accepted step again only observes it. Later steps may start
        only after every previous request has a verified acceptance receipt.
        expected is the reviewed UID/resourceVersion (and optionally shape).
        """
        self.check()
        if not isinstance(step, str) or not re.fullmatch(r"[a-zA-Z0-9_.:/-]{1,500}", step):
            raise Held("Storage write step is invalid")
        dest = target(method, path, body, self.ref["namespace"])
        if ctype not in (None, "application/merge-patch+json") or (method == "PATCH" and ctype != "application/merge-patch+json"):
            raise Held("Unsupported storage patch type")
        fingerprint = _payload(body)
        existing = next((e for e in self.entries if e["step"] == step), None)
        if existing:
            if (existing["method"], existing["target"], existing["payload"]) != (method, dest, fingerprint):
                raise Held("A completed storage step cannot be reused for a different request")
            if method != "POST" and any((expected or {}).get(key) != existing["before"][key] for key in ("uid", "resourceVersion")):
                raise Held("A completed storage step cannot be reused with a different reviewed identity")
            return self.observe(existing)
        current = self._get(dest["path"])
        before = None
        if method == "POST":
            if expected is not None or current is not None:
                raise Held("The storage creation name is already used; it will not be adopted")
        else:
            self._check_target(current, dest)
            before = identity(current)
            if not isinstance(expected, dict) or any(before[key] != expected.get(key) for key in before):
                raise Held("Storage resource changed after review; no request was sent")
            if expected.get("shape") and shape(current) != expected["shape"]:
                raise Held("Storage resource configuration changed after review")
            if current["metadata"].get("deletionTimestamp"):
                raise Held("Storage resource is already deleting; inspect it before continuing")
        request = copy.deepcopy(body)
        if method in ("PUT", "PATCH"):
            meta = request.setdefault("metadata", {})
            if any(meta.get(key) is not None and meta[key] != before[key] for key in before):
                raise Held("Storage mutation was prepared from a different resource version")
            meta.update(before)
        elif method == "DELETE":
            request = {"apiVersion": "v1", "kind": "DeleteOptions", "propagationPolicy": "Background", **(request or {})}
            if request.get("preconditions") not in (None, before):
                raise Held("Storage delete preconditions do not match the reviewed object")
            request["preconditions"] = before
        if self.before_write is not None:
            self.before_write()
        event = {"step": step, "method": method, "target": dest, "payload": fingerprint,
                 "before": before, "state": "intent"}
        if self.preview:
            # All validation above still runs. No intent, durable save or API
            # dispatch may occur in an inspector's request-local journal.
            raise NextWrite(step, method, dest, before, fingerprint)
        self.entries.append(event)
        self._save()  # must reach durable storage before the API call
        try:
            result = self.send(method, path, request, **({"ctype": ctype} if ctype else {}))
        except Exception as error:
            self.failed = True
            event["state"] = "refused" if isinstance(error, urllib.error.HTTPError) and error.code in (400, 401, 403, 404, 409, 422) else "uncertain"
            try:
                self._save()
            except Held:
                pass  # the durable intent is sufficient to prohibit replay
            raise Held("Storage request failed or its outcome is uncertain; inspect retained resources. Nothing was retried") from None
        try:
            if method == "DELETE" and isinstance(result, dict) and result.get("kind") == "Status":
                if result.get("status") != "Success" or result.get("details", {}).get("uid") not in (None, before["uid"]):
                    raise Held("Storage deletion receipt could not be verified")
            else:
                self._check_target(result, dest)
                after = identity(result)
                if before and after["uid"] != before["uid"]:
                    raise Held("Storage mutation receipt belongs to a replacement object")
                event.update(after=after, shape=shape(result))
            event["state"] = "accepted"
        except Exception:
            self.failed = True
            event["state"] = "unverified"
            try:
                self._save()
            except Held:
                pass
            raise Held("Storage API receipt could not be verified; no resource will be adopted or request repeated") from None
        self._save()
        return result
