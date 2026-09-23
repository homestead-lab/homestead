"""Which namespaces are yours, and making and removing them.

A Harvester cluster is full of namespaces that belong to Harvester, Rancher,
Longhorn and Kubernetes itself - dozens, several named after generated IDs.
None is a place to put an app, so pickers offer only the rest. A system
namespace is known by name, by the patterns Rancher generates, or by the
annotation Rancher puts on the ones it considers its own.
"""
import re
import time
import urllib.error

kget = ksend = None
DEFAULT_NS = "lab"
OWN_NS = "lab"
SYSTEM = {"kube-system", "kube-public", "kube-node-lease", "local", "cdi", "kube-ovn", "fleet-local",
          "fleet-default", "fleet-system", "forklift", "longhorn-system", "rancher-operator-system"}
SYSTEM_PREFIXES = ("kube-", "cattle-", "harvester-", "longhorn-", "fleet-", "rancher-", "cluster-fleet-",
                   "calico-", "tigera-", "metallb-", "cert-manager", "kubevirt")
# Names Rancher generates: projects (p-), users (u-, user-), clusters (c-).
GENERATED = re.compile(r"^(?:p|u|c)-[a-z0-9]{5}$|^user-[a-z0-9]{5}$|^c-m-[a-z0-9]{8}$")
NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


def bind(_kget, _ksend, default_ns, own_ns):
    global kget, ksend, DEFAULT_NS, OWN_NS
    kget, ksend, DEFAULT_NS, OWN_NS = _kget, _ksend, default_ns, own_ns


def is_system(namespace):
    meta = namespace.get("metadata") or {}
    name = meta.get("name", "")
    annotations = meta.get("annotations") or {}
    return (name in SYSTEM or name.startswith(SYSTEM_PREFIXES) or bool(GENERATED.match(name))
            or annotations.get("management.cattle.io/system-namespace") == "true")


def _all():
    return kget("/api/v1/namespaces").get("items", [])


def names(include_system=False):
    """Namespace names for a picker: yours, unless all are asked for."""
    return sorted(n["metadata"]["name"] for n in _all() if include_system or not is_system(n))


def _count(path, namespace):
    try:
        return len(kget(f"{path.format(ns=namespace)}").get("items", []))
    except Exception:
        return 0


def _contents(namespace):
    return {
        "deployments": _count("/apis/apps/v1/namespaces/{ns}/deployments", namespace),
        "statefulsets": _count("/apis/apps/v1/namespaces/{ns}/statefulsets", namespace),
        "volumes": _count("/api/v1/namespaces/{ns}/persistentvolumeclaims", namespace),
        "vms": _count("/apis/kubevirt.io/v1/namespaces/{ns}/virtualmachines", namespace),
    }


def _protected(name):
    if name == DEFAULT_NS:
        return "new workloads go here by default"
    if name == OWN_NS:
        return "Homestead itself runs here"
    if name == "default":
        return "Kubernetes' own default namespace"
    return ""


def inventory():
    """Your namespaces, what is in each, and whether one can go."""
    rows = []
    for n in _all():
        if is_system(n):
            continue
        meta = n.get("metadata") or {}
        name = meta["name"]
        contents = _contents(name)
        rows.append({"name": name, "created": meta.get("creationTimestamp", ""),
                     "phase": (n.get("status") or {}).get("phase", ""),
                     "homestead": (meta.get("labels") or {}).get("homestead.io/managed") == "true",
                     "protected": _protected(name), **contents,
                     "empty": not any(contents.values())})
    hidden = sum(1 for n in _all() if is_system(n))
    return {"namespaces": sorted(rows, key=lambda r: r["name"]), "system_hidden": hidden,
            "default": DEFAULT_NS}


def create(name):
    name = str(name or "").strip().lower()
    if not NAME.fullmatch(name):
        raise ValueError("a namespace name is up to 63 lowercase letters, numbers and dashes")
    if name in SYSTEM or name.startswith(SYSTEM_PREFIXES) or GENERATED.match(name):
        raise ValueError(f"{name} looks like a Harvester, Rancher or Kubernetes system namespace")
    try:
        ksend("POST", "/api/v1/namespaces", {"apiVersion": "v1", "kind": "Namespace",
                                             "metadata": {"name": name, "labels": {"homestead.io/managed": "true"}}})
    except urllib.error.HTTPError as error:
        if error.code == 409:
            raise ValueError(f"{name} already exists")
        raise
    return {"ok": True, "name": name}


def delete(name, confirm=""):
    name = str(name or "").strip()
    namespace = next((n for n in _all() if n["metadata"]["name"] == name), None)
    if not namespace:
        raise ValueError(f"there is no namespace called {name}")
    if is_system(namespace):
        raise ValueError(f"{name} belongs to the platform, not to you")
    reason = _protected(name)
    if reason:
        raise ValueError(f"{name} cannot be removed: {reason}")
    contents = _contents(name)
    held = [f"{count} {kind}" for kind, count in contents.items() if count]
    if held:
        raise ValueError(f"{name} still holds {', '.join(held)} - move or delete them first")
    if confirm != name:
        raise ValueError(f"type {name} to confirm")
    ksend("DELETE", f"/api/v1/namespaces/{name}")
    return {"ok": True, "name": name, "at": int(time.time())}
