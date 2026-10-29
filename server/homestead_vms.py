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
import copy
import json
import re
import urllib.error
import urllib.parse

import homestead_vm_hardware as HARDWARE
import homestead_vm_profiles as PROFILES
import homestead_routes
import homestead_kube as KUBE
# Not used here since the split (#623); tests reach it through this module.
import homestead_hvimage as HVIMAGE  # noqa: F401
import homestead_lifecycle as LC
import homestead_operations as OPS
import homestead_outage as OUTAGE

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
# An ISO volume a CD-ROM can hold now (homestead_isos.ready_volume), set by
# the server; None where ISOs are not offered.
iso_ready = None
MODELS = ("virtio", "e1000", "e1000e", "rtl8139")
# Bound by the server: what the cluster is, and Harvester's images.
platform = lambda: {}
images = lambda: []


display_icon = lambda annotations: ""
# Bound by the server: the namespace a VM is in when none is named, and its
# logo and monitoring settings, which it keeps beside a container's.
DEFAULT_NS = "lab"
set_logos = set_monitoring = None


def bind(_kget, _ksend, _events_for, _display_icon=None, default_ns=None, _set_logos=None, _set_monitoring=None):
    global kget, ksend, events_for, display_icon, DEFAULT_NS, set_logos, set_monitoring
    kget, ksend, events_for = _kget, _ksend, _events_for
    if _display_icon:
        display_icon = _display_icon
    DEFAULT_NS = default_ns or DEFAULT_NS
    set_logos, set_monitoring = _set_logos or set_logos, _set_monitoring or set_monitoring


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


def _stopped_after_crash(vm, vmi, instance_known=True):
    """Clear a previous boot's backoff only after the VM is confirmed off."""
    spec, status = vm.get("spec") or {}, vm.get("status") or {}
    if not instance_known or status.get("printableStatus") != "CrashLoopBackOff":
        return False
    strategy = spec.get("runStrategy")
    stopped_strategy = strategy in ("Halted", "Manual") or (not strategy and spec.get("running") is False)
    # A queued power request or a live/unfinished instance is still in flight.
    # Automatic restart strategies must retain their backoff between attempts.
    return bool(stopped_strategy and not status.get("stateChangeRequests")
                and (not vmi or (vmi.get("status") or {}).get("phase") in ("Succeeded", "Failed")))


def _crash_retries_without_guest(vm, vmi, instance_known=True):
    status = vm.get("status") or {}
    return bool(instance_known and not (vm.get("metadata") or {}).get("deletionTimestamp")
                and status.get("printableStatus") == "CrashLoopBackOff"
                and _strategy(vm) == "RerunOnFailure" and not status.get("stateChangeRequests")
                and (not vmi or (vmi.get("status") or {}).get("phase") in ("Succeeded", "Failed")))


def _status(vm, vmi, instance_known=True):
    if (vm.get("metadata") or {}).get("deletionTimestamp"):
        # Deleted in the foreground: it stays until its instance and the
        # disks it owns are gone, and says so rather than looking stuck.
        return "Deleting"
    printable = ((vm.get("status") or {}).get("printableStatus") or "")
    if _stopped_after_crash(vm, vmi, instance_known):
        return "Stopped"
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


def _problem(vm, vmi, instance_known=True):
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
            status = _status(vm, vmi, instance_known)
            if condition_type == "Ready" and (status in ("Starting", "Provisioning", "WaitingForVolumeBinding")
                                              or _stopped_after_crash(vm, vmi, instance_known)) \
                    and "not reported as running" in message.lower():
                continue
            return message
    return ""


def _name(value, label):
    value = str(value or "")
    if not re.fullmatch(r"[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?", value):
        raise ValueError(f"that is not a {label}")
    return value


def _get(ns, name):
    return kget(f"{API}/namespaces/{_name(ns, 'namespace')}/virtualmachines/{_name(name, 'VM name')}")


def detail(ns, name, include_sensitive=True):
    vm = _get(ns, name)
    try:
        vmi = KUBE.get_optional(f"{API}/namespaces/{ns}/virtualmachineinstances/{name}", read=kget)
        # No instance (404) is known too: the VM is off.
        instance_known = vmi is None or bool(vmi.get("metadata"))
    except urllib.error.HTTPError:
        vmi, instance_known = None, False
    vmi = vmi or {}
    row = _row(vm, vmi, _claims(ns), _datavolumes(ns), instance_known)
    row["resource_profile"] = copy.deepcopy(vm["spec"].get("instancetype") or {})
    row["preference_profile"] = copy.deepcopy(vm["spec"].get("preference") or {})
    row["profile_error"] = ""
    if row["resource_profile"] or row["preference_profile"]:
        try:
            expanded = PROFILES.expand(vm, kget)
            row["cores"], row["memory"] = _cores(expanded["domain"]), _memory(expanded["domain"])
        except (ValueError, OSError):
            row["cores"], row["memory"] = None, ""
            row["profile_error"] = "Profile resources could not be resolved. They are left unchanged; check KubeVirt and Homestead RBAC before changing hardware."
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
    if include_sensitive:
        row["cloud_init"] = _read_cloud_init(vm, ns)
    else:
        row["sensitive_hidden"] = True
    import homestead_passthrough as PASSTHROUGH
    row["host_devices"] = PASSTHROUGH.vm_devices(vm)
    row["node_selector"] = ((vm["spec"]["template"].get("spec") or {}).get("nodeSelector") or {}).get(HOST, "")
    row["hardware"] = HARDWARE.read(vm)
    return row


def _refusal(error):
    try:
        return json.loads(error.read().decode("utf-8", "replace")).get("message") or f"HTTP {error.code}"
    except Exception:
        return f"HTTP {error.code}"


def _halt_crash_retries(ns, name):
    """Stop an automatic boot retry when there is no guest to shut down."""
    vm = _get(ns, name)
    if (vm.get("status") or {}).get("printableStatus") != "CrashLoopBackOff" or _strategy(vm) != "RerunOnFailure":
        return None
    meta, status = vm.get("metadata") or {}, vm.get("status") or {}
    identity = _identity(vm)
    if (meta.get("namespace"), meta.get("name")) != (ns, name) or meta.get("deletionTimestamp"):
        raise ValueError("The VM identity changed or is deleting; refresh before stopping it")
    if status.get("stateChangeRequests"):
        raise ValueError("A VM power request is already queued; wait for it before stopping retries")
    vmi = KUBE.get_optional(f"{API}/namespaces/{ns}/virtualmachineinstances/{name}", read=kget)
    if vmi is not None:
        imeta = vmi.get("metadata") or {}
        if (imeta.get("namespace"), imeta.get("name")) != (ns, name) or not imeta.get("uid"):
            raise ValueError("The VM instance could not be verified; refresh before stopping retries")
        if (vmi.get("status") or {}).get("phase") not in ("Succeeded", "Failed"):
            return None  # A live or unfinished instance uses KubeVirt's stop API.
        if not any(owner.get("controller") is True and owner.get("kind") == "VirtualMachine"
                   and owner.get("apiVersion", "").split("/")[0] == "kubevirt.io"
                   and (owner.get("uid"), owner.get("name")) == (identity["uid"], name)
                   for owner in imeta.get("ownerReferences") or []):
            raise ValueError("The completed VM instance has a different owner; refresh before stopping retries")
    # KubeVirt refuses /stop for RerunOnFailure without an unfinished VMI.
    # This is a deliberate stop of retries, never a fallback after a refusal.
    # Fence the identity/version; a conflict is returned without retrying.
    ksend("PATCH", f"{API}/namespaces/{ns}/virtualmachines/{name}", [
        {"op": "test", "path": "/metadata/uid", "value": identity["uid"]},
        {"op": "test", "path": "/metadata/resourceVersion", "value": identity["resourceVersion"]},
        {"op": "test", "path": "/spec/runStrategy", "value": "RerunOnFailure"},
        {"op": "replace", "path": "/spec/runStrategy", "value": "Halted"}
    ], ctype="application/json-patch+json")
    return {"ok": True, "detail": f"Boot retries stopped for {name}; the VM is kept off until you start it"}


def power(ns, name, action, *, raw_errors=False):
    """start, stop, force-stop, restart, reset, pause, unpause. Reset is a restart
    that does not wait for the guest: its instance is replaced at once, as a
    reset switch would."""
    _name(ns, "namespace"), _name(name, "VM name")
    try:
        if action in ("stop", "force-stop"):
            stopped = _halt_crash_retries(ns, name)
            if stopped:
                return stopped
        if action in ("pause", "unpause"):
            ksend("PUT", f"{SUB}/namespaces/{ns}/virtualmachineinstances/{name}/{action}", {})
        elif action == "force-stop":
            ksend("PUT", f"{SUB}/namespaces/{ns}/virtualmachines/{name}/stop", {"gracePeriod": 0})
        elif action == "reset":
            ksend("PUT", f"{SUB}/namespaces/{ns}/virtualmachines/{name}/restart", {"gracePeriodSeconds": 0})
        elif action in ("start", "stop", "restart"):
            # A validation/conflict response is not authority to rewrite the
            # VM's restart policy. Any strategy change needs its own review.
            ksend("PUT", f"{SUB}/namespaces/{ns}/virtualmachines/{name}/{action}", {})
        else:
            raise ValueError("the action is start, stop, force-stop, restart, reset, pause or unpause")
    except urllib.error.HTTPError as error:
        if raw_errors:
            raise
        raise ValueError(f"KubeVirt refused to {action} {name}: {_refusal(error)}")
    words = {"start": "starting", "stop": "stopping", "force-stop": "being stopped at once", "restart": "restarting", "reset": "being reset",
             "pause": "paused", "unpause": "resumed"}
    return {"ok": True, "detail": f"{name} is {words[action]}"}


def _release(ns, claim, uid, vm_name=""):
    """A disk made from the VM's dataVolumeTemplates belongs to the VM, and
    Kubernetes deletes what a deleted owner owned. A disk that is being kept
    is let go of first: its DataVolume and claim stop naming the VM.

    Only "not found" is passed over (CDI may have cleaned up the DataVolume;
    the claim holds the data). A disk that can't be read, or still names the
    VM after letting go, stops the delete: kept means kept (#600)."""
    for path in (f"/apis/cdi.kubevirt.io/v1beta1/namespaces/{ns}/datavolumes/{urllib.parse.quote(claim)}",
                 f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{urllib.parse.quote(claim)}"):
        owners = _owners(path, claim)
        if owners is None:
            continue
        kept = [o for o in owners if o.get("uid") != uid]
        if len(kept) != len(owners):
            try:
                ksend("PATCH", path, {"metadata": {"ownerReferences": kept or None}},
                      ctype="application/merge-patch+json")
            except urllib.error.HTTPError as error:
                raise ValueError(f"{claim} could not be kept, so nothing was deleted: {_refusal(error)}")
            if any(o.get("uid") == uid for o in _owners(path, claim) or []):
                raise ValueError(f"{claim} still belongs to {vm_name or 'the VM'}, so nothing was deleted; try again")


def _owners(path, claim):
    """Who owns the object at path; None when it isn't there."""
    try:
        return (kget(path).get("metadata") or {}).get("ownerReferences") or []
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise ValueError(f"{claim} could not be checked, so nothing was deleted: {_refusal(error)}") from None
    except Exception as error:
        raise ValueError(f"{claim} could not be checked, so nothing was deleted: {str(error)[:120]}") from None


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
    if with_disks:
        # Never a disk that is not this VM's alone: an ISO from the library (or
        # the drivers CD) other VMs share, or any claim another VM also uses.
        # Unreadable, nothing is taken as this VM's alone.
        try:
            others = kget(f"{API}/namespaces/{ns}/virtualmachines")
            if not isinstance(others.get("items"), list) or (others.get("metadata") or {}).get("continue"):
                raise ValueError("incomplete")
        except Exception:
            raise ValueError("the other VMs could not be read, so no disk is deleted with it; "
                             "delete the VM alone, or try again") from None
        shared = {_volume_claim(v) for other in others["items"] if other["metadata"]["name"] != name
                  for v in ((other.get("spec") or {}).get("template") or {}).get("spec", {}).get("volumes") or []}
        claims_now = _claims(ns, strict=True)
        library = {c for c in every if ((claims_now.get(c) or {}).get("metadata", {}).get("labels") or {})
                   .get("homestead.io/iso") == "true"}
        spared = sorted((shared | library) & set(every))
        every = [c for c in every if c not in spared]
    else:
        spared = []
    dvs = _datavolumes(ns)
    unfinished = [c for c in every if ((dvs.get((ns, c)) or {}).get("status") or {}).get("phase") not in (None, "", "Succeeded")
                  and (ns, c) in dvs]
    claims = every if with_disks else unfinished
    uid = vm["metadata"].get("uid", "")
    for claim in (spared if with_disks else [c for c in every if c not in unfinished]):
        if uid:
            _release(ns, claim, uid, name)
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
    OUTAGE.forget("vm", ns, vm)     # its outage webhook's Secret goes with it (#931)
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
                KUBE.delete_if_present(path, send=ksend)
                gone = True  # deleted now, or already gone
            except urllib.error.HTTPError:
                pass
        if gone:
            removed.append(claim)
    detail = f"{name} is being deleted"
    if with_disks:
        detail += f" with {len(removed)} disk{'s' if len(removed) != 1 else ''}"
        if spared:
            detail += f"; {', '.join(spared)} kept, as other VMs or the ISO library use {'it' if len(spared) == 1 else 'them'}"
    else:
        kept = [c for c in every if c not in unfinished]
        if unfinished:
            detail += f"; the unfinished download{'s' if len(unfinished) != 1 else ''} ({', '.join(unfinished)}) stopped and removed"
        if kept:
            detail += f"; {', '.join(kept)} kept"
    return {"ok": True, "detail": detail}


def _delete_route(request):
    homestead_routes.forget("vms")
    b = request.body
    return delete(b.get("ns", DEFAULT_NS), b.get("name", ""), bool(b.get("disks")))


def _migrate_route(request):
    b = request.body
    ns = b.get("ns", DEFAULT_NS)
    result = LC.vm_migrate(ns, b["name"], b.get("target"))
    if result.get("migration"):
        result["operation"] = OPS.start(
            "vm-migration", f"Migrate {b['name']}",
            {"kind": "VirtualMachine", "name": b["name"], "namespace": ns},
            "/vms", {"namespace": ns, "name": result["migration"]})
    return result


# Its routes and who may use them (homestead_routes.py).
ROUTES = {
    ("POST", "/api/vm/delete"): ("admin", _delete_route),
    ("POST", "/api/vm/migrate"): ("operator", _migrate_route),
    ("POST", "/api/vms/logo"): ("operator", lambda request: set_logos(request.body)),
    ("POST", "/api/vms/monitoring"): ("operator", lambda request: set_monitoring(request.body)),
    ("GET", "/api/vms"): ("viewer", lambda request: homestead_routes.cached("vms", homestead_routes.PAGE_SECONDS, list_vms)),
    ("GET", "/api/vm"): ("viewer", lambda request: detail((request.query.get("ns") or [""])[0], (request.query.get("name") or [""])[0], include_sensitive=request.role == "admin")),
}


# Moved by feature (#623). Re-exported for one release, so other modules,
# server.py and tests that use or replace these names here keep working.
from homestead_vm_list import (  # noqa: E402,F401
    _claims, _datavolumes, STUCK_AFTER, WAITING_PHASES, _age, _why_not_filling, _image_filling,
    _stuck_problem, _filling, _display, _row, list_vms,
)
from homestead_vm_edit import (  # noqa: E402,F401
    _claim_templates, _set_claim_templates, _dv_templates, _forget_templates, _template_size,
    _disk_source, _size, _bytes, _image, _disk_volume, _volume_claim, _edit_disks, _set_network,
    _network_kind, _edit_nics, _cloud_volume, _secret_values, _read_cloud_init, _identity,
    _queue_secret, _edit_cloud_init, prepare_edit, _recheck_edit, commit_edit, edit,
)
