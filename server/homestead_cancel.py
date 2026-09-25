"""Cancelling a job: what it stops, what it puts back, and what it cannot.

Every job in the Activity tray can be cancelled. For each kind this says,
before anything changes, what a cancel would do - the plan the person reads -
and then does it:

  rollback  puts things back as they were before the job: a deploy removed,
            a rollout returned to the version before it, a volume move
            started again on its untouched original, a k3s cluster's VMs,
            disks and addresses removed;
  stop      halts it and keeps what is already done, when what is done
            cannot be taken back - a node that has already dropped an image;
  forget    for the few things Kubernetes cannot take back once asked - a
            claim marked for deletion - it stops being tracked here, and the
            plan says plainly that it carries on.

A job part-way through a step that must not be interrupted - a volume swap,
Homestead switching onto its moved data - is refused until the step is done.

Only what the job made is removed: objects are checked for the job's label or
name before they are deleted, and volumes a job borrowed are never touched.
Each step is safe to repeat, as a cancel stopped part-way is asked again.
"""
import calendar
import copy
import time
import urllib.error
import urllib.parse

import homestead_disks as DISKS
import homestead_helm as HELM
import homestead_imports as IMP
import homestead_ipam as IPAM
import homestead_k3scluster as K3SC
import homestead_longhorn as LH
import homestead_move_engine as MOVE_ENGINE
import homestead_names as NAMES
import homestead_reclass as RECLASS
import homestead_restructure as RESTRUCTURE
import homestead_smart as SMART
import homestead_updates as UPDATES
import homestead_vms as VMS

kget = ksend = None
# The server's own ways of doing these, so a cancel does them the same way:
# deleting a workload also removes its Services and LAN network, and a
# finished restore's class is tidied away.
remove_workload = None
tidy_restore = None
REVISION = "deployment.kubernetes.io/revision"
LHNS = "longhorn-system"
LHAPI = "/apis/longhorn.io/v1beta2"
# A rollout this long before the job started is not the job's own.
CLOCK_SLACK = 120


def bind(_kget, _ksend, _remove_workload=None, _tidy_restore=None):
    global kget, ksend, remove_workload, tidy_restore
    kget, ksend = _kget, _ksend
    remove_workload, tidy_restore = _remove_workload, _tidy_restore


def _q(value):
    return urllib.parse.quote(str(value), safe="")


def _get(path):
    try:
        return kget(path)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


def _delete(path):
    """Delete, and say whether there was anything to delete."""
    try:
        ksend("DELETE", path)
        return True
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return False
        raise


def _stamp(value):
    try:
        return calendar.timegm(time.strptime(str(value or "")[:19], "%Y-%m-%dT%H:%M:%S"))
    except (TypeError, ValueError):
        return 0


def _s(count, word, plural=None):
    return f"{count} {word if count == 1 else plural or word + 's'}"


def _names(values):
    values = [str(v) for v in values if v]
    return ", ".join(values[:-1]) + (" and " if len(values) > 1 else "") + values[-1] if values else ""


# ---------------------------------------------------------------- rollouts
def _deployment_path(ns, name):
    return f"/apis/apps/v1/namespaces/{_q(ns)}/deployments/{_q(name)}"


def _revision(obj):
    try:
        return int(((obj.get("metadata") or {}).get("annotations") or {}).get(REVISION) or 0)
    except (TypeError, ValueError):
        return 0


def _images(template):
    return [c.get("image", "") for c in ((template or {}).get("spec") or {}).get("containers") or []]


def rollout_target(ns, name, since):
    """The version a Deployment ran before a job changed it: the ReplicaSet
    one revision back - but only when the current one was made by that job,
    or undoing it would undo someone else's change instead.

    Returns (deployment, replicaset or None, why not)."""
    dep = _get(_deployment_path(ns, name))
    if not dep:
        return None, None, f"{name} no longer exists"
    meta = dep.get("metadata") or {}
    labels = ((dep.get("spec") or {}).get("selector") or {}).get("matchLabels") or {}
    selector = _q(",".join(f"{k}={v}" for k, v in sorted(labels.items())))
    sets = (kget(f"/apis/apps/v1/namespaces/{_q(ns)}/replicasets?labelSelector={selector}").get("items", [])
            if labels else [])
    owned = [rs for rs in sets if any(o.get("uid") == meta.get("uid")
                                      for o in (rs.get("metadata") or {}).get("ownerReferences") or [])]
    current = _revision(dep)
    now = next((rs for rs in owned if _revision(rs) == current), None)
    if not now or _stamp((now.get("metadata") or {}).get("creationTimestamp")) < _stamp(since) - CLOCK_SLACK:
        return dep, None, f"{name}'s current version was not made by this job"
    older = sorted((rs for rs in owned if 0 < _revision(rs) < current), key=_revision)
    if not older:
        return dep, None, f"Kubernetes kept no earlier version of {name}"
    return dep, older[-1], ""


def roll_back(ns, name, replicaset, replicas=None):
    """Put a Deployment's pod template back to a ReplicaSet's, as kubectl
    rollout undo does; with a replica count, it is started at that too."""
    dep = kget(_deployment_path(ns, name))
    template = copy.deepcopy((replicaset.get("spec") or {}).get("template") or {})
    (template.setdefault("metadata", {}).get("labels") or {}).pop("pod-template-hash", None)
    dep["spec"]["template"] = template
    if replicas is not None:
        dep["spec"]["replicas"] = int(replicas)
        (dep["metadata"].get("annotations") or {}).pop(RESTRUCTURE.HELD, None)
    dep["metadata"].pop("managedFields", None)
    ksend("PUT", _deployment_path(ns, name), dep)


# -------------------------------------------------------------- deployments
def _deployment_undo(item):
    ref = item.get("ref") or {}
    if ref.get("undo") in ("delete", "rollout", "keep"):
        return ref["undo"]
    # Jobs started by releases before this one say only their title.
    title = str(item.get("title") or "")
    if title.startswith(("Deploy ", "Install ")):
        return "delete"
    if title.startswith(("Create share ", "Update share ", "Remove share ")):
        return "keep"
    return "rollout"


def _claims_of(dep):
    spec = (((dep or {}).get("spec") or {}).get("template") or {}).get("spec") or {}
    return sorted({(v.get("persistentVolumeClaim") or {}).get("claimName")
                   for v in spec.get("volumes") or [] if v.get("persistentVolumeClaim")} - {None})


def deployment_plan(item):
    ref = item["ref"]
    ns, name = ref["namespace"], ref["name"]
    undo = _deployment_undo(item)
    if undo == "keep":
        return {"mode": "forget", "keeps": [
            f"The change is saved already: {name} carries on restarting with it. "
            "Edit or remove the share to change it back."]}
    if undo == "delete":
        dep = _get(_deployment_path(ns, name))
        if not dep:
            return {"mode": "stop", "keeps": [f"{name} no longer exists; there is nothing to remove"]}
        claims = _claims_of(dep)
        return {"mode": "rollback", "severity": "high",
                "undo": [f"Deletes {name} and stops its pods",
                         "Deletes the Services that point at it"],
                "keeps": ([f"Its {'volume' if len(claims) == 1 else 'volumes'} {_names(claims)} "
                           f"{'is' if len(claims) == 1 else 'are'} kept, with anything written so far; "
                           "delete them from Volumes if they are not wanted"] if claims else [])}
    dep, target, why = rollout_target(ns, name, item.get("started_at"))
    if not target:
        return {"mode": "forget", "keeps": [f"The change is saved in Kubernetes already ({why}); "
                                            f"edit {name} to change it back"]}
    return {"mode": "rollback", "severity": "high",
            "undo": [f"{name} goes back to the version it ran before this job "
                     f"(revision {_revision(target)}: {_names(_images(target['spec']['template']))})"],
            "keeps": [f"{name}'s pods restart once more, onto that version"]}


def deployment_cancel(item, _options):
    ref = item["ref"]
    ns, name = ref["namespace"], ref["name"]
    undo = _deployment_undo(item)
    if undo == "keep":
        return "Stopped following it; the share change stays"
    if undo == "delete":
        if not _get(_deployment_path(ns, name)):
            return f"{name} was already gone"
        if remove_workload:
            remove_workload(ns, name)
        else:
            _delete(_deployment_path(ns, name))
        return f"{name} was removed; its volumes are kept"
    _, target, why = rollout_target(ns, name, item.get("started_at"))
    if not target:
        # What the plan said: the change stays, only the tracking stops.
        return f"Stopped following it; the change stays ({why})"
    roll_back(ns, name, target)
    return f"{name} is going back to revision {_revision(target)}"


# ------------------------------------------------------------ image updates
def image_plan(item):
    ref = item["ref"]
    dep = _get(_deployment_path(ref["namespace"], ref["name"]))
    previous = UPDATES._annotation_json(dep, UPDATES.PREVIOUS).get("images") if dep else {}
    if not previous:
        return {"mode": "forget", "keeps": [f"{ref['name']} keeps no earlier image to go back to; "
                                            "the new one stays"]}
    return {"mode": "rollback", "severity": "high",
            "undo": [f"{ref['name']}'s {container} goes back to {image}" for container, image in sorted(previous.items())],
            "keeps": [f"{ref['name']}'s pods restart once more, onto that image"]}


def image_cancel(item, _options):
    ref = item["ref"]
    UPDATES.rollback(ref["namespace"], ref["name"])
    UPDATES.invalidate()
    return f"{ref['name']} is going back to the image it ran before"


# ------------------------------------------------------------- image cache
def pull_plan(item):
    return {"mode": "stop", "undo": [f"Stops pulling {item['resource'].get('name') or 'the image'} "
                                     "and removes the pull's pods"],
            "keeps": ["Nodes that have finished keep the image; clear it from Image cache if it is not wanted"]}


def pull_cancel(item, _options):
    IMP.stop_prepull(item["ref"]["name"])
    return "Pull stopped; nodes that had finished keep the image"


def _cleanup_pods(item):
    ns = item["ref"]["namespace"]
    out = []
    for name in item["ref"].get("pods") or []:
        pod = _get(f"/api/v1/namespaces/{_q(ns)}/pods/{_q(name)}")
        out.append((name, ((pod or {}).get("status") or {}).get("phase") if pod else "gone"))
    return out


def cleanup_plan(item):
    pods = _cleanup_pods(item)
    done = sum(phase in ("Succeeded", "gone") for _, phase in pods)
    return {"mode": "stop", "needs": "admin",
            "undo": [f"Stops the removal on the {_s(len(pods) - done, 'node')} it has not finished on"],
            "keeps": [f"The {_s(done, 'node')} already cleaned no longer cache the image; "
                      "it is fetched again when something needs it"]}


def cleanup_cancel(item, _options):
    ns, stopped = item["ref"]["namespace"], 0
    for name, phase in _cleanup_pods(item):
        if phase not in ("Succeeded", "Failed", "gone"):
            stopped += _delete(f"/api/v1/namespaces/{_q(ns)}/pods/{_q(name)}")
    return f"Stopped on {_s(stopped, 'node')}; nodes already cleaned stay so"


# ----------------------------------------------------------------- volumes
def volume_delete_plan(item):
    ref = item["ref"]
    return {"mode": "forget", "needs": "admin", "keeps": [
        f"Kubernetes cannot take a delete back once asked: {ref['name']} is removed as soon as "
        "nothing uses it" + (", and its data with it" if ref.get("action") != "delete_claim" else "") + "."]}


def restore_plan(item):
    ref = item["ref"]
    return {"mode": "rollback", "needs": "admin", "severity": "high",
            "undo": [f"Deletes {ref['namespace']}/{ref['name']}, the claim being restored into, "
                     "with its partly restored volume"],
            "keeps": ["The backup is not touched; restore from it again at any time"]}


def restore_cancel(item, _options):
    ref = item["ref"]
    gone = _delete(f"/api/v1/namespaces/{_q(ref['namespace'])}/persistentvolumeclaims/{_q(ref['name'])}")
    if tidy_restore:
        try:
            tidy_restore()
        except Exception:
            pass
    return (f"Restore stopped; {ref['name']} is being deleted" if gone
            else f"Restore stopped; {ref['name']} was already gone")


def backup_plan(item):
    ref = item["ref"]
    backup = _get(f"{LHAPI}/namespaces/{LHNS}/backups/{_q(ref['name'])}") or {}
    snapshot = (backup.get("spec") or {}).get("snapshotName", "")
    return {"mode": "rollback",
            "undo": ["Stops the backup and removes what it has written to the backup store"]
                    + ([f"Deletes {snapshot}, the snapshot it was taken from"] if snapshot else []),
            "keeps": [f"{item['resource'].get('name') or 'The volume'} and earlier backups are not touched"]}


def backup_cancel(item, _options):
    ref = item["ref"]
    backup = _get(f"{LHAPI}/namespaces/{LHNS}/backups/{_q(ref['name'])}") or {}
    snapshot = (backup.get("spec") or {}).get("snapshotName", "")
    _delete(f"{LHAPI}/namespaces/{LHNS}/backups/{_q(ref['name'])}")
    if snapshot:
        _delete(f"{LHAPI}/namespaces/{LHNS}/snapshots/{_q(snapshot)}")
    return "Backup stopped and removed" + (f", with its snapshot {snapshot}" if snapshot else "")


def run_plan(_item):
    return {"mode": "stop", "undo": ["Stops the run and removes its job"],
            "keeps": ["Snapshots and backups it had finished are kept"]}


def job_cancel(item, _options):
    ref = item["ref"]
    _delete(f"/apis/batch/v1/namespaces/{_q(ref['namespace'])}/jobs/{_q(ref['name'])}?propagationPolicy=Background")
    return "Stopped"


# ----------------------------------------------------------------- imports
def import_plan(item):
    plan = IMP.import_cleanup_plan(item["ref"]["name"])
    made = [row["name"] for row in plan["volumes"] if row["created"]]
    borrowed = [row["name"] for row in plan["volumes"] if not row["created"]]
    keeps = ["The source is not touched"]
    if borrowed:
        one = len(borrowed) == 1
        keeps.append(f"{_names(borrowed)} {'was' if one else 'were'} there before the import and "
                     f"{'is' if one else 'are'} kept, with whatever was copied in")
    return {"mode": "rollback", "needs": "admin", "severity": "high" if made or plan["workload"] else "low",
            "undo": ["Stops the copy and removes the import job"]
                    + ([f"Deletes {plan['workload']}, the workload it made"] if plan["workload"] else []),
            "keeps": keeps,
            "options": ([{"id": "volumes", "default": True,
                          "label": f"Also delete {_names(made)}, the volume{'s' if len(made) != 1 else ''} it made",
                          "detail": "They hold a partial copy. Untick to keep what was copied so far."}]
                        if made else [])}


def import_cancel(item, options):
    plan = IMP.import_cleanup_plan(item["ref"]["name"])
    IMP.delete_import(item["ref"]["name"])
    done = ["copy stopped"]
    if plan["workload"]:
        if remove_workload:
            remove_workload(plan["namespace"], plan["workload"])
        else:
            _delete(_deployment_path(plan["namespace"], plan["workload"]))
        IMP.wait_for_pods_gone(plan["namespace"], f"app={plan['workload']}")
        done.append(f"{plan['workload']} removed")
    if options.get("volumes"):
        made = [row["name"] for row in plan["volumes"] if row["created"]]
        for claim in made:
            _delete(f"/api/v1/namespaces/{_q(plan['namespace'])}/persistentvolumeclaims/{_q(claim)}")
        if made:
            done.append(f"{_names(made)} deleted")
    return "Import cancelled: " + ", ".join(done)


def disk_import_plan(item):
    ref = item["ref"]
    return {"mode": "rollback", "needs": "admin",
            "undo": [f"Stops the download and deletes {ref['name']}, the disk it was filling"],
            "keeps": ["The image at its source is not touched"]}


def disk_import_cancel(item, _options):
    ref = item["ref"]
    ns, name = _q(ref["namespace"]), _q(ref["name"])
    # The DataVolume first: while it exists it would make the claim again.
    _delete(f"/apis/cdi.kubevirt.io/v1beta1/namespaces/{ns}/datavolumes/{name}")
    _delete(f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{name}")
    return f"Download stopped; {ref['name']} is being deleted"


# --------------------------------------------------------------------- VMs
def migration_plan(item):
    vm = item["resource"].get("name", "the VM")
    vmi = _get(f"/apis/kubevirt.io/v1/namespaces/{_q(item['ref']['namespace'])}"
               f"/virtualmachineinstances/{_q(vm)}") or {}
    node = (vmi.get("status") or {}).get("nodeName", "")
    return {"mode": "rollback", "undo": [f"Aborts the migration: {vm} keeps running"
                                         + (f" on {node}" if node else " where it is")],
            "keeps": [f"{vm} is not restarted"]}


def migration_cancel(item, _options):
    ref = item["ref"]
    _delete(f"/apis/kubevirt.io/v1/namespaces/{_q(ref['namespace'])}"
            f"/virtualmachineinstancemigrations/{_q(ref['name'])}")
    return "Migration aborted; the VM stays where it was"


def _cluster_vms(ref):
    """The cluster's VMs that are still there and carry its label - never a
    VM that merely shares a name."""
    out = []
    for node in ref.get("nodes") or []:
        vm = _get(f"/apis/kubevirt.io/v1/namespaces/{_q(ref['namespace'])}/virtualmachines/{_q(node['name'])}")
        if vm and ((vm.get("metadata") or {}).get("labels") or {}).get(K3SC.LABEL) == ref["name"]:
            out.append(node)
    return out


def k3s_plan(item):
    ref = item["ref"]
    vms = _cluster_vms(ref)
    addresses = [n["address"] for n in ref.get("nodes") or []]
    return {"mode": "rollback", "needs": "admin", "severity": "high", "confirm": ref["name"],
            "undo": ([f"Deletes {_s(len(vms), 'VM')} - {_names([n['name'] for n in vms])} - with their disks"]
                     if vms else ["Its VMs are gone already"])
                    + [f"Forgets {_names(addresses)} in IP addresses, so they can be used again"],
            "keeps": ["Anything installed inside the VMs so far is lost",
                      "The VM image the nodes started from is kept"]}


def k3s_cancel(item, _options):
    ref = item["ref"]
    removed = []
    for node in _cluster_vms(ref):
        try:
            VMS.delete(ref["namespace"], node["name"], with_disks=True)
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
        removed.append(node["name"])
    forget_addresses({n["address"]: n["name"] for n in ref.get("nodes") or []})
    return (f"Cluster {ref['name']} cancelled: {_names(removed)} {'is' if len(removed) == 1 else 'are'} being "
            "deleted with their disks, and their addresses are free again" if removed
            else f"Cluster {ref['name']} cancelled; its VMs were already gone")


def forget_addresses(names_by_ip):
    """Drop the address records made for these VMs - only a record still
    under the VM's name, so an address someone has since taken is kept."""
    def change(data):
        for ip, name in names_by_ip.items():
            record = data["records"].get(ip)
            if record and record.get("name") == name:
                data["records"].pop(ip, None)
        return {"ok": True}
    try:
        IPAM.update(change)
    except Exception:
        pass


# --------------------------------------------------------- data and storage
def restructure_plan(item):
    ref = item["ref"]
    name, phase = ref["name"], ref.get("phase") or "stopping"
    if phase not in ("stopping", "copying"):
        return {"mode": "forget", "keeps": [f"The copy is done; {name} is starting on its new storage"]}
    _, target, _ = rollout_target(ref["namespace"], name, item.get("started_at"))
    undo = ["Stops the copy"] if phase == "copying" else []
    keeps = ["The old volumes were only read, and are as they were",
             "Volumes the edit made are kept, with anything copied so far; delete them from Volumes if not wanted"]
    if not target:
        return {"mode": "stop", "undo": undo or ["Stops before anything is copied"],
                "keeps": [f"{name} stays stopped, with the edited storage: point its paths back and start it"] + keeps}
    count = int(ref.get("replicas") or 0)
    return {"mode": "rollback", "severity": "high",
            "undo": undo + [f"{name} goes back to its storage as it was before the edit"
                            + (f" and starts again ({_s(count, 'replica')})" if count else ", stopped as it was")],
            "keeps": keeps}


def restructure_cancel(item, _options):
    ref = item["ref"]
    ns, name = ref["namespace"], ref["name"]
    if ref.get("job"):
        _delete(f"/apis/batch/v1/namespaces/{_q(ns)}/jobs/{_q(ref['job'])}?propagationPolicy=Background")
    if ref.get("phase") not in ("stopping", "copying"):
        return "Stopped following it"
    _, target, _ = rollout_target(ns, name, item.get("started_at"))
    ref["phase"] = "cancelled"
    if not target:
        return f"Copy stopped; {name} stays stopped with the edited storage"
    roll_back(ns, name, target, int(ref.get("replicas") or 0))
    return f"{name} is back on its storage as it was" + (" and starting" if ref.get("replicas") else "")


def reclass_plan(item):
    ref = item["ref"]
    phase, claim = ref.get("phase", "stop"), ref["claim"]
    users = [c["name"] for c in ref.get("consumers") or []]
    if phase in RECLASS.BEFORE_SWAP:
        return {"mode": "rollback", "needs": "admin", "severity": "high" if users else "low",
                "undo": [f"Stops the copy and deletes the new volume on {ref['target']}"]
                        + ([f"Starts {_names(users)} again as {'it was' if len(users) == 1 else 'they were'}, "
                            f"on the original {claim}"] if users else []),
                "keeps": [f"{claim} and its data were only read, and are as they were"]}
    if phase == "swap":
        return {"can": False, "needs": "admin",
                "why_not": f"{claim} is being swapped onto the new volume. That takes seconds and must not stop "
                           f"half-way; once it has finished, move {claim} back if you need to."}
    return {"mode": "forget", "needs": "admin",
            "keeps": [f"The move has finished: {claim} is on {ref['target']}, and the old copy is kept as "
                      f"{ref.get('old_pv', 'an old copy')}. Only the wait for things to start again stops."]}


def reclass_cancel(item, _options):
    ref = item["ref"]
    if ref.get("phase", "stop") in RECLASS.BEFORE_SWAP:
        RECLASS._rollback(ref["namespace"], ref, "")
        return (f"Cancelled: the new volume was removed and {ref['claim']} is in use again as before, "
                "on its untouched original")
    return "Stopped waiting; the move itself had finished"


def self_move_plan(item):
    ref = item["ref"]
    if _switched(ref):
        return {"can": False, "needs": "admin",
                "why_not": f"Homestead is restarting onto {ref['new']} already; that cannot be stopped half-way"}
    return {"mode": "rollback", "needs": "admin",
            "undo": ["Stops the copy", f"Deletes {ref['new']}, the new claim it was copying into"],
            "keeps": [f"Homestead carries on with its data on {ref['old']}, as before"]}


def _switched(ref):
    dep = _get(_deployment_path(ref["namespace"], NAMES.BRAND)) or {}
    volumes = (((dep.get("spec") or {}).get("template") or {}).get("spec") or {}).get("volumes") or []
    return any((v.get("persistentVolumeClaim") or {}).get("claimName") == ref["new"] for v in volumes)


def self_move_cancel(item, _options):
    ref = item["ref"]
    if _switched(ref):
        raise ValueError(f"Homestead is restarting onto {ref['new']} already")
    ns = _q(ref["namespace"])
    _delete(f"/apis/batch/v1/namespaces/{ns}/jobs/{_q(ref['job'])}?propagationPolicy=Background")
    _delete(f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{_q(ref['new'])}")
    return f"Copy stopped; Homestead keeps its data on {ref['old']}"


def disk_retire_plan(item):
    ref = item["ref"]
    phase = ref.get("phase", "stop")
    if phase in ("remove", "cleanup"):
        return {"can": False, "needs": "admin",
                "why_not": "The disk is being taken out of Longhorn; that finishes in moments and "
                           "cannot be stopped half-way"}
    removed = len(ref.get("removed") or [])
    return {"mode": "rollback", "needs": "admin",
            "undo": ["Lets Longhorn place replicas on the disk again, as before"],
            "keeps": ([f"The {_s(removed, 'failed replica')} already let go of cannot come back; "
                       f"{'its volume rebuilds' if removed == 1 else 'their volumes rebuild'} from healthy copies"]
                      if removed else ["No replica has been removed yet"])}


def disk_retire_cancel(item, _options):
    ref = item["ref"]
    DISKS._patch(f"{DISKS.LH}/nodes/{ref['node']}",
                 {"spec": {"disks": {ref["disk"]: {"allowScheduling": bool(ref.get("was_scheduling", True))}}}},
                 "letting replicas onto it again")
    ref["phase"] = "cancelled"
    return f"The disk on {ref['node']} takes replicas again, as before"


# --------------------------------------------------------------- the rest
def _helm_action(item):
    ref = item["ref"]
    if ref.get("action") in ("install", "upgrade", "uninstall"):
        return ref["action"]
    # Jobs from releases before this one: the job's name and the title say.
    if str(ref.get("name", "")).startswith("helm-delete-"):
        return "uninstall"
    return "upgrade" if str(item.get("title", "")).startswith("Helm upgrade ") else "install"


def _helmchart(item):
    name = item["resource"].get("name", "")
    return name, _get(f"/apis/helm.cattle.io/v1/namespaces/{_q(HELM.CONTROLLER_NS)}/helmcharts/{_q(name)}")


def helm_plan(item):
    action = _helm_action(item)
    name, chart = _helmchart(item)
    if action == "uninstall" or not chart:
        return {"mode": "forget", "needs": "admin", "keeps": [
            f"{name} is being removed already, and Kubernetes cannot take that back; install it again once it is gone"]}
    if action == "upgrade":
        if not ((chart.get("metadata") or {}).get("annotations") or {}).get(HELM.PREVIOUS_SPEC):
            return {"mode": "forget", "needs": "admin", "keeps": [
                f"No earlier settings of {name} were kept to go back to; the upgrade carries on"]}
        return {"mode": "rollback", "needs": "admin", "severity": "high",
                "undo": [f"Puts {name}'s chart version and values back as they were before the upgrade; "
                         "Helm runs again with them"],
                "keeps": ["Anything the upgrade changed in the chart's own data - a database migration - "
                          "is not undone by Helm"]}
    return {"mode": "rollback", "needs": "admin", "severity": "high",
            "undo": [f"Stops the install and deletes the chart {name}: the Helm controller then uninstalls "
                     "whatever it made so far"],
            "keeps": ["Volumes the chart made may be kept, as its own settings say"]}


def helm_cancel(item, _options):
    action = _helm_action(item)
    name, chart = _helmchart(item)
    if action == "uninstall" or not chart:
        return "Stopped following it; the removal carries on"
    ns = _q(HELM.CONTROLLER_NS)
    if action == "upgrade":
        if not HELM.restore_previous(chart):
            return "Stopped following it; the upgrade carries on"
        return f"{name} is going back to its settings from before the upgrade"
    _delete(f"/apis/batch/v1/namespaces/{ns}/jobs/{_q(item['ref']['name'])}?propagationPolicy=Background")
    _delete(f"/apis/helm.cattle.io/v1/namespaces/{ns}/helmcharts/{_q(name)}")
    return f"Install stopped; the Helm controller is removing what {name} made"


def smart_plan(item):
    ref = item["ref"]
    return {"mode": "stop", "needs": "admin",
            "undo": [f"Aborts the {ref.get('test', '')} self-test on {ref['disk']} ({ref['node']})"],
            "keeps": ["The drive logs the test as aborted; the disk and its data are not touched"]}


def smart_cancel(item, _options):
    ref = item["ref"]
    SMART.abort_test(ref["node"], ref["disk"])
    return f"Self-test on {ref['disk']} aborted"


def service_plan(item):
    ref = item["ref"]
    svc = _get(f"/api/v1/namespaces/{_q(ref['namespace'])}/services/{_q(ref['name'])}") or {}
    address = ",".join(i.get("ip", "") for i in ((svc.get("status") or {}).get("loadBalancer") or {}).get("ingress") or []
                       if i.get("ip"))
    return {"mode": "rollback",
            "undo": [f"Removes the Service {ref['namespace']}/{ref['name']}"
                     + (f" and frees {address}" if address else "")],
            "keeps": ["The workload it pointed at is not touched"]}


def service_cancel(item, _options):
    ref = item["ref"]
    _delete(f"/api/v1/namespaces/{_q(ref['namespace'])}/services/{_q(ref['name'])}")
    return f"Service {ref['name']} removed"


def move_plan(item):
    move = MOVE_ENGINE._find((item.get("ref") or {}).get("move", ""))
    if not move:
        return {"mode": "forget", "needs": "admin", "keeps": ["This move's record is gone"]}
    return {"mode": "rollback", "needs": "admin", "severity": "high",
            "undo": [f"{move['name']} starts again on {move['cluster']}, where it came from",
                     "What had arrived here - the workload, its Services and claims - is removed"],
            "keeps": ["Backups made for the move stay in the backup store",
                      "If the move pointed this cluster's Longhorn at the other cluster's backup store, "
                      "it stays pointed there; change it under Data protection"]}


def move_cancel(item, _options):
    moved = MOVE_ENGINE.abandon((item.get("ref") or {}).get("move", ""))
    return moved.get("message") or "Put back"


def register(ops):
    """Say, for each kind of job, what cancelling it does."""
    for kinds, plan, run in (
            (("deployment",), deployment_plan, deployment_cancel),
            (("image-update", "image-rollback"), image_plan, image_cancel),
            (("image-pull",), pull_plan, pull_cancel),
            (("image-cleanup",), cleanup_plan, cleanup_cancel),
            (("volume-delete",), volume_delete_plan, None),
            (("volume-restore",), restore_plan, restore_cancel),
            (("backup",), backup_plan, backup_cancel),
            (("protect-run",), run_plan, job_cancel),
            (("import",), import_plan, import_cancel),
            (("vm-disk-import",), disk_import_plan, disk_import_cancel),
            (("vm-migration",), migration_plan, migration_cancel),
            (("k3s-cluster",), k3s_plan, k3s_cancel),
            (("restructure",), restructure_plan, restructure_cancel),
            (("reclass",), reclass_plan, reclass_cancel),
            (("self-data-move",), self_move_plan, self_move_cancel),
            (("disk-retire",), disk_retire_plan, disk_retire_cancel),
            (("helm",), helm_plan, helm_cancel),
            (("smart-test",), smart_plan, smart_cancel),
            (("network-service",), service_plan, service_cancel),
            (("move",), move_plan, move_cancel)):
        for kind in kinds:
            ops.CANCELLERS[kind] = (plan, run or (lambda _item, _options: ""))
    # A k3s build that fails leaves its VMs running, their disks and their
    # addresses taken; the same cancel removes them afterwards.
    ops.CLEANUPS.add("k3s-cluster")
