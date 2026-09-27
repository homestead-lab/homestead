"""Auxiliary VM containers and known additional RAM, without sensitive bodies.

Defaults follow KubeVirt's renderresources, serialconsolelog, container-disk and
virtiofs implementations (v1.3.1/v1.9.0). These supplement, not replace, the
planning allowance and observed launcher evidence. Webhook injection is unknown.
"""
import json
from decimal import Decimal, ROUND_CEILING
from urllib.parse import quote

import homestead_pod_resources as RESOURCES
from homestead_vm_network import DNS

MIB = 1024**2


def project(vm, spec, config, network, read=None):
    domain = spec.get("domain") or {}
    devices = domain.get("devices") or {}
    resource = domain.get("resources") or {}
    requests, limits = resource.get("requests") or {}, resource.get("limits") or {}
    guaranteed = bool((domain.get("cpu") or {}).get("dedicatedCpuPlacement")) or all(
        RESOURCES.quantity(requests.get(key), key) > 0 and
        RESOURCES.quantity(requests.get(key), key) == RESOURCES.quantity(limits.get(key), key)
        for key in ("cpu", "memory"))
    result = {"containers": [], "initContainers": [], "volumes": [], "dependencies": {},
              "blockers": [], "warnings": [], "extra_memory": network["memory_overhead"]}
    definitions = config.get("supportContainerResources") or []

    def support(kind, name, defaults, default_limits, init=False):
        chosen = [entry.get("resources") or {} for entry in definitions if entry.get("type") == kind]
        if len(chosen) > 1:
            result["blockers"].append(f"KubeVirt has ambiguous {kind} support-container resources")
        configured = chosen[0] if chosen else {}
        values = {"requests": dict(defaults), "limits": dict(default_limits)}
        for bucket in values:
            for key in ("cpu", "memory"):
                if key in (configured.get(bucket) or {}):
                    values[bucket][key] = configured[bucket][key]
        if guaranteed:
            if kind == "sidecar":
                values["limits"] = {"cpu": "200m", "memory": "64M", **values["limits"]}
            for key in ("cpu", "memory"):
                if key in values["limits"]:
                    values["requests"][key] = values["limits"][key]
        for key in set(values["requests"]) | set(values["limits"]):
            request = RESOURCES.quantity(values["requests"].get(key, values["limits"].get(key)), key)
            if key in values["limits"] and request > RESOURCES.quantity(values["limits"][key], key):
                result["blockers"].append(f"KubeVirt {kind} request exceeds its limit")
        result["initContainers" if init else "containers"].append({"name": name, "resources": values})

    if devices.get("autoattachSerialConsole") is not False:
        logging = devices.get("logSerialConsole")
        if logging is None:
            logging = (config.get("virtualMachineOptions") or {}).get("disableSerialConsoleLog") is None
        if logging:
            support("guest-console-log", "guest-console-log", {"cpu": "5m", "memory": "35M"}, {"cpu": "15m", "memory": "60M"})
    volumes = spec.get("volumes") or []
    disks = sum("containerDisk" in volume for volume in volumes)
    disks += bool(((domain.get("firmware") or {}).get("kernelBoot") or {}).get("container"))
    for index in range(disks):
        support("containerDisk", f"container-disk-{index}", {"cpu": "1m", "memory": "1M", "ephemeral-storage": "50M"}, {"cpu": "10m", "memory": "40M"})
        support("containerDisk", f"container-disk-{index}-init", {"cpu": "1m", "memory": "1M", "ephemeral-storage": "50M"}, {"cpu": "10m", "memory": "40M"}, True)
    if disks:
        support("containerDisk", "container-disk-binary", {"cpu": "10m", "memory": "1M"},
                {"cpu": "10m" if guaranteed else "100m", "memory": "40M"}, True)
    for index, filesystem in enumerate(devices.get("filesystems") or []):
        if not any(volume.get("name") == filesystem.get("name") for volume in volumes):
            result["blockers"].append("VM filesystem has no matching volume")
        support("virtiofs", f"virtiofs-{index}", {"cpu": "10m", "memory": "1M"}, {"cpu": "100m", "memory": "80M"})
    annotations = ((vm.get("spec") or {}).get("template", {}).get("metadata") or {}).get("annotations") or {}
    hooks = []
    if "hooks.kubevirt.io/hookSidecars" in annotations:
        try:
            hooks = json.loads(annotations["hooks.kubevirt.io/hookSidecars"])
            if not isinstance(hooks, list) or len(hooks) > 100 or any(not isinstance(hook, dict) for hook in hooks):
                raise ValueError()
        except (ValueError, TypeError):
            hooks = []
            result["blockers"].append("VM hook sidecars could not be parsed; their resources and dependencies are unknown")
    for index in range(len(hooks) + network["sidecars"]):
        support("sidecar", f"hook-sidecar-{index}", {}, {})
    namespace = vm["metadata"]["namespace"]
    for index, hook in enumerate(hooks):
        for key, api in (("configMap", "configmaps"), ("pvc", "persistentvolumeclaims")):
            if key not in hook:
                continue
            name = (hook.get(key) or {}).get("name")
            if not isinstance(name, str) or not DNS.fullmatch(name):
                result["blockers"].append("VM hook dependency name is invalid")
                continue
            path = f"/api/v1/namespaces/{quote(namespace, safe='')}/{api}/{quote(name, safe='')}"
            try:
                value = read(path) if callable(read) else {}
                meta = value.get("metadata") or {}
                if (meta.get("namespace") != namespace or meta.get("name") != name or not meta.get("uid")
                        or not meta.get("resourceVersion") or meta.get("deletionTimestamp")):
                    raise ValueError()
                if key == "configMap" and (hook[key].get("key") not in (value.get("data") or {}) and
                                            hook[key].get("key") not in (value.get("binaryData") or {})):
                    raise ValueError()
                result["dependencies"][path] = {field: meta.get(field) for field in ("namespace", "name", "uid", "resourceVersion")}
                if key == "pvc":
                    result["volumes"].append({"name": f"hook-claim-{index}", "persistentVolumeClaim": {"claimName": name}})
            except Exception:
                result["blockers"].append(f"VM hook {key} {name} identity/content availability could not be verified")
    extras = 0
    exec_probes = sum(bool((spec.get(key) or {}).get("exec")) for key in ("readinessProbe", "livenessProbe"))
    if exec_probes:
        extras += (100 + 10 * exec_probes) * MIB
    if devices.get("gpus") or devices.get("hostDevices") or any("sriov" in iface for iface in devices.get("interfaces") or []):
        extras += 1024 * MIB
    if "sev" in (domain.get("launchSecurity") or {}):
        extras += 256 * MIB
    if devices.get("tpm") is not None:
        extras += 53 * MIB
    if spec.get("architecture") == "arm64":
        extras += 128 * MIB
    ratio = config.get("additionalGuestMemoryOverheadRatio")
    if ratio not in (None, ""):
        # Operator scaling affects core/probe/device overhead, not the binding
        # plugin's separately configured memory. The independent planning
        # allowance is floored by the caller, not this scheduler-request term.
        scale = Decimal(str(ratio))
        if not scale.is_finite() or scale <= 0:
            raise ValueError("Invalid additional guest-memory overhead ratio")
        extras = int((extras * scale).to_integral_value(rounding=ROUND_CEILING))
    result["extra_memory"] += extras
    if result["containers"] or result["initContainers"]:
        amount, unbounded = RESOURCES.memory_estimate(result)
        result["support_memory"] = amount
        if unbounded:
            result["warnings"].append("VM hook sidecars have no memory limit; their configured requests are only a lower bound")
    else:
        result["support_memory"] = 0
    return result
