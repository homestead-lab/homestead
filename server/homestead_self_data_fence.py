"""Startup/write decisions for the self-data handoff (integration in progress).

A copied marker is a pointer, never an authorization. Startup always checks the
Kubernetes control record. A source-volume process cannot resume after cutover;
a destination may serve read-only while its actual application startup is being
verified. The wrapper must gate imports/background jobs and mutating requests.
"""
import json
import os
import re
import stat
import urllib.error

import homestead_self_data_anchor as A
from homestead_storage_journal import Held, identity, shape


MARKER = ".self-data-handoff-v1.json"


def read_marker(directory):
    path = os.path.join(directory, MARKER)
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError:
        raise Held("The data handoff marker cannot be inspected") from None
    try:
        if not stat.S_ISREG(before.st_mode):
            raise Held("The data handoff marker is not a regular local record")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(descriptor, "rb") as handle:
            current = os.fstat(handle.fileno())
            if not stat.S_ISREG(current.st_mode) or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino):
                raise Held("The data handoff marker changed while opening it")
            raw = handle.read(8193)
            after = os.fstat(handle.fileno())
            if (current.st_size, current.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise Held("The data handoff marker changed while reading it")
        published = os.lstat(path)
        if (published.st_dev, published.st_ino, published.st_size, published.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise Held("The data handoff marker changed while reading it")
    except (OSError, UnicodeError):
        raise Held("The data handoff marker cannot be read; data access must be recovered") from None
    if len(raw) > 8192:
        raise Held("The data handoff marker is invalid")
    try:
        value = json.loads(raw)
        A._keys(value, ("protocol", "namespace", "deployment", "operation", "anchor_uid", "source", "source_uid", "destination", "destination_uid"))
        if type(value["protocol"]) is not int or value["protocol"] != 1:
            raise ValueError()
        for key in ("namespace", "deployment", "source", "destination"):
            A._name(value[key])
        for key in ("operation", "anchor_uid", "source_uid", "destination_uid"):
            if not isinstance(value[key], str) or not 0 < len(value[key]) <= 1024:
                raise ValueError()
        if not re.fullmatch(r"[a-f0-9]{24}", value["operation"]):
            raise ValueError()
        if value["source"] == value["destination"] or value["source_uid"] == value["destination_uid"]:
            raise ValueError()
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise Held("The data handoff marker is invalid; it cannot authorize startup") from None
    return value


class Fence:
    def __init__(self, read, namespace, deployment, pod, container, directory):
        for name in (namespace, deployment, pod, container):
            A._name(name)
        self.read, self.namespace, self.deployment = read, namespace, deployment
        self.pod, self.container, self.directory = pod, container, directory
        self.checked = False
        self.observed_marker = False

    def _anchor(self):
        return A.Anchor(self.read, lambda *_: (_ for _ in ()).throw(Held("Startup never writes control records")),
                        self.namespace, self.deployment)

    def inspect(self):
        """Fresh API proof; returns normal/start/done or raises a safe hold."""
        try:
            return self._inspect()
        except Held:
            raise
        except Exception:
            raise Held("Data handoff state is unavailable; startup and writes remain held") from None

    def _inspect(self):
        marker = read_marker(self.directory)
        if marker is None and self.observed_marker:
            raise Held("The recorded data handoff marker disappeared; it is not safe to resume")
        self.observed_marker = self.observed_marker or marker is not None
        control = self._anchor()
        if marker is None:
            try:
                self.read(control.path)
            except urllib.error.HTTPError as error:
                if error.code == 404:
                    self.checked = True
                    return {"mode": "normal", "writable": True}
                raise
            # Finding a same-name record after a missing/lost pointer is not
            # enough to establish which operation this mounted data belongs to.
            raise Held("A data handoff exists but its local receipt is missing; inspect both retained volumes")
        if (marker["namespace"], marker["deployment"]) != (self.namespace, self.deployment):
            raise Held("This data handoff marker belongs to another Homestead installation")
        control.load(operation=marker["operation"], uid=marker["anchor_uid"])
        state = control.state
        if state.get("runtime", {}).get("state") == "held":
            raise Held("The data move needs a recovery review; startup and writes remain held")
        if state.get("pointer_receipt") != A.pointer_digest(self.namespace, state, marker["anchor_uid"]):
            raise Held("The data handoff local receipt was not confirmed; startup and writes remain held")
        plan = state.get("plan", {})
        if (state["source"]["name"], state["source"]["uid"], state["destination"],
                plan.get("destination_pvc", {}).get("uid")) != (
                marker["source"], marker["source_uid"], marker["destination"], marker["destination_uid"]):
            raise Held("The local data handoff receipt no longer matches the reviewed volumes")
        entries = state["journal"]["ref"]["storage_writes"]
        if any(e["state"] != "accepted" for e in entries):
            raise Held("The data handoff has an unresolved request; no jobs will be resumed")
        phase = state["phase"]
        if phase not in ("start", "done") or state.get("copy_receipt", {}).get("state") != "verified":
            raise Held("Homestead is held while its data is being moved; the coordinator owns progress")
        dep_path = f"/apis/apps/v1/namespaces/{self.namespace}/deployments/{self.deployment}"
        starts = [e for e in entries if e["step"] == "start" and e["method"] == "PUT" and e["target"]["path"] == dep_path]
        releases = [e for e in entries if e["step"] == "release-copy" and e["method"] == "DELETE" and e["target"]["kind"] == "Job"]
        if len(starts) != 1 or len(releases) != 1:
            raise Held("The data handoff has no verified restart and copy-release receipts")
        dep = self.read(dep_path)
        if (identity(dep)["uid"] != state["deployment"]["uid"] or dep["metadata"].get("deletionTimestamp")
                or identity(dep)["uid"] != starts[0]["after"]["uid"]):
            raise Held("Homestead's Deployment identity changed during its data move")
        # During startup the exact reviewed spec must still be in effect. After
        # completion later normal updates are allowed, but data identity isn't.
        if phase == "start" and shape(dep) != starts[0]["shape"]:
            raise Held("Homestead changed before its data-move restart was verified")
        self._mounted_destination(marker, plan, dep)
        self.checked = True
        return {"mode": phase, "writable": phase == "done", "operation": state["operation"],
                "anchor_uid": marker["anchor_uid"], "destination_uid": marker["destination_uid"]}

    def _mounted_destination(self, marker, plan, dep):
        pod = self.read(f"/api/v1/namespaces/{self.namespace}/pods/{self.pod}")
        meta, spec = pod.get("metadata", {}), pod.get("spec", {})
        if (meta.get("name") != self.pod or meta.get("namespace") != self.namespace
                or not identity(pod)["uid"] or meta.get("deletionTimestamp")):
            raise Held("The current Homestead pod cannot be identified")
        owners = [o for o in meta.get("ownerReferences", []) if o.get("controller") is True]
        if len(owners) != 1 or owners[0].get("kind") != "ReplicaSet":
            raise Held("The current pod does not belong to Homestead's reviewed Deployment")
        A._name(owners[0].get("name"))
        replica_set = self.read(f"/apis/apps/v1/namespaces/{self.namespace}/replicasets/{owners[0]['name']}")
        if identity(replica_set)["uid"] != owners[0].get("uid") or not any(
                o.get("controller") is True and o.get("kind") == "Deployment" and o.get("uid") == identity(dep)["uid"]
                for o in replica_set.get("metadata", {}).get("ownerReferences", [])):
            raise Held("The current pod's controller was replaced or belongs elsewhere")
        containers = [c for c in spec.get("containers", []) if c.get("name") == self.container]
        if len(containers) != 1:
            raise Held("The current Homestead data mount is ambiguous")
        mounts = [m for m in containers[0].get("volumeMounts", []) if m.get("mountPath", "").rstrip("/") == self.directory.rstrip("/")]
        if (len(mounts) != 1 or any(mounts[0].get(k) for k in ("subPath", "subPathExpr", "readOnly"))
                or mounts[0].get("name") != plan["data_volume"]):
            raise Held("Data moves require the reviewed whole-volume Homestead data mount")
        volumes = [v for v in spec.get("volumes", []) if v.get("name") == mounts[0]["name"]]
        if len(volumes) != 1 or volumes[0].get("persistentVolumeClaim", {}).get("readOnly"):
            raise Held("The current Homestead data claim cannot be verified")
        if volumes[0].get("persistentVolumeClaim", {}).get("claimName") != marker["destination"]:
            raise Held("This pod still mounts the original data volume; its jobs must not resume")
        expected = plan["destination_pvc"]
        pvc = self.read(f"/api/v1/namespaces/{self.namespace}/persistentvolumeclaims/{marker['destination']}")
        pv = self.read("/api/v1/persistentvolumes/" + plan["destination_pv"]["name"])
        if (identity(pvc)["uid"] != expected["uid"] or pvc["metadata"].get("deletionTimestamp")
                or pvc.get("status", {}).get("phase") != "Bound"
                or pvc.get("spec", {}).get("volumeName") != plan["destination_pv"]["name"]
                or identity(pv)["uid"] != plan["destination_pv"]["uid"] or pv["metadata"].get("deletionTimestamp")
                or (pv.get("spec", {}).get("claimRef", {}).get("namespace"), pv["spec"].get("claimRef", {}).get("name"),
                    pv["spec"].get("claimRef", {}).get("uid")) != (self.namespace, marker["destination"], expected["uid"])):
            raise Held("The mounted destination no longer has its reviewed storage binding")
        data = [v for v in dep.get("spec", {}).get("template", {}).get("spec", {}).get("volumes", []) if v.get("name") == plan["data_volume"]]
        if len(data) != 1 or data[0].get("persistentVolumeClaim", {}).get("claimName") != marker["destination"]:
            raise Held("The Deployment no longer points to the verified data volume")

    def require_write(self):
        """Gate each mutation, including background writes; no cached allow in a move.

        A normal boot with no marker needs no extra API read on every write.
        Setup must durably publish the marker on the shared source BEFORE it
        authorizes stopping writers. Once observed, removing it is a hard hold.
        """
        if self.checked and not self.observed_marker and read_marker(self.directory) is None:
            return
        result = self.inspect()
        if not result["writable"]:
            raise Held("Homestead is verifying its new data volume; changes and background jobs are held")
