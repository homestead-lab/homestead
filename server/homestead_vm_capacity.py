"""VM admission preview: read-only observations, not a reservation or action.

The API layer must bind/recheck context and exact prepared input before writes.
This module never sends power, changes policy or provisions a dependency.
"""
import copy
import urllib.error
from urllib.parse import quote

import homestead_place as PLACE
import homestead_vm_resources as VMRES
import homestead_pod_resources as RESOURCES
import homestead_vm_state as STATE


def _items(read, path):
    value = read(path)
    if not isinstance(value.get("items"), list) or (value.get("metadata") or {}).get("continue"):
        raise ValueError("incomplete VM admission inventory")
    return value["items"]


def _optional(read, path):
    try:
        value = read(path)
        if not (value.get("metadata") or {}).get("name"):
            raise ValueError("incomplete VM dependency response")
        return value
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


def dependencies(vm, read, planned_claims=None, *, pods=None):
    """Additional VM storage/network readiness, with identity-only context.

    Planned claims are allowed only after a verified 404. Existing imports and
    claims must be ready. Block/API read failures are not capacity overrides.
    The common placement checker handles PV/node/access-mode constraints.
    """
    namespace = vm["metadata"]["namespace"]
    spec = vm["spec"]["template"]["spec"]
    blockers, warnings, context, seen = [], [], {}, set()
    disks = {disk.get("name"): disk for disk in ((spec.get("domain") or {}).get("devices") or {}).get("disks") or []}
    def check(path, label, planned=False):
        try:
            obj = _optional(read, path)
            context[path] = VMRES.identity(obj) if obj else None
            if not obj:
                if planned:
                    warnings.append(f"{label} is planned; provisioning, import completion and free storage are not guaranteed")
                else:
                    blockers.append(f"{label} does not exist")
            elif (obj.get("metadata") or {}).get("deletionTimestamp"):
                blockers.append(f"{label} is being deleted")
            elif not all(context[path].get(key) for key in ("uid", "resourceVersion")):
                blockers.append(f"{label} identity/version is unavailable")
            return obj
        except Exception:
            blockers.append(f"{label} could not be checked; refresh permissions/connectivity before continuing")
            return None
    for volume in spec.get("volumes") or []:
        dv_name = (volume.get("dataVolume") or {}).get("name")
        claim = (volume.get("persistentVolumeClaim") or {}).get("claimName") or dv_name
        if not claim or claim in seen:
            continue
        seen.add(claim)
        disk = disks.get(volume.get("name")) or {}
        readonly = bool((volume.get("persistentVolumeClaim") or {}).get("readOnly") or
                        (disk.get("disk") or {}).get("readOnly") or (disk.get("lun") or {}).get("readOnly"))
        if pods is None:
            blockers.append(f"VM disk {claim} consumers could not be verified")
        elif not readonly:
            consumers = [pod for pod in pods if (pod.get("metadata") or {}).get("namespace") == namespace and
                         (pod.get("status") or {}).get("phase") not in ("Succeeded", "Failed") and
                         any((v.get("persistentVolumeClaim") or {}).get("claimName") == claim
                             for v in (pod.get("spec") or {}).get("volumes") or [])]
            if consumers:
                # RWX permits attachment, not concurrent guest filesystem writers.
                # Only the exact reviewed resident/restarting VM's UID-owned pods
                # were excluded by the caller; names/labels never waive this check.
                blockers.append(f"VM disk {claim} is still used by another pod; shared attachment does not make concurrent disk writers safe")
                context["consumers:" + claim] = [VMRES.identity(pod) for pod in consumers]
        proposed = (planned_claims or {}).get(claim)
        pvc = check(f"/api/v1/namespaces/{quote(namespace, safe='')}/persistentvolumeclaims/{quote(claim, safe='')}",
                    f"VM disk {claim}", bool(proposed))
        if pvc:
            phase = (pvc.get("status") or {}).get("phase")
            if phase != "Bound":
                # A pending WFFC claim is intentionally bound by the first
                # consumer, not evidence of a broken disk. Verify the class.
                klass = (pvc.get("spec") or {}).get("storageClassName")
                sc = check("/apis/storage.k8s.io/v1/storageclasses/" + quote(klass, safe=""),
                           f"storage class {klass}") if klass else None
                if phase == "Pending" and sc and (sc.get("volumeBindingMode") == "WaitForFirstConsumer"):
                    warnings.append(f"VM disk {claim} waits for first-consumer placement; provisioning capacity is unverified")
                else:
                    blockers.append(f"VM disk {claim} is not Bound ({phase or 'unknown'})")
        if dv_name:
            dv = check(f"/apis/cdi.kubevirt.io/v1beta1/namespaces/{quote(namespace, safe='')}/datavolumes/{quote(dv_name, safe='')}",
                       f"disk import {dv_name}", bool(proposed and not pvc))
            if dv and (dv.get("status") or {}).get("phase") not in ("Succeeded", "WaitForFirstConsumer"):
                blockers.append(f"disk import {dv_name} has not completed")
    for network in spec.get("networks") or []:
        name = (network.get("multus") or {}).get("networkName")
        if not name:
            continue
        parts = name.split("/")
        if len(parts) == 1:
            parts.insert(0, namespace)
        if len(parts) != 2 or not all(parts):
            blockers.append("VM LAN network reference is invalid")
            continue
        nad = check(f"/apis/k8s.cni.cncf.io/v1/namespaces/{quote(parts[0], safe='')}/network-attachment-definitions/{quote(parts[1], safe='')}",
                    f"LAN network {name}")
        if nad:
            warnings.append(f"LAN network {name} exists; host bridge, plugin and physical link readiness must also be verified")
    return {"blockers": blockers, "warnings": warnings, "context": context}


def plan(vm, read, nodes, *, action="start", current=None, warning_percent=88,
         planned_claims=None, expanded_spec=None):
    if action not in ("start", "restart", "unpause", "create", "edit"):
        raise ValueError("unsupported VM admission action")
    namespace, name = vm["metadata"]["namespace"], vm["metadata"]["name"]
    blockers, warnings = [], []
    context = {"action": action, "vm": VMRES.identity(current or vm)}
    if ((current or vm).get("metadata") or {}).get("deletionTimestamp"):
        blockers.append("VM is being deleted")
    kubevirt_version = None
    try:
        configs = _items(read, "/apis/kubevirt.io/v1/kubevirts")
        if len(configs) != 1 or (configs[0].get("metadata") or {}).get("deletionTimestamp"):
            raise ValueError("KubeVirt configuration is ambiguous")
        configuration = (configs[0].get("spec") or {}).get("configuration") or {}
        kubevirt_version = VMRES.CPU.observed_version(configs[0])
        context["kubevirt"] = VMRES.identity(configs[0])
        if not all(context["kubevirt"].values()):
            raise ValueError("KubeVirt configuration identity unavailable")
    except Exception:
        configuration = None
    model = VMRES.project(vm, configuration, expanded_spec=expanded_spec, read=read, kubevirt_version=kubevirt_version)
    try:
        pods = _items(read, "/api/v1/pods")
    except Exception:
        pods = None
        warnings.append("pod inventory is incomplete; launcher ownership and reservations are unknown")
    vmi, owned, ownership_known = None, [], False
    if (current or vm).get("metadata", {}).get("uid"):
        try:
            vmi = _optional(read, f"/apis/kubevirt.io/v1/namespaces/{quote(namespace, safe='')}/virtualmachineinstances/{quote(name, safe='')}")
            context["vmi"] = VMRES.identity(vmi) if vmi else None
            owned, ownership_known = VMRES.launchers(current or vm, vmi, pods)
            if vmi:
                if not ownership_known:
                    blockers.append("current VMI/launcher ownership could not be verified")
                if not all(VMRES.identity(vmi).values()):
                    blockers.append("current VMI identity/version is incomplete")
                if (vmi.get("metadata") or {}).get("deletionTimestamp"):
                    blockers.append("current VMI is terminating; wait for it to finish")
                migration = (vmi.get("status") or {}).get("migrationState") or {}
                if migration and not (migration.get("completed") or migration.get("failed")):
                    blockers.append("current VMI is migrating; wait for migration to finish")
        except Exception:
            blockers.append("current VMI state is unavailable; power/placement cannot be checked")
    context["launchers"] = [VMRES.identity(pod) for pod in owned]
    manifest = model["manifest"]
    resident_node = None
    dependency_pods = pods
    if action == "unpause":
        try:
            launcher = VMRES.resident(current or vm, vmi, pods)
            resident_node = launcher["spec"]["nodeName"]
            manifest = {"spec": {"template": {"metadata": copy.deepcopy(launcher["metadata"]), "spec": launcher["spec"]}}}
            # Reuse exact observed launcher requests and placement. Proposed
            # edit/template resources do not describe a paused, already-running VM.
            live_vm = copy.deepcopy(current or vm)
            live_vm["spec"]["template"]["spec"] = copy.deepcopy(vmi["spec"])
            live_vm["spec"]["template"]["metadata"] = copy.deepcopy(vmi.get("metadata") or {})
            live_model = VMRES.project(live_vm, configuration, expanded_spec=vmi["spec"], read=read, kubevirt_version=kubevirt_version)
            model = live_model
            model["memory_estimate_bytes"] = max(model["memory_estimate_bytes"], RESOURCES.memory_estimate(launcher["spec"])[0])
            pods = [pod for pod in pods if pod.get("metadata", {}).get("uid") != launcher["metadata"]["uid"]]
            dependency_pods = pods
            warnings.append("Unpause reuses the current launcher; its live RAM is already included in the host metric")
        except ValueError as error:
            blockers.append(str(error))
    elif action in ("restart", "edit") and owned and ownership_known:
        owned_uids = {pod["metadata"]["uid"] for pod in owned}
        dependency_pods = [pod for pod in pods if pod.get("metadata", {}).get("uid") not in owned_uids]
        if action == "restart":
            pods = dependency_pods
            warnings.append("post-stop placement assumes owned launchers fully terminate and release disks/devices; this is not guaranteed")
            warnings.append("live RAM includes the old VM and is not subtracted from the conservative restart projection")
        else:
            # Save is not Stop. LiveUpdate may resize or migrate; never credit
            # released launcher reservations, or accept a plan fitting only on
            # some other host while the old instance remains here.
            try:
                launcher = VMRES.resident(current or vm, vmi, pods)
                node = launcher["spec"]["nodeName"]
                selector = manifest["spec"]["template"]["spec"].setdefault("nodeSelector", {})
                if selector.get("kubernetes.io/hostname") not in (None, node):
                    blockers.append("Stop the running VM before editing its pinned host; Save does not move its current instance")
                selector["kubernetes.io/hostname"] = node
            except ValueError as error:
                blockers.append("Live edit requires a stable, verified resident launcher: " + str(error))
            warnings.append("Live edit conservatively retains current launcher reservations and checks the proposed VM on its current host too. Stop the VM and review again if this overlap cannot fit; Save does not guarantee a live resize or migration.")
    elif action == "start" and vmi and (vmi.get("status") or {}).get("phase") not in ("Succeeded", "Failed"):
        blockers.append("VM already has an active instance; refresh and use its appropriate power action")
    blockers.extend(model["blockers"])
    warnings.extend(model["warnings"])
    dependency_vm = copy.deepcopy(vm)
    if action == "unpause" and resident_node:
        dependency_vm["spec"]["template"]["spec"] = copy.deepcopy(vmi["spec"])
    elif expanded_spec is not None:
        dependency_vm["spec"]["template"]["spec"] = copy.deepcopy(expanded_spec)
    state = STATE.inspect(dependency_vm, dependency_vm["spec"]["template"]["spec"], configuration, read,
                          vmi=vmi, version=kubevirt_version, cold=action != "unpause")
    blockers.extend(state["blockers"])
    warnings.extend(state["warnings"])
    planned_claims = dict(planned_claims or {})
    if set(planned_claims) & set(state["planned_claims"]):
        blockers.append("VM disk name collides with controller-managed persistent state")
    else:
        planned_claims.update(state["planned_claims"])
    for volume in state["volumes"]:
        claim = volume["persistentVolumeClaim"]["claimName"]
        if action == "unpause" and resident_node and not any(
                (row.get("persistentVolumeClaim") or {}).get("claimName") == claim
                for row in manifest["spec"]["template"]["spec"].get("volumes") or []):
            blockers.append("The current launcher does not mount its verified persistent state PVC; inspect it before resuming")
        for target in (manifest["spec"]["template"]["spec"], dependency_vm["spec"]["template"]["spec"]):
            volumes = target.setdefault("volumes", [])
            if not any((row.get("persistentVolumeClaim") or {}).get("claimName") == claim for row in volumes):
                volumes.append(copy.deepcopy(volume))
    evidence = dependencies(dependency_vm, read, planned_claims, pods=dependency_pods)
    blockers.extend(evidence["blockers"])
    warnings.extend(evidence["warnings"])
    context["dependencies"] = {**model["dependencies"], **state["dependencies"], **evidence["context"]}
    result = PLACE.manifest_plan(manifest, namespace, name, 1, warning_percent,
                                 planned_claims=planned_claims, pod_snapshot=pods, nodes_snapshot=nodes,
                                 read=read, memory_estimate_bytes=model["memory_estimate_bytes"],
                                 workload_kind="vm", resident_node=resident_node)
    result["warnings"] = sorted(set(result["warnings"] + warnings))
    result["blockers"] = sorted(set(blockers))
    result["blocked"] |= bool(blockers)
    result["requires_confirmation"] = bool(result["warnings"])
    result["vm"] = {"action": action, "guest_memory_gb": round(model["guest_memory_bytes"] / 1024**3, 2),
                    "request_is_lower_bound": action != "unpause", "ownership_known": ownership_known,
                    "cpu_request_is_estimate": action != "unpause" and model["cpu_request_is_estimate"],
                    "state_initialization": state["initialization"],
                    "resident_node": resident_node, "context": context}
    return result
