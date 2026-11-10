"""After a host restart: whether the cluster is ready for the next one (#1290).

A host restart moves apps and volume copies onto the other hosts, and its job
ends once the host is back. The apps it moved can still be starting elsewhere
then, and copies still rebuilding. The Hosts page says "Ready for the next host
restart" once every app the last restart moved runs again and no volume is
short of copies; the next restart's review warns while that is not true. It
is a warning, never a block.

"The last restart": the newest host restart job (node-power, reboot) that has
ended, within the last RECENT seconds; older, and nothing is said. What it
moved is what its job recorded: the apps its review found on the host
(plan.workloads), what it stopped to wait for the host (held), and the VMs that
ran there (plan.vms). Pods it drained that belong to nothing it recorded are not
followed. Reads are lists, read once per answer."""
import calendar
import time
import urllib.error

import homestead_operations as OPS

RECENT = 24 * 3600
APPS = "/apis/apps/v1"
VMS = "/apis/kubevirt.io/v1"
LONGHORN_VOLUMES = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system/volumes"
# A VM its owner means to be stopped is not waited for.
VM_STOPPED = ("Halted", "Manual")


def _ended(item):
    try:
        return calendar.timegm(time.strptime(item.get("finished_at") or "", "%Y-%m-%dT%H:%M:%SZ"))
    except ValueError:
        return None


def last_restart(jobs, now=None):
    """The newest ended host restart within RECENT, or None."""
    now = time.time() if now is None else now
    ended = [(_ended(item), item) for item in jobs
             if item.get("kind") == "node-power" and (item.get("ref") or {}).get("action") == "reboot"
             and item.get("status") in OPS.TERMINAL]
    ended = [(at, item) for at, item in ended if at is not None and now - at <= RECENT]
    return max(ended, key=lambda row: row[0])[1] if ended else None


def moved(item):
    """(kind, namespace, name) of each app the restart moved, as its job recorded them."""
    ref = item.get("ref") or {}
    plan = ref.get("plan") or {}
    out = {("Deployment", w.get("ns"), w.get("name")) for w in plan.get("workloads") or [] if w.get("ns") and w.get("name")}
    out |= {(h.get("kind"), h.get("ns"), h.get("name")) for h in ref.get("held") or []
            if h.get("kind") in ("Deployment", "StatefulSet", "VirtualMachine") and h.get("ns") and h.get("name")}
    for vm in plan.get("vms") or []:
        namespace, _, name = str(vm).partition("/")
        if namespace and name:
            out.add(("VirtualMachine", namespace, name))
    return out


def _list(read, path, optional=False):
    try:
        return read(path).get("items", [])
    except urllib.error.HTTPError as error:
        if optional and error.code == 404:
            return []
        raise


def _by_name(rows):
    return {((row.get("metadata") or {}).get("namespace"), (row.get("metadata") or {}).get("name")): row for row in rows}


def _scaled_running(obj):
    want = (obj.get("spec") or {}).get("replicas")
    want = 1 if want is None else want
    return ((obj.get("status") or {}).get("readyReplicas") or 0) >= want


def _vm_running(vm, vmi):
    spec = vm.get("spec") or {}
    if spec.get("running") is False or spec.get("runStrategy") in VM_STOPPED:
        return True  # stopped on purpose: nothing to wait for
    return ((vmi or {}).get("status") or {}).get("phase") == "Running"


def not_running(apps, read, vmis=None):
    """namespace/name of each moved app that is not running again. One gone
    since, or scaled to zero, is not waited for."""
    kinds = {kind for kind, _, _ in apps}
    deployments = _by_name(_list(read, f"{APPS}/deployments")) if "Deployment" in kinds else {}
    statefulsets = _by_name(_list(read, f"{APPS}/statefulsets")) if "StatefulSet" in kinds else {}
    vms = instances = {}
    if "VirtualMachine" in kinds:
        vms = _by_name(_list(read, f"{VMS}/virtualmachines", optional=True))
        instances = _by_name(vmis if vmis is not None else _list(read, f"{VMS}/virtualmachineinstances", optional=True))
    waiting = set()
    for kind, namespace, name in apps:
        key = (namespace, name)
        if kind == "VirtualMachine":
            if key in vms and not _vm_running(vms[key], instances.get(key)):
                waiting.add(f"{namespace}/{name}")
            continue
        obj = (deployments if kind == "Deployment" else statefulsets).get(key)
        if obj is not None and not _scaled_running(obj):
            waiting.add(f"{namespace}/{name}")
    return sorted(waiting)


def short_of_copies(read, volumes=None):
    """namespace/claim of each Longhorn volume with fewer whole copies than it asks for."""
    rows = volumes if volumes is not None else _list(read, LONGHORN_VOLUMES, optional=True)
    out = []
    for volume in rows:
        status = volume.get("status") or {}
        if status.get("robustness") in ("degraded", "faulted"):
            claim = status.get("kubernetesStatus") or {}
            out.append("/".join(x for x in (claim.get("namespace"), claim.get("pvcName")) if x)
                       or (volume.get("metadata") or {}).get("name", ""))
    return sorted(out)


def state(read, jobs, now=None, volumes=None, vmis=None):
    """{"node": None} with no recent host restart, else the host it restarted,
    what it moved that is not running yet, the volumes short of copies, and
    whether the cluster is ready for the next one."""
    item = last_restart(jobs, now)
    if item is None:
        return {"node": None}
    waiting = not_running(moved(item), read, vmis)
    short = short_of_copies(read, volumes)
    return {"node": (item.get("ref") or {}).get("node"), "finished_at": item.get("finished_at"),
            "not_running": waiting, "short_of_copies": short, "ready": not waiting and not short}


def _count(n, one, many):
    return f"{n} {one if n == 1 else many}"


def _named(names, shown=5):
    more = len(names) - shown
    return ", ".join(names[:shown]) + (f" and {more} more" if more > 0 else "")


def warning(answer):
    """The review's warning while the last restart has not settled, or ""."""
    if not answer.get("node") or answer.get("ready"):
        return ""
    words = ["The cluster is not ready for the next host restart."]
    waiting, short = answer["not_running"], answer["short_of_copies"]
    if waiting:
        words.append(f"{_count(len(waiting), 'app', 'apps')} moved by the restart of {answer['node']} "
                     f"{'is' if len(waiting) == 1 else 'are'} not running yet: {_named(waiting)}.")
    if short:
        words.append(f"{_count(len(short), 'volume is', 'volumes are')} short of copies: {_named(short)}.")
    words.append("Restarting another host now can stop at the drain, or leave a volume with fewer copies.")
    return " ".join(words)
