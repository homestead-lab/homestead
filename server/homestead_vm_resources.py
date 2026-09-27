"""Read-only VM resource evidence, never a replacement KubeVirt renderer.

Cold-start requests below are lower bounds. Launcher overhead, sidecars,
defaults and admission can change the final Pod. A separate operational RAM
allowance must not be written as a Kubernetes request/limit or called exact.
For an existing VMI, only its UID-owned launcher supplies observed requests.
"""
import copy
from decimal import Decimal, ROUND_CEILING

import homestead_pod_resources as RESOURCES

MIB = 1024**2


def identity(obj):
    meta = (obj or {}).get("metadata") or {}
    return {key: meta.get(key) for key in ("namespace", "name", "uid", "resourceVersion")}


def _controller(obj):
    refs = [ref for ref in ((obj or {}).get("metadata") or {}).get("ownerReferences") or []
            if ref.get("controller") is True]
    return refs[0] if len(refs) == 1 else {}


def _owned(child, parent, kind):
    cm, pm = (child or {}).get("metadata") or {}, (parent or {}).get("metadata") or {}
    ref = _controller(child)
    return bool(pm.get("uid") and cm.get("namespace") == pm.get("namespace") and
                ref.get("apiVersion", "").split("/")[0] == "kubevirt.io" and
                ref.get("kind") == kind and ref.get("uid") == pm["uid"] and ref.get("name") == pm.get("name"))


def launchers(vm, vmi, pods):
    """Return observed ownership, not a promise that resources can be freed.

    A missing VMI is not proof that an old launcher has gone away. Callers must
    keep all reservations in that case, and must not pick one migration pod.
    Terminal pods are excluded; terminating pods still reserve resources.
    """
    if pods is None or not vmi or not _owned(vmi, vm, "VirtualMachine"):
        return [], False
    result = [pod for pod in pods if _owned(pod, vmi, "VirtualMachineInstance") and
              (pod.get("status") or {}).get("phase") not in ("Succeeded", "Failed")]
    return result, True


def resident(vm, vmi, pods):
    """One stable launcher required to reuse resources for Unpause.

    Restart must project the proposed VM template, not reuse this result.
    The caller must bind/recheck all three identities before sending power.
    """
    owned, known = launchers(vm, vmi, pods)
    if not known or len(owned) != 1:
        raise ValueError("a single UID-owned VM launcher could not be verified")
    pod = owned[0]
    status = vmi.get("status") or {}
    node = (pod.get("spec") or {}).get("nodeName")
    migration = status.get("migrationState") or {}
    if ((vm.get("metadata") or {}).get("deletionTimestamp") or
            (vmi.get("metadata") or {}).get("deletionTimestamp") or
            (pod.get("metadata") or {}).get("deletionTimestamp") or
            status.get("phase") != "Running" or (pod.get("status") or {}).get("phase") != "Running" or
            not node or status.get("nodeName") != node or
            (migration and not (migration.get("completed") or migration.get("failed")))):
        raise ValueError("VM launcher is changing, migrating or not running; refresh before continuing")
    for obj in (vm, vmi, pod):
        if not all(identity(obj).values()):
            raise ValueError("VM launcher identity/version is incomplete")
    return copy.deepcopy(pod)


def _positive(value, label):
    try:
        number = Decimal(str(value))
    except Exception:
        raise ValueError(f"invalid VM {label}") from None
    if not number.is_finite() or number <= 0:
        raise ValueError(f"VM {label} must be positive")
    return number


def project(vm, configuration=None, *, expanded_spec=None):
    """Project the proposed VMI into placement input with explicit uncertainty.

    configuration is the *observed* KubeVirt spec.configuration, or None when
    unreadable. expanded_spec, if supplied, must come from KubeVirt expansion
    of this exact VM (not from a previous running VMI with stale settings).
    No credentials, disk URLs or cloud-init contents are included in the result.
    """
    outer = vm.get("spec") or {}
    spec = copy.deepcopy(expanded_spec if expanded_spec is not None else (outer.get("template") or {}).get("spec") or {})
    warnings, blockers = [], []
    if expanded_spec is None and any(outer.get(key) for key in ("instancetype", "preference")):
        blockers.append("VM instance type/preferences must be expanded before resource and hardware checks")
    config = configuration or {}
    if configuration is None:
        warnings.append("KubeVirt configuration is unavailable; CPU allocation and emulation defaults are unverified")
        blockers.append("KubeVirt emulation/device configuration must be readable before checking host eligibility")
    domain = spec.get("domain") or {}
    cpu, memory, devices = (domain.get(key) or {} for key in ("cpu", "memory", "devices"))
    resources = domain.get("resources") or {}
    requests, limits = resources.get("requests") or {}, resources.get("limits") or {}
    guest = RESOURCES.quantity(memory.get("guest") or requests.get("memory") or limits.get("memory"))
    if not guest:
        blockers.append("VM guest memory could not be resolved")
    # Unspecified memory is not silently converted into an invented small VM.
    requested_memory = RESOURCES.quantity(requests.get("memory") or memory.get("guest") or limits.get("memory"))
    developer = config.get("developerConfiguration") or {}
    if "memory" not in requests and memory.get("guest") and not (memory.get("hugepages") or cpu.get("dedicatedCpuPlacement")):
        overcommit = _positive(developer.get("memoryOvercommit", 100), "memory overcommit percentage")
        requested_memory = int((Decimal(guest * 100) / overcommit).to_integral_value(rounding=ROUND_CEILING))
        if overcommit != 100:
            warnings.append("implicit memory overcommit changes scheduler requests, not the VM's possible physical RAM use")
    physical = max(guest, requested_memory, RESOURCES.quantity(memory.get("maxGuest")))
    overhead = max(256 * MIB, (physical + 19) // 20) if physical else 0
    estimate = max(physical + overhead, RESOURCES.quantity(limits.get("memory")))
    warnings.append("RAM projection includes a planning allowance of max(256 MiB, 5% of guest/reserved RAM), not KubeVirt's exact launcher overhead or a memory limit")
    warnings.append("launcher sidecars, admission defaults and runtime overhead may require more resources than this lower-bound request")
    if not limits.get("memory"):
        warnings.append("VM launcher memory is not explicitly limited")

    topology = any(cpu.get(key) for key in ("cores", "sockets", "threads"))
    vcpus = 1
    for key in ("cores", "sockets", "threads"):
        count = _positive(cpu[key] if key in cpu else 1, key)
        if count != int(count):
            raise ValueError(f"VM {key} must be a whole number")
        vcpus *= int(count)
    explicit_cpu = requests.get("cpu", limits.get("cpu"))
    dedicated = cpu.get("dedicatedCpuPlacement") is True
    if dedicated:
        amount = RESOURCES.quantity(explicit_cpu, "cpu") if explicit_cpu is not None else vcpus * 1000
        if amount % 1000 or not amount:
            blockers.append("dedicated VM CPU requests must be positive whole cores")
        if topology and explicit_cpu is not None and amount != vcpus * 1000:
            blockers.append("dedicated VM CPU topology conflicts with its CPU request")
        if requests.get("cpu") is not None and limits.get("cpu") is not None and RESOURCES.quantity(limits["cpu"], "cpu") != amount:
            blockers.append("dedicated VM CPU request and limit differ")
        if cpu.get("isolateEmulatorThread"):
            amount += 1000  # minimum; host SMT alignment can add another core
            warnings.append("isolated emulator CPU includes one extra core; host SMT alignment may require another")
        warnings.append("dedicated CPU topology, NUMA and CPU-manager allocations still require kubelet admission")
    elif explicit_cpu is not None:
        amount = RESOURCES.quantity(explicit_cpu, "cpu")
    else:
        ratio = _positive(developer.get("cpuAllocationRatio", 10), "CPU allocation ratio")
        amount = int((Decimal(vcpus * 1000) / ratio).to_integral_value(rounding=ROUND_CEILING))
    projected_requests = {"cpu": f"{amount}m", "memory": str(requested_memory)}
    huge = (memory.get("hugepages") or {}).get("pageSize")
    if huge:
        page_size = RESOURCES.quantity(huge)
        if not page_size or guest % page_size:
            blockers.append("VM guest memory must be a multiple of its hugepage size")
        projected_requests["hugepages-" + str(huge)] = str(guest)
        # Hugepage guest memory is not also an ordinary-memory request.
        projected_requests["memory"] = "0"
        warnings.append("hugepage RAM and ordinary launcher overhead are separate; NUMA-local availability is unverified")
    if resources.get("overcommitGuestOverhead"):
        warnings.append("guest overhead is overcommitted; scheduler reservations do not cover its physical RAM")
    for resource in set(requests) | set(limits):
        if resource not in ("cpu", "memory"):
            amount = RESOURCES.quantity(requests.get(resource, limits.get(resource)), resource)
            projected_requests[resource] = str(max(int(projected_requests.get(resource, "0")), amount))

    pod = {key: copy.deepcopy(spec[key]) for key in
           ("nodeSelector", "affinity", "tolerations", "schedulerName", "priorityClassName", "topologySpreadConstraints", "nodeName", "resourceClaims") if key in spec}
    selectors = pod.setdefault("nodeSelector", {})
    def select(key, value):
        if key in selectors and selectors[key] != value:
            blockers.append(f"VM node selector conflicts with required {key}={value}")
        else:
            selectors[key] = value
    select("kubevirt.io/schedulable", "true")
    if spec.get("architecture"):
        select("kubernetes.io/arch", spec["architecture"])
    if dedicated:
        select("cpumanager", "true")
    model = cpu.get("model") or config.get("cpuModel")
    if model and model not in ("host-model", "host-passthrough"):
        select("cpu-model.node.kubevirt.io/" + model, "true")
    for feature in cpu.get("features") or []:
        policy = feature.get("policy") or "require"
        if policy == "require":
            select("cpu-feature.node.kubevirt.io/" + feature["name"], "true")
        elif policy == "forbid":
            # Required node-affinity terms are OR alternatives. Add the new
            # constraint to every alternative, never as a permissive OR term.
            required = pod.setdefault("affinity", {}).setdefault("nodeAffinity", {}).setdefault(
                "requiredDuringSchedulingIgnoredDuringExecution", {"nodeSelectorTerms": [{}]})
            for term in required.get("nodeSelectorTerms") or []:
                term.setdefault("matchExpressions", []).append({"key": "cpu-feature.node.kubevirt.io/" + feature["name"],
                                                                 "operator": "DoesNotExist"})
    if config.get("developerConfiguration", {}).get("useEmulation") is True:
        warnings.append("software emulation is enabled; CPU performance and nested virtualization are not guaranteed")
    elif configuration is not None:
        projected_requests["devices.kubevirt.io/kvm"] = "1"
    device_counts = {}
    for device in (devices.get("gpus") or []) + (devices.get("hostDevices") or []):
        resource = device.get("deviceName")
        if resource:
            device_counts[resource] = device_counts.get(resource, 0) + 1
        else:
            blockers.append("VM passthrough device resource name is unresolved")
    for resource, count in device_counts.items():
        projected_requests[resource] = str(max(count, int(projected_requests.get(resource, "0"))))
    if devices.get("interfaces") or devices.get("autoattachPodInterface") is not False:
        warnings.append("network binding plugins may add tun, vhost-net or SR-IOV device requirements")
    if cpu.get("numa") or domain.get("launchSecurity") or devices.get("autoattachVSOCK") or spec.get("resourceClaims"):
        warnings.append("NUMA, confidential-compute, VSOCK or dynamic-device requirements need additional host admission")
    if domain.get("ioThreadsPolicy") or devices.get("filesystems"):
        warnings.append("IO threads and filesystem sidecars add resource overhead not yet rendered")
    pod["containers"] = [{"name": "vm-launcher-estimate", "resources": {"requests": projected_requests}}]
    pod["volumes"] = []
    for volume in spec.get("volumes") or []:
        claim = (volume.get("persistentVolumeClaim") or {}).get("claimName") or (volume.get("dataVolume") or {}).get("name")
        if claim:
            pod["volumes"].append({"name": volume["name"], "persistentVolumeClaim": {"claimName": claim}})
        elif volume.get("hostDisk"):
            pod["volumes"].append({"name": volume["name"], "hostPath": {"path": volume["hostDisk"].get("path")}})
            warnings.append("VM host disk contents and available space must be verified on the chosen host")
        elif volume.get("containerDisk"):
            warnings.append("container-disk download and sidecar resources are not fully rendered")
        elif volume.get("emptyDisk"):
            warnings.append("ephemeral VM disk consumes node storage; free space must be checked")
    # Do not copy VM template annotations: they can contain credentials or
    # Harvester claim templates with source URLs. Labels support pod affinity.
    template = {"metadata": {"labels": copy.deepcopy((outer.get("template", {}).get("metadata") or {}).get("labels") or {})}, "spec": pod}
    return {"manifest": {"spec": {"template": template}}, "guest_memory_bytes": guest,
            "memory_estimate_bytes": estimate, "planning_overhead_bytes": overhead,
            "request_is_lower_bound": True, "warnings": sorted(set(warnings)), "blockers": blockers}
