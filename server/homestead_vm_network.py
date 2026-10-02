"""Read-only VM network admission evidence, not a CNI readiness guarantee.

KubeVirt renderresources/network render.go (v1.3.1 and v1.9.0) request one
annotated resource per Multus network, and tun/vhost-net for launcher networking.
Pin every read NAD by UID/version; never copy CNI config or plugin images into
public plans. Physical bridges, CNI binaries and link health remain separate.
"""
import re
from urllib.parse import quote

import homestead_pod_resources as RESOURCES

ISOLATED = "homestead.io/network-isolated"

RESOURCE = "k8s.v1.cni.cncf.io/resourceName"
DNS = re.compile(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?")
QUALIFIED = re.compile(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?/[A-Za-z0-9](?:[A-Za-z0-9_.-]*[A-Za-z0-9])?")


def isolated(vm):
    return ((vm.get("metadata") or {}).get("annotations") or {}).get(ISOLATED) == "true"


def isolation_requested(cfg, default=False):
    if "isolated" in cfg and type(cfg["isolated"]) is not bool:
        raise ValueError("Isolated VM must be a checkbox value")
    return cfg.get("isolated", default)


def apply_isolation(vm, cfg):
    """Persist explicit isolation; a form/API cannot silently add NICs to it."""
    enabled = isolation_requested(cfg, isolated(vm))
    if enabled and (cfg.get("add_nics") or any(not row.get("remove") for row in cfg.get("nics") or [])):
        raise ValueError("Clear Isolated VM before adding or configuring network cards")
    spec = vm["spec"]["template"]["spec"]
    devices = spec["domain"].get("devices") or {}
    annotations = (vm.get("metadata") or {}).get("annotations") or {}
    before = (annotations.get(ISOLATED), devices.get("autoattachPodInterface"),
              devices.get("interfaces"), spec.get("networks"))
    if enabled:
        vm.setdefault("metadata", {}).setdefault("annotations", {})[ISOLATED] = "true"
        annotations = vm["metadata"]["annotations"]
        devices = spec["domain"].setdefault("devices", {})
        devices["autoattachPodInterface"] = False
        devices["interfaces"], spec["networks"] = [], []
    elif "isolated" in cfg:
        annotations.pop(ISOLATED, None)
    after = (annotations.get(ISOLATED), devices.get("autoattachPodInterface"),
             devices.get("interfaces"), spec.get("networks"))
    return before != after


def evidence(spec, namespace, configuration, read=None):
    devices = (spec.get("domain") or {}).get("devices") or {}
    interfaces = devices.get("interfaces") or []
    networks = spec.get("networks") or []
    config = configuration or {}
    result = {"requests": {}, "dependencies": {}, "blockers": [], "warnings": [],
              "sidecars": 0, "memory_overhead": 0}
    requests, blockers = result["requests"], result["blockers"]
    if interfaces or devices.get("autoattachPodInterface") is not False:
        requests["devices.kubevirt.io/tun"] = 1
    implicit = not interfaces and not networks and devices.get("autoattachPodInterface") is not False
    if config.get("developerConfiguration", {}).get("useEmulation") is not True and (
            implicit or any(row.get("model", "virtio") in ("", "virtio") for row in interfaces)):
        requests["devices.kubevirt.io/vhost-net"] = 1
    names = [row.get("name") for row in networks]
    iface_names = [row.get("name") for row in interfaces]
    if len(names) != len(set(names)) or len(iface_names) != len(set(iface_names)):
        blockers.append("VM network/interface names must be unique before device allocation can be checked")
    by_name = {row.get("name"): row for row in interfaces}
    cache = {}

    def attachment(reference):
        if not isinstance(reference, str):
            blockers.append("VM network attachment reference is invalid")
            return None
        parts = reference.split("/") if "/" in reference else [namespace, reference]
        if len(parts) != 2 or any(not DNS.fullmatch(part) for part in parts):
            blockers.append("VM network attachment reference is invalid")
            return None
        ns, name = parts
        path = f"/apis/k8s.cni.cncf.io/v1/namespaces/{quote(ns, safe='')}/network-attachment-definitions/{quote(name, safe='')}"
        if path in cache:
            return cache[path]
        cache[path] = None
        try:
            value = read(path) if callable(read) else None
            meta = (value or {}).get("metadata") or {}
            if (meta.get("name") != name or meta.get("namespace") != ns or not meta.get("uid")
                    or not meta.get("resourceVersion") or meta.get("deletionTimestamp")):
                raise ValueError("Unverified attachment identity")
            result["dependencies"][path] = {key: meta.get(key) for key in ("namespace", "name", "uid", "resourceVersion")}
            resource = (meta.get("annotations") or {}).get(RESOURCE) or ""
            if resource and (not isinstance(resource, str) or not QUALIFIED.fullmatch(resource)):
                raise ValueError("Unresolved device resource")
            cache[path] = resource
            result["warnings"].append(f"LAN network {ns}/{name} is defined; physical links, bridge and CNI readiness still need host validation")
            return resource
        except Exception:
            blockers.append(f"LAN network {ns}/{name} identity/device requirements could not be verified")
            return None

    for network in networks:
        iface = by_name.get(network.get("name")) or {}
        if network.get("resourceClaim"):
            blockers.append("VM network uses dynamic resource allocation; resolved claim/device placement must be checked before starting")
        if "multus" in network:
            resource = attachment((network.get("multus") or {}).get("networkName"))
            if resource:
                requests[resource] = requests.get(resource, 0) + 1
            if "sriov" in iface and not resource:
                blockers.append("SR-IOV network has no verified device resource annotation")
        if network.get("name") not in by_name and interfaces:
            blockers.append("VM network has no matching interface")
    for iface in interfaces:
        if iface.get("name") not in names:
            blockers.append("VM interface has no matching network")

    plugins = (config.get("network") or {}).get("binding") or {}
    seen = set()
    for iface in interfaces:
        binding = iface.get("binding")
        if binding is None:
            continue
        name = binding.get("name") if isinstance(binding, dict) else None
        plugin = plugins.get(name) if isinstance(name, str) else None
        if not name or not isinstance(plugin, dict):
            blockers.append("VM network binding plugin is not registered in KubeVirt configuration")
            continue
        if plugin.get("networkAttachmentDefinition"):
            resource = attachment(plugin["networkAttachmentDefinition"])
            # Additional binding NADs may be resource-injected by a webhook.
            # Include that demand, but do not claim to reproduce admission.
            if resource:
                requests[resource] = requests.get(resource, 0) + 1
                result["warnings"].append("Binding-network devices include webhook-dependent allocation; review the resulting launcher")
        if name not in seen:
            seen.add(name)
            result["sidecars"] += bool(plugin.get("sidecarImage"))
            overhead = (plugin.get("computeResourceOverhead") or {}).get("requests") or {}
            try:
                result["memory_overhead"] += RESOURCES.quantity(overhead.get("memory"))
            except (ValueError, TypeError):
                blockers.append("Network binding memory overhead is invalid")
            if set(overhead) - {"memory"}:
                result["warnings"].append("Network binding declares non-memory overhead; version-dependent launcher admission may add resources")
    return result
