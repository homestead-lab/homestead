"""What happens to each app and VM on a host that is going down: it moves to
another host, or it stops and waits for this one.

Moving is the drain: Kubernetes starts the app elsewhere and KubeVirt live-
migrates the VM. Waiting is for what cannot or should not move - an app on a
single host, one pinned to this host's hardware, a VM that cannot migrate:
Homestead stops it cleanly before the host goes (a Deployment or StatefulSet
scaled to none, a VM shut down from inside), so its volumes detach in good
order instead of with the power, and starts it again when the host is back.

What was stopped is written on the object itself as well as on the job: the
replicas or run strategy it had, and which job stopped it. Restoring checks
that mark, so it never starts something someone has since changed, and works
even if the job's record is lost.
"""
import urllib.error
import urllib.parse

HELD_BY = "homestead.io/held-for-power"
HELD_AS = "homestead.io/held-as"
SYSTEM = ("kube-", "cattle-", "harvester-", "longhorn-", "fleet-")
SYSTEM_NS = {"kube-system", "longhorn-system", "kubevirt", "cdi", "system-upgrade", "local"}
SUB = "/apis/subresources.kubevirt.io/v1"
VM_API = "/apis/kubevirt.io/v1"

kget = ksend = None
OWN = ("", "")          # Homestead's own namespace and Deployment: never stopped


def bind(_kget, _ksend, own=("", "")):
    global kget, ksend, OWN
    kget, ksend, OWN = _kget, _ksend, tuple(own)


def _q(text):
    return urllib.parse.quote(str(text), safe="")


def _system(ns):
    return ns in SYSTEM_NS or ns.startswith(SYSTEM)


def _owner(meta, kind):
    return next((o for o in meta.get("ownerReferences") or []
                 if o.get("controller") and o.get("kind") == kind and o.get("name")), None)


def _workload(pod, replicasets):
    """(kind, namespace, name) of the Deployment or StatefulSet running a pod."""
    meta = pod.get("metadata") or {}
    ns = meta.get("namespace", "")
    owner = _owner(meta, "StatefulSet")
    if owner:
        return "StatefulSet", ns, owner["name"]
    owner = _owner(meta, "ReplicaSet")
    if owner:
        rs = replicasets.get((ns, owner["name"])) or {}
        deployment = _owner(rs.get("metadata") or {}, "Deployment")
        if deployment:
            return "Deployment", ns, deployment["name"]
    return None


def candidates(node, pods, vmis, replicasets, ready_hosts, eligible=None, single_host=False):
    """Each app and VM on the host, what it may do, and what it does by default.

    eligible: Homestead's placement view, {(ns, name): [hosts]} for the
    Deployments it knows; anything else may move if another host is Ready."""
    eligible = eligible or {}
    others = sorted(h for h in ready_hosts if h != node)
    running = [p for p in pods if (p.get("status") or {}).get("phase") not in ("Succeeded", "Failed")
               and not (p.get("metadata") or {}).get("deletionTimestamp")]
    found = {}
    for pod in running:
        meta = pod.get("metadata") or {}
        if _system(meta.get("namespace", "")) or (meta.get("labels") or {}).get("kubevirt.io") == "virt-launcher":
            continue
        workload = _workload(pod, replicasets)
        if not workload or workload[1:] == OWN:
            continue
        row = found.setdefault(workload, {"here": 0, "elsewhere": 0})
        row["here" if (pod.get("spec") or {}).get("nodeName") == node else "elsewhere"] += 1
    items = []
    for (kind, ns, name), row in sorted(found.items()):
        if not row["here"]:
            continue
        hosts = eligible.get((ns, name), others) if kind == "Deployment" else others
        can_move = bool(hosts) and not single_host
        # Waiting stops every replica, so only where every one is on this host.
        options = (["move"] if can_move else []) + (["wait"] if not row["elsewhere"] else [])
        if not options:
            options = ["move"]
        items.append({"id": f"{kind}/{ns}/{name}", "kind": kind, "ns": ns, "name": name,
                      "here": row["here"], "elsewhere": row["elsewhere"], "hosts": hosts,
                      "options": options, "default": "move" if can_move or "wait" not in options else "wait",
                      "why": "" if can_move else "the only host" if single_host else "no other host it can run on"})
    for vmi in vmis:
        meta, status = vmi.get("metadata") or {}, vmi.get("status") or {}
        if status.get("nodeName") != node or status.get("phase") in ("Succeeded", "Failed"):
            continue
        owned = bool(_owner(meta, "VirtualMachine"))
        migratable = any(c.get("type") == "LiveMigratable" and c.get("status") == "True"
                         for c in status.get("conditions") or [])
        can_move = migratable and bool(others) and not single_host
        options = (["move"] if can_move else []) + (["wait"] if owned else [])
        why = ("the only host" if single_host else "" if can_move else
               "it cannot live-migrate" + (f": {next((c.get('message') for c in status.get('conditions') or [] if c.get('type') == 'LiveMigratable'), '')}" if not migratable else "")
               if others else "no other host")
        items.append({"id": f"VirtualMachine/{meta.get('namespace', '')}/{meta.get('name', '')}", "kind": "VirtualMachine",
                      "ns": meta.get("namespace", ""), "name": meta.get("name", ""), "here": 1, "elsewhere": 0,
                      "hosts": others if can_move else [], "options": options,
                      "default": "move" if can_move else "wait" if owned else "",
                      "why": why if options else "not managed by a VirtualMachine; stop it by hand"})
    return items


def choose(items, choices):
    """Each item's choice: the one asked for if it is allowed, else its default."""
    out = {}
    for item in items:
        pick = (choices or {}).get(item["id"]) or item["default"]
        if pick not in item["options"]:
            raise ValueError(f"{item['kind']} {item['ns']}/{item['name']} cannot {pick or 'be left'} here: {item['why'] or 'review again'}")
        out[item["id"]] = pick
    return out


def _path(kind, ns, name):
    if kind == "VirtualMachine":
        return f"{VM_API}/namespaces/{_q(ns)}/virtualmachines/{_q(name)}"
    plural = "deployments" if kind == "Deployment" else "statefulsets"
    return f"/apis/apps/v1/namespaces/{_q(ns)}/{plural}/{_q(name)}"


def _strategy(vm):
    spec = vm.get("spec") or {}
    if spec.get("runStrategy"):
        return spec["runStrategy"]
    return "running" if spec.get("running") else "Halted"


def stop(item, job):
    """Stop one app or VM to wait for its host, marking what it was."""
    obj = kget(_path(item["kind"], item["ns"], item["name"]))
    meta = obj.get("metadata") or {}
    mark = (meta.get("annotations") or {}).get(HELD_BY)
    if mark and mark != job:
        raise ValueError(f"{item['ns']}/{item['name']} is already held by another host power job")
    if item["kind"] == "VirtualMachine":
        was = (meta.get("annotations") or {}).get(HELD_AS) if mark == job else _strategy(obj)
        ksend("PATCH", _path(item["kind"], item["ns"], item["name"]),
              {"metadata": {"annotations": {HELD_BY: job, HELD_AS: was}}}, ctype="application/merge-patch+json")
        if _strategy(obj) != "Halted":
            # Shut down from inside: the guest's own ACPI shutdown, within its grace period.
            ksend("PUT", f"{SUB}/namespaces/{_q(item['ns'])}/virtualmachines/{_q(item['name'])}/stop", {})
        return {**item, "was": was}
    replicas = int((obj.get("spec") or {}).get("replicas", 1) or 0)
    was = (meta.get("annotations") or {}).get(HELD_AS) if mark == job else str(replicas)
    ksend("PATCH", _path(item["kind"], item["ns"], item["name"]),
          {"metadata": {"annotations": {HELD_BY: job, HELD_AS: was}}, "spec": {"replicas": 0}},
          ctype="application/merge-patch+json")
    return {**item, "was": was}


def gone(item, node, pods, vmis):
    """Whether a stopped app or VM has left the host."""
    if item["kind"] == "VirtualMachine":
        return not any((v.get("metadata") or {}).get("namespace") == item["ns"] and (v.get("metadata") or {}).get("name") == item["name"]
                       for v in vmis)
    return not any(_held_pod(p, item) for p in pods)


def _held_pod(pod, item):
    meta = pod.get("metadata") or {}
    if meta.get("namespace") != item["ns"]:
        return False
    owner = _owner(meta, "StatefulSet" if item["kind"] == "StatefulSet" else "ReplicaSet")
    if not owner:
        return False
    if item["kind"] == "StatefulSet":
        return owner["name"] == item["name"]
    # A ReplicaSet is named after its Deployment and a template hash.
    return owner["name"].rsplit("-", 1)[0] == item["name"]


def migrate(item):
    """Live-migrate a VM that is moving; KubeVirt picks the host."""
    ksend("POST", f"{VM_API}/namespaces/{_q(item['ns'])}/virtualmachineinstancemigrations",
          {"apiVersion": "kubevirt.io/v1", "kind": "VirtualMachineInstanceMigration",
           "metadata": {"generateName": f"{item['name'][:40]}-host-", "namespace": item["ns"]},
           "spec": {"vmiName": item["name"]}})


def restore(held, job):
    """Start again what this job stopped, as it was. Skips anything whose
    mark has gone or changed - someone started or changed it since - and
    returns what it started and what it left."""
    started, left = [], []
    for item in held:
        path = _path(item["kind"], item["ns"], item["name"])
        try:
            obj = kget(path)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                left.append(f"{item['ns']}/{item['name']} (deleted)")
                continue
            raise
        notes = (obj.get("metadata") or {}).get("annotations") or {}
        if notes.get(HELD_BY) != job:
            left.append(f"{item['ns']}/{item['name']} (changed since)")
            continue
        was = notes.get(HELD_AS, item.get("was", ""))
        unmark = {HELD_BY: None, HELD_AS: None}
        if item["kind"] == "VirtualMachine":
            spec = ({"running": True} if was == "running" else
                    {"runStrategy": was} if was and was not in ("Halted", "Manual") else {})
            ksend("PATCH", path, {"metadata": {"annotations": unmark}, **({"spec": spec} if spec else {})},
                  ctype="application/merge-patch+json")
            if was == "Manual":
                # A manual VM starts only when asked to.
                ksend("PUT", f"{SUB}/namespaces/{_q(item['ns'])}/virtualmachines/{_q(item['name'])}/start", {})
        else:
            replicas = int(was) if str(was).isdigit() else 1
            ksend("PATCH", path, {"metadata": {"annotations": unmark}, "spec": {"replicas": replicas}},
                  ctype="application/merge-patch+json")
        started.append(f"{item['ns']}/{item['name']}")
    return started, left
