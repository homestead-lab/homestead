"""Virtual machines: what each one is doing, and what can be done to it.

A KubeVirt VirtualMachine says whether it should run through its run
strategy - Harvester writes spec.runStrategy (RerunOnFailure, Halted...), not
the older spec.running - and KubeVirt sums up what it is actually doing in
status.printableStatus: Running, Stopped, Starting, Paused, ErrorUnschedulable
and so on. Actions follow from that, so a stopped VM offers Start and a
running one Stop, Restart and Pause, and one stuck starting can be stopped.

Editing covers what the VM is made of: CPU, memory, run strategy,
description and host; its disks (boot order, bus, growing, detaching, adding a
disk or CD-ROM, and the source of a disk not yet made); its network
interfaces; and its cloud-init, inline or in Harvester's secret. KubeVirt
applies most of that at the next boot, so a save can restart it at once.
New disks are made the way the cluster makes them - Harvester's volume claim
templates, CDI DataVolumes, or a plain claim - as a new VM's are.
Deleting can take the VM's disks with it; on Harvester that is its own
harvesterhci.io/removedPVCs annotation, which its UI uses the same way.
"""
import base64
import json
import re
import time
import urllib.error
import urllib.parse

import homestead_hvimage as HVIMAGE
import homestead_vmusage as VMUSAGE

kget = ksend = None
events_for = lambda ns, name, uid="": []
API = "/apis/kubevirt.io/v1"
SUB = "/apis/subresources.kubevirt.io/v1"
RUN_STRATEGIES = ("RerunOnFailure", "Always", "Manual", "Halted")
DESCRIPTION = "field.cattle.io/description"
OS_LABEL = "harvesterhci.io/os"
CLAIM_TEMPLATES = "harvesterhci.io/volumeClaimTemplates"
HOST = "kubernetes.io/hostname"
BUSES = ("virtio", "sata", "scsi")
MODELS = ("virtio", "e1000", "e1000e", "rtl8139")
# Bound by the server: what the cluster is, and Harvester's images.
platform = lambda: {}
images = lambda: []


def bind(_kget, _ksend, _events_for):
    global kget, ksend, events_for
    kget, ksend, events_for = _kget, _ksend, _events_for


def _strategy(vm):
    spec = vm.get("spec") or {}
    if spec.get("runStrategy"):
        return spec["runStrategy"]
    return "Always" if spec.get("running") else "Halted"


def _cores(dom):
    cpu = dom.get("cpu") or {}
    return int(cpu.get("cores") or 1) * int(cpu.get("sockets") or 1) * int(cpu.get("threads") or 1)


def _memory(dom):
    res = dom.get("resources") or {}
    return str((dom.get("memory") or {}).get("guest") or (res.get("limits") or {}).get("memory")
               or (res.get("requests") or {}).get("memory") or "")


def _status(vm, vmi):
    if (vm.get("metadata") or {}).get("deletionTimestamp"):
        # Deleted in the foreground: it stays until its instance and the
        # disks it owns are gone, and says so rather than looking stuck.
        return "Deleting"
    printable = ((vm.get("status") or {}).get("printableStatus") or "")
    if printable:
        return printable
    phase = (vmi.get("status") or {}).get("phase", "")
    return "Running" if phase == "Running" else phase or "Stopped"


def actions_for(status, migratable=False):
    """What can sensibly be asked of a VM in this state."""
    if status == "Running":
        return ["console", "stop", "restart", "pause"] + (["migrate"] if migratable else [])
    if status == "Paused":
        return ["unpause", "stop", "force-stop"]
    if status in ("Stopped", "Halted", "Succeeded", "Failed"):
        return ["start"]
    if status in ("Stopping", "Terminating"):
        return ["force-stop"]
    if status == "Migrating":
        return ["console"]
    if status == "Deleting":
        return []
    # Starting, Provisioning, WaitingForVolumeBinding, ErrorUnschedulable,
    # ErrImagePull, CrashLoopBackOff, DataVolumeError...: stop the attempt.
    return ["stop", "force-stop"]


def _problem(vm, vmi):
    """What is wrong, if anything. Failure is True when something failed -
    a DataVolume CDI refused, say - while Ready, PodScheduled and
    Synchronized are False. A stopped VM's Ready says only that it has no
    instance, which is not a problem."""
    conditions = ((vm.get("status") or {}).get("conditions") or []) + ((vmi.get("status") or {}).get("conditions") or [])
    text = lambda c: " ".join(str(c.get("message") or c.get("reason") or "").split())[:300]
    for condition in conditions:
        if condition.get("type") == "Failure" and condition.get("status") == "True" and text(condition):
            message = text(condition)
            if "DataVolume" in message:
                message += " — the disk was never made, so the VM cannot start. Edit its disk source, or delete the VM."
            return message
    # A detailed placement or controller refusal is more useful than the
    # generic Ready=False condition KubeVirt also writes.  During every normal
    # boot that Ready condition briefly says "Guest VM is not reported as
    # running"; presenting it as a failure made a VM which was successfully
    # scheduling look broken.
    for condition_type in ("PodScheduled", "Synchronized", "Ready"):
        for condition in conditions:
            if condition.get("type") != condition_type or condition.get("status") != "False" \
                    or not condition.get("message"):
                continue
            message = text(condition)
            if "VMI does not exist" in message:
                continue
            status = _status(vm, vmi)
            if condition_type == "Ready" and status in ("Starting", "Provisioning", "WaitingForVolumeBinding") \
                    and "not reported as running" in message.lower():
                continue
            return message
    return ""


def _claims(ns):
    try:
        items = kget(f"/api/v1/namespaces/{ns}/persistentvolumeclaims").get("items", [])
    except Exception:
        return {}
    return {i["metadata"]["name"]: i for i in items}


def _datavolumes(ns=""):
    try:
        path = f"/apis/cdi.kubevirt.io/v1beta1{'/namespaces/' + ns if ns else ''}/datavolumes"
        return {(d["metadata"]["namespace"], d["metadata"]["name"]): d for d in kget(path).get("items", [])}
    except Exception:
        return {}


# How long a DataVolume may sit waiting before the page says it is stuck.
STUCK_AFTER = 180
WAITING_PHASES = {"Pending", "ImportScheduled", "CloneScheduled", "UploadScheduled", "WaitForFirstConsumer",
                  "PendingPopulation", "ImportInProgress", "CloneInProgress", "Unknown"}


def _age(stamp):
    try:
        import calendar
        return time.time() - calendar.timegm(time.strptime(str(stamp), "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        return 0


def _why_not_filling(ns, dv):
    """Why a DataVolume is not getting on: its importer pod (named for the
    DataVolume, or for its "prime" claim when CDI uses volume populators),
    the claims it waits on - its own, prime and scratch - and their warning
    events. CDI's own phase only says it has been scheduled."""
    name = dv["metadata"]["name"]
    reasons = []
    for c in (dv.get("status") or {}).get("conditions") or []:
        if c.get("status") == "False" and c.get("message") and c.get("type") in ("Bound", "Running"):
            reasons.append(c["message"])
    try:
        claim = kget(f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{urllib.parse.quote(name)}")
        uid = claim["metadata"].get("uid", "")
    except Exception:
        claim, uid = None, ""
    watched = [name, f"{name}-scratch"] + ([f"prime-{uid}", f"prime-{uid}-scratch"] if uid else [])
    try:
        pods = kget(f"/api/v1/namespaces/{ns}/pods").get("items", [])
    except Exception:
        pods = []
    for pod in pods:
        pname = pod["metadata"]["name"]
        if not pname.startswith("importer-") or not (name in pname or (uid and uid in pname)):
            continue
        watched.append(pname)
        for c in (pod.get("status") or {}).get("conditions") or []:
            if c.get("type") == "PodScheduled" and c.get("status") == "False" and c.get("message"):
                reasons.append(f"the importer cannot be placed: {c['message']}")
        for cs in (pod.get("status") or {}).get("containerStatuses") or []:
            waiting = (cs.get("state") or {}).get("waiting") or {}
            if waiting.get("reason") in ("ErrImagePull", "ImagePullBackOff", "CrashLoopBackOff", "CreateContainerError"):
                reasons.append(f"the importer {waiting['reason']}: {waiting.get('message', '')}".rstrip(": "))
    for other in watched:
        for event in events_for(ns, other, "")[:5]:
            if event.get("type") == "Warning" and event.get("message"):
                reasons.append(event["message"])
    seen, out = set(), []
    for reason in reasons:
        text = " ".join(str(reason).split())[:240]
        if text not in seen:
            seen.add(text)
            out.append(text)
    return out[:4]


def _image_filling(vm):
    """Disks waiting on a Harvester image still downloading: made from the
    VM's claim templates, which name the image they start from."""
    wanted = {}
    for t in _claim_templates(vm):
        ref = ((t.get("metadata") or {}).get("annotations") or {}).get("harvesterhci.io/imageId", "")
        if ref:
            wanted[ref] = (t.get("metadata") or {}).get("name", "")
    if not wanted:
        return []
    out = []
    for item in images():
        claim = wanted.get(f"{item.get('namespace')}/{item.get('name')}")
        if not claim or item.get("ready"):
            continue
        out.append({"claim": claim, "phase": "Failed" if item.get("failed") else "ImageDownloading",
                    "progress": float(item.get("progress") or 0), "seconds": 0,
                    "stuck": bool(item.get("failed")), "image": item.get("display", ""),
                    "why": [item["message"]] if item.get("message") and item.get("failed") else []})
    return out


def _stuck_problem(filling):
    stuck = next((f for f in filling if f["stuck"]), None)
    if not stuck:
        return ""
    return (f"disk {stuck['claim']} has been {stuck['phase']} for {stuck['seconds'] // 60} min"
            + (f": {stuck['why'][0]}" if stuck["why"] else ""))


def _filling(vm, dvs, explain=False):
    """Disks CDI is still filling - a download or a copy - with how far it got:
    a Provisioning VM is waiting on these, not stuck. One that has waited long
    says why, from the importer and the claims it waits on."""
    ns = (vm.get("metadata") or {}).get("namespace", "")
    out = []
    for v in ((vm.get("spec") or {}).get("template") or {}).get("spec", {}).get("volumes") or []:
        dv = dvs.get((ns, _volume_claim(v)))
        status = dict((dv or {}).get("status") or {})
        # A DataVolume CDI refused outright - ErrClaimNotValid, a class it
        # cannot read - never gets a phase at all; it is waiting, and why.
        if dv and not status.get("phase"):
            status["phase"] = "Pending"
        if dv and status.get("phase") != "Succeeded":
            progress = str(status.get("progress") or "").rstrip("%")
            try:
                percent = float(progress)
            except ValueError:
                percent = None
            since = _age(dv["metadata"].get("creationTimestamp"))
            row = {"claim": dv["metadata"]["name"], "phase": status["phase"], "progress": percent,
                   "seconds": int(since), "stuck": False, "why": []}
            waiting = status["phase"] in WAITING_PHASES and not percent
            if explain or (waiting and since > STUCK_AFTER) or status["phase"] == "Failed":
                row["why"] = _why_not_filling(ns, dv)
                row["stuck"] = bool(waiting and since > STUCK_AFTER) or status["phase"] == "Failed"
            out.append(row)
    return out


def _row(vm, vmi, claims=None, dvs=None):
    meta, spec = vm.get("metadata") or {}, vm.get("spec") or {}
    tspec = (spec.get("template") or {}).get("spec") or {}
    dom = tspec.get("domain") or {}
    istatus = vmi.get("status") or {}
    status = _status(vm, vmi)
    conditions = istatus.get("conditions") or []
    migratable = any(c.get("type") == "LiveMigratable" and c.get("status") == "True" for c in conditions)
    volumes = {v.get("name"): v for v in tspec.get("volumes") or []}
    disks = []
    for d in (dom.get("devices") or {}).get("disks") or []:
        v = volumes.get(d.get("name"), {})
        claim = (v.get("persistentVolumeClaim") or {}).get("claimName") or (v.get("dataVolume") or {}).get("name") or ""
        kind = ("cd-rom" if "cdrom" in d else "cloud-init" if "cloudInitNoCloud" in v or "cloudInitConfigDrive" in v
                else "container disk" if "containerDisk" in v else "disk")
        pvc = (claims or {}).get(claim) or {}
        disks.append({"name": d.get("name", ""), "kind": kind, "claim": claim, "boot": d.get("bootOrder"),
                      "bus": (d.get("disk") or d.get("cdrom") or {}).get("bus", ""),
                      "size": ((pvc.get("status") or {}).get("capacity") or {}).get("storage")
                              or (((pvc.get("spec") or {}).get("resources") or {}).get("requests") or {}).get("storage", ""),
                      "storage_class": (pvc.get("spec") or {}).get("storageClassName", "")})
    networks = {n.get("name"): n for n in tspec.get("networks") or []}
    live = {i.get("name"): i for i in istatus.get("interfaces") or []}
    nics = []
    for i in (dom.get("devices") or {}).get("interfaces") or []:
        net = networks.get(i.get("name"), {})
        state = live.get(i.get("name"), {})
        nics.append({"name": i.get("name", ""), "model": i.get("model", "virtio"),
                     "network": (net.get("multus") or {}).get("networkName") or ("pod network" if "pod" in net else ""),
                     "mac": state.get("mac") or i.get("macAddress", ""),
                     "ips": [a for a in (state.get("ipAddresses") or [state.get("ipAddress")]) if a]})
    guest = istatus.get("guestOSInfo") or {}
    labels, annotations = meta.get("labels") or {}, meta.get("annotations") or {}
    restart_required = any(c.get("type") == "RestartRequired" and c.get("status") == "True"
                           for c in (vm.get("status") or {}).get("conditions") or [])
    filling = _filling(vm, dvs or {})
    status_word = ((vm.get("status") or {}).get("printableStatus") or "")
    if status_word not in ("Running", "Paused", "Migrating"):
        filling += _image_filling(vm)
    return {"ns": meta.get("namespace", ""), "name": meta.get("name", ""), "status": status,
            "run_strategy": _strategy(vm), "running": istatus.get("phase") == "Running",
            "node": istatus.get("nodeName", ""), "cores": _cores(dom), "memory": _memory(dom),
            "ip": next((ip for n in nics for ip in n["ips"] if ":" not in ip), ""), "nics": nics, "disks": disks,
            # Every IPv4 address, first first, and the network the VM is on.
            "ips": list(dict.fromkeys(ip for n in nics for ip in n["ips"] if ":" not in ip)),
            "network": next((n["network"] for n in nics if n["network"]), ""),
            # A node of a k3s cluster made here, and which.
            "cluster": labels.get("homestead.io/k3s-cluster", ""), "cluster_role": labels.get("homestead.io/k3s-role", ""),
            "os": guest.get("prettyName") or labels.get(OS_LABEL, ""), "hostname": guest.get("hostname") or istatus.get("guestOSInfo", {}).get("name", ""),
            "description": annotations.get(DESCRIPTION, ""), "created": meta.get("creationTimestamp", ""),
            "uid": meta.get("uid", ""), "migratable": migratable, "restart_required": restart_required,
            "problem": _problem(vm, vmi) or _stuck_problem(filling),
            "filling": filling,
            "actions": actions_for(status, migratable)}


def list_vms():
    try:
        vms = kget(f"{API}/virtualmachines").get("items", [])
    except Exception:
        return []
    try:
        vmis = {(v["metadata"]["namespace"], v["metadata"]["name"]): v for v in kget(f"{API}/virtualmachineinstances").get("items", [])}
    except Exception:
        vmis = {}
    claims = {}
    for ns in {v["metadata"]["namespace"] for v in vms}:
        claims.update(_claims(ns))
    dvs = _datavolumes()
    rows = sorted((_row(v, vmis.get((v["metadata"]["namespace"], v["metadata"]["name"]), {}), claims, dvs) for v in vms),
                  key=lambda r: (r["ns"], r["name"]))
    try:
        measured, note = VMUSAGE.usage()
    except Exception:
        measured, note = {}, ""
    for row in rows:
        got = measured.get((row["ns"], row["name"])) if row["running"] else None
        if got:
            cpus, memory = max(1, int(row["cores"] or 1)), _bytes(row["memory"])
            row["usage"] = {**got,
                            "cpu_pct": round(100 * got["cpu"] / cpus, 1) if "cpu" in got else None,
                            "mem_pct": round(100 * got["mem"] / memory, 1) if "mem" in got and memory else None,
                            "io_note": note}
    return rows


def _name(value, label):
    value = str(value or "")
    if not re.fullmatch(r"[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?", value):
        raise ValueError(f"that is not a {label}")
    return value


def _get(ns, name):
    return kget(f"{API}/namespaces/{_name(ns, 'namespace')}/virtualmachines/{_name(name, 'VM name')}")


def detail(ns, name):
    vm = _get(ns, name)
    try:
        vmi = kget(f"{API}/namespaces/{ns}/virtualmachineinstances/{name}")
    except urllib.error.HTTPError:
        vmi = {}
    row = _row(vm, vmi, _claims(ns), _datavolumes(ns))
    # The VM's own page always explains a disk that is still filling.
    row["filling"] = _filling(vm, _datavolumes(ns), explain=True)
    guest = (vmi.get("status") or {}).get("guestOSInfo") or {}
    row["guest"] = {k: guest.get(k, "") for k in ("prettyName", "kernelRelease", "version", "id")}
    row["conditions"] = [{"type": c.get("type"), "status": c.get("status"), "reason": c.get("reason", ""),
                          "message": (c.get("message") or "")[:300]}
                         for c in ((vm.get("status") or {}).get("conditions") or []) + ((vmi.get("status") or {}).get("conditions") or [])]
    row["events"] = events_for(ns, name, "")[:30]
    claims = _claims(ns)
    for disk in row["disks"]:
        disk.update(_disk_source(vm, ns, disk["claim"], claims))
    row["cloud_init"] = _read_cloud_init(vm, ns)
    row["node_selector"] = ((vm["spec"]["template"].get("spec") or {}).get("nodeSelector") or {}).get(HOST, "")
    return row


# ---- what a disk is made from ---------------------------------------------

def _claim_templates(vm):
    try:
        return json.loads(((vm.get("metadata") or {}).get("annotations") or {}).get(CLAIM_TEMPLATES) or "[]")
    except ValueError:
        return []


def _set_claim_templates(vm, items):
    annotations = vm["metadata"].setdefault("annotations", {})
    if items:
        annotations[CLAIM_TEMPLATES] = json.dumps(items)
    else:
        annotations.pop(CLAIM_TEMPLATES, None)


def _dv_templates(vm):
    return vm["spec"].get("dataVolumeTemplates") or []


def _forget_templates(vm, claim):
    vm["spec"]["dataVolumeTemplates"] = [t for t in _dv_templates(vm) if (t.get("metadata") or {}).get("name") != claim]
    if not vm["spec"]["dataVolumeTemplates"]:
        vm["spec"].pop("dataVolumeTemplates")
    _set_claim_templates(vm, [t for t in _claim_templates(vm) if (t.get("metadata") or {}).get("name") != claim])


def _template_size(vm, claim):
    for t in _dv_templates(vm):
        if (t.get("metadata") or {}).get("name") == claim:
            box = t["spec"].get("storage") or t["spec"].get("pvc") or {}
            return ((box.get("resources") or {}).get("requests") or {}).get("storage", ""), box.get("storageClassName", "")
    for t in _claim_templates(vm):
        if (t.get("metadata") or {}).get("name") == claim:
            spec = t.get("spec") or {}
            return ((spec.get("resources") or {}).get("requests") or {}).get("storage", ""), spec.get("storageClassName", "")
    return "", ""


def _disk_source(vm, ns, claim, claims):
    """Where a disk comes from, and whether it has been made yet. A disk that
    was never made - CDI refused its source, say - can be given another."""
    if not claim:
        return {"made": True, "template": "", "source": {}}
    made = claim in claims
    for t in _dv_templates(vm):
        if (t.get("metadata") or {}).get("name") == claim:
            src = t["spec"].get("source") or {}
            phase = ""
            try:
                dv = kget(f"/apis/cdi.kubevirt.io/v1beta1/namespaces/{ns}/datavolumes/{urllib.parse.quote(claim)}")
                phase = (dv.get("status") or {}).get("phase", "")
            except Exception:
                pass
            source = ({"url": src["http"].get("url", "")} if "http" in src else {"blank": True} if "blank" in src
                      else {"other": next(iter(src), "")})
            size, klass = _template_size(vm, claim)
            return {"made": made and phase in ("Succeeded", ""), "template": "datavolume", "phase": phase,
                    "source": source, "template_size": size, "template_class": klass}
    for t in _claim_templates(vm):
        if (t.get("metadata") or {}).get("name") == claim:
            image = ((t.get("metadata") or {}).get("annotations") or {}).get("harvesterhci.io/imageId", "")
            size, klass = _template_size(vm, claim)
            return {"made": made, "template": "claim", "source": {"image": image} if image else {"blank": True},
                    "template_size": size, "template_class": klass}
    return {"made": made, "template": "", "source": {}}


def _size(value):
    value = str(value or "").strip()
    if re.fullmatch(r"\d+", value):
        value += "Gi"
    if not re.fullmatch(r"\d+(Mi|Gi|Ti)", value):
        raise ValueError("a disk size is like 20Gi")
    return value


def _bytes(quantity):
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(Ki|Mi|Gi|Ti|K|M|G|T)?", str(quantity or "").strip())
    if not match:
        return 0
    scale = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40, "K": 10**3, "M": 10**6, "G": 10**9, "T": 10**12}
    return float(match.group(1)) * scale.get(match.group(2) or "", 1)


def _image(ref):
    ns, _, name = str(ref).rpartition("/")
    for item in images():
        if item["name"] == name and (not ns or item.get("namespace") == ns):
            if not item.get("storage_class"):
                raise ValueError(f"image {item['display']} is not ready yet")
            return item
    raise ValueError(f"image {ref} was not found")


def _disk_volume(vm, ns, claim, size, klass, source, to_create):
    """A disk named claim, made the way this cluster makes disks; returns the
    VM volume that points at it. A plain claim is queued in to_create."""
    p = platform() or {}
    harvester, cdi = bool(p.get("harvester")), p.get("cdi", True)
    url, image = str(source.get("url") or "").strip(), str(source.get("image") or "").strip()
    if url:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError(f"{url} is not a download address; give the full http(s):// URL"
                             + (", or pick the Harvester image" if harvester else ""))
        if not cdi and not harvester:
            raise ValueError("CDI is not installed, so a disk cannot be downloaded; install it from kubevirt.io")
    if image and not harvester:
        raise ValueError("images from the image list are Harvester's; use an image URL on this cluster")
    _forget_templates(vm, claim)
    if harvester:
        template = {"metadata": {"name": claim, "annotations": {}},
                    "spec": {"accessModes": ["ReadWriteMany"], "volumeMode": "Block",
                             "resources": {"requests": {"storage": size}}}}
        if url:
            # Harvester's own download: one of its images, the disk a copy of it.
            downloaded = HVIMAGE.download(kget, ksend, (vm.get("metadata") or {}).get("namespace") or ns, url, klass)
            template["metadata"]["annotations"]["harvesterhci.io/imageId"] = f"{downloaded['namespace']}/{downloaded['name']}"
            template["spec"]["storageClassName"] = downloaded["storage_class"]
        elif image:
            item = _image(image)
            template["metadata"]["annotations"]["harvesterhci.io/imageId"] = f"{item['namespace']}/{item['name']}"
            template["spec"]["storageClassName"] = item["storage_class"]
            if item.get("size_gb") and _bytes(size) < item["size_gb"] * 2**30:
                raise ValueError(f"the disk must be at least as big as the image ({item['size_gb']} GB)")
        elif klass:
            template["spec"]["storageClassName"] = klass
        _set_claim_templates(vm, _claim_templates(vm) + [template])
        return {"persistentVolumeClaim": {"claimName": claim}}
    if cdi:
        storage = {"resources": {"requests": {"storage": size}}}
        if klass:
            storage["storageClassName"] = klass
        if harvester:
            storage.update(accessModes=["ReadWriteMany"], volumeMode="Block")
        vm["spec"]["dataVolumeTemplates"] = _dv_templates(vm) + [{
            "metadata": {"name": claim},
            "spec": {"source": {"http": {"url": url}} if url else {"blank": {}}, "storage": storage}}]
        return {"dataVolume": {"name": claim}}
    claim_obj = {"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": {"name": claim, "namespace": ns},
                 "spec": {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": size}}}}
    if klass:
        claim_obj["spec"]["storageClassName"] = klass
    to_create.append(claim_obj)
    return {"persistentVolumeClaim": {"claimName": claim}}


def _volume_claim(volume):
    return (volume.get("persistentVolumeClaim") or {}).get("claimName") or (volume.get("dataVolume") or {}).get("name") or ""


def _edit_disks(vm, ns, edits, adds, claims, to_create, resize, dropped):
    tspec = vm["spec"]["template"]["spec"]
    devices = tspec["domain"].setdefault("devices", {})
    disks, volumes = devices.get("disks") or [], tspec.get("volumes") or []
    changed = False
    for e in edits:
        d = next((x for x in disks if x.get("name") == e.get("name")), None)
        if not d:
            raise ValueError(f"there is no disk {e.get('name')}")
        v = next((x for x in volumes if x.get("name") == d["name"]), {})
        claim = _volume_claim(v)
        if e.get("remove"):
            disks.remove(d)
            if v in volumes:
                volumes.remove(v)
            if claim:
                _forget_templates(vm, claim)
                dropped.append(claim)
            changed = True
            continue
        kind = "cdrom" if "cdrom" in d else "disk" if "disk" in d else ""
        if "boot" in e:
            boot = int(e["boot"]) if str(e["boot"] if e["boot"] is not None else "").strip() else None
            if boot is not None and not 1 <= boot <= 64:
                raise ValueError("boot order is a number from 1")
            if d.get("bootOrder") != boot:
                if boot is None:
                    d.pop("bootOrder", None)
                else:
                    d["bootOrder"] = boot
                changed = True
        if e.get("bus") and kind:
            if e["bus"] not in BUSES or (kind == "cdrom" and e["bus"] == "virtio"):
                raise ValueError(f"{e['bus']} is not a bus for a {'CD-ROM' if kind == 'cdrom' else 'disk'}")
            if (d.get(kind) or {}).get("bus") != e["bus"]:
                d[kind] = dict(d.get(kind) or {}, bus=e["bus"])
                changed = True
        made = claim in claims
        if e.get("source") is not None and claim and not made:
            size, klass = _template_size(vm, claim)
            size = _size(e.get("size") or size or "20Gi")
            volume = _disk_volume(vm, ns, claim, size, e.get("storage_class") or klass, e["source"] or {}, to_create)
            v.clear()
            v.update({"name": d["name"], **volume})
            # A DataVolume CDI took but could not fill is made again from the new source.
            try:
                ksend("DELETE", f"/apis/cdi.kubevirt.io/v1beta1/namespaces/{ns}/datavolumes/{urllib.parse.quote(claim)}")
            except urllib.error.HTTPError:
                pass
            changed = True
        elif e.get("size") and claim:
            size = _size(e["size"])
            pvc = claims.get(claim)
            if pvc:
                current = (((pvc.get("status") or {}).get("capacity") or {}).get("storage")
                           or (((pvc.get("spec") or {}).get("resources") or {}).get("requests") or {}).get("storage", ""))
                if _bytes(size) < _bytes(current):
                    raise ValueError(f"{claim} is {current}; a disk can grow but not shrink")
                if _bytes(size) > _bytes(current):
                    resize.append((claim, size))
            else:
                for t in _dv_templates(vm):
                    if t["metadata"].get("name") == claim:
                        box = t["spec"].get("storage") or t["spec"].setdefault("pvc", {})
                        box.setdefault("resources", {}).setdefault("requests", {})["storage"] = size
                        changed = True
                items = _claim_templates(vm)
                for t in items:
                    if t["metadata"].get("name") == claim:
                        t["spec"].setdefault("resources", {}).setdefault("requests", {})["storage"] = size
                        changed = True
                _set_claim_templates(vm, items)
    taken = {d.get("name") for d in disks} | {v.get("name") for v in volumes}
    claim_names = set(claims) | {_volume_claim(v) for v in volumes}
    for a in adds:
        cdrom = a.get("kind") == "cd-rom"
        index = 0
        while f"{'cdrom' if cdrom else 'disk'}-{index}" in taken:
            index += 1
        disk_name = f"{'cdrom' if cdrom else 'disk'}-{index}"
        claim = f"{vm['metadata']['name']}-{disk_name}"
        while claim in claim_names:
            claim += "-x"
        source = {"url": a["url"]} if a.get("url") else {"image": a["image"]} if a.get("image") else {}
        if cdrom and not source:
            raise ValueError("a CD-ROM needs an image to hold")
        bus = a.get("bus") or ("sata" if cdrom else "virtio")
        if bus not in BUSES or (cdrom and bus == "virtio"):
            raise ValueError(f"{bus} is not a bus for a {'CD-ROM' if cdrom else 'disk'}")
        volume = _disk_volume(vm, ns, claim, _size(a.get("size") or "20Gi"), a.get("storage_class") or "", source, to_create)
        device = {"name": disk_name, ("cdrom" if cdrom else "disk"): {"bus": bus}}
        if a.get("boot"):
            device["bootOrder"] = int(a["boot"])
        disks.append(device)
        volumes.append({"name": disk_name, **volume})
        taken.add(disk_name)
        claim_names.add(claim)
        changed = True
    devices["disks"], tspec["volumes"] = disks, volumes
    return changed


def _set_network(iface, net, network):
    for binding in ("masquerade", "bridge"):
        iface.pop(binding, None)
    net.pop("pod", None)
    net.pop("multus", None)
    if network == "pod":
        net["pod"] = {}
        iface["masquerade"] = {}
    else:
        if not re.fullmatch(r"[a-z0-9-]+/[a-z0-9.-]+", network or ""):
            raise ValueError("a network is 'pod' or namespace/name of a network attachment")
        net["multus"] = {"networkName": network}
        iface["bridge"] = {}


def _edit_nics(tspec, edits, adds):
    devices = tspec["domain"].setdefault("devices", {})
    ifaces, nets = devices.get("interfaces") or [], tspec.get("networks") or []
    changed = False
    for e in edits:
        iface = next((x for x in ifaces if x.get("name") == e.get("name")), None)
        if not iface:
            raise ValueError(f"there is no network interface {e.get('name')}")
        net = next((x for x in nets if x.get("name") == iface["name"]), None)
        if net is None:
            net = {"name": iface["name"]}
            nets.append(net)
        if e.get("remove"):
            ifaces.remove(iface)
            nets.remove(net)
            changed = True
            continue
        if e.get("model") and e["model"] != iface.get("model", "virtio"):
            if e["model"] not in MODELS:
                raise ValueError(f"the model is one of {', '.join(MODELS)}")
            iface["model"] = e["model"]
            changed = True
        if "mac" in e:
            mac = str(e.get("mac") or "").strip().lower()
            if mac and not re.fullmatch(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", mac):
                raise ValueError(f"{mac} is not a MAC address")
            if mac != iface.get("macAddress", ""):
                if mac:
                    iface["macAddress"] = mac
                else:
                    iface.pop("macAddress", None)
                changed = True
        current = "pod" if "pod" in net else (net.get("multus") or {}).get("networkName", "")
        if e.get("network") and e["network"] != current:
            _set_network(iface, net, e["network"])
            changed = True
    for a in adds:
        index = 0
        names = {i.get("name") for i in ifaces}
        while f"nic-{index}" in names:
            index += 1
        iface, net = {"name": f"nic-{index}", "model": a.get("model") or "virtio"}, {"name": f"nic-{index}"}
        if iface["model"] not in MODELS:
            raise ValueError(f"the model is one of {', '.join(MODELS)}")
        _set_network(iface, net, a.get("network") or "pod")
        ifaces.append(iface)
        nets.append(net)
        changed = True
    if sum(1 for n in nets if "pod" in n) > 1:
        raise ValueError("a VM can be on the pod network once")
    devices["interfaces"], tspec["networks"] = ifaces, nets
    return changed


# ---- cloud-init -------------------------------------------------------------

def _cloud_volume(tspec):
    for v in tspec.get("volumes") or []:
        for key in ("cloudInitNoCloud", "cloudInitConfigDrive"):
            if key in v:
                return v, key
    return None, ""


def _secret_values(ns, name):
    data = (kget(f"/api/v1/namespaces/{ns}/secrets/{urllib.parse.quote(name)}").get("data") or {})
    return {k: base64.b64decode(v).decode("utf-8", "replace") for k, v in data.items()}


def _read_cloud_init(vm, ns):
    volume, key = _cloud_volume(vm["spec"]["template"].get("spec") or {})
    if not volume:
        return {"user_data": "", "network_data": "", "source": ""}
    c = volume[key]
    user, network, source = c.get("userData", ""), c.get("networkData", ""), "inline"
    try:
        if (c.get("secretRef") or c.get("userDataSecretRef") or {}).get("name"):
            values = _secret_values(ns, (c.get("secretRef") or c.get("userDataSecretRef"))["name"])
            user, source = values.get("userdata", values.get("userData", "")), "secret"
        if (c.get("networkDataSecretRef") or {}).get("name"):
            values = _secret_values(ns, c["networkDataSecretRef"]["name"])
            network = values.get("networkdata", values.get("networkData", ""))
    except Exception:
        source = "unreadable"
    return {"user_data": user, "network_data": network, "source": source}


def _write_secret(ns, name, key, text):
    ksend("PATCH", f"/api/v1/namespaces/{ns}/secrets/{urllib.parse.quote(name)}",
          {"data": {key: base64.b64encode(text.encode()).decode()}}, ctype="application/merge-patch+json")


def _edit_cloud_init(vm, ns, cfg):
    tspec = vm["spec"]["template"]["spec"]
    user, network = str(cfg.get("user_data") or ""), str(cfg.get("network_data") or "")
    volume, key = _cloud_volume(tspec)
    current = _read_cloud_init(vm, ns)
    if user == current["user_data"] and network == current["network_data"]:
        return False
    if not volume:
        tspec["domain"].setdefault("devices", {}).setdefault("disks", []).append(
            {"name": "cloudinitdisk", "disk": {"bus": "virtio"}})
        tspec.setdefault("volumes", []).append(
            {"name": "cloudinitdisk", "cloudInitNoCloud": dict({"userData": user}, **({"networkData": network} if network else {}))})
        return True
    c = volume[key]
    user_ref = (c.get("secretRef") or c.get("userDataSecretRef") or {}).get("name")
    if user_ref:
        # Harvester keeps cloud-init in a secret of the VM's; that is what changes.
        if user != current["user_data"]:
            _write_secret(ns, user_ref, "userdata", user)
    else:
        c["userData"] = user
    net_ref = (c.get("networkDataSecretRef") or {}).get("name")
    if net_ref:
        if network != current["network_data"]:
            _write_secret(ns, net_ref, "networkdata", network)
    elif network:
        c["networkData"] = network
    else:
        c.pop("networkData", None)
    return True


def _refusal(error):
    try:
        return json.loads(error.read().decode("utf-8", "replace")).get("message") or f"HTTP {error.code}"
    except Exception:
        return f"HTTP {error.code}"


def _set_strategy(ns, name, strategy):
    vm = _get(ns, name)
    vm["spec"]["runStrategy"] = strategy
    vm["spec"].pop("running", None)
    ksend("PUT", f"{API}/namespaces/{ns}/virtualmachines/{name}", vm)


def power(ns, name, action):
    """start, stop, force-stop, restart, pause, unpause."""
    _name(ns, "namespace"), _name(name, "VM name")
    try:
        if action in ("pause", "unpause"):
            ksend("PUT", f"{SUB}/namespaces/{ns}/virtualmachineinstances/{name}/{action}", {})
        elif action == "force-stop":
            ksend("PUT", f"{SUB}/namespaces/{ns}/virtualmachines/{name}/stop", {"gracePeriod": 0})
        elif action in ("start", "stop", "restart"):
            try:
                ksend("PUT", f"{SUB}/namespaces/{ns}/virtualmachines/{name}/{action}", {})
            except urllib.error.HTTPError as error:
                # Some run strategies refuse start or stop requests ("Always does not
                # support manual start requests"); the strategy itself then says it.
                if error.code in (400, 409) and action in ("start", "stop"):
                    _set_strategy(ns, name, "RerunOnFailure" if action == "start" else "Halted")
                else:
                    raise
        else:
            raise ValueError("the action is start, stop, force-stop, restart, pause or unpause")
    except urllib.error.HTTPError as error:
        raise ValueError(f"KubeVirt refused to {action} {name}: {_refusal(error)}")
    words = {"start": "starting", "stop": "stopping", "force-stop": "being stopped at once", "restart": "restarting",
             "pause": "paused", "unpause": "resumed"}
    return {"ok": True, "detail": f"{name} is {words[action]}"}


def edit(ns, name, cfg):
    """Everything the VM is made of. Changes to its hardware, disks, network,
    host or cloud-init take effect at the next boot, so restart says whether
    to restart now."""
    vm = _get(ns, name)
    spec = vm["spec"]
    tspec = spec["template"]["spec"]
    dom = tspec["domain"]
    changed_hardware = False
    to_create, resize, dropped = [], [], []
    if "node" in cfg:
        selector = dict(tspec.get("nodeSelector") or {})
        node = str(cfg.get("node") or "")
        if node != selector.get(HOST, ""):
            if node:
                selector[HOST] = _name(node, "node name")
            else:
                selector.pop(HOST, None)
            if selector:
                tspec["nodeSelector"] = selector
            else:
                tspec.pop("nodeSelector", None)
            changed_hardware = True
    if cfg.get("disks") or cfg.get("add_disks"):
        changed_hardware |= _edit_disks(vm, ns, cfg.get("disks") or [], cfg.get("add_disks") or [],
                                        _claims(ns), to_create, resize, dropped)
    if cfg.get("nics") or cfg.get("add_nics"):
        changed_hardware |= _edit_nics(tspec, cfg.get("nics") or [], cfg.get("add_nics") or [])
    if cfg.get("cloud_init") is not None:
        changed_hardware |= _edit_cloud_init(vm, ns, cfg["cloud_init"])
    if "cores" in cfg:
        cores = int(cfg["cores"])
        if not 1 <= cores <= 128:
            raise ValueError("between 1 and 128 cores")
        cpu = dom.setdefault("cpu", {})
        per = int(cpu.get("sockets") or 1) * int(cpu.get("threads") or 1)
        if cores % per:
            raise ValueError(f"this VM has {cpu.get('sockets', 1)} socket(s) of {cpu.get('threads', 1)} thread(s); cores must be a multiple of {per}")
        if cpu.get("cores") != cores // per:
            cpu["cores"] = cores // per
            changed_hardware = True
        limits = (dom.get("resources") or {}).get("limits")
        if limits and "cpu" in limits:
            limits["cpu"] = str(cores)
    if "memory" in cfg:
        memory = str(cfg["memory"]).strip()
        if not re.fullmatch(r"\d+(\.\d+)?(Mi|Gi)", memory):
            raise ValueError("memory is like 4Gi or 512Mi")
        if _memory(dom) != memory:
            changed_hardware = True
        dom.setdefault("memory", {})["guest"] = memory
        resources = dom.setdefault("resources", {})
        if (resources.get("limits") or {}).get("memory") is not None:
            resources["limits"]["memory"] = memory
        elif (resources.get("requests") or {}).get("memory") is not None or not resources:
            resources.setdefault("requests", {})["memory"] = memory
    if cfg.get("run_strategy"):
        if cfg["run_strategy"] not in RUN_STRATEGIES:
            raise ValueError(f"the run strategy is one of {', '.join(RUN_STRATEGIES)}")
        spec["runStrategy"] = cfg["run_strategy"]
        spec.pop("running", None)
    if "description" in cfg:
        annotations = vm["metadata"].setdefault("annotations", {})
        text = " ".join(str(cfg.get("description") or "").split())[:300]
        if text:
            annotations[DESCRIPTION] = text
        else:
            annotations.pop(DESCRIPTION, None)
    vm["metadata"].pop("managedFields", None)
    for claim in to_create:
        try:
            ksend("POST", f"/api/v1/namespaces/{ns}/persistentvolumeclaims", claim)
        except urllib.error.HTTPError as error:
            raise ValueError(f"the disk {claim['metadata']['name']} could not be made: {_refusal(error)}")
    try:
        ksend("PUT", f"{API}/namespaces/{ns}/virtualmachines/{name}", vm)
    except urllib.error.HTTPError as error:
        raise ValueError(f"the VM was not saved: {_refusal(error)}")
    grown = []
    for claim, size in resize:
        try:
            ksend("PATCH", f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{urllib.parse.quote(claim)}",
                  {"spec": {"resources": {"requests": {"storage": size}}}}, ctype="application/merge-patch+json")
            grown.append(f"{claim} to {size}")
        except urllib.error.HTTPError as error:
            raise ValueError(f"the VM was saved, but {claim} could not grow: {_refusal(error)}")
    restarted = False
    if changed_hardware and cfg.get("restart"):
        try:
            ksend("PUT", f"{SUB}/namespaces/{ns}/virtualmachines/{name}/restart", {})
            restarted = True
        except urllib.error.HTTPError:
            pass
    detail = f"{name} saved"
    if grown:
        detail += "; growing " + ", ".join(grown)
    if dropped:
        detail += f"; {', '.join(dropped)} detached and kept"
    if changed_hardware:
        detail += "; restarting now to use the changes" if restarted else "; the changes apply when it next starts"
    return {"ok": True, "detail": detail, "restart_needed": changed_hardware and not restarted}


def _release(ns, claim, uid):
    """A disk made from the VM's dataVolumeTemplates belongs to the VM, and
    Kubernetes deletes what a deleted owner owned. A disk that is being kept
    is let go of first: its DataVolume and claim stop naming the VM."""
    for path in (f"/apis/cdi.kubevirt.io/v1beta1/namespaces/{ns}/datavolumes/{urllib.parse.quote(claim)}",
                 f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{urllib.parse.quote(claim)}"):
        try:
            owners = (kget(path).get("metadata") or {}).get("ownerReferences") or []
        except Exception:
            continue
        kept = [o for o in owners if o.get("uid") != uid]
        if len(kept) != len(owners):
            try:
                ksend("PATCH", path, {"metadata": {"ownerReferences": kept or None}},
                      ctype="application/merge-patch+json")
            except urllib.error.HTTPError as error:
                raise ValueError(f"{claim} could not be kept, so nothing was deleted: {_refusal(error)}")


def delete(ns, name, with_disks=False):
    """Deletes the VM; with its disks too when asked. The disks are named on
    the VM first, the way Harvester's own UI asks its controller to remove
    them, and deleted here as well for clusters without that controller.
    Disks that are kept are released from the VM first, or they would go
    with it - but not one CDI is still filling: half a download is no disk,
    and released it would carry on downloading with no VM to use it. That
    stays the VM's and goes with it, which stops the import.

    The delete runs in the foreground, so the VM is listed as Deleting until
    its instance and the disks it owns are gone."""
    vm = _get(ns, name)
    every = [d["claim"] for d in _row(vm, {})["disks"] if d["claim"] and d["kind"] in ("disk", "cd-rom")]
    dvs = _datavolumes(ns)
    unfinished = [c for c in every if ((dvs.get((ns, c)) or {}).get("status") or {}).get("phase") not in (None, "", "Succeeded")
                  and (ns, c) in dvs]
    claims = every if with_disks else unfinished
    uid = vm["metadata"].get("uid", "")
    for claim in ([] if with_disks else [c for c in every if c not in unfinished]):
        if uid:
            _release(ns, claim, uid)
    if claims:
        vm["metadata"].setdefault("annotations", {})["harvesterhci.io/removedPVCs"] = ",".join(claims)
        vm["metadata"].pop("managedFields", None)
        try:
            ksend("PUT", f"{API}/namespaces/{ns}/virtualmachines/{name}", vm)
        except urllib.error.HTTPError:
            pass
    try:
        ksend("DELETE", f"{API}/namespaces/{ns}/virtualmachines/{name}",
              {"kind": "DeleteOptions", "apiVersion": "v1", "propagationPolicy": "Foreground"})
    except urllib.error.HTTPError as error:
        raise ValueError(f"KubeVirt refused to delete {name}: {_refusal(error)}")
    removed = []
    for claim in claims:
        # The DataVolume first: deleting it stops CDI's importer; the claim
        # alone would be made again by a DataVolume still wanting it.
        paths = ([f"/apis/cdi.kubevirt.io/v1beta1/namespaces/{ns}/datavolumes/{urllib.parse.quote(claim)}"]
                 if (ns, claim) in dvs else [])
        paths.append(f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{urllib.parse.quote(claim)}")
        gone = False
        for path in paths:
            try:
                ksend("DELETE", path)
                gone = True
            except urllib.error.HTTPError as error:
                gone = gone or error.code == 404
        if gone:
            removed.append(claim)
    detail = f"{name} is being deleted"
    if with_disks:
        detail += f" with {len(removed)} disk{'s' if len(removed) != 1 else ''}"
    else:
        kept = [c for c in every if c not in unfinished]
        if unfinished:
            detail += f"; the unfinished download{'s' if len(unfinished) != 1 else ''} ({', '.join(unfinished)}) stopped and removed"
        if kept:
            detail += f"; {', '.join(kept)} kept"
    return {"ok": True, "detail": detail}
