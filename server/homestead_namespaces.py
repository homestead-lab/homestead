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


def _running_unowned(pod):
    """A pod no app, stateful set or daemon set owns: one started by hand or
    by a job, still running or waiting to."""
    owners = {o.get("kind") for o in (pod.get("metadata") or {}).get("ownerReferences") or []}
    return ((pod.get("status") or {}).get("phase") in ("Pending", "Running", "Unknown")
            and not owners & {"ReplicaSet", "StatefulSet", "DaemonSet"})


# What deleting a namespace would delete or stop, as the delete review words
# each: (key, list path, which items count, one, several).
KINDS = (
    ("deployments", "/apis/apps/v1/namespaces/{ns}/deployments", None, "app", "apps"),
    ("statefulsets", "/apis/apps/v1/namespaces/{ns}/statefulsets", None, "stateful set", "stateful sets"),
    ("daemonsets", "/apis/apps/v1/namespaces/{ns}/daemonsets", None, "daemon set", "daemon sets"),
    ("cronjobs", "/apis/batch/v1/namespaces/{ns}/cronjobs", None, "scheduled job", "scheduled jobs"),
    ("pods", "/api/v1/namespaces/{ns}/pods", _running_unowned, "running pod", "running pods"),
    ("volumes", "/api/v1/namespaces/{ns}/persistentvolumeclaims", None, "volume", "volumes"),
    ("vms", "/apis/kubevirt.io/v1/namespaces/{ns}/virtualmachines", None, "VM", "VMs"),
    ("disks", "/apis/cdi.kubevirt.io/v1beta1/namespaces/{ns}/datavolumes", None, "VM disk", "VM disks"),
)


def _contents(namespace):
    """How many of each kind the namespace holds, and the kinds that could not
    be read. Not found means the cluster has no such API (no KubeVirt, say):
    there are none. Any other failure is not "none": a namespace whose
    contents can't be read is never offered for deletion (#599)."""
    counts, unread = {}, []
    for key, path, keep, _one, _many in KINDS:
        try:
            items = kget(path.format(ns=namespace)).get("items", [])
        except urllib.error.HTTPError as error:
            if error.code != 404:
                unread.append(key)
            items = []
        except Exception:
            unread.append(key)
            items = []
        counts[key] = sum(1 for item in items if keep is None or keep(item))
    return counts, unread


def _all_contents():
    """_contents for every namespace at once: one cluster-wide list of each
    kind, grouped by namespace, so the page costs the same eight lists for
    two namespaces or fifty (#985). Returns (counts by namespace, kinds no
    namespace could be read for)."""
    counts, unread = {}, []
    for key, path, keep, _one, _many in KINDS:
        try:
            items = kget(path.replace("/namespaces/{ns}", "")).get("items", [])
        except urllib.error.HTTPError as error:
            if error.code != 404:
                unread.append(key)
            items = []
        except Exception:
            unread.append(key)
            items = []
        for item in items:
            if keep is None or keep(item):
                where = counts.setdefault((item.get("metadata") or {}).get("namespace", ""), {})
                where[key] = where.get(key, 0) + 1
    return counts, unread


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
    rows, every = [], _all()
    held, unread = _all_contents()
    for n in every:
        if is_system(n):
            continue
        meta = n.get("metadata") or {}
        name = meta["name"]
        contents = {key: held.get(name, {}).get(key, 0) for key, *_rest in KINDS}
        rows.append({"name": name, "created": meta.get("creationTimestamp", ""),
                     "phase": (n.get("status") or {}).get("phase", ""),
                     "homestead": (meta.get("labels") or {}).get("homestead.io/managed") == "true",
                     "protected": _protected(name), **contents, "unread": list(unread),
                     "empty": not unread and not any(contents.values())})
    hidden = sum(1 for n in every if is_system(n))
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
    contents, unread = _contents(name)
    if unread:
        raise ValueError(f"could not check {', '.join(unread)} in {name}, so nothing was deleted; try again")
    held = [f"{contents[key]} {one if contents[key] == 1 else many}"
            for key, _path, _keep, one, many in KINDS if contents[key]]
    if held:
        raise ValueError(f"{name} still holds {', '.join(held)} - move or delete them first")
    if confirm != name:
        raise ValueError(f"type {name} to confirm")
    ksend("DELETE", f"/api/v1/namespaces/{name}")
    return {"ok": True, "name": name, "at": int(time.time())}


# Its routes and who may use them (homestead_routes.py).
def _refusing(fn):
    """A refusal is a 409 with its reason, as it always was."""
    def route(request):
        import homestead_routes as ROUTER
        try:
            return fn(request.body)
        except (ValueError, PermissionError) as error:
            return ROUTER.Reply(409, {"error": str(error)})
    return route


ROUTES = {
    ("POST", "/api/namespaces/create"): ("admin", _refusing(lambda b: create(b.get("name")))),
    ("POST", "/api/namespaces/delete"): ("admin", _refusing(lambda b: delete(b.get("name"), str(b.get("confirm") or "")))),
    ("GET", "/api/namespaces/manage"): ("viewer", lambda request: inventory()),
    # Places to put an app: Harvester's, Rancher's and Kubernetes'
    # own namespaces are left out unless all are asked for.
    ("GET", "/api/namespaces"): ("viewer", lambda request: names((request.query.get("all") or [""])[0] == "1")),
}
