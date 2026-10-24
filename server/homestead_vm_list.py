"""
Listing virtual machines: one row per VM, with its status, its disks and
whether a disk is still being filled (and why not, when it is stuck).

Split by feature from homestead_vms (#623), moved unchanged. Names that stay
there, that server.py binds there, or that tests replace there, are read
through it (V.name); homestead_vms re-exports everything here.
"""
import concurrent.futures
import re
import time
import urllib.error
import urllib.parse
import homestead_logos as LOGOS
import homestead_names as NAMES
import homestead_schedules as SCHEDULES
import homestead_outage as OUTAGE
import homestead_vmusage as VMUSAGE
import homestead_vm_network as VMNETWORK


def _claims(ns, strict=False):
    try:
        result = V.kget(f"/api/v1/namespaces/{ns}/persistentvolumeclaims")
        if strict and (not isinstance(result.get("items"), list) or (result.get("metadata") or {}).get("continue")):
            raise ValueError("PVC inventory is incomplete; refresh before editing VM disks")
        items = result.get("items", [])
    except Exception:
        if strict:
            raise
        return {}
    return {i["metadata"]["name"]: i for i in items}


def _datavolumes(ns=""):
    try:
        path = f"/apis/cdi.kubevirt.io/v1beta1{'/namespaces/' + ns if ns else ''}/datavolumes"
        return {(d["metadata"]["namespace"], d["metadata"]["name"]): d for d in V.kget(path).get("items", [])}
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
        claim = V.kget(f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{urllib.parse.quote(name)}")
        uid = claim["metadata"].get("uid", "")
    except Exception:
        claim, uid = None, ""
    watched = [name, f"{name}-scratch"] + ([f"prime-{uid}", f"prime-{uid}-scratch"] if uid else [])
    try:
        pods = V.kget(f"/api/v1/namespaces/{ns}/pods").get("items", [])
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
        for event in V.events_for(ns, other, "")[:5]:
            if event.get("type") == "Warning" and event.get("message"):
                reasons.append(event["message"])
    # Combined events can put the real failure beyond the display's text limit.
    # Diagnose the complete message before shortening generic event output.
    if any(re.search(r"(?:cannot open|open).*?/dev/cdi-block-volume[^\n]*permission denied",
                     str(reason), re.IGNORECASE) for reason in reasons):
        reasons.insert(0, "CDI cannot access its block device. On k3s/RKE2, enable nonroot-devices on every node, "
                          "restart the node service, then recreate the failed importer pod. Keep the disk PVC and DataVolume.")
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
    for t in V._claim_templates(vm):
        ref = ((t.get("metadata") or {}).get("annotations") or {}).get("harvesterhci.io/imageId", "")
        if ref:
            wanted[ref] = (t.get("metadata") or {}).get("name", "")
    if not wanted:
        return []
    out = []
    for item in V.images():
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
        dv = dvs.get((ns, V._volume_claim(v)))
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


def _display(devices):
    """virtual: the console's screen shows it. gpu: no virtual display, and a
    GPU passed through - its picture is on the GPU's own outputs. none:
    neither, so only the serial console reaches it."""
    if devices.get("autoattachGraphicsDevice") is not False:
        return "virtual"
    return "gpu" if devices.get("gpus") or devices.get("hostDevices") else "none"


def _row(vm, vmi, claims=None, dvs=None, instance_known=True):
    meta, spec = vm.get("metadata") or {}, vm.get("spec") or {}
    tspec = (spec.get("template") or {}).get("spec") or {}
    dom = tspec.get("domain") or {}
    istatus = vmi.get("status") or {}
    status = V._status(vm, vmi, instance_known)
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
    filling = V._filling(vm, dvs or {})
    status_word = ((vm.get("status") or {}).get("printableStatus") or "")
    if status_word not in ("Running", "Paused", "Migrating"):
        filling += _image_filling(vm)
    return {"ns": meta.get("namespace", ""), "name": meta.get("name", ""), "status": status,
            "run_strategy": V._strategy(vm), "running": istatus.get("phase") == "Running",
            "stop_retries": V._crash_retries_without_guest(vm, vmi, instance_known),
            "node": istatus.get("nodeName", ""), "cores": V._cores(dom), "memory": V._memory(dom),
            "ip": next((ip for n in nics for ip in n["ips"] if ":" not in ip), ""), "nics": nics, "disks": disks,
            "isolated": VMNETWORK.isolated(vm),
            "implicit_network": not nics and not networks and (dom.get("devices") or {}).get("autoattachPodInterface") is not False,
            "display": _display(dom.get("devices") or {}),
            # Every IPv4 address, first first, and the network the VM is on.
            "ips": list(dict.fromkeys(ip for n in nics for ip in n["ips"] if ":" not in ip)),
            "network": next((n["network"] for n in nics if n["network"]), ""),
            # A node of a k3s cluster made here, and which.
            "cluster": labels.get("homestead.io/k3s-cluster", ""), "cluster_role": labels.get("homestead.io/k3s-role", ""),
            "os": guest.get("prettyName") or labels.get(V.OS_LABEL, ""), "hostname": guest.get("hostname") or istatus.get("guestOSInfo", {}).get("name", ""),
            # Its logo: one set for it, else its OS's, from what the guest
            # agent says, Harvester's label, or the OS chosen for it.
            "icon": V.display_icon(annotations),
            "has_logo": bool(NAMES.read(annotations, "icon")),
            "logo_os_set": bool(NAMES.read(annotations, "logo-os")),
            # How it is monitored (homestead_uptime): blank is automatic.
            "monitoring": NAMES.read(annotations, "uptime"),
            "schedule": SCHEDULES.read(annotations, NAMES),
            "outage_actions": OUTAGE.read(annotations, NAMES),
            "os_logo": (NAMES.read(annotations, "logo-os") if NAMES.read(annotations, "logo-os") in LOGOS.OS_KEYS else "")
                       or LOGOS.os_logo(guest.get("id"), guest.get("prettyName"), guest.get("name"), labels.get(V.OS_LABEL)),
            "description": annotations.get(V.DESCRIPTION, ""), "created": meta.get("creationTimestamp", ""),
            "uid": meta.get("uid", ""), "migratable": migratable, "restart_required": restart_required,
            "problem": V._problem(vm, vmi, instance_known) or _stuck_problem(filling),
            "filling": filling,
            "actions": V.actions_for(status, migratable)}


def list_vms():
    # A list that could not be read raises (#620): taken as "no VMs", it let the
    # power schedules forget what they had done and the pages say there were none.
    # Only a cluster without KubeVirt has none.
    vms = V.KUBE.items(f"{V.API}/virtualmachines", optional_group="kubevirt.io", read=V.kget)
    instance_known = False
    try:
        inventory = V.kget(f"{V.API}/virtualmachineinstances")
        vmis = {(v["metadata"]["namespace"], v["metadata"]["name"]): v for v in inventory["items"]}
        instance_known = isinstance(inventory["items"], list) and not (inventory.get("metadata") or {}).get("continue")
    except Exception:
        vmis = {}
    # An unreadable or partial inventory cannot prove that an instance is gone.
    # Each namespace's claims and the DataVolumes do not depend on each other:
    # read at the same time (#705). Each read keeps its own way of failing.
    namespaces = sorted({v["metadata"]["namespace"] for v in vms})
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(namespaces) + 1)) as pool:
        dv_read = pool.submit(V._datavolumes)
        claims = {}
        for found in pool.map(V._claims, namespaces):
            claims.update(found)
        dvs = dv_read.result()
    rows = sorted((V._row(v, vmis.get((v["metadata"]["namespace"], v["metadata"]["name"]), {}), claims, dvs, instance_known) for v in vms),
                  key=lambda r: (r["ns"], r["name"]))
    try:
        measured, note = VMUSAGE.usage()
    except Exception:
        measured, note = {}, ""
    for row in rows:
        got = measured.get((row["ns"], row["name"])) if row["running"] else None
        if got:
            cpus, memory = max(1, int(row["cores"] or 1)), V._bytes(row["memory"])
            row["usage"] = {**got,
                            "cpu_pct": round(100 * got["cpu"] / cpus, 1) if "cpu" in got else None,
                            "mem_pct": round(100 * got["mem"] / memory, 1) if "mem" in got and memory else None,
                            "io_note": note}
    return rows


# homestead_vms holds the bound state and re-exports this module, so it is imported last.
import homestead_vms as V  # noqa: E402
