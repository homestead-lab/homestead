"""Moving a volume to another storage class.

Kubernetes cannot change a claim's class, so the data moves instead, and the
claim keeps its name - so nothing that mounts it by name has to change:

  1. every workload and VM using the claim is stopped, and how each was
     running is recorded;
  2. a new volume the same size is made on the new class;
  3. a Job copies the data across and checks it: files with rsync, owners,
     permissions, ACLs and extended attributes kept, then compared by
     checksum; a raw disk (a VM's) block for block, skipping what is empty,
     then compared byte for byte;
  4. the new volume takes the claim's name: both volumes are set to keep
     their data, the temporary claim and the original are deleted, and a
     claim of the original name is bound to the new volume;
  5. everything is started again the way it was.

Before the swap, a failure puts everything back as it was - the new volume
removed, the workloads started on their untouched original. After it, the
swap only goes forward. The original volume is kept, released, until it is
removed from Volumes, so the old data is there if the new copy disappoints.

Each step is recorded on the operation and advanced by the operations poll,
so a Homestead restart part-way picks up where it stopped.
"""
import json
import re
import secrets
import time
import urllib.error
import urllib.parse

import homestead_names as NAMES

kget = ksend = ktext = None
storage_classes = lambda: []
capacity = None                      # Longhorn's per-node room, when there is Longhorn
OWN_NS = ""
IMAGE = "alpine:3.20"
OLD_COPY = NAMES.key("reclass-old")  # on a released original: namespace/claim it was
TEMP_SUFFIX = "-reclass"
# A copy that has not begun by then never will: the new volume could not be
# made or attached. Everything is put back rather than left stopped.
START_LIMIT = 600
GiB = 1024 ** 3
STEPS = [("stop", "Stop what uses it"), ("create", "Make the new volume"), ("copy", "Copy the data"),
         ("verify", "Check the copy"), ("swap", "Swap the new volume in"), ("start", "Start everything again")]
RSYNC = re.compile(r"([\d,]+)\s+(\d+)%\s+(\S+/s)")
DD = re.compile(r"(\d+) bytes .*? copied, [\d.]+ s, ([\d.,]+ \S+/s)")


def bind(_kget, _ksend, _ktext, _storage_classes, _capacity=None, own_ns=""):
    global kget, ksend, ktext, storage_classes, capacity, OWN_NS
    kget, ksend, ktext, storage_classes, capacity, OWN_NS = _kget, _ksend, _ktext, _storage_classes, _capacity, own_ns


def _get(path):
    try:
        return kget(path)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


def _items(path):
    try:
        return kget(path).get("items", [])
    except Exception:
        return []


def _bytes(quantity):
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(Ki|Mi|Gi|Ti|K|M|G|T)?", str(quantity or "").strip())
    if not match:
        return 0
    scale = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40, "K": 10**3, "M": 10**6, "G": 10**9, "T": 10**12}
    return int(float(match.group(1)) * scale.get(match.group(2) or "", 1))


def _gb(value):
    return round((value or 0) / GiB, 1)


# ---- who uses the claim ----------------------------------------------------------

def _claims_in(podspec):
    return {(v.get("persistentVolumeClaim") or {}).get("claimName") for v in (podspec or {}).get("volumes") or []}


def consumers(ns, claim):
    """Everything in the namespace that mounts the claim, and how it runs now."""
    out = []
    for kind, path, spec_of in (
            ("Deployment", f"/apis/apps/v1/namespaces/{ns}/deployments", lambda o: o["spec"]["template"]["spec"]),
            ("StatefulSet", f"/apis/apps/v1/namespaces/{ns}/statefulsets", lambda o: o["spec"]["template"]["spec"]),
            ("DaemonSet", f"/apis/apps/v1/namespaces/{ns}/daemonsets", lambda o: o["spec"]["template"]["spec"]),
            ("CronJob", f"/apis/batch/v1/namespaces/{ns}/cronjobs",
             lambda o: o["spec"]["jobTemplate"]["spec"]["template"]["spec"])):
        for obj in _items(path):
            try:
                if claim not in _claims_in(spec_of(obj)):
                    continue
            except KeyError:
                continue
            row = {"kind": kind, "name": obj["metadata"]["name"]}
            if kind in ("Deployment", "StatefulSet"):
                row["replicas"] = int(obj["spec"].get("replicas", 1) if obj["spec"].get("replicas") is not None else 1)
                row["running"] = int((obj.get("status") or {}).get("readyReplicas", 0) or 0) > 0
            elif kind == "CronJob":
                row["suspend"] = bool(obj["spec"].get("suspend"))
                row["running"] = False
            else:
                row["running"] = True
            out.append(row)
    for vm in _items(f"/apis/kubevirt.io/v1/namespaces/{ns}/virtualmachines"):
        tspec = ((vm.get("spec") or {}).get("template") or {}).get("spec") or {}
        vols = tspec.get("volumes") or []
        via = next(("pvc" if (v.get("persistentVolumeClaim") or {}).get("claimName") == claim else "dv"
                    for v in vols if (v.get("persistentVolumeClaim") or {}).get("claimName") == claim
                    or (v.get("dataVolume") or {}).get("name") == claim), None)
        if not via:
            continue
        spec = vm.get("spec") or {}
        strategy = spec.get("runStrategy") or ("Always" if spec.get("running") else "Halted")
        out.append({"kind": "VirtualMachine", "name": vm["metadata"]["name"], "run_strategy": strategy,
                    "running": (vm.get("status") or {}).get("printableStatus") == "Running", "via": via})
    return out


def _helper(pod):
    """Homestead's own file-browser pod: it holds the claim for up to half an
    hour after Volumes > Files was opened, and is Homestead's to close."""
    labels = (pod.get("metadata") or {}).get("labels") or {}
    return labels.get(NAMES.key("task")) == "files"


def _close_helpers(ns, claim):
    closed = []
    for pod in _using_pods(ns, claim, helpers=True):
        if not _helper(pod):
            continue
        try:
            ksend("DELETE", f"/api/v1/namespaces/{ns}/pods/{pod['metadata']['name']}?gracePeriodSeconds=0")
            closed.append(pod["metadata"]["name"])
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
    return closed


def _using_pods(ns, claim, helpers=True):
    return [p for p in _items(f"/api/v1/namespaces/{ns}/pods")
            if claim in _claims_in(p.get("spec")) and (p.get("status") or {}).get("phase") not in ("Succeeded", "Failed")
            and (helpers or not _helper(p))]


def _longhorn_used(ns, claim):
    for v in _items("/apis/longhorn.io/v1beta2/namespaces/longhorn-system/volumes"):
        k8s = (v.get("status") or {}).get("kubernetesStatus") or {}
        if k8s.get("pvcName") == claim and k8s.get("namespace") == ns:
            return int((v.get("status") or {}).get("actualSize") or 0)
    return None


# ---- the review ------------------------------------------------------------------

def plan(ns, claim, target):
    """Everything that would stop the move or surprise someone, and the room it takes."""
    pvc = _get(f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{claim}")
    if not pvc:
        raise ValueError(f"volume {claim} does not exist in {ns}")
    spec = pvc.get("spec") or {}
    current = spec.get("storageClassName") or ""
    classes = {row["name"]: row for row in storage_classes()}
    row = classes.get(target)
    if not row:
        raise ValueError(f"storage class {target} does not exist")
    if target == current:
        raise ValueError(f"{claim} is already on {target}")
    blockers, warnings = [], []
    if row.get("internal"):
        blockers.append(f"{target} is reserved by Harvester")
    if (pvc.get("status") or {}).get("phase") != "Bound" or not spec.get("volumeName"):
        blockers.append(f"{claim} is not bound to a volume yet, so there is nothing to copy")
    modes, mode = spec.get("accessModes") or ["ReadWriteOnce"], spec.get("volumeMode") or "Filesystem"
    size = _bytes(((pvc.get("status") or {}).get("capacity") or {}).get("storage")
                  or ((spec.get("resources") or {}).get("requests") or {}).get("storage"))
    if "ReadWriteMany" in modes and mode == "Filesystem" and row.get("migratable"):
        blockers.append(f"{target} makes live-migratable VM disks, which Longhorn cannot mount as a shared "
                        "folder; a ReadWriteMany volume needs a class without migratable=true")
    if mode == "Block" and row.get("provisioner") == "rancher.io/local-path":
        blockers.append(f"{target} (local-path) cannot hold a raw disk")
    used = consumers(ns, claim)
    for c in used:
        if c["kind"] == "DaemonSet":
            blockers.append(f"DaemonSet {c['name']} runs on every node and cannot be stopped for the copy")
        if c["kind"] == "Deployment" and ns == OWN_NS and c["name"] == NAMES.BRAND:
            blockers.append("this is Homestead's own data; move it from Settings › Redundancy instead")
    known = {(c["kind"], c["name"]) for c in used}
    if any(_helper(pod) for pod in _using_pods(ns, claim)):
        warnings.append("the file browser open on it (Volumes > Files) is closed")
    for pod in _using_pods(ns, claim, helpers=False):
        owners = pod["metadata"].get("ownerReferences") or []
        if not owners:
            blockers.append(f"pod {pod['metadata']['name']} uses it and belongs to nothing Homestead can stop")
        elif owners[0].get("kind") == "Job" and not any(k == "CronJob" for k, _ in known):
            blockers.append(f"job {owners[0].get('name')} is using it; let it finish first")
    for c in used:
        if c["kind"] == "StatefulSet" and re.fullmatch(rf".+-{re.escape(c['name'])}-\d+", claim):
            blockers.append(f"{claim} was made by StatefulSet {c['name']}'s template, which would make it again")
    annotations = (pvc.get("metadata") or {}).get("annotations") or {}
    if annotations.get("harvesterhci.io/imageId"):
        warnings.append(f"it was made from the image {annotations['harvesterhci.io/imageId']}; the copy is "
                        "a disk of its own, no longer tied to that image")
    if any(c.get("via") == "dv" for c in used):
        warnings.append("the VM's DataVolume is turned into a plain volume, as a moved VM's is")

    actual = _longhorn_used(ns, claim)
    replicas = int((row.get("replicas") or "1") or 1) if row.get("provisioner") == "driver.longhorn.io" else 1
    space = {"size_gb": _gb(size), "used_gb": _gb(actual) if actual is not None else None,
             "replicas": replicas, "allocated_gb": _gb(size * replicas),
             "written_gb": _gb((actual if actual is not None else size) * replicas),
             "longhorn": row.get("provisioner") == "driver.longhorn.io", "room_gb": None}
    if space["longhorn"] and capacity:
        try:
            cap = capacity()
            room = (cap.get("largest") or {}).get(str(min(replicas, 3)))
            space["room_gb"] = room
            if room is not None and room < space["size_gb"]:
                blockers.append(f"there is room for a {room} GB volume with {replicas} cop{'y' if replicas == 1 else 'ies'} "
                                f"on {target}, and this one is {space['size_gb']} GB; free some space or add a disk first")
        except Exception:
            pass
    elif not space["longhorn"]:
        warnings.append(f"{target} is not Longhorn, so Homestead cannot check it has room for {space['size_gb']} GB")
    moving = space["used_gb"] if space["used_gb"] is not None else space["size_gb"]
    return {"ok": not blockers, "blockers": blockers, "warnings": warnings, "namespace": ns, "claim": claim,
            "from_class": current, "to_class": target, "volume_mode": mode, "access_modes": modes,
            "consumers": used, "space": space,
            # Roughly: a LAN-speed disk copy, and as long again to check it.
            "minutes": max(1, round(moving * 1024 / 100 / 60 * 2)),
            "downtime": any(c.get("running") for c in used)}


def stopped_attempt(ns, claim, ops):
    """An earlier move of this claim that stopped part-way, which can carry on."""
    for other in ops.list_operations():
        res = other.get("resource") or {}
        if other.get("kind") == "reclass" and other.get("status") == "failed" and other.get("resumable")                 and res.get("name") == claim and res.get("namespace") == ns:
            return other
    return None


def start(ns, claim, target, ops):
    review = plan(ns, claim, target)
    if not review["ok"]:
        raise ValueError("; ".join(review["blockers"]))
    for other in ops.list_operations():
        ref_claim = (other.get("resource") or {}).get("name")
        if other.get("kind") == "reclass" and other.get("status") == "running" and ref_claim == claim \
                and (other.get("resource") or {}).get("namespace") == ns:
            raise ValueError(f"{claim} is already being moved")
    pvc = kget(f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{claim}")
    ref = {"namespace": ns, "claim": claim, "target": target, "phase": "stop",
           "consumers": review["consumers"], "mode": review["volume_mode"],
           "old_pv": pvc["spec"]["volumeName"], "from_class": review["from_class"],
           "size": str(((pvc.get("status") or {}).get("capacity") or {}).get("storage")
                       or pvc["spec"]["resources"]["requests"]["storage"]),
           "temp": (claim[:63 - len(TEMP_SUFFIX)].rstrip("-") + TEMP_SUFFIX),
           "job_name": f"{claim[:34].rstrip('-')}-reclass-{secrets.token_hex(3)}",
           "claim_spec": {"accessModes": pvc["spec"].get("accessModes") or ["ReadWriteOnce"],
                          "volumeMode": review["volume_mode"]},
           "labels": (pvc.get("metadata") or {}).get("labels") or {},
           "annotations": {k: v for k, v in ((pvc.get("metadata") or {}).get("annotations") or {}).items()
                           if not k.startswith(("pv.kubernetes.io/", "volume.beta.kubernetes.io/",
                                                "volume.kubernetes.io/", "kubectl.kubernetes.io/"))},
           "started": time.time()}
    return ops.start("reclass", f"Move {claim} to {target}",
                     {"kind": "PersistentVolumeClaim", "name": claim, "namespace": ns},
                     "/volumes?" + urllib.parse.urlencode({"q": claim}), ref,
                     f"Stopping what uses {claim}")


# ---- the steps -------------------------------------------------------------------

def _steps(item, phase, copy=None):
    order = [s for s, _ in STEPS]
    at = order.index(phase) if phase in order else len(order)
    item["steps"] = [{"id": s, "label": label, "state": "done" if i < at else "active" if i == at else "todo"}
                     for i, (s, label) in enumerate(STEPS)]
    if copy is not None:
        item["copy"] = copy


def _stop(ns, ref):
    _close_helpers(ns, ref["claim"])
    for c in ref["consumers"]:
        if c.get("stopped"):
            continue
        name = c["name"]
        if c["kind"] in ("Deployment", "StatefulSet"):
            plural = "deployments" if c["kind"] == "Deployment" else "statefulsets"
            ksend("PATCH", f"/apis/apps/v1/namespaces/{ns}/{plural}/{name}", {"spec": {"replicas": 0}},
                  ctype="application/merge-patch+json")
        elif c["kind"] == "CronJob":
            ksend("PATCH", f"/apis/batch/v1/namespaces/{ns}/cronjobs/{name}", {"spec": {"suspend": True}},
                  ctype="application/merge-patch+json")
        elif c["kind"] == "VirtualMachine":
            vm = kget(f"/apis/kubevirt.io/v1/namespaces/{ns}/virtualmachines/{name}")
            vm["spec"]["runStrategy"] = "Halted"
            vm["spec"].pop("running", None)
            annotations = vm["metadata"].setdefault("annotations", {})
            # Harvester makes a VM's claims from this list; one missing for a
            # moment during the swap must not be made again, empty.
            key = "harvesterhci.io/volumeClaimTemplates"
            if annotations.get(key):
                try:
                    templates = [t for t in json.loads(annotations[key])
                                 if (t.get("metadata") or {}).get("name") != ref["claim"]]
                    annotations[key] = json.dumps(templates)
                except ValueError:
                    pass
            if c.get("via") == "dv":
                # A DataVolume owns its claim and would make it again: the VM
                # mounts the claim itself from now on, as a moved VM does.
                for v in vm["spec"]["template"]["spec"].get("volumes") or []:
                    if (v.get("dataVolume") or {}).get("name") == ref["claim"]:
                        v.pop("dataVolume")
                        v["persistentVolumeClaim"] = {"claimName": ref["claim"]}
                vm["spec"]["dataVolumeTemplates"] = [t for t in vm["spec"].get("dataVolumeTemplates") or []
                                                     if (t.get("metadata") or {}).get("name") != ref["claim"]]
                if not vm["spec"]["dataVolumeTemplates"]:
                    vm["spec"].pop("dataVolumeTemplates")
            vm["metadata"].pop("managedFields", None)
            ksend("PUT", f"/apis/kubevirt.io/v1/namespaces/{ns}/virtualmachines/{name}", vm)
            if c.get("via") == "dv":
                pvc = _get(f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{ref['claim']}") or {}
                owners = [o for o in (pvc.get("metadata") or {}).get("ownerReferences") or [] if o.get("kind") != "DataVolume"]
                ksend("PATCH", f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{ref['claim']}",
                      {"metadata": {"ownerReferences": owners or None}}, ctype="application/merge-patch+json")
                try:
                    ksend("DELETE", f"/apis/cdi.kubevirt.io/v1beta1/namespaces/{ns}/datavolumes/{ref['claim']}",
                          {"kind": "DeleteOptions", "apiVersion": "v1", "propagationPolicy": "Orphan"})
                except urllib.error.HTTPError as error:
                    if error.code != 404:
                        raise
        c["stopped"] = True


def _start(ns, ref):
    for c in ref["consumers"]:
        name = c["name"]
        try:
            if c["kind"] in ("Deployment", "StatefulSet"):
                plural = "deployments" if c["kind"] == "Deployment" else "statefulsets"
                ksend("PATCH", f"/apis/apps/v1/namespaces/{ns}/{plural}/{name}", {"spec": {"replicas": c["replicas"]}},
                      ctype="application/merge-patch+json")
            elif c["kind"] == "CronJob":
                ksend("PATCH", f"/apis/batch/v1/namespaces/{ns}/cronjobs/{name}", {"spec": {"suspend": c.get("suspend", False)}},
                      ctype="application/merge-patch+json")
            elif c["kind"] == "VirtualMachine":
                ksend("PATCH", f"/apis/kubevirt.io/v1/namespaces/{ns}/virtualmachines/{name}",
                      {"spec": {"runStrategy": c["run_strategy"]}}, ctype="application/merge-patch+json")
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise


def _started(ns, ref):
    waiting = []
    for c in ref["consumers"]:
        if c["kind"] in ("Deployment", "StatefulSet") and c.get("replicas"):
            plural = "deployments" if c["kind"] == "Deployment" else "statefulsets"
            obj = _get(f"/apis/apps/v1/namespaces/{ns}/{plural}/{c['name']}") or {}
            if int((obj.get("status") or {}).get("readyReplicas", 0) or 0) < c["replicas"]:
                waiting.append(c["name"])
        elif c["kind"] == "VirtualMachine" and c.get("running"):
            vm = _get(f"/apis/kubevirt.io/v1/namespaces/{ns}/virtualmachines/{c['name']}") or {}
            if (vm.get("status") or {}).get("printableStatus") != "Running":
                waiting.append(c["name"])
    return waiting


def job_body(ns, ref):
    """The copy, then the check. Both claims are mounted with nothing else
    using them: the originals were stopped first."""
    block = ref["mode"] == "Block"
    if block:
        script = "\n".join([
            # pipefail: the progress filter must not hide a copy that failed.
            "set -e -o pipefail", "apk add --no-cache coreutils >/dev/null",
            "echo '==> copying'",
            # The new volume starts empty, so skipping blocks of zeros keeps
            # it as thin as the original.
            "dd if=/dev/src of=/dev/dst bs=4M conv=sparse,fsync status=progress 2>&1 | tr '\\r' '\\n'",
            "echo '==> verifying'",
            "cmp /dev/src /dev/dst && echo '==> verified'"])
        mounts = {"volumeDevices": [{"name": "src", "devicePath": "/dev/src"}, {"name": "dst", "devicePath": "/dev/dst"}]}
    else:
        script = "\n".join([
            "set -e -o pipefail", "apk add --no-cache rsync >/dev/null",
            "echo '==> copying'",
            "rsync -aHAX --numeric-ids --info=progress2 --no-inc-recursive /src/ /dst/ | tr '\\r' '\\n'",
            "echo '==> verifying'",
            # Every file compared by checksum; anything listed differs.
            "diff=$(rsync -aHAXn --checksum --numeric-ids --delete --out-format='%n' /src/ /dst/ | head -20)",
            'if [ -n "$diff" ]; then echo "==> differs: $diff"; exit 3; fi',
            "sync; echo '==> verified'"])
        mounts = {"volumeMounts": [{"name": "src", "mountPath": "/src", "readOnly": True},
                                   {"name": "dst", "mountPath": "/dst"}]}
    # Each attempt its own: an earlier attempt's finished job lingers for a
    # day, and sharing its name read that job's result as this one's.
    name = ref.get("job_name") or f"{ref['claim'][:40].rstrip('-')}-reclass-copy"
    return name, {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {"name": name, "namespace": ns, "labels": NAMES.labels("reclass", ref["claim"])},
        "spec": {"backoffLimit": 0, "ttlSecondsAfterFinished": 86400,
                 "template": {"metadata": {"labels": NAMES.labels("reclass", ref["claim"])},
                              "spec": {"restartPolicy": "Never",
                                       "containers": [{"name": "copy", "image": IMAGE, "command": ["sh", "-c", script],
                                                       "securityContext": {"runAsUser": 0}, **mounts}],
                                       "volumes": [{"name": "src", "persistentVolumeClaim": {"claimName": ref["claim"], "readOnly": not block}},
                                                   {"name": "dst", "persistentVolumeClaim": {"claimName": ref["temp"]}}]}}},
    }


def _log(ns, job):
    try:
        pods = _items(f"/api/v1/namespaces/{ns}/pods?labelSelector=" + urllib.parse.quote(f"job-name={job}", safe=""))
        if not pods:
            return ""
        return str(ktext(f"/api/v1/namespaces/{ns}/pods/{pods[0]['metadata']['name']}/log?tailLines=40") or "")
    except Exception:
        return ""


def copy_progress(log, size_bytes):
    """Where the copy is, from its own output: a percentage, the speed, and
    whether it has moved on to checking."""
    verifying = "==> verifying" in log
    pct, speed = 0, ""
    for line in log.splitlines():
        m = RSYNC.search(line)
        if m:
            pct, speed = int(m.group(2)), m.group(3)
        m = DD.search(line)
        if m and size_bytes:
            pct, speed = min(100, int(int(m.group(1)) * 100 / size_bytes)), m.group(2)
    return {"percent": 100 if verifying else pct, "speed": speed, "verifying": verifying,
            "verified": "==> verified" in log}


def _rollback(ns, ref, why):
    paths = [f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{ref['temp']}"]
    if ref.get("job"):
        paths.insert(0, f"/apis/batch/v1/namespaces/{ns}/jobs/{ref['job']}?propagationPolicy=Background")
    for path in paths:
        try:
            ksend("DELETE", path)
        except urllib.error.HTTPError:
            pass
    _start(ns, ref)
    ref["phase"] = "rolled-back"
    return "failed", 0, f"{why} Nothing changed: the new volume was removed and everything started again on the original."


# Kubernetes answering "not found" part-way: once is a race (a claim being
# replaced, a job cleaned up), so the step is tried again; this many in a row
# is not, and the job stops saying what was missing.
MISS_LIMIT = 12
BEFORE_SWAP = ("stop", "stopping", "create", "copy")


def _missing(error):
    path = urllib.parse.urlparse(getattr(error, "url", "") or getattr(error, "filename", "") or "").path
    parts = [part for part in path.split("/") if part]
    return " ".join(parts[-2:]) if len(parts) >= 2 else "something it needed"


def resolve(item):
    """One step, and a "not found" from Kubernetes taken in its stride.

    The operations poll marks a job failed on any 404 it sees, which left a
    move stopped half-way through its swap. Every step here is safe to run
    again, so a 404 is retried; only a persistent one stops the job - before
    the swap by putting everything back, after it by saying what is left.
    """
    ref = item["ref"]
    try:
        result = _resolve(item)
        ref.pop("misses", None)
        return result
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        what = _missing(error)
        ref["misses"] = int(ref.get("misses", 0) or 0) + 1
        if ref["misses"] < MISS_LIMIT:
            return "running", item.get("progress", 0), f"Kubernetes did not find {what}; trying again"
        ref.pop("misses", None)
        if ref.get("phase", "stop") in BEFORE_SWAP:
            return _rollback(ref["namespace"], ref, f"Kubernetes kept answering that {what} does not exist.")
        return "failed", item.get("progress", 0), (
            f"Stopped at '{ref.get('phase')}': Kubernetes kept answering that {what} does not exist. "
            "Nothing is lost - the original is kept as an old copy - and Carry on picks up from this step.")


def resumable(item):
    """Why a stopped move cannot carry on, or "" when it can."""
    phase = (item.get("ref") or {}).get("phase", "stop")
    if phase in ("rolled-back", "done"):
        return "it finished: everything was put back or completed"
    return ""


def _resolve(item):
    ref = item["ref"]
    ns, claim = ref["namespace"], ref["claim"]
    phase = ref.get("phase", "stop")
    size = _bytes(ref.get("size"))

    if phase == "stop":
        _steps(item, "stop")
        _stop(ns, ref)
        ref["phase"] = "stopping"
        return "running", 5, "Stopping " + ", ".join(c["name"] for c in ref["consumers"]) if ref["consumers"] else "Nothing uses it"
    if phase == "stopping":
        _steps(item, "stop")
        _close_helpers(ns, claim)
        left = _using_pods(ns, claim)
        if left:
            return "running", 8, f"Waiting for {len(left)} pod{'s' if len(left) != 1 else ''} to let go of {claim}"
        ref["phase"] = "create"
        return "running", 10, f"{claim} is free"
    if phase == "create":
        _steps(item, "create")
        body = {"apiVersion": "v1", "kind": "PersistentVolumeClaim",
                "metadata": {"name": ref["temp"], "namespace": ns, "labels": NAMES.labels("reclass", claim)},
                "spec": {**ref["claim_spec"], "storageClassName": ref["target"],
                         "resources": {"requests": {"storage": ref["size"]}}}}
        try:
            ksend("POST", f"/api/v1/namespaces/{ns}/persistentvolumeclaims", body)
        except urllib.error.HTTPError as error:
            if error.code != 409:
                return _rollback(ns, ref, f"The new volume could not be made (HTTP {error.code}).")
        name, job = job_body(ns, ref)
        try:
            ksend("POST", f"/apis/batch/v1/namespaces/{ns}/jobs", job)
        except urllib.error.HTTPError as error:
            if error.code != 409:
                return _rollback(ns, ref, f"The copy could not start (HTTP {error.code}).")
        ref.update(phase="copy", job=name, copy_since=time.time())
        return "running", 12, f"Making the new volume on {ref['target']}"
    if phase == "copy":
        job = _get(f"/apis/batch/v1/namespaces/{ns}/jobs/{ref['job']}")
        if not job:
            return _rollback(ns, ref, "The copy job went missing.")
        status = job.get("status") or {}
        log = _log(ns, ref["job"])
        progress = copy_progress(log, size)
        _steps(item, "verify" if progress["verifying"] else "copy", progress)
        if status.get("succeeded") and progress["verified"]:
            ref["phase"] = "swap"
            return "running", 80, "Copy checked: every byte matches"
        if status.get("failed"):
            why = next((line[4:] for line in reversed(log.splitlines()) if line.startswith("==> differs")), "")
            tail = " ".join(log.split()[-30:])
            return _rollback(ns, ref, (f"The copy did not match the original ({why})." if why
                                       else f"The copy failed. Last output: {tail[-240:]}"))
        if progress["verifying"]:
            return "running", 70, "Checking the copy against the original"
        # A job counts a pod that cannot start - its new volume never made -
        # as active, so what matters is whether the pod itself got going.
        pods = _items(f"/api/v1/namespaces/{ns}/pods?labelSelector="
                      + urllib.parse.quote(f"job-name={ref['job']}", safe=""))
        started = any((pod.get("status") or {}).get("phase") in ("Running", "Succeeded", "Failed") for pod in pods)
        if not started:
            waited = time.time() - ref.get("copy_since", time.time())
            if waited > START_LIMIT:
                tmp = _get(f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{ref['temp']}") or {}
                phase = (tmp.get("status") or {}).get("phase", "missing")
                return _rollback(ns, ref, f"The copy never started: after {int(waited // 60)} min {int(waited % 60)} s the new volume on "
                                 f"{ref['target']} is still {phase}.")
            return "running", 13, "Waiting for the new volume to attach"
        return "running", 15 + int(progress["percent"] * 0.5), (
            f"Copying {progress['percent']}%" + (f" at {progress['speed']}" if progress["speed"] else ""))
    if phase == "swap":
        _steps(item, "swap")
        return _swap(item, ns, ref)
    if phase == "start":
        _steps(item, "start")
        _start(ns, ref)
        ref.update(phase="starting", started_at=time.time())
        return "running", 92, "Starting everything again"
    if phase == "starting":
        _steps(item, "start")
        waiting = _started(ns, ref)
        if waiting and time.time() - ref.get("started_at", 0) < 600:
            return "running", 95, "Waiting for " + ", ".join(waiting) + " to be ready"
        _steps(item, "done")
        ref["phase"] = "done"
        item["old_pv"] = ref["old_pv"]
        tail = (f" {', '.join(waiting)} has not come up yet - see its page." if waiting else "")
        return ("succeeded", 100, f"{claim} is on {ref['target']} now. The old copy is kept as {ref['old_pv']} "
                "until you remove it from Volumes." + tail)
    return item.get("status", "succeeded"), item.get("progress", 100), item.get("message", "")


def _swap(item, ns, ref):
    """The temporary claim's volume takes the original's name. Each part is
    safe to run again, so a restart part-way carries on."""
    claim, temp = ref["claim"], ref["temp"]
    if not ref.get("new_pv"):
        tmp = _get(f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{temp}") or {}
        if not (tmp.get("spec") or {}).get("volumeName"):
            return "running", 82, "Waiting for the new volume to be bound"
        ref["new_pv"] = tmp["spec"]["volumeName"]
    old_pv, new_pv = ref["old_pv"], ref["new_pv"]
    # 1. Both volumes keep their data whatever happens to their claims.
    for pv in (old_pv, new_pv):
        ksend("PATCH", f"/api/v1/persistentvolumes/{pv}", {"spec": {"persistentVolumeReclaimPolicy": "Retain"}},
              ctype="application/merge-patch+json")
    ksend("PATCH", f"/api/v1/persistentvolumes/{old_pv}",
          {"metadata": {"annotations": {OLD_COPY: f"{ns}/{claim}"}}}, ctype="application/merge-patch+json")
    try:
        ksend("DELETE", f"/apis/batch/v1/namespaces/{ns}/jobs/{ref['job']}?propagationPolicy=Background")
    except urllib.error.HTTPError:
        pass
    # 2. The temporary claim goes, and its volume is freed to be claimed again.
    if _get(f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{temp}"):
        ksend("DELETE", f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{temp}")
        return "running", 84, "Releasing the new volume from its temporary name"
    pv = _get(f"/api/v1/persistentvolumes/{new_pv}") or {}
    ref_claim = (pv.get("spec") or {}).get("claimRef") or {}
    if ref_claim.get("name") == temp:
        ksend("PATCH", f"/api/v1/persistentvolumes/{new_pv}", {"spec": {"claimRef": None}},
              ctype="application/merge-patch+json")
    # 3. The original claim goes; its volume stays, released, as the old copy.
    current = _get(f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{claim}")
    if current and (current.get("spec") or {}).get("volumeName") == old_pv:
        if not (current.get("metadata") or {}).get("deletionTimestamp"):
            ksend("DELETE", f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{claim}")
            return "running", 86, f"Letting go of the original {claim}"
        return "running", 86, _held(ns, ref, current)
    # 4. A claim of the original name, bound to the new volume.
    if not current:
        body = {"apiVersion": "v1", "kind": "PersistentVolumeClaim",
                "metadata": {"name": claim, "namespace": ns, "labels": ref.get("labels") or {},
                             "annotations": ref.get("annotations") or {}},
                "spec": {**ref["claim_spec"], "storageClassName": ref["target"], "volumeName": new_pv,
                         "resources": {"requests": {"storage": ref["size"]}}}}
        ksend("POST", f"/api/v1/namespaces/{ns}/persistentvolumeclaims", body)
        return "running", 88, f"{claim} now points at the new volume"
    if (current.get("status") or {}).get("phase") != "Bound":
        return "running", 89, f"Waiting for {claim} to bind to the new volume"
    # 5. The new volume is deleted with its claim again, as the class says.
    policy = next((r.get("reclaim", "Delete") for r in storage_classes() if r["name"] == ref["target"]), "Delete")
    ksend("PATCH", f"/api/v1/persistentvolumes/{new_pv}", {"spec": {"persistentVolumeReclaimPolicy": policy}},
          ctype="application/merge-patch+json")
    ref["phase"] = "start"
    return "running", 90, f"{claim} is on {ref['target']}"


def _held(ns, ref, claim_obj):
    """Why a deleted claim is still there, and anything done about it.

    Kubernetes keeps a claim that any pod still mounts. What was stopped for
    the move can have been started again meanwhile - by hand, or by whatever
    manages it - so that is stopped again; anything else is named.
    """
    claim = ref["claim"]
    again = []
    for c in ref["consumers"]:
        if c["kind"] not in ("Deployment", "StatefulSet"):
            continue
        plural = "deployments" if c["kind"] == "Deployment" else "statefulsets"
        obj = _get(f"/apis/apps/v1/namespaces/{ns}/{plural}/{c['name']}")
        if obj and int((obj.get("spec") or {}).get("replicas") or 0) > 0:
            c["stopped"] = False
            again.append(c["name"])
    if again:
        _stop(ns, ref)
        return (f"{', '.join(again)} had started again and was holding the original {claim}; "
                "stopped it again")
    closed = _close_helpers(ns, claim)
    if closed:
        return f"Closed the file browser that was holding the original {claim}"
    pods = [p["metadata"]["name"] for p in _using_pods(ns, claim)]
    if pods:
        return (f"Waiting for {', '.join(pods[:3])}{' and more' if len(pods) > 3 else ''} to stop using "
                f"{claim}: Kubernetes keeps a claim while a pod mounts it. Stop "
                f"{'it' if len(pods) == 1 else 'them'} and this carries on")
    others = [f for f in (claim_obj.get("metadata") or {}).get("finalizers") or []
              if f != "kubernetes.io/pvc-protection"]
    if others:
        return f"Waiting for {', '.join(others)} to release the original {claim}"
    return f"Letting go of the original {claim}"


# ---- the old copies --------------------------------------------------------------

def old_copies():
    """Originals kept after a move, until someone removes them."""
    out = []
    for pv in _items("/api/v1/persistentvolumes"):
        meta = pv.get("metadata") or {}
        was = (meta.get("annotations") or {}).get(OLD_COPY)
        if not was or (pv.get("status") or {}).get("phase") != "Released":
            continue
        out.append({"pv": meta["name"], "was": was, "storage_class": (pv.get("spec") or {}).get("storageClassName", ""),
                    "size": ((pv.get("spec") or {}).get("capacity") or {}).get("storage", ""),
                    "since": meta.get("creationTimestamp", "")})
    return out


def remove_old_copy(pv):
    obj = _get(f"/api/v1/persistentvolumes/{pv}")
    if not obj or not ((obj.get("metadata") or {}).get("annotations") or {}).get(OLD_COPY):
        raise ValueError(f"{pv} is not an old copy Homestead kept")
    if (obj.get("status") or {}).get("phase") != "Released":
        raise ValueError(f"{pv} is in use again, so it is not removed")
    # Deleted the way its class deletes volumes: the provisioner removes the
    # data along with it.
    ksend("PATCH", f"/api/v1/persistentvolumes/{pv}", {"spec": {"persistentVolumeReclaimPolicy": "Delete"}},
          ctype="application/merge-patch+json")
    return {"ok": True, "detail": f"removing the old copy {pv}"}
