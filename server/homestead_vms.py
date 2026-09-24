"""Virtual machines: what each one is doing, and what can be done to it.

A KubeVirt VirtualMachine says whether it should run through its run
strategy - Harvester writes spec.runStrategy (RerunOnFailure, Halted...), not
the older spec.running - and KubeVirt sums up what it is actually doing in
status.printableStatus: Running, Stopped, Starting, Paused, ErrorUnschedulable
and so on. Actions follow from that, so a stopped VM offers Start and a
running one Stop, Restart and Pause, and one stuck starting can be stopped.

Editing changes the VM's CPU, memory, run strategy and description. KubeVirt
applies CPU and memory at the next boot, so a save can restart it at once.
Deleting can take the VM's disks with it; on Harvester that is its own
harvesterhci.io/removedPVCs annotation, which its UI uses the same way.
"""
import json
import re
import urllib.error
import urllib.parse

kget = ksend = None
events_for = lambda ns, name, uid="": []
API = "/apis/kubevirt.io/v1"
SUB = "/apis/subresources.kubevirt.io/v1"
RUN_STRATEGIES = ("RerunOnFailure", "Always", "Manual", "Halted")
DESCRIPTION = "field.cattle.io/description"
OS_LABEL = "harvesterhci.io/os"


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
    # Starting, Provisioning, WaitingForVolumeBinding, ErrorUnschedulable,
    # ErrImagePull, CrashLoopBackOff, DataVolumeError...: stop the attempt.
    return ["stop", "force-stop"]


def _problem(vm, vmi):
    for condition in ((vm.get("status") or {}).get("conditions") or []) + ((vmi.get("status") or {}).get("conditions") or []):
        if condition.get("type") in ("Failure", "Ready", "PodScheduled", "Synchronized") and condition.get("status") == "False" \
                and condition.get("message"):
            return " ".join(str(condition["message"]).split())[:300]
    return ""


def _claims(ns):
    try:
        items = kget(f"/api/v1/namespaces/{ns}/persistentvolumeclaims").get("items", [])
    except Exception:
        return {}
    return {i["metadata"]["name"]: i for i in items}


def _row(vm, vmi, claims=None):
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
    return {"ns": meta.get("namespace", ""), "name": meta.get("name", ""), "status": status,
            "run_strategy": _strategy(vm), "running": istatus.get("phase") == "Running",
            "node": istatus.get("nodeName", ""), "cores": _cores(dom), "memory": _memory(dom),
            "ip": next((ip for n in nics for ip in n["ips"] if ":" not in ip), ""), "nics": nics, "disks": disks,
            "os": guest.get("prettyName") or labels.get(OS_LABEL, ""), "hostname": guest.get("hostname") or istatus.get("guestOSInfo", {}).get("name", ""),
            "description": annotations.get(DESCRIPTION, ""), "created": meta.get("creationTimestamp", ""),
            "uid": meta.get("uid", ""), "migratable": migratable, "restart_required": restart_required,
            "problem": _problem(vm, vmi) if status not in ("Running", "Stopped", "Paused") else "",
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
    return sorted((_row(v, vmis.get((v["metadata"]["namespace"], v["metadata"]["name"]), {}), claims) for v in vms),
                  key=lambda r: (r["ns"], r["name"]))


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
    row = _row(vm, vmi, _claims(ns))
    guest = (vmi.get("status") or {}).get("guestOSInfo") or {}
    row["guest"] = {k: guest.get(k, "") for k in ("prettyName", "kernelRelease", "version", "id")}
    row["conditions"] = [{"type": c.get("type"), "status": c.get("status"), "reason": c.get("reason", ""),
                          "message": (c.get("message") or "")[:300]}
                         for c in ((vm.get("status") or {}).get("conditions") or []) + ((vmi.get("status") or {}).get("conditions") or [])]
    row["events"] = events_for(ns, name, "")[:30]
    return row


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
    """CPU, memory, run strategy and description. CPU and memory take effect
    at the next boot, so restart says whether to restart now."""
    vm = _get(ns, name)
    spec = vm["spec"]
    dom = spec["template"]["spec"]["domain"]
    changed_hardware = False
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
    try:
        ksend("PUT", f"{API}/namespaces/{ns}/virtualmachines/{name}", vm)
    except urllib.error.HTTPError as error:
        raise ValueError(f"the VM was not saved: {_refusal(error)}")
    restarted = False
    if changed_hardware and cfg.get("restart"):
        try:
            ksend("PUT", f"{SUB}/namespaces/{ns}/virtualmachines/{name}/restart", {})
            restarted = True
        except urllib.error.HTTPError:
            pass
    detail = f"{name} saved"
    if changed_hardware:
        detail += "; restarting now to use the new CPU and memory" if restarted else "; the new CPU and memory apply when it next starts"
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
    with it."""
    vm = _get(ns, name)
    every = [d["claim"] for d in _row(vm, {})["disks"] if d["claim"] and d["kind"] == "disk"]
    claims = every if with_disks else []
    uid = vm["metadata"].get("uid", "")
    for claim in ([] if with_disks else every):
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
        ksend("DELETE", f"{API}/namespaces/{ns}/virtualmachines/{name}")
    except urllib.error.HTTPError as error:
        raise ValueError(f"KubeVirt refused to delete {name}: {_refusal(error)}")
    removed = []
    for claim in claims:
        try:
            ksend("DELETE", f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{urllib.parse.quote(claim)}")
            removed.append(claim)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                removed.append(claim)
    return {"ok": True, "detail": f"{name} deleted" + (f" with {len(removed)} disk{'s' if len(removed) != 1 else ''}" if claims else "; its disks are kept")}
