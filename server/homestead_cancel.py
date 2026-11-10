"""Cancelling a job: what it stops, what it puts back, and what it cannot.

Every job in the Activity tray can be cancelled. For each kind this says,
before anything changes, what a cancel would do - the plan the person reads -
and then does it:

  rollback  puts things back as they were before the job: a deploy removed,
            a rollout returned to the version before it, a volume move
            started again on its untouched original;
  stop      halts it and keeps what is already done, when what is done
            cannot be taken back - a node that has already dropped an image;
  forget    for the few things Kubernetes cannot take back once asked - a
            claim marked for deletion - it stops being tracked here, and the
            plan says plainly that it carries on.

A job part-way through a step that must not be interrupted - a volume swap,
Homestead switching onto its moved data - is refused until the step is done.

Only what the job made is removed: objects are checked for the job's label or
name before they are deleted, and volumes a job borrowed are never touched.
Each step is safe to repeat, as a cancel stopped part-way is run again by the
leader (homestead_cancel_rerun). A step that destroys what a later step reads, or
that a repeat would aim at something new of the same name, saves what it needs first.
"""
import calendar
import copy
import hashlib
import json
import time
import urllib.error
import urllib.parse

import homestead_cancel_rerun as RERUN
import homestead_cdi_cleanup as CDI_CLEANUP
import homestead_disks as DISKS
import homestead_env_secrets as ENV_SECRETS
import homestead_helm as HELM
import homestead_container_import as CONTAINER_IMPORT
import homestead_image_cache as IMAGES
import homestead_kube as KUBE
import homestead_longhorn as LH
import homestead_move_actions as MOVE_ACTIONS
import homestead_move_state as MOVE_STATE
import homestead_names as NAMES
import homestead_network_services as SERVICES
import homestead_operations as OPS
import homestead_reclass as RECLASS
import homestead_restructure as RESTRUCTURE
import homestead_smart as SMART
import homestead_updates as UPDATES

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
# On a Deployment a cancel rolled back, with the job's id, in the write itself: once
# it lands the old ReplicaSet has the top revision and the job's is "one back".
CANCEL_ROLLBACK = NAMES.key("cancel-rollback")


def bind(_kget, _ksend, _remove_workload=None, _tidy_restore=None):
    global kget, ksend, remove_workload, tidy_restore
    kget, ksend = _kget, _ksend
    remove_workload, tidy_restore = _remove_workload, _tidy_restore


def _q(value):
    return urllib.parse.quote(str(value), safe="")


def _delete(path):
    """Delete, and say whether there was anything to delete."""
    try:
        ksend("DELETE", path)
        return True
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return False
        raise


def _delete_saved(item, *paths):
    """Delete these, each only if it is what this cancel's first run saw: run again,
    it must not take a new object made since by the same name."""
    return RERUN.delete_saved(item, kget, ksend, *paths)


def _kept_note(item, *paths):
    """Words for what is left because it is new since this cancel began, or ""."""
    try:
        kept = RERUN.kept_since(item, kget, *paths)
    except Exception:   # the deletes are done: not a reason to call the cancel failed
        return ". Homestead could not read whether anything with that name was created since"
    if not kept:
        return ""
    return (f". {_names(kept)} {'was' if len(kept) == 1 else 'were'} created again since this cancel "
            f"began, so the new {'one is' if len(kept) == 1 else 'ones are'} kept")


def _running_again(item):
    """Whether this is the leader running a cancel again, not a person's review. Only then
    does what an earlier run saved fence what is there: a person has just looked at it."""
    record = item.get("cancel_record") or {}
    return bool(record.get("reruns")) and not record.get("parked")


def _reviewed(item):
    """What the person reviewed, when this cancel is running again: a half-done cancel
    must not read as if it would now do something else."""
    return (item.get("cancel_record") or {}).get("mode")


def _note_workload(item, ns, name, dep):
    """Save a workload's uid, and the Services and Secret only it can lead to."""
    names = set(SERVICES.workload_service_names(ns, name)) | {name}
    found = {service: KUBE.get_optional(f"/api/v1/namespaces/{_q(ns)}/services/{_q(service)}", read=kget)
             for service in sorted(names)}
    item["ref"]["cancel_removing"] = {"uid": dep["metadata"].get("uid"), "secret": ENV_SECRETS.own(dep),
                                      "services": {service: row["metadata"].get("uid")
                                                   for service, row in found.items() if row}}
    OPS.keep_cancel_ref(item)


def _remove_workload_once(item, ns, name):
    """Remove a workload as remove_workload does, so that a run cut off after the
    Deployment went still removes its Services and Secret, which can only be
    found from the Deployment. -> "removed", "gone" or "kept" (another of that name)."""
    ref, path = item["ref"], _deployment_path(ns, name)
    dep = KUBE.get_optional(path, read=kget)
    saved = ref.get("cancel_removing")
    if not dep:
        if not saved:
            return "gone"
        for service, uid in (saved.get("services") or {}).items():
            RERUN._delete_same(f"/api/v1/namespaces/{_q(ns)}/services/{_q(service)}", uid, ksend)
        if saved.get("secret"):
            ENV_SECRETS.remove(ns, {"metadata": {"annotations": {ENV_SECRETS.ANNOTATION: saved["secret"]}}}, kget, ksend)
        return "removed"
    if saved and dep["metadata"].get("uid") != saved.get("uid"):
        if _running_again(item):
            return "kept"
        saved = None
    if not saved:
        _note_workload(item, ns, name, dep)
    if remove_workload:
        remove_workload(ns, name)
    else:
        _delete(path)
    return "removed"


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
    dep = KUBE.get_optional(_deployment_path(ns, name), read=kget)
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


def roll_back(ns, name, replicaset, replicas=None, job=""):
    """Put a Deployment's pod template back to a ReplicaSet's, as kubectl
    rollout undo does; with a replica count, it is started at that too. job marks
    the Deployment as rolled back by it, in the same write."""
    dep = kget(_deployment_path(ns, name))
    if job:
        dep["metadata"]["annotations"] = {**(dep["metadata"].get("annotations") or {}), CANCEL_ROLLBACK: job}
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


def _reviewed_target(item):
    return (item.get("cancel_record") or {}).get("target")


def _rolled_back_by(item, dep):
    """Whether this job's cancel has rolled this Deployment back already."""
    return bool(item.get("id")) and ((dep or {}).get("metadata", {}).get("annotations") or {}).get(CANCEL_ROLLBACK) == item["id"]


def deployment_plan(item):
    ref = item["ref"]
    ns, name = ref["namespace"], ref["name"]
    undo = _deployment_undo(item)
    if undo == "keep":
        return {"mode": "forget", "keeps": [
            f"The change is saved already: {name} carries on restarting with it. "
            "Edit or remove the share to change it back."]}
    if undo == "delete":
        dep = KUBE.get_optional(_deployment_path(ns, name), read=kget)
        if not dep and ref.get("cancel_removing"):
            return {"mode": "rollback", "severity": "high",
                    "undo": [f"Finishes removing {name}: the Services that pointed at it"]}
        if not dep:
            return {"mode": "stop", "keeps": [f"{name} no longer exists, so there is nothing to remove"]}
        claims = _claims_of(dep)
        return {"mode": "rollback", "severity": "high",
                "undo": [f"Deletes {name} and stops its pods",
                         "Deletes the Services that point at it"],
                "keeps": ([f"Its {'volume' if len(claims) == 1 else 'volumes'} {_names(claims)} "
                           f"{'is' if len(claims) == 1 else 'are'} kept, with anything written so far. "
                           "Delete them on the Volumes page if they are not wanted."] if claims else [])}
    if _rolled_back_by(item, KUBE.get_optional(_deployment_path(ns, name), read=kget)):
        return {"mode": "rollback", "severity": "high",
                "undo": [f"{name} is going back to the version it ran before this job already"],
                "reviewed_target": _reviewed_target(item)}
    dep, target, why = rollout_target(ns, name, item.get("started_at"))
    if not target:
        return {"mode": "forget", "keeps": [f"The change is already saved in Kubernetes ({why}). "
                                            f"Edit {name} to change it back."]}
    return {"mode": "rollback", "severity": "high",
            # A later rollout moves "one back": run again, this must still be what was reviewed.
            "reviewed_target": {"replicaset": target["metadata"].get("uid")},
            "undo": [f"{name} goes back to the version it ran before this job "
                     f"(revision {_revision(target)}: {_names(_images(target['spec']['template']))})"],
            "keeps": [f"{name}'s pods restart once more, onto that version"]}


def deployment_cancel(item, _options):
    ref = item["ref"]
    ns, name = ref["namespace"], ref["name"]
    undo = _deployment_undo(item)
    if undo == "keep":
        return "Stopped following it. The share change stays."
    if undo == "delete":
        done = _remove_workload_once(item, ns, name)
        if done == "gone":
            return f"{name} was already gone"
        if done == "kept":
            return f"{name} was made again since this cancel began, so the new one is kept"
        return f"{name} was removed. Its volumes are kept."
    if _rolled_back_by(item, KUBE.get_optional(_deployment_path(ns, name), read=kget)):
        return f"{name} is going back to the version it ran before this job"
    _, target, why = rollout_target(ns, name, item.get("started_at"))
    if not target:
        # What the plan said: the change stays, only the tracking stops.
        return f"Stopped following it. The change stays ({why})."
    roll_back(ns, name, target, job=item.get("id", ""))
    return f"{name} is going back to revision {_revision(target)}"


# ------------------------------------------------------------ image updates
def image_plan(item):
    ref = item["ref"]
    dep = KUBE.get_optional(_deployment_path(ref["namespace"], ref["name"]), read=kget)
    previous = UPDATES._annotation_json(dep, UPDATES.PREVIOUS).get("images") if dep else {}
    if not previous:
        return {"mode": "forget", "keeps": [f"{ref['name']} has no earlier image to return to, "
                                            "so the new one stays"]}
    return {"mode": "rollback", "severity": "high",
            "can": False, "why_not": "Image rollback requires a fresh image and capacity review",
            "image_review": {"ns": ref["namespace"], "name": ref["name"]},
            "undo": [f"{ref['name']}'s {container} goes back to {image}" for container, image in sorted(previous.items())],
            "keeps": [f"{ref['name']}'s pods restart once more, onto that image"]}


def image_cancel(item, _options):
    if image_plan(item).get("image_review"):
        raise ValueError("Image rollback requires a fresh image and capacity review")
    return "Stopped tracking. There is no earlier image to recover, and the app is not changed."


# ------------------------------------------------------------- image cache
def pull_plan(item):
    return {"mode": "stop", "undo": [f"Stops pulling {item['resource'].get('name') or 'the image'} "
                                     "and removes the pull's pods"],
            "keeps": ["Hosts that have finished keep the image. Clear it from Image cache if it is not wanted."]}


def pull_cancel(item, _options):
    name = str(item["ref"]["name"])
    if not any(name.startswith(prefix) for prefix in IMAGES.PREPULL_NAMES) or "/" in name:
        raise ValueError("that is not an image pre-pull")
    path = f"/apis/apps/v1/namespaces/{IMAGES.I.NS}/daemonsets/{name}?propagationPolicy=Background"
    _delete_saved(item, path)
    return "Download stopped. Hosts that had finished keep the image" + _kept_note(item, path)


def _cleanup_pods(item):
    ns = item["ref"]["namespace"]
    out = []
    for name in item["ref"].get("pods") or []:
        pod = KUBE.get_optional(f"/api/v1/namespaces/{_q(ns)}/pods/{_q(name)}", read=kget)
        out.append((name, ((pod or {}).get("status") or {}).get("phase") if pod else "gone"))
    return out


def cleanup_plan(item):
    pods = _cleanup_pods(item)
    done = sum(phase in ("Succeeded", "gone") for _, phase in pods)
    return {"mode": "stop", "needs": "admin",
            "undo": [f"Stops the removal on the {_s(len(pods) - done, 'node')} it has not finished on"],
            "keeps": [f"The {_s(done, 'host')} already cleaned no longer keep the image. "
                      "It is downloaded again when something needs it."]}


def cleanup_cancel(item, _options):
    ns = item["ref"]["namespace"]
    paths = [f"/api/v1/namespaces/{_q(ns)}/pods/{_q(name)}" for name, phase in _cleanup_pods(item)
             if phase not in ("Succeeded", "Failed", "gone")]
    stopped = _delete_saved(item, *paths)
    return f"Stopped on {_s(stopped, 'host')}. Hosts already cleaned stay that way" + _kept_note(item, *paths)


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
            "keeps": ["The backup does not change. It can be restored again at any time."]}


def restore_cancel(item, _options):
    ref = item["ref"]
    path = f"/api/v1/namespaces/{_q(ref['namespace'])}/persistentvolumeclaims/{_q(ref['name'])}"
    claim = KUBE.get_optional(path, read=kget)
    # Only the claim this restore made: one of the same name made since - the
    # restore removed and the name used again - is someone's data.
    made_by = (((claim or {}).get("metadata") or {}).get("annotations") or {}).get("homestead.io/restored-from-backup")
    if claim and ref.get("backup") and made_by != ref["backup"]:
        return f"Restore stopped. {ref['name']} is no longer the volume it created, so it is kept."
    # This restore's own snapshot only: another restore into the same claim
    # name may have made its pair and not yet its claim. A job from before
    # the job kept it names it in the claim it made.
    snapshot = ref.get("snapshot") or (((claim or {}).get("spec") or {}).get("dataSource") or {}).get("name")
    # The delete is fenced with the uid of the claim checked above, never one read
    # again: a claim that takes the name in between is someone's data (#1349).
    # Absent at the check, None is saved, so nothing of that name is deleted.
    uids = ref.setdefault("cancel_uids", {})
    if path not in uids:
        uids[path] = ((claim or {}).get("metadata") or {}).get("uid")
        OPS.keep_cancel_ref(item)
    gone = _delete_saved(item, path)
    # Its snapshot is removed only once a claim binds: with the claim gone,
    # it is removed here (a backup is never touched).
    if snapshot:
        try:
            import homestead_csi_restore as CSI_RESTORE
            CSI_RESTORE.discard(ref["namespace"], ref["name"], snapshot)
        except Exception as error:
            print(f"Restore metadata cleanup deferred: {error}", flush=True)
    if tidy_restore:
        try:
            tidy_restore()
        except Exception:
            pass
    kept = _kept_note(item, path)
    if kept:
        return f"Restore stopped{kept}"
    return (f"Restore stopped. {ref['name']} is being deleted." if gone
            else f"Restore stopped. {ref['name']} had already been removed.")


def backup_plan(item):
    ref = item["ref"]
    backup = KUBE.get_optional(f"{LHAPI}/namespaces/{LHNS}/backups/{_q(ref['name'])}", read=kget) or {}
    snapshot = (backup.get("spec") or {}).get("snapshotName", "")
    return {"mode": "rollback",
            "undo": ["Stops the backup and removes what it has written to the backup store"]
                    + ([f"Deletes {snapshot}, the snapshot it was taken from"] if snapshot else []),
            "keeps": [f"{item['resource'].get('name') or 'The volume'} and earlier backups are not touched"]}


def backup_cancel(item, _options):
    ref = item["ref"]
    backup = KUBE.get_optional(f"{LHAPI}/namespaces/{LHNS}/backups/{_q(ref['name'])}", read=kget) or {}
    snapshot = (backup.get("spec") or {}).get("snapshotName", "") or ref.get("cancel_snapshot", "")
    if snapshot and ref.get("cancel_snapshot") != snapshot:
        # Only the Backup names it: once that goes, a run cut off here could not find it.
        ref["cancel_snapshot"] = snapshot
        OPS.keep_cancel_ref(item)
    paths = [f"{LHAPI}/namespaces/{LHNS}/backups/{_q(ref['name'])}",
             *([f"{LHAPI}/namespaces/{LHNS}/snapshots/{_q(snapshot)}"] if snapshot else [])]
    _delete_saved(item, *paths)
    kept = _kept_note(item, *paths)
    if kept:
        return f"Backup stopped{kept}"
    return "Backup stopped and removed" + (f", with its snapshot {snapshot}" if snapshot else "")


def run_plan(_item):
    return {"mode": "stop", "undo": ["Stops the run and removes its job"],
            "keeps": ["Snapshots and backups it had finished are kept"]}


def job_cancel(item, _options):
    ref = item["ref"]
    path = f"/apis/batch/v1/namespaces/{_q(ref['namespace'])}/jobs/{_q(ref['name'])}?propagationPolicy=Background"
    _delete_saved(item, path)
    return "Stopped" + _kept_note(item, path)


# ----------------------------------------------------------------- imports
def _import_found(item):
    """What an earlier run of this cancel saved of its import Job, unless that Job has
    been replaced and a person is looking at the new one."""
    found = item["ref"].get("cancel_found")
    if not found or _running_again(item):
        return found
    job = KUBE.get_optional(_import_job_path(item, found), read=kget)
    return None if job and job["metadata"].get("uid") != found.get("job_uid") else found


def import_plan(item):
    plan = _import_found(item) or CONTAINER_IMPORT.import_cleanup_plan(item["ref"]["name"], item["ref"].get("namespace"))
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


def _import_job_path(item, plan):
    return f"/apis/batch/v1/namespaces/{_q(plan['namespace'])}/jobs/{_q(item['ref']['name'])}"


def _import_claims(plan):
    return [f"/api/v1/namespaces/{_q(plan['namespace'])}/persistentvolumeclaims/{_q(row['name'])}"
            for row in plan["volumes"] if row["created"]]


def import_cancel(item, options):
    # Read from the import Job, which goes first: saved, or a run cut off after that
    # would find nothing to remove and say it had finished.
    plan = _import_found(item)
    if not plan:
        plan = CONTAINER_IMPORT.import_cleanup_plan(item["ref"]["name"], item["ref"].get("namespace"))
        job = KUBE.get_optional(_import_job_path(item, plan), read=kget)
        plan["job_uid"] = ((job or {}).get("metadata") or {}).get("uid")
        item["ref"]["cancel_found"] = plan
        dep = KUBE.get_optional(_deployment_path(plan["namespace"], plan["workload"]), read=kget) if plan["workload"] else None
        if dep:
            _note_workload(item, plan["namespace"], plan["workload"], dep)
        RERUN.remember(item, kget, *_import_claims(plan))
        OPS.keep_cancel_ref(item)
    job = KUBE.get_optional(_import_job_path(item, plan), read=kget)
    if job and job["metadata"].get("uid") != plan.get("job_uid"):
        # Its name used again since this cancel began: another import's copy.
        done = [f"{item['ref']['name']} was made again since, so the new one is kept"]
    else:
        CONTAINER_IMPORT.delete_import(item["ref"]["name"], item["ref"].get("namespace"),
                                       **({"uid": plan["job_uid"]} if plan.get("job_uid") else {}))
        done = ["copy stopped"]
    if plan["workload"]:
        if _remove_workload_once(item, plan["namespace"], plan["workload"]) == "kept":
            done.append(f"{plan['workload']} was created again since, so the new one is kept")
        else:
            CONTAINER_IMPORT.wait_for_pods_gone(plan["namespace"], f"app={plan['workload']}")
            done.append(f"{plan['workload']} removed")
    if options.get("volumes"):
        claims = _import_claims(plan)
        _delete_saved(item, *claims)
        try:
            kept = RERUN.kept_since(item, kget, *claims)
        except Exception:   # the deletes are done: not a reason to call the cancel failed
            kept = []
            done.append("Homestead could not read whether anything with those names was created since")
        made = [row["name"] for row in plan["volumes"] if row["created"] and row["name"] not in kept]
        if made:
            done.append(f"{_names(made)} deleted")
        if kept:
            done.append(f"{_names(kept)} {'was' if len(kept) == 1 else 'were'} created again since, "
                        f"so the new {'one is' if len(kept) == 1 else 'ones are'} kept")
    return "Import cancelled: " + ", ".join(done)


def _disk_import_paths(ref):
    ns, name = _q(ref["namespace"]), _q(ref["name"])
    # The DataVolume first: while it exists it would make the claim again.
    return (f"/apis/cdi.kubevirt.io/v1beta1/namespaces/{ns}/datavolumes/{name}",
            f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{name}")


def _disk_import_found(item):
    """What is there under the import's name now: [(path, object, whether it is this
    import's)]. With a receipt (#1338), only what carries it is this import's: the
    DataVolume with its stamp, and the claim the receipt recorded or that DataVolume
    owns. An older import kept no receipt and goes by name, as it always did."""
    ref = item["ref"]
    work = ref.get("cdi_cleanup") or {}
    disk = (work.get("disks") or {}).get(ref["name"]) or {}
    found, dv_uid = [], disk.get("dv_uid")
    for path in _disk_import_paths(ref):
        obj = KUBE.get_optional(path, read=kget)
        if not obj:
            continue
        meta = obj.get("metadata") or {}
        if not work.get("id"):
            ours = True
        elif "/datavolumes/" in path:
            ours = (meta.get("annotations") or {}).get(CDI_CLEANUP.STAMP) == work["id"]
            dv_uid = dv_uid or (meta.get("uid") if ours else None)
        else:
            ours = bool(meta.get("uid")) and (meta["uid"] == disk.get("target_uid") or bool(
                dv_uid and CDI_CLEANUP.owner(obj, "DataVolume", dv_uid)))
        found.append((path, obj, ours))
    return found


def _not_this_imports_words(name):
    return f"{name} was not made by this import, so it is kept."


def disk_import_plan(item):
    ref = item["ref"]
    if (ref.get("cdi_cleanup") or {}).get("id"):
        # Its job ends by itself from here; deleting now would take a finished disk.
        if ref.get("phase") == "cleanup":
            return {"can": False, "needs": "admin", "why_not": (
                f"The import of {ref['name']} is clearing its temporary storage. It ends by itself once that is done.")}
        if ref.get("phase") == "creating":
            return {"can": False, "needs": "admin",
                    "why_not": f"The import of {ref['name']} is still being set up. Try again in a moment."}
        if any(not ours for path, _, ours in _disk_import_found(item) if "/datavolumes/" in path):
            return {"mode": "forget", "needs": "admin", "keeps": [_not_this_imports_words(ref["name"])]}
    return {"mode": "rollback", "needs": "admin",
            "undo": [f"Stops the download and deletes {ref['name']}, the disk it was filling"],
            "keeps": ["The image at its source is not touched"]}


def disk_import_cancel(item, _options):
    ref = item["ref"]
    if not (ref.get("cdi_cleanup") or {}).get("id"):
        # An older import, by name as it always was: each path's uid saved on the first run.
        paths = _disk_import_paths(ref)
        _delete_saved(item, *paths)
        kept = _kept_note(item, *paths)
        return f"Download stopped{kept}" if kept else f"Download stopped. {ref['name']} is being deleted."
    found = _disk_import_found(item)
    ours = [path for path, _, mine in found if mine]
    if ours:
        _delete_saved(item, *ours)
    if any(not mine for _, _, mine in found):
        return f"Download stopped. {_not_this_imports_words(ref['name'])}"
    kept = _kept_note(item, *ours) if ours else ""
    return f"Download stopped{kept}" if kept else f"Download stopped. {ref['name']} is being deleted."


# --------------------------------------------------------------------- VMs
def migration_plan(item):
    vm = item["resource"].get("name", "the VM")
    vmi = KUBE.get_optional(f"/apis/kubevirt.io/v1/namespaces/{_q(item['ref']['namespace'])}"
               f"/virtualmachineinstances/{_q(vm)}", read=kget) or {}
    node = (vmi.get("status") or {}).get("nodeName", "")
    return {"mode": "rollback", "undo": [f"Aborts the migration: {vm} keeps running"
                                         + (f" on {node}" if node else " where it is")],
            "keeps": [f"{vm} is not restarted"]}


def migration_cancel(item, _options):
    ref = item["ref"]
    path = (f"/apis/kubevirt.io/v1/namespaces/{_q(ref['namespace'])}"
            f"/virtualmachineinstancemigrations/{_q(ref['name'])}")
    _delete_saved(item, path)
    kept = _kept_note(item, path)
    return f"Move stopped{kept}" if kept else "Move cancelled. The VM stays where it was."


def k3s_plan(item):
    ref = item["ref"]
    if ref.get("dispatch_protocol") == 2:
        return {"mode": "forget", "can": False, "needs": "admin", "keeps": ["All resources and receipts remain unchanged"],
                "why_not": "Use Check save outcome for this reviewed batch. Stopping tracking cannot cancel changes already sent."}
    legacy = not ref.get("retain_resources")
    kept = ["All VMs, disks, Secrets and IP-address records remain. Guest installation may continue.",
            "This does not stop or undo installation, confirm readiness, or free any addresses.",
            "Inspect each VM and use its separate delete/impact controls if it is no longer wanted.",
            "After this is accepted, the finished record can be cleared. Save any recovery details that are still needed."]
    if legacy:
        kept.insert(0, "This older job has no records of what it created. A matching VM name or cluster label does not prove it is the original VM, so nothing is cleaned up automatically.")
    return {"mode": "forget", "tracking_only": True, "can": ref.get("phase") != "provisioning",
            "needs": "admin" if legacy or item.get("status") == "failed" else "operator",
            "confirm": ref.get("name", ""),
            "why_not": "VM creation may still be in flight. Wait for its receipt or failure before stopping tracking.",
            "keeps": kept}


def k3s_cancel(item, _options):
    ref = item["ref"]
    if ref.get("dispatch_protocol") == 2:
        raise ValueError("A reviewed VM batch needs its outcome checked first. Nothing was cancelled or deleted.")
    if ref.get("phase") == "provisioning":
        raise ValueError("The VMs may still be being created. Nothing was cancelled or deleted.")
    # Legacy names/labels cannot prove historical ownership. No cluster writes.
    ref["retain_resources"] = True
    return "Stopped tracking the guests' start-up. All VMs, disks, Secrets and IP address records are kept. Nothing was cleaned up, and readiness was not checked."


# --------------------------------------------------------- data and storage
def restructure_plan(item):
    return {"mode": "forget", "needs": "admin", "confirm": item["ref"]["name"],
            "action": "Acknowledge and stop tracking",
            "keeps": ["Older copy jobs and all volumes stay, and a copy already started may continue.",
                      "Check the copy and its storage in Kubernetes before removing any older hold manually. Stopping tracking does not release it."],
            "options": [{"id": "ack", "label": "The copy was checked. Accept incomplete data or late effects.",
                         "detail": "Stopping tracking does not stop the copy, restore the storage settings, or restart the app.", "default": False}]}


def restructure_cancel(item, options):
    if options.get("ack") is not True:
        raise ValueError("Acknowledge the copy's partial data and possible late effects first")
    item["ref"]["retain_resources"] = False
    return "Only the tracking stopped. Copy jobs, app settings and all volumes are not changed, and nothing was restarted or rolled back."


def reclass_plan(item):
    ref = item["ref"]
    phase, claim = ref.get("phase", "stop"), ref["claim"]
    users = [c["name"] for c in ref.get("consumers") or []]
    if phase in RECLASS.BEFORE_SWAP:
        return {"mode": "rollback", "needs": "admin", "severity": "high" if users else "low",
                "undo": ([] if RECLASS._made_nothing(ref) else [f"Stops the copy and deletes the new volume on {ref['target']}"])
                        + RECLASS.cancel_starts(ref),
                "keeps": [f"{claim} and its data were only read, and are as they were"]}
    if phase == "swap":
        return {"can": False, "needs": "admin",
                "why_not": f"{claim} is being swapped onto the new volume. That takes seconds and must not stop "
                           f"part-way. Once it has finished, move {claim} back if needed."}
    return {"mode": "forget", "needs": "admin",
            "keeps": [f"The move has finished: {claim} is on {ref['target']}, and the old copy is kept as "
                      f"{ref.get('old_pv', 'an old copy')}. Only the wait for things to start again stops."]}


def reclass_cancel(item, _options):
    ref = item["ref"]
    if ref.get("phase", "stop") in RECLASS.BEFORE_SWAP:
        said = RECLASS._rollback(ref["namespace"], ref, "")[2]
        return said if str(said).startswith("Cancelled.") else f"Cancelled. {said}"
    return "Stopped waiting. The move itself had finished."


def self_move_plan(item):
    return {"can": False, "needs": "admin", "why_not": "This older data move has no confirmed handoff records. Check both kept volumes before recovering. Nothing is rolled back or deleted automatically."}


def self_move_cancel(item, _options):
    raise ValueError("This older data move must be reviewed manually. Both volumes are kept.")


def disk_retire_plan(item):
    ref = item["ref"]
    phase = ref.get("phase", "stop")
    if phase in ("remove", "cleanup"):
        return {"can": False, "needs": "admin",
                "why_not": "The disk is being removed from Longhorn. That finishes in moments and "
                           "cannot be stopped part-way."}
    # A failed retire holds nothing. The cancel asks this under the job store's lock
    # and marks it active in the same hold (DISKS.retire_held); the review before it
    # asks too, as a preview.
    held = DISKS.retire_held(item) if item.get("status") == "failed" else ""
    if held:
        return {"can": False, "needs": "admin", "why_not": held}
    removed = len(ref.get("removed") or [])
    back = bool(ref.get("was_scheduling", True))
    plan = {"mode": "rollback", "needs": "admin",
            "undo": ["Lets Longhorn place replicas on the disk again, as before"] if back else [],
            "keeps": ([] if back else ["Nothing on the disk changes: it took no new replicas when the retire started"
                                       if item.get("status") == "failed"
                                       else "Scheduling stays off on the disk, as it was when the retire started"])
            + ([f"The {_s(removed, 'failed volume copy', 'failed volume copies')} already released cannot come back. "
                f"{'Its volume is' if removed == 1 else 'Their volumes are'} rebuilt from healthy copies."]
               if removed else ["No volume copy has been removed yet"])}
    if item.get("status") == "failed":
        plan.update(action="Let replicas back" if back else "Close the retire", cleanup_says_action=True,
                    lead=("This disk replacement failed. Cleaning up lets volume copies onto the disk again, and "
                          if back else "This disk replacement failed. Cleaning up closes it and changes nothing on the disk, and ")
                    + "the job stays in the list as failed.")
    return plan


def disk_retire_cancel(item, _options):
    ref = item["ref"]
    back = bool(ref.get("was_scheduling", True))
    if not back and item.get("previous_status") == "failed":
        # Failed, it holds nothing, and a person may have let replicas back since.
        ref["phase"] = "cancelled"
        return "The retire is closed, and nothing on the disk was changed"
    try:
        DISKS._lh_disk(ref["node"], ref["disk"])
    except (ValueError, urllib.error.HTTPError) as gone:  # a merge patch would make a disk with no path
        if getattr(gone, "code", 404) != 404:
            raise
        ref["phase"] = "cancelled"
        return f"The disk is no longer in Longhorn on {ref['node']}, so there was nothing to put back"
    DISKS._patch(f"{DISKS.LH}/nodes/{ref['node']}", {"spec": {"disks": {ref["disk"]: {"allowScheduling": back}}}},
                 "letting replicas onto it again")
    ref["phase"] = "cancelled"
    if back:
        return f"The disk on {ref['node']} takes replicas again, as before"
    return f"The disk on {ref['node']} stays off new replicas, as it was when the retire started"


# --------------------------------------------------------------- the rest
def _helm_action(item):
    return HELM.job_action(item)


def _helm_ns(item):
    return _q((item.get("ref") or {}).get("namespace") or HELM.CONTROLLER_NS)


def _helmchart(item):
    name = item["resource"].get("name", "")
    return name, KUBE.get_optional(f"/apis/helm.cattle.io/v1/namespaces/{_helm_ns(item)}/helmcharts/{_q(name)}", read=kget)


def _moved_since_written(item, chart):
    """Whether the chart is not the one this job's upgrade left: made again, or its
    settings changed since. The controller's own writes (status, finalizer) leave the
    generation be. A job from before this has no such record."""
    ref, meta = item["ref"], chart.get("metadata") or {}
    return bool(ref.get("chart_uid")) and (
        meta.get("uid") != ref["chart_uid"] or meta.get("generation") != ref.get("chart_generation"))


def _changed_words(name):
    return f"{name} has changed since this upgrade was written, so restoring it would undo that. Nothing was changed."


def _no_put_back(item, action, name, chart):
    """Why a failed Helm job may not put its chart back, or "". The earlier settings
    belong to the chart as this job's upgrade left it: one changed or made again
    since has someone's later work on it. A job from before this has no such record."""
    ref = item["ref"]
    if action != "upgrade":
        return f"A failed {action} has nothing to put back"
    if not chart:
        return f"{name}'s chart is gone, so there is nothing to put back"
    kept = HELM.previous_spec(chart)
    if kept is None:
        if ((chart.get("metadata") or {}).get("annotations") or {}).get(HELM.PREVIOUS_SPEC):
            return f"The earlier settings kept on {name} cannot be read, so there is nothing to put back"
        return f"No earlier settings of {name} were kept to go back to"
    if not ref.get("chart_uid"):
        return f"This upgrade is from an older Homestead and kept no record of the chart it wrote, so {name} is not put back from here"
    if HELM.settings({"spec": kept}) == HELM.settings(chart):
        return f"{name}'s settings are already the ones that were kept, so there is nothing to put back"
    if _moved_since_written(item, chart):
        return _changed_words(name)
    if _finished_since(item):
        return f"Helm has since finished this upgrade of {name}, so there is nothing to put back"
    return ""


def _finished_since(item):
    """Whether the chart's Job is done after all: one the controller made after the
    job failed at its wait, or made again after a Job that failed. The Job from before
    this job's write is not that, and a chart moved since is refused before this."""
    ref = item["ref"]
    job = KUBE.get_optional(f"/apis/batch/v1/namespaces/{_q(ref.get('namespace') or HELM.CONTROLLER_NS)}/jobs/"
                            f"{_q(ref.get('name', ''))}", read=kget) or {}
    uid = (job.get("metadata") or {}).get("uid")
    return bool(uid) and uid != ref.get("earlier_job") and bool((job.get("status") or {}).get("succeeded"))


def _went_back(item, chart):
    """A failed upgrade's put-back that went through without saying so: its reply
    was lost, or the server stopped. Its earlier settings are gone from the chart
    it wrote, and the chart's settings moved exactly once since: restore_previous
    takes the settings off in the write that puts them back. Anyone else's change
    leaves them on, so this does not take a later change for it. Only an upgrade
    records a generation."""
    ref, meta = item["ref"], (chart or {}).get("metadata") or {}
    return (bool(ref.get("chart_uid")) and meta.get("uid") == ref["chart_uid"]
            and isinstance(ref.get("chart_generation"), int) and meta.get("generation") == ref["chart_generation"] + 1
            and not (meta.get("annotations") or {}).get(HELM.PREVIOUS_SPEC))


def _was_failed(item):
    return "failed" in (item.get("status"), item.get("previous_status"))


def _chart_seen(chart):
    """The chart a rollback was reviewed on, without keeping its values on the job: a
    later upgrade changes this, and putting the earlier settings back would undo it."""
    meta = chart.get("metadata") or {}
    seen = [meta.get("uid"), chart.get("spec"), (meta.get("annotations") or {}).get(HELM.PREVIOUS_SPEC)]
    return {"chart": hashlib.sha256(json.dumps(seen, sort_keys=True).encode()).hexdigest()}


def helm_plan(item):
    action = _helm_action(item)
    name, chart = _helmchart(item)
    if _was_failed(item) and _went_back(item, chart):
        return {"mode": "rollback", "needs": "admin", "review_title": "Close it", "action": "Close it", "cleanup_says_action": True, "keeps": [],
                "reviewed_target": _reviewed_target(item),
                "lead": f"{name}'s earlier settings are already restored, because a restore finished before it could report it. "
                        "Closing this changes nothing on the chart, and the job stays in the list as failed."}
    # A failed job offers one thing: an upgrade's earlier settings, on the chart it wrote.
    if item.get("status") == "failed":
        why = _no_put_back(item, action, name, chart)
        if why:
            return {"can": False, "needs": "admin", "review_title": "Put back earlier settings", "why_not": why}
    # This cancel's own first run got that far: what it found changed, not what it does. An
    # upgrade that recorded its chart proves that (_went_back); anything else on that
    # chart, or an older job's with no record, falls through or goes by what is there.
    if _reviewed(item) == "rollback" and action != "uninstall" and (
            _went_back(item, chart) if item["ref"].get("chart_uid") else
            not chart or not ((chart.get("metadata") or {}).get("annotations") or {}).get(HELM.PREVIOUS_SPEC)):
        return {"mode": "rollback", "needs": "admin", "severity": "high", "reviewed_target": _reviewed_target(item),
                "undo": [f"Finishes what cancelling {name} began"]}
    if action == "uninstall" or not chart:
        return {"mode": "forget", "needs": "admin", "keeps": [
            f"{name} is already being removed, and Kubernetes cannot undo that. Install it again once it has been removed."]}
    if action == "upgrade":
        if item["ref"].get("no_change"):
            return {"mode": "forget", "needs": "admin", "keeps": [
                f"This upgrade changed nothing on {name}, so there is nothing of its own to put back"]}
        if not ((chart.get("metadata") or {}).get("annotations") or {}).get(HELM.PREVIOUS_SPEC):
            return {"mode": "forget", "needs": "admin", "keeps": [
                f"No earlier settings of {name} were kept to restore, so the upgrade continues."]}
        if HELM.previous_spec(chart) is None:
            return {"mode": "forget", "needs": "admin", "keeps": [_unreadable_words(name)]}
        # The settings kept are from before the latest upgrade, not this one's: going back
        # to them would undo that. A cancel of a failed job's put-back has its own path.
        if not _was_failed(item) and _moved_since_written(item, chart):
            # A put-back cut off may have landed its write, so it can't say nothing was changed.
            # A stop cut off writes nothing: the leader's look runs it again (#1334).
            if item.get("cancel_record") and _reviewed(item) != "forget":
                return {"can": False, "needs": "admin",
                        "why_not": f"{name} has changed since this upgrade was written, so it was not put back"}
            # Cancel is the job's only button: refused, a chart that keeps failing read as
            # running until the job's own time limit. It stops following instead (#1334).
            return {"mode": "forget", "needs": "admin", "keeps": [
                f"{name} changed since this upgrade, so nothing is put back. The later changes stay."]}
        plan = {"mode": "rollback", "needs": "admin", "severity": "high", "reviewed_target": _chart_seen(chart),
                "undo": [f"Restores {name}'s chart version and values from before the upgrade, "
                         "and Helm runs again with them"],
                "keeps": ["Anything the upgrade changed in the chart's own data, such as a database migration, "
                          "is not undone by Helm"]}
        if _was_failed(item):
            plan.update(review_title="Put back earlier settings", action="Put back earlier settings", cleanup_says_action=True,
                        lead="This upgrade failed. Restoring brings back the chart's version and values, "
                             "and the job stays in the list as failed.")
        return plan
    return {"mode": "rollback", "needs": "admin", "severity": "high",
            "undo": [f"Stops the install and deletes the chart {name}: the Helm controller then uninstalls "
                     "whatever it made so far"],
            "keeps": ["Volumes the chart made may be kept, as its own settings say"]}


def _helm_view(item, out):
    """Reads no chart: this runs under the jobs lock on every poll. A chart gone since
    the job failed is refused by helm_plan when the person asks."""
    if out.get("cleanable"):
        ref = item["ref"]
        out["cleanable"] = _helm_action(item) == "upgrade" and bool(ref.get("chart_uid")) and not ref.get("chart_gone")


def _unreadable_words(name):
    return f"The earlier settings kept on {name} cannot be read, so nothing is put back and the upgrade continues."


def helm_cancel(item, options):
    """The Helm controller's status writes move the chart's resourceVersion between
    this read and the put-back's write, and change nothing anyone decided: a 409 reads
    the chart again and runs the cancel once more, with every check on the fresh read.
    Only an upgrade's cancel writes the chart; the other actions keep their answer."""
    try:
        return _helm_cancel(item, options)
    except urllib.error.HTTPError as error:
        if error.code != 409 or _helm_action(item) != "upgrade":
            raise
        error.close()
    try:
        return _helm_cancel(item, options)
    except urllib.error.HTTPError as error:
        if error.code != 409:
            raise
        error.close()
    raise ValueError(f"{item['resource'].get('name', '')} kept being updated while its settings were restored. "
                     "Nothing was put back. Try again in a moment.")


def _helm_cancel(item, _options):
    action = _helm_action(item)
    name, chart = _helmchart(item)
    # Read again here: the review read the chart a moment ago, and this is its own read. So
    # too for the leader running a running upgrade's cancel again: a later upgrade is not undone.
    if item.get("previous_status") == "failed" or (
            _running_again(item) and action == "upgrade" and item["ref"].get("chart_uid") and not item["ref"].get("no_change")
            and _reviewed(item) != "forget"):
        if _went_back(item, chart):
            return f"{name} went back to its settings from before the upgrade"
        why = _no_put_back(item, action, name, chart)
        if why:
            raise ValueError(why)
    if action == "uninstall" or not chart:
        return "Stopped following it. The removal continues."
    ns = _helm_ns(item)
    if action == "upgrade":
        if item["ref"].get("no_change"):
            return "Stopped following it. It changed nothing."
        # The review read the chart a moment ago; this is the canceller's own read.
        if _moved_since_written(item, chart) and HELM.previous_spec(chart) is not None:
            # Stopping was reviewed: nothing is written. A put-back reviewed is refused.
            if _reviewed(item) == "forget":
                return f"Stopped following it. The later changes to {name} stay."
            raise ValueError(_changed_words(name))
        if not HELM.restore_previous(chart):
            # Still there and unreadable: nothing was written, whatever was reviewed.
            if ((chart.get("metadata") or {}).get("annotations") or {}).get(HELM.PREVIOUS_SPEC):
                return (f"Stopped following it. Nothing was put back: the earlier settings kept on {name} "
                        "cannot be read. The upgrade continues.")
            # Restoring drops the settings kept, in the same write: a rollback was
            # reviewed, so a run cut off after it finds them gone, and it went back.
            if (item.get("cancel_record") or {}).get("mode") == "rollback":
                return f"{name} is going back to its settings from before the upgrade"
            return "Stopped following it. The upgrade continues."
        return f"{name} is going back to its settings from before the upgrade"
    charts = f"/apis/helm.cattle.io/v1/namespaces/{ns}/helmcharts/{_q(name)}"
    _delete_saved(item, f"/apis/batch/v1/namespaces/{ns}/jobs/{_q(item['ref']['name'])}?propagationPolicy=Background", charts)
    if item["ref"]["cancel_uids"][charts] != chart["metadata"].get("uid"):
        return f"{name} was made again since this cancel began, so the new one is kept"
    return f"Install stopped. The Helm controller is removing what {name} created."


def smart_plan(item):
    ref = item["ref"]
    return {"mode": "stop", "needs": "admin",
            "undo": [f"Aborts the {ref.get('test', '')} self-test on {ref['disk']} ({ref['node']})"],
            "keeps": ["The drive records the test as stopped. The disk and its data do not change."]}


def smart_cancel(item, _options):
    ref = item["ref"]
    SMART.abort_test(ref["node"], ref["disk"])
    return f"Self-test on {ref['disk']} aborted"


def service_plan(item):
    ref = item["ref"]
    svc = KUBE.get_optional(f"/api/v1/namespaces/{_q(ref['namespace'])}/services/{_q(ref['name'])}", read=kget) or {}
    address = ",".join(i.get("ip", "") for i in ((svc.get("status") or {}).get("loadBalancer") or {}).get("ingress") or []
                       if i.get("ip"))
    return {"mode": "rollback",
            "undo": [f"Removes the Service {ref['namespace']}/{ref['name']}"
                     + (f" and frees {address}" if address else "")],
            "keeps": ["The workload it pointed at is not touched"]}


def service_cancel(item, _options):
    ref = item["ref"]
    path = f"/api/v1/namespaces/{_q(ref['namespace'])}/services/{_q(ref['name'])}"
    _delete_saved(item, path)
    kept = _kept_note(item, path)
    return f"Service stopped{kept}" if kept else f"Service {ref['name']} removed"


def move_plan(item):
    move = MOVE_STATE._find((item.get("ref") or {}).get("move", ""))
    if not move:
        return {"mode": "forget", "needs": "admin", "keeps": ["This move's record is gone"]}
    return {"mode": "rollback", "needs": "admin", "severity": "high",
            "undo": [f"{move['name']} starts again on {move['cluster']}, where it came from",
                     "What had arrived here, the app with its services and volumes, is removed"],
            "keeps": ["Backups made for the move stay in the backup store",
                      "If the move pointed this cluster's Longhorn at the other cluster's backup store, "
                      "it stays pointed there. Change it under Data protection."]}


def move_cancel(item, _options):
    move_id = (item.get("ref") or {}).get("move", "")
    if not MOVE_STATE._find(move_id):
        return "Stopped tracking it. This move's record is removed."
    moved = MOVE_ACTIONS.abandon(move_id)
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
    # Failed k3s jobs allow a retention/tracking review, not automatic deletion.
    ops.CLEANUPS.add("k3s-cluster")
    # An import from a web address that CDI never finishes ran for ever: its canceller
    # deletes only what carries its receipt's stamp, so it may be cancelled (#1338).
    ops.CDI_CANCELLABLE.add("vm-disk-import")
    # A failed Helm upgrade had Cancel until then; its earlier settings are on its chart.
    ops.CLEANUPS.add("helm")
    ops.VIEWS["helm"] = _helm_view
    # A retire that failed waiting on Longhorn had Cancel until then, and its disk
    # still takes no replicas: it can still be put back.
    ops.CLEANUPS.add("disk-retire")
