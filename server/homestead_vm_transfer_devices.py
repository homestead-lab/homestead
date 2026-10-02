"""Destination hardware choices for a VM transfer; preparation is read-only."""
import base64
import copy
import hashlib
import json

import homestead_passthrough as PASS


def signature(definition):
    data = {"devices": PASS.vm_devices(definition["object"]), "vbios": definition.get("vbios", {})}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def prepare(definition, namespace, choices=None):
    vm = copy.deepcopy(definition["object"])
    devices = PASS.vm_devices(vm)
    annotations = vm["spec"]["template"].get("metadata", {}).get("annotations", {})
    own_cm = vm["metadata"]["name"] + "-vbios"
    try:
        hooks = json.loads(annotations.get(PASS.HOOK_ANNOTATION, "[]") or "[]")
        if not isinstance(hooks, list) or any(not isinstance(hook, dict) for hook in hooks):
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError("The VM's hook sidecar configuration cannot be read; repair it before transferring") from None
    if any((hook.get("configMap") or {}).get("name") != own_cm for hook in hooks) or hooks and not any(d["rom"] for d in devices):
        raise ValueError("This VM uses a custom hook sidecar; configure its dependencies separately before transferring it")
    if not devices:
        if choices:
            raise ValueError("This VM has no passthrough devices to map")
        return vm, [], [], []
    if "vbios" not in definition:
        raise ValueError("Update Homestead on the source before transferring passthrough devices")
    choices = choices or {}
    if not isinstance(choices, dict) or set(choices) - {d["name"] for d in devices}:
        raise ValueError("Device mappings must match the source VM's devices")
    available = PASS.resources()["resources"]
    resources = {r["resource"]: r for r in available}
    cfg = {"map": {}, "remove": [], "roms": {}}
    common_hosts = None
    for device in devices:
        name = device["name"]
        choice = choices.get(name)
        if not isinstance(choice, dict) or "resource" not in choice:
            raise ValueError(f"Choose a destination device or Leave out for {name}")
        resource = choice["resource"]
        if not isinstance(resource, str):
            raise ValueError(f"Choose a valid destination device or Leave out for {name}")
        if not resource:
            cfg["remove"].append(name)
            continue
        target = resources.get(resource)
        if not target or not target.get("nodes"):
            raise ValueError(f"The destination device for {name} is not offered by a host; prepare it under Nodes → Hardware")
        if device["gpu"] and target["kind"] != "pci":
            raise ValueError(f"GPU {name} needs a PCI device")
        cfg["map"][name] = resource
        hosts = set(target["nodes"])
        common_hosts = hosts if common_hosts is None else common_hosts & hosts
        rom = choice.get("rom", "source")
        data = (definition["vbios"].get(name) or "") if rom == "source" else rom
        if data and target["kind"] == "usb":
            raise ValueError(f"USB device {name} cannot use a vBIOS file")
        cfg["roms"][name] = data
    if common_hosts is not None and not common_hosts:
        raise ValueError("No destination host offers all the selected passthrough devices; choose devices on the same host")
    roms = {name: PASS.check_rom(data) for name, data in definition["vbios"].items()}
    effects = []
    PASS.edit_vm(vm, namespace, cfg, effects, roms)
    # A physical host name from another cluster must not pin the destination.
    spec = vm["spec"]["template"]["spec"]
    spec.get("nodeSelector", {}).pop("kubernetes.io/hostname", None)
    spec.pop("nodeName", None)
    return vm, effects, devices, sorted(common_hosts or [])


def export(vm, read):
    devices = PASS.vm_devices(vm)
    if not any(d["rom"] for d in devices):
        return {}
    path = f"/api/v1/namespaces/{vm['metadata']['namespace']}/configmaps/{vm['metadata']['name']}-vbios"
    roms = PASS.roms_from_configmap(vm, read(path))
    return {name: base64.b64encode(data).decode() for name, data in roms.items()}
