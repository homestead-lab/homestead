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
import threading
import time
from contextlib import contextmanager

try:
    import fcntl
except ImportError:
    fcntl = None

import homestead_self_data_anchor as A
from homestead_storage_journal import Held, identity, shape


MARKER = ".self-data-handoff-v1.json"


class WriteBarrier:
    """Shared activity locks drain all upgraded replicas before publication.

    The lock file is never removed or replaced. Normal activity takes shared
    locks, so requests remain concurrent. Freeze takes an exclusive lock and
    must publish the durable startup fence before releasing it. New activity
    fails promptly while frozen; an existing activity can finish nested writes.
    A timeout does not stop processes or prove that they have drained.
    """
    def __init__(self, directory, check):
        self.directory, self.check = directory, check
        self.local = threading.local()

    def _open(self):
        if fcntl is None:
            raise Held("Self-data moves need Linux shared-file locking")
        path = os.path.join(self.directory, ".self-data-access-v1.lock")
        try:
            os.makedirs(self.directory, exist_ok=True)
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        except OSError:
            raise Held("The shared data activity lock is unavailable; no change was authorized") from None
        try:
            info, current = os.fstat(fd), os.lstat(path)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino)):
                raise Held("The shared data activity lock cannot be verified")
            return fd
        except BaseException:
            os.close(fd)
            raise

    @contextmanager
    def activity(self):
        if getattr(self.local, "depth", 0):
            self.check()
            self.local.depth += 1
            try:
                yield
            finally:
                self.local.depth -= 1
            return
        self.check()
        fd = self._open()
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Held("Homestead is finishing current work for its data move. Wait before making changes") from None
            except OSError:
                raise Held("The shared data activity lock is unavailable; no change was authorized") from None
            self.local.depth = 1
            self.check()  # Fence may have been published while opening the lock.
            yield
        finally:
            self.local.depth = 0
            os.close(fd)

    @contextmanager
    def freeze(self, timeout=30):
        if getattr(self.local, "depth", 0):
            raise Held("The data handoff must drain outside an active request or job")
        self.check()
        fd = self._open()
        try:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise Held("Current Homestead activity has not finished. No copy or shutdown was authorized") from None
                    time.sleep(.05)
                except OSError:
                    raise Held("The data activity lock could not be acquired; no copy or shutdown was authorized") from None
            self.local.depth = 1
            self.check()
            yield
        finally:
            self.local.depth = 0
            os.close(fd)


def mounted_data(directory):
    """Verify the process actually sees a distinct, whole writable data mount.

    Pod specs describe intent, not the process mount namespace. Resolve the
    opened directory's mount ID through procfs and reject symlink/subdirectory
    layouts. The PVC association still comes from the verified runtime Pod;
    this is not independent CSI/device attestation.
    """
    if os.name != "posix" or not os.path.isabs(directory) or os.path.normpath(directory) != directory or directory == "/":
        raise Held("Data moves require an absolute, dedicated Linux data mount")
    descriptor = None
    try:
        if os.path.realpath(directory) != directory:
            raise Held("The data directory must not be a symbolic link")
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        before = os.fstat(descriptor)
        with open(f"/proc/self/fdinfo/{descriptor}", encoding="utf-8") as handle:
            ids = [line.split(":", 1)[1].strip() for line in handle if line.startswith("mnt_id:")]
        if len(ids) != 1 or not ids[0].isdigit():
            raise Held("The current data mount identity is unavailable")
        with open("/proc/self/mountinfo", encoding="utf-8") as handle:
            records = [line.split() for line in handle]
        selected = [row for row in records if row and row[0] == ids[0]]
        if len(selected) != 1:
            raise Held("The current data mount cannot be identified")
        row = selected[0]
        split = row.index("-")
        unescape = lambda value: re.sub(r"\\(040|011|012|134)", lambda m: chr(int(m[1], 8)), value)
        if (len(row) < split + 4 or split < 6 or unescape(row[4]) != directory
                or "rw" not in row[5].split(",") or "rw" not in row[split + 3].split(",")
                or row[2] != f"{os.major(before.st_dev)}:{os.minor(before.st_dev)}"
                or row[split + 1] in ("overlay", "tmpfs", "ramfs", "proc", "sysfs")):
            raise Held("Homestead's data directory is not its writable persistent volume mount")
        after = os.stat(directory, follow_symlinks=False)
        if (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino):
            raise Held("The data mount changed during verification")
        return {"mount_id": ids[0], "device": row[2], "inode": before.st_ino,
                "root": unescape(row[3]), "filesystem": row[split + 1]}
    except Held:
        raise
    except Exception:
        raise Held("The running process could not verify its data mount; no move was authorized") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def pin_app_image(deployment, image):
    """Keep the restarted app on the exact source binary, not a mutable tag."""
    rows = [c for c in deployment["spec"]["template"]["spec"].get("containers", [])
            if c.get("name") == deployment["metadata"]["name"]]
    if len(rows) != 1 or not re.fullmatch(r"[a-zA-Z0-9./:_-]+@sha256:[a-f0-9]{64}", image or ""):
        raise Held("The restarted Homestead image must be its reviewed source digest")
    rows[0]["image"] = image
    for init in deployment["spec"]["template"]["spec"].get("initContainers", []):
        if init.get("name") == "data-permissions":
            init["image"] = image


def require_app_readiness(pod_spec, container_name):
    """A Ready condition without the app's HTTP probe cannot prove app startup.

    Keep the shipped command/HTTP endpoint; custom launchers/proxies need their
    own verified protocol rather than treating a running process as readiness.
    """
    rows = [c for c in pod_spec.get("containers", []) if c.get("name") == container_name]
    if len(rows) != 1:
        raise Held("Homestead's application container cannot be identified for restart")
    c = rows[0]
    if c.get("command") not in (None, [], ["python3", "/srv/server.py"]) or c.get("args"):
        raise Held("Custom Homestead startup commands need a verified data-move readiness protocol")
    ports = [e for e in c.get("env", []) if e.get("name") == "PORT"]
    if len(ports) > 1 or ports and (set(ports[0]) != {"name", "value"} or not isinstance(ports[0]["value"], str)):
        raise Held("Homestead's HTTP port must be explicit for restart verification")
    if c.get("envFrom") and not ports:
        raise Held("Set Homestead's PORT explicitly before moving data with inherited environment settings")
    port = ports[0]["value"] if ports else "8080"
    if not port.isascii() or not port.isdecimal() or not 1 <= int(port) <= 65535:
        raise Held("Homestead's HTTP port is invalid")
    probe = c.get("readinessProbe", {})
    http = probe.get("httpGet", {})
    selected = http.get("port")
    if isinstance(selected, str):
        matches = [p for p in c.get("ports", []) if p.get("name") == selected and p.get("protocol", "TCP") == "TCP"]
        selected = matches[0].get("containerPort") if len(matches) == 1 else None
    if (any(k in probe for k in ("exec", "tcpSocket", "grpc")) or not http
            or set(http) - {"path", "port", "scheme", "host"} or http.get("host")
            or http.get("scheme", "HTTP") != "HTTP" or http.get("path") != "/healthz"
            or type(selected) is not int or selected != int(port)):
        raise Held("Moving data requires Homestead's HTTP readiness probe at /healthz on its application port")


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
        self.completed_marker = None
        self.completed_checked = False

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
        from homestead_self_data_finish import read as read_completed
        completed = read_completed(self.directory, self.namespace, self.deployment)
        if completed:
            receipt, saved = completed
            aborted = saved.state.get("setup_aborted") is True or saved.state.get("recovery", {}).get("action") == "return-original"
            pointer = A.pointer(self.namespace, saved.state, saved.handle()["uid"]) if "plan" in saved.state else None
            if marker is None or marker == pointer:
                live = None
                try: live = self.read(saved.path)
                except urllib.error.HTTPError as error:
                    if error.code != 404: raise
                if live is None or identity(live)["uid"] == saved.handle()["uid"]:
                    dep = self.read(f"/apis/apps/v1/namespaces/{self.namespace}/deployments/{self.deployment}")
                    if identity(dep)["uid"] != saved.state["deployment"]["uid"] or dep["metadata"].get("deletionTimestamp"):
                        raise Held("The completed data move belongs to a different Deployment")
                    if aborted:
                        binding = receipt["source_binding"]
                        self._mounted_destination({"destination": saved.state["source"]["name"]}, binding, dep)
                    else:
                        self._mounted_destination(pointer, saved.state["plan"], dep)
                    self.checked = True
                    self.completed_marker = pointer
                    self.completed_checked = True
                    return {"mode": "done", "writable": True, "operation": saved.state["operation"], "anchor_uid": saved.handle()["uid"], "cleanup": True}
        if marker is None and self.observed_marker:
            raise Held("The recorded data handoff marker disappeared; it is not safe to resume")
        self.observed_marker = self.observed_marker or marker is not None
        control = self._anchor()
        if marker is None:
            try:
                obj = self.read(control.path)
            except urllib.error.HTTPError as error:
                if error.code == 404:
                    self.checked = True
                    return {"mode": "normal", "writable": True}
                raise
            operation = obj.get("metadata", {}).get("labels", {}).get(A.LABEL)
            control.load(operation=operation, uid=identity(obj)["uid"])
            return self.recovery(control)
        if (marker["namespace"], marker["deployment"]) != (self.namespace, self.deployment):
            raise Held("This data handoff marker belongs to another Homestead installation")
        control.load(operation=marker["operation"], uid=marker["anchor_uid"])
        state = control.state
        if "pointer_receipt" not in state:
            return self.recovery(control)
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
        if state.get("recovery", {}).get("action") == "return-original":
            starts = [e for e in entries if e["step"] == "recover-start" and e["method"] == "PUT"
                      and e["target"]["path"] == f"/apis/apps/v1/namespaces/{self.namespace}/deployments/{self.deployment}"]
            if len(starts) != 1 or any(e["step"] in ("switch", "start") for e in entries):
                raise Held("Original-volume recovery has no verified restart or has crossed cutover")
            if any(e["step"] == "copy-job" for e in entries) and not any(
                    e["step"] in ("release-copy", "recover-release-copy") and e["method"] == "DELETE" for e in entries):
                raise Held("Original-volume recovery has no copy mount release receipt")
            dep = self.read(f"/apis/apps/v1/namespaces/{self.namespace}/deployments/{self.deployment}")
            if identity(dep)["uid"] != state["deployment"]["uid"] or dep["metadata"].get("deletionTimestamp"):
                raise Held("Original-volume recovery Deployment was replaced")
            if state["phase"] != "done" and shape(dep) != starts[0]["shape"]:
                raise Held("Homestead changed before original-volume recovery was verified")
            binding = {"data_volume": plan["data_volume"], "destination_pvc": {"name": state["source"]["name"], "uid": state["source"]["uid"]},
                       "destination_pv": {k: plan["source_pv"][k] for k in ("name", "uid")}}
            self._mounted_destination({"destination": state["source"]["name"]}, binding, dep)
            self.checked = True
            return {"mode": "recovery", "writable": state["phase"] == "done", "operation": state["operation"],
                    "anchor_uid": marker["anchor_uid"], "source_binding": binding}
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

    def recovery(self, control=None):
        """Only an unchanged original mount may abandon unpublished preparation.

        This is not rollback: no stop, copy or claim switch has been authorized.
        The abort CAS races safely with publication, and permanently disarms it.
        """
        if control is None:
            obj = self.read(self._anchor().path)
            control = self._anchor().load(operation=obj["metadata"]["labels"][A.LABEL], uid=identity(obj)["uid"])
        state = control.state
        if state["phase"] != "prepare" or "pointer_receipt" in state or state["journal"]["ref"]["storage_writes"]:
            raise Held("This move has been handed over. Recovery must inspect both retained volumes; automatic rollback is not safe")
        marker = read_marker(self.directory)
        if marker is not None and marker != A.pointer(self.namespace, state, control.handle()["uid"]):
            raise Held("The local move receipt does not match this preparation")
        dep = self.read(f"/apis/apps/v1/namespaces/{self.namespace}/deployments/{self.deployment}")
        if identity(dep)["uid"] != state["deployment"]["uid"] or dep["metadata"].get("deletionTimestamp"):
            raise Held("The original Deployment was replaced")
        source = state["source"]
        pvc = self.read(f"/api/v1/namespaces/{self.namespace}/persistentvolumeclaims/{source['name']}")
        pv = self.read("/api/v1/persistentvolumes/" + pvc["spec"]["volumeName"])
        binding = {"data_volume": "data", "destination_pvc": {"name": source["name"], "uid": source["uid"]},
                   "destination_pv": {"name": pv["metadata"]["name"], "uid": identity(pv)["uid"]}}
        self._mounted_destination({"destination": source["name"]}, binding, dep)
        return {"mode": "recovery", "writable": bool(state.get("setup_aborted")), "operation": state["operation"],
                "anchor_uid": control.handle()["uid"], "source_binding": binding}

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
        marker = read_marker(self.directory)
        if self.checked and self.completed_checked and marker in (None, self.completed_marker):
            return  # Verified destination retirement; a new move's marker revokes this.
        if self.checked and not self.observed_marker and marker is None:
            return
        result = self.inspect()
        if not result["writable"]:
            raise Held("Homestead is verifying its new data volume; changes and background jobs are held")
