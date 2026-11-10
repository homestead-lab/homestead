"""Restore into this app (#743): a backup is restored into a new claim, and the
app is moved onto it - the swap the wiki used to leave to the person.

A restore still never overwrites a volume. The job, step by step, each
checkpointed before it writes so a restart carries on and repeats nothing:

  1. restoring: the backup goes into a new claim, as /api/lh/restore does; the
     app keeps running meanwhile;
  2. stopping: the Deployment's replicas are recorded, then it is scaled to 0
     and Homestead waits until no pod uses its claim;
  3. swapping: only the template volume that mounts the claim is pointed at
     the new one, guarded by the Deployment's generation so an edit made
     meanwhile is never overwritten; the old claim is marked as the app's
     previous volume;
  4. starting: the replicas are put back;
  5. watching: the checks of an automatic update - ready in time, then the
     uptime check answers (or, with no check, it stays ready).

If the app does not come up on the restored data, it is swapped back on its
own and the job fails, saying why: the restored claim is then the previous
volume, kept for a person to look at. A failure before the swap starts the app
again unchanged.

Which claims are previous volumes is Homestead's own record on its data
volume (previous-volumes.json), keyed by claim and uid, so whichever replica
leads carries the expiry on. Labels and annotations on the claim only mirror
it for people and kubectl: anyone who may edit claims can write those. A
previous volume is deleted keep_days after it was replaced (default 7; 0 keeps
it until a person deletes it), never less than a day after its reminder was
raised, and never while anything mounts it or a job holds it. Settings never
saved mean the default; settings that cannot be read right now mean nothing
is deleted that hour.

While watching, one bad poll is not enough to swap back: not ready, or a
fresh "down" from the uptime check, must last FLAP_WAIT seconds. The time
limits (ready within ROLLOUT_WAIT, an answer within ANSWER_WAIT) are windows
already and act at once.
"""
import contextlib
import datetime
import hashlib
import json
import os
import re
import secrets
import time
import urllib.error
import urllib.parse

import homestead_alerts as ALERTS
import homestead_autoupdate as AUTOUPDATE
import homestead_cancel as CANCEL
import homestead_job_errors as JOB_ERRORS
import homestead_kube as KUBE
import homestead_leader as LEADER
import homestead_longhorn_restore as RESTORE
import homestead_names as NAMES
import homestead_operations as OPS
import homestead_reclass as RECLASS
import homestead_revert as REVERT
import homestead_routes as MODULE_ROUTES
import homestead_shared as SHARED
import homestead_storage_guard as STORAGE_GUARD
import homestead_updates as UPDATES
import homestead_volumes as VOLUMES

KIND = "restore-swap"
STOP_WAIT = 600                   # the app's pods have this long to let go of the claim;
                                  # a swap back retries passing errors this long too
FLAP_WAIT = 60                    # a failed check must last this long before a swap back
DAY = 86400
DEFAULT_KEEP_DAYS = 7
PREVIOUS = NAMES.key("previous-volume")         # label: "true" on a kept previous volume
KEEP = NAMES.key("keep-previous")               # annotation: "true" when a person chose Keep
REMINDED = NAMES.key("reminded-at")
RESTORED_INTO = NAMES.key("restored-into")
MARKS = tuple(NAMES.key(k) for k in ("previous-of", "replaced-by", "replaced-at", "replaced-job", "replaced-uid",
                                      "replaced-reason", "reminded-at", "keep-previous"))
TERMINAL = ("succeeded", "failed", "cancelled")
# Kinds whose running job holds a claim this job must not touch.
HOLDING = ("reclass", "snapshot-revert", "snapshot-delete", "volume-delete", "volume-restore", KIND)
NAME = re.compile(r"[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?")


# What it reads and writes through, looked up when called so a test can put
# its own in place (HOOKS lists them).
def kget(path, **options):
    return KUBE.kget(path, **options)


def ksend(method, path, body=None, ctype="application/json", **options):
    return KUBE.ksend(method, path, body, ctype, **options)


def fence(handle):
    """Storage moves and this job never meet on one volume (revert does the same)."""
    return STORAGE_GUARD.volume(OPS, kget, handle) if handle else contextlib.nullcontext()


backup_object = lambda name: RESTORE._backup(name)
restore_plan = lambda backup, ns, name: RESTORE.restore_plan(backup, ns, name)
restore_backup = lambda cfg: RESTORE.restore_backup(cfg)
restore_resolver = lambda item: OPS.RESOLVERS["volume-restore"](item)
restore_cancel = lambda item: CANCEL.restore_cancel(item, {})
uptime_report = lambda: AUTOUPDATE.uptime_report()
is_self = lambda ns, name: REVERT.is_self(ns, name)
system_namespaces = lambda: VOLUMES.SYSTEM_NAMESPACES
is_leader = lambda: LEADER.is_leader()
note = lambda fact: ALERTS.note(fact)
deletion_plan = lambda ns, name: VOLUMES.deletion_plan(ns, name)
delete_volume = lambda cfg: VOLUMES.delete_reviewed(cfg)


def settings():
    import homestead_settings_helpers as SETTINGS_HELPERS
    return SETTINGS_HELPERS.get_app_settings()


def settings_strict():
    """The expiry's own reading of keep_days (Andreas, 2026-10-21):
    - settings never saved (no ConfigMap, no settings.json, or no
      restore_swap.keep_days in it): {} - the 7-day default;
    - settings that cannot be read right now (an error or a timeout on the
      read, or a stored value that is not valid): an error, so the expiry
      deletes nothing this hour and tries again the next.
    Only its own section is read: another setting someone broke does not
    stop the expiry."""
    import homestead_settings_helpers as SETTINGS_HELPERS
    path = f"/api/v1/namespaces/{SETTINGS_HELPERS.DEFAULT_NS}/configmaps/{SETTINGS_HELPERS._settings_map()}"
    try:
        cm = SETTINGS_HELPERS.kget(path)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return {}
        raise
    text = (cm.get("data") or {}).get("settings.json")
    if not text:
        return {}
    stored = json.loads(text)
    if not isinstance(stored, dict):
        raise ValueError("the saved settings are not an object")
    section = stored.get("restore_swap")
    if section is None:
        return {}
    if not isinstance(section, dict):
        raise ValueError("restore_swap is not saved as a section")
    if "keep_days" not in section:
        return {}
    days = section["keep_days"]
    if isinstance(days, bool) or not isinstance(days, int) or days < 0 or days > 90:
        raise ValueError(f"restore_swap.keep_days is saved as {days!r}")
    return {"restore_swap": {"keep_days": days}}


def save_settings(change):
    import homestead_settings_helpers as SETTINGS_HELPERS
    return SETTINGS_HELPERS.save_app_settings(change)


# Which claims are previous volumes, kept in Homestead's own store on its data
# volume: "ns/claim" -> {uid, app, replaced_by, replaced_at, job, reason,
# reminded_at, kept}. Labels and annotations on a claim can be written by anyone
# who may edit claims in that namespace, so they only mirror this for people
# and kubectl; nothing is deleted, listed or swapped on their word.
_records_lock = SHARED.SharedLock("previous-volumes")


def _records_path():
    return os.path.join(SHARED.DIR, "previous-volumes.json")


def read_records():
    return SHARED.read_json(_records_path(), {})


def update_records(fn):
    return SHARED.update_file(_records_path(), fn, _records_lock, {})


HOOKS = ("read_records", "update_records", "kget", "ksend", "fence", "OPS", "backup_object", "restore_plan", "restore_backup", "restore_resolver",
         "restore_cancel", "uptime_report", "is_self", "system_namespaces", "is_leader", "note", "deletion_plan",
         "delete_volume", "settings", "settings_strict", "save_settings")


# ---- small helpers -----------------------------------------------------------

def _valid(value, what):
    value = str(value or "").strip()
    if not NAME.fullmatch(value):
        raise ValueError(f"{what} must be a valid Kubernetes name")
    return value


def _pvc_path(ns, name):
    return f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{name}"


def _dep_path(ns, name):
    return f"/apis/apps/v1/namespaces/{ns}/deployments/{name}"


def _digest(*parts):
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:20]


def keep_days(values=None):
    try:
        days = int(((values if values is not None else settings()) or {}).get("restore_swap", {}).get("keep_days", DEFAULT_KEEP_DAYS))
    except (TypeError, ValueError):
        days = DEFAULT_KEEP_DAYS
    return days


def _day(at):
    """"7 Nov": the day unpadded on any platform (strftime's %-d is glibc's)."""
    day = datetime.datetime.fromtimestamp(at, datetime.timezone.utc)
    return f"{day.day} {day:%b}"


def _until(at, days):
    if not days:
        return "until it is deleted"
    return "until " + _day(at + days * DAY)


def _gib(quantity):
    """A claim's requested size in whole GiB (rounded up), 0 when unreadable."""
    text = str(quantity or "").strip()
    units = {"Ki": 1024, "Mi": 1024 ** 2, "Gi": 1024 ** 3, "Ti": 1024 ** 4, "K": 1000, "M": 1000 ** 2, "G": 1000 ** 3, "T": 1000 ** 4}
    for unit, factor in sorted(units.items(), key=lambda u: -len(u[0])):
        if text.endswith(unit):
            text, scale = text[:-len(unit)], factor
            break
    else:
        scale = 1
    try:
        return -(-int(float(text) * scale) // 1024 ** 3)
    except ValueError:
        return 0


def _volumes(dep):
    return ((dep.get("spec") or {}).get("template") or {}).get("spec", {}).get("volumes") or []


def _indexes(dep, claim):
    """Which template volumes mount the claim."""
    return [i for i, v in enumerate(_volumes(dep)) if (v.get("persistentVolumeClaim") or {}).get("claimName") == claim]


def _replicas(dep):
    value = (dep.get("spec") or {}).get("replicas")
    return 1 if value is None else int(value)


def _handle(pvc):
    """The Longhorn volume behind a claim, for the storage fence."""
    pv_name = (pvc.get("spec") or {}).get("volumeName")
    if not pv_name:
        return ""
    pv = KUBE.get_optional(f"/api/v1/persistentvolumes/{pv_name}", read=kget) or {}
    csi = (pv.get("spec") or {}).get("csi") or {}
    return csi.get("volumeHandle", "") if csi.get("driver") == "driver.longhorn.io" else ""


SPECS = (("Deployment", "apps/v1", "deployments", lambda o: o["spec"]["template"]["spec"]),
         ("StatefulSet", "apps/v1", "statefulsets", lambda o: o["spec"]["template"]["spec"]),
         ("DaemonSet", "apps/v1", "daemonsets", lambda o: o["spec"]["template"]["spec"]),
         ("CronJob", "batch/v1", "cronjobs", lambda o: o["spec"]["jobTemplate"]["spec"]["template"]["spec"]))


def _users(ns, claim):
    """What mounts the claim: workloads, and pods that belong to none of them."""
    found = []
    for kind, group, plural, spec_of in SPECS:
        for obj in kget(f"/apis/{group}/namespaces/{ns}/{plural}").get("items", []):
            try:
                if claim in RECLASS._claims_in(spec_of(obj)):
                    found.append({"kind": kind, "name": obj["metadata"]["name"], "obj": obj})
            except (KeyError, TypeError):
                raise ValueError("The app list is incomplete, so what uses the volume cannot be checked") from None
    loose = [p["metadata"]["name"] for p in _pods_using(ns, claim) if not _owned(p, found)]
    return found, loose


def _owned(pod, users):
    """Whether a pod belongs to one of the workloads, by its owner references
    (a Deployment's through its ReplicaSet <name>-<hash>, a CronJob's through
    its Job <name>-<n>): a pod merely named like the app is not the app's."""
    for owner in (pod.get("metadata") or {}).get("ownerReferences") or []:
        kind, name = owner.get("kind"), str(owner.get("name") or "")
        for user in users:
            if (kind == "ReplicaSet" and user["kind"] == "Deployment" and name.rsplit("-", 1)[0] == user["name"]
                    or kind == user["kind"] and kind in ("StatefulSet", "DaemonSet") and name == user["name"]
                    or kind == "Job" and user["kind"] == "CronJob" and name.rsplit("-", 1)[0] == user["name"]):
                return True
    return False


def _pods_using(ns, claim):
    return [p for p in kget(f"/api/v1/namespaces/{ns}/pods").get("items", [])
            if claim in RECLASS._claims_in(p.get("spec")) and (p.get("status") or {}).get("phase") not in ("Succeeded", "Failed")]


def _names_in(ref):
    return {n for n in (ref.get("claim"), ref.get("name"), ref.get("temp"), ref.get("old_claim"), ref.get("new_claim")) if n}


def _busy(items, ns, app, claims):
    """A job still running on the app or one of the claims."""
    for item in items or []:
        if item.get("status") in TERMINAL:
            continue
        ref, resource = item.get("ref") or {}, item.get("resource") or {}
        where = ref.get("namespace") or resource.get("namespace")
        if where != ns:
            continue
        if item.get("kind") == KIND and ref.get("app") == app:
            return item
        if item.get("kind") in HOLDING and (_names_in(ref) | {resource.get("name")}) & set(claims):
            return item
        if resource.get("kind") == "Deployment" and resource.get("name") == app:
            return item
    return None


# ---- the review --------------------------------------------------------------

def _backup_claim(status):
    try:
        k8s = json.loads((status.get("labels") or {}).get("KubernetesStatus") or "{}") or {}
    except (TypeError, ValueError):
        k8s = {}
    return k8s.get("namespace", ""), k8s.get("pvcName", "")


def _free_name(backup, ns, claim, wanted):
    """The new claim's name and the restore plan for it: the one asked for, or
    <claim>-restore, -restore-2 ... when that is taken."""
    if wanted:
        return wanted, restore_plan(backup, ns, wanted)
    base = (claim[:240] + "-restore")
    for n in range(1, 10):
        name = base if n == 1 else f"{base}-{n}"
        found = restore_plan(backup, ns, name)
        if not found.get("conflict"):
            return name, found
    return base, restore_plan(backup, ns, base)


def _restore_classes(found, volume_mode):
    """The classes this volume can be restored onto, and why not the others:
    ready has to mean the job's first step will not refuse it."""
    classes, refused = [], dict(found.get("refused_classes") or {})
    for name in found.get("storage_classes") or []:
        why = RESTORE.class_refusal(KUBE.get_optional(f"/apis/storage.k8s.io/v1/storageclasses/{name}", read=kget) or {},
                                    volume_mode)
        if why:
            refused[name] = why
        else:
            classes.append(name)
    return classes, refused


def plan(backup, ns="", app="", claim="", new_name="", storage_class="", size_gb=None, now=None):
    """What restoring into the app would do and what stops it, before anything changes."""
    now = now or time.time()
    item = backup_object(str(backup or ""))
    status = item.get("status") or {}
    blockers, notices = [], []
    try:
        RESTORE._restore_source(item)
    except ValueError as error:
        blockers.append(str(error))
    source_ns, source_claim = _backup_claim(status)
    ns, claim = ns or source_ns, claim or source_claim
    if not ns or not claim:
        raise ValueError("This backup does not say which volume it came from. Choose the app and its volume.")
    ns, claim = _valid(ns, "namespace"), _valid(claim, "volume name")
    if new_name:
        new_name = _valid(new_name, "the new volume's name")
    if storage_class:
        storage_class = _valid(storage_class, "storage class")
    if ns in system_namespaces():
        blockers.append("system namespaces are changed with Kubernetes administration tools, not here")
    pvc = KUBE.get_optional(_pvc_path(ns, claim), read=kget)
    if pvc is None:
        raise ValueError(f"{ns}/{claim} no longer exists. Restore the backup into a new volume instead.")
    users, loose = _users(ns, claim)
    deployments = [u for u in users if u["kind"] == "Deployment"]
    if not app:
        app = deployments[0]["name"] if len(deployments) == 1 else (users[0]["name"] if len(users) == 1 else "")
    app = _valid(app, "app") if app else ""
    mine = next((u for u in users if u["name"] == app), None)
    dep = mine["obj"] if mine and mine["kind"] == "Deployment" else {}
    if not app or not mine:
        blockers.append(f"No app mounts {claim}. Restore the backup into a new volume instead.")
    elif mine["kind"] != "Deployment":
        blockers.append(f"{app} is a {mine['kind']}: restoring into one is not supported yet. "
                        "Restore the backup into a new volume instead.")
    others = [u for u in users if u is not mine]
    if others:
        blockers.append(f"{claim} is also used by {', '.join(u['kind'] + ' ' + u['name'] for u in others[:3])}. "
                        "Only a volume that one app uses can be swapped.")
    if loose:
        blockers.append(f"Stop {', '.join(loose[:3])} first. It use{'s' if len(loose) == 1 else ''} {claim} and belong"
                        f"{'s' if len(loose) == 1 else ''} to nothing Homestead can stop and start again")
    if app and is_self(ns, app) or claim == "homestead-data":
        blockers.append("this is Homestead's own data: swapping it would stop Homestead part-way")
    name, found = _free_name(item["metadata"]["name"], ns, claim, new_name)
    if found.get("conflict"):
        blockers.append(found["conflict"]["message"])
    busy = _busy(OPS.snapshot(), ns, app, (claim, name))
    if busy:
        blockers.append(f"{app or claim} already has a job running ({busy.get('title') or busy.get('kind')}). "
                        "Let it finish first.")
    managed = UPDATES.managed_by(dep) if dep else {}
    if managed:
        notices.append(f"{app} is applied by {managed['by']}"
                       + (f" ({managed['source']})" if managed.get("source") else "")
                       + f": its next sync puts {claim} back. Change the claim where {managed['by']} reads it too.")
    others_mounted = sorted({(v.get("persistentVolumeClaim") or {}).get("claimName") for v in _volumes(dep)} - {None, "", claim})
    if others_mounted:
        notices.append(f"{app} also uses {', '.join(others_mounted)}. Only {claim} is restored, "
                       "so what they hold may no longer match.")
    indexes = _indexes(dep, claim) if dep else []
    volume = _volumes(dep)[indexes[0]]["name"] if indexes else ""
    containers = [{"name": c.get("name", ""), "path": m.get("mountPath", "")}
                  for c in ((dep.get("spec") or {}).get("template") or {}).get("spec", {}).get("containers") or []
                  for m in c.get("volumeMounts") or [] if m.get("name") == volume] if dep else []
    spec = pvc.get("spec") or {}
    classes, refused_classes = _restore_classes(found, spec.get("volumeMode") or "Filesystem")
    current_class = spec.get("storageClassName", "")
    # A refused class is named, not swapped for the default in silence: the review asks for another.
    chosen_class = storage_class or (current_class if current_class in classes or current_class in refused_classes
                                     else found.get("storage_class", ""))
    if chosen_class not in classes:
        why = refused_classes.get(chosen_class) or f"{chosen_class or 'No class'} is not a storage class a restore can use"
        blockers.append(f"{why}; " + ("choose another storage class" if classes
                                      else "no other class can take it, so make one first"))
    access = (spec.get("accessModes") or ["ReadWriteOnce"])[0]
    if access not in ("ReadWriteOnce", "ReadWriteMany"):
        blockers.append(f"{claim} is {access}. A restore makes only ReadWriteOnce or ReadWriteMany volumes.")
    current_gb = _gib(((spec.get("resources") or {}).get("requests") or {}).get("storage"))
    minimum = int(found.get("minimum_size_gb") or 1)
    size = int(size_gb or max(minimum, current_gb))
    # Bytes, as the restore itself compares: 11 GiB is larger than a 10.5 GiB backup though both round to 11.
    backup_bytes = int(found.get("volume_size_bytes") or minimum * 1024 ** 3)
    backup_gib = f"{backup_bytes / 1024 ** 3:.1f}".removesuffix(".0")
    if size < minimum:
        blockers.append(f"{claim} is {size} GiB, smaller than this backup's {backup_gib} GiB. "
                        f"Choose a size of at least {minimum} GiB")
    if size * 1024 ** 3 > backup_bytes and chosen_class:
        sc = KUBE.get_optional(f"/apis/storage.k8s.io/v1/storageclasses/{chosen_class}", read=kget) or {}
        if sc.get("allowVolumeExpansion") is not True:
            blockers.append(f"Choose a class that allows expansion. {claim} is {size} GiB, larger than this backup's "
                            f"{backup_gib} GiB, and {chosen_class} cannot grow a restored volume")
    days = keep_days()
    answers = uptime_report().get(f"{ns}/{app}") or {}
    meta = dep.get("metadata") or {}
    out = {
        "app": {"namespace": ns, "name": app, "kind": mine["kind"] if mine else "", "replicas": _replicas(dep) if dep else 0},
        "claim": claim, "mount": {"volume": volume, "containers": containers},
        "backup": item["metadata"]["name"], "created": status.get("backupCreatedAt", ""),
        "new_claim": name, "storage_class": chosen_class, "storage_classes": classes,
        "refused_classes": refused_classes, "size_gb": size,
        "minimum_size_gb": found.get("minimum_size_gb"),
        "steps": ["restore", "stop", "swap", "start", "watch"],
        "check": "uptime" if answers and answers.get("state") != "off" else "ready",
        "keep_days": days, "expires_preview": "" if not days else datetime.datetime.fromtimestamp(
            now + days * DAY, datetime.timezone.utc).strftime("%Y-%m-%d"),
        "managed": managed, "blockers": blockers, "notices": notices,
    }
    out["digest"] = _digest(ns, app, claim, (pvc.get("metadata") or {}).get("uid"), meta.get("generation"),
                            out["backup"], name, chosen_class, size)
    out["ready"] = not blockers
    out["_internal"] = {"pvc": pvc, "dep": dep, "indexes": indexes, "access_mode": access,
                        "volume_mode": spec.get("volumeMode") or "Filesystem",
                        "source_size_bytes": found.get("volume_size_bytes")}
    return out


def public(p):
    return {k: v for k, v in p.items() if not k.startswith("_")}


def start(body):
    """Restore and swap, as reviewed: a 409 with a fresh plan when the review
    is stale. The review reads a lot, so it runs outside the jobs' lock; under
    it only what could have changed is checked again, and the job is
    recorded. The job itself makes the new claim, so a start the job store
    refuses leaves nothing behind."""
    b = body or {}
    p = plan(b.get("backup"), b.get("namespace") or "", b.get("app") or "", b.get("claim") or "",
             b.get("new_name") or "", b.get("storage_class") or "", b.get("size_gb"))
    stale = MODULE_ROUTES.Reply(409, {"error": "The app, its volume or the backup changed since this review. "
                                               "Check the plan again.", "plan": public(p)})
    if str(b.get("digest") or "") != p["digest"]:
        return stale
    if p["blockers"]:
        raise ValueError(p["blockers"][0])
    if p["managed"] and b.get("accept_gitops") is not True:
        raise ValueError(f"{p['app']['name']} is applied by {p['managed']['by']}, whose next sync puts the old "
                         "volume back. Confirm that this has been read to continue.")
    ns, app, old, new = p["app"]["namespace"], p["app"]["name"], p["claim"], p["new_claim"]
    pvc, dep, inner = p["_internal"]["pvc"], p["_internal"]["dep"], p["_internal"]
    restore_id = secrets.token_hex(12)
    cfg = {"backup": p["backup"], "namespace": ns, "name": new, "storage_class": p["storage_class"],
           "size_gb": p["size_gb"], "access_mode": inner["access_mode"], "volume_mode": inner["volume_mode"],
           "restore_id": restore_id, "source_size_bytes": inner["source_size_bytes"],
           "annotations": {"homestead.io/restore-id": restore_id, RESTORED_INTO: f"{ns}/{app}"}}
    ref = {"namespace": ns, "app": app, "kind": "Deployment", "volume": p["mount"]["volume"],
           "indexes": inner["indexes"], "old_claim": old, "new_claim": new,
           "old_uid": (pvc.get("metadata") or {}).get("uid", ""), "old_volume": _handle(pvc),
           "backup": p["backup"], "generation": (dep.get("metadata") or {}).get("generation"), "new_uid": "",
           "direction": "restore", "phase": "restoring", "since": time.time(),
           "restore": {"namespace": ns, "name": new, "backup": p["backup"], "restore_config": cfg,
                       "restore_started": False}}
    with OPS.transaction():
        now_dep = KUBE.get_optional(_dep_path(ns, app), read=kget) or {}
        now_pvc = KUBE.get_optional(_pvc_path(ns, old), read=kget) or {}
        if ((now_dep.get("metadata") or {}).get("generation") != (dep.get("metadata") or {}).get("generation")
                or (now_pvc.get("metadata") or {}).get("uid") != ref["old_uid"]):
            return stale
        if KUBE.get_optional(_pvc_path(ns, new), read=kget) is not None:
            raise ValueError(f"{ns}/{new} exists now. Check the plan again for a free name.")
        busy = _busy(OPS.snapshot(), ns, app, (old, new))
        if busy:
            raise ValueError(f"{app} already has a job running ({busy.get('title') or busy.get('kind')}). Let it finish first.")
        operation = OPS.start(KIND, f"Restore {app} from a backup", {"kind": "Deployment", "name": app, "namespace": ns},
                              "/data-protection", ref, f"Restoring into {new}")
    return {"ok": True, "operation": operation, "name": new,
            "detail": f"Restoring into {new}. {app} keeps running until the data is ready."}


# ---- the job ----------------------------------------------------------------

def _scale(ref, replicas):
    return ksend("PATCH", _dep_path(ref["namespace"], ref["app"]), {"spec": {"replicas": replicas}},
                 ctype="application/merge-patch+json")


def _point(ref, generation, frm, to):
    """Only the template volumes that mount frm, and only if nothing else
    changed the Deployment since: Kubernetes refuses the whole patch otherwise."""
    ops = [{"op": "test", "path": "/metadata/generation", "value": generation}] if generation is not None else []
    for i in ref["indexes"]:
        where = f"/spec/template/spec/volumes/{i}/persistentVolumeClaim/claimName"
        ops += [{"op": "test", "path": where, "value": frm}]
    for i in ref["indexes"]:
        ops += [{"op": "replace", "path": f"/spec/template/spec/volumes/{i}/persistentVolumeClaim/claimName", "value": to}]
    return ksend("PATCH", _dep_path(ref["namespace"], ref["app"]), ops, ctype="application/json-patch+json")


def _points_at(dep, ref):
    claims = {(_volumes(dep)[i].get("persistentVolumeClaim") or {}).get("claimName")
              for i in ref["indexes"] if i < len(_volumes(dep))}
    return claims.pop() if len(claims) == 1 else ""


def _mark(item, claim, expected_uid, replaced_by, reason, now, kept=False):
    """Keep a claim as the app's previous volume, from now: Homestead's own
    record first (what the expiry trusts), then the marks on the claim that
    show it to people and to kubectl. Only the claim the job knows - the uid
    it reviewed or made: one made again under the name since is someone's
    data, and is neither recorded nor marked. True when it is recorded."""
    ref = item["ref"]
    ns = ref["namespace"]
    pvc = KUBE.get_optional(_pvc_path(ns, claim), read=kget)
    uid = ((pvc or {}).get("metadata") or {}).get("uid", "")
    if not pvc or not expected_uid or uid != expected_uid:
        return False
    key = f"{ns}/{claim}"
    known = read_records().get(key) or {}
    labelled = ((pvc.get("metadata") or {}).get("labels") or {}).get(PREVIOUS) == "true"
    if known.get("job") == item.get("id") and known.get("reason") == reason and known.get("uid") == uid and labelled:
        return True     # marked by this job already: a restart keeps its date

    if known.get("job") == item.get("id") and known.get("reason") == reason and known.get("uid") == uid:
        now = known.get("replaced_at") or now     # the record was written; only the marks failed

    def put(records):
        records[key] = {"uid": uid, "app": ref["app"], "replaced_by": replaced_by, "replaced_at": int(now),
                        "replaced_by_uid": ref.get("old_uid") if replaced_by == ref.get("old_claim") else ref.get("new_uid"),
                        "job": item.get("id", ""), "reason": reason, "reminded_at": None, "kept": bool(kept)}
    update_records(put)
    ksend("PATCH", _pvc_path(ns, claim), {"metadata": {
        "labels": {PREVIOUS: "true"},
        "annotations": {NAMES.key("previous-of"): f"{ns}/{ref['app']}",
                        NAMES.key("replaced-by"): replaced_by, NAMES.key("replaced-at"): str(int(now)),
                        NAMES.key("replaced-job"): item.get("id", ""), NAMES.key("replaced-uid"): uid,
                        NAMES.key("replaced-reason"): reason, REMINDED: None, KEEP: "true" if kept else None}}},
          ctype="application/merge-patch+json")
    return True


def _forget(ns, claim):
    update_records(lambda records: records.pop(f"{ns}/{claim}", None))


def _unmark(ns, claim):
    _forget(ns, claim)
    if KUBE.get_optional(_pvc_path(ns, claim), read=kget) is None:
        return
    ksend("PATCH", _pvc_path(ns, claim), {"metadata": {"labels": {PREVIOUS: None},
                                                       "annotations": {k: None for k in MARKS}}},
          ctype="application/merge-patch+json")


def _days():
    try:
        return keep_days()
    except Exception:
        return DEFAULT_KEEP_DAYS


def _ours(pvc, ref):
    """Whether a claim is the one this job's restore made: its restore-id."""
    restore_id = ref.get("restore", {}).get("restore_config", {}).get("restore_id")
    notes = ((pvc or {}).get("metadata") or {}).get("annotations") or {}
    return bool(restore_id) and notes.get("homestead.io/restore-id") == restore_id


def _put_back(item, why, now):
    """Before the swap: the app runs again as it was; a restored claim that
    was made is kept as a previous volume, so it expires."""
    ref = item["ref"]
    # Decided: from here the job only puts back, even if a write below fails
    # and the next poll has to carry it on (the "put-back" step).
    if ref.get("phase") != "put-back":
        ref.update(phase="put-back", why=why, since=now)
    with fence(ref.get("old_volume")):
        if ref.get("stopped"):
            _scale(ref, ref.get("replicas_before", 1))
        made = ""
        if ref["direction"] == "restore":
            pvc = KUBE.get_optional(_pvc_path(ref["namespace"], ref["new_claim"]), read=kget)
            if pvc and _ours(pvc, ref):
                # Its uid as this restore made it (the restore-id proves it), when not known yet.
                uid = ref.get("new_uid") or (pvc.get("metadata") or {}).get("uid", "")
                if _mark(item, ref["new_claim"], uid, ref["old_claim"], "not-used", now):
                    made = ref["new_claim"]
    tail = f" The restored volume {made} is kept {_until(now, _days())}." if made else ""
    return "failed", 100, f"{why}. Nothing was changed, and {ref['app']} runs on {ref['old_claim']} as before.{tail}"


def _swap_back_writes(item, now):
    """Point the app at the claim it left, and keep the other as the previous volume."""
    ref = item["ref"]
    with fence(ref.get("old_volume")):
        _scale(ref, 0)
        dep = kget(_dep_path(ref["namespace"], ref["app"]))
        if _points_at(dep, ref) != ref["old_claim"]:
            _point(ref, None, ref["new_claim"], ref["old_claim"])
        _unmark(ref["namespace"], ref["old_claim"])
        # A volume a person chose to keep stays kept after a swap back that failed.
        _mark(item, ref["new_claim"], ref.get("new_uid"), ref["old_claim"], "not-started", now, kept=ref.get("new_kept"))
        _scale(ref, ref.get("replicas_before", 1))


def _pointed_at(ref):
    try:
        return _points_at(kget(_dep_path(ref["namespace"], ref["app"])), ref) or "a volume Homestead could not read"
    except Exception:
        return "a volume Homestead could not read"


def _swap_back(item, to, why, now):
    ref = item["ref"]
    if ref.get("phase") != "swapping-back":
        to("swapping-back", why=why)
    try:
        _swap_back_writes(item, now)
    except Exception as error:
        refused = isinstance(error, urllib.error.HTTPError) and error.code in (409, 422)
        if not refused and now - float(ref.get("since") or now) <= STOP_WAIT:
            raise           # a passing error: the job stays running and the next poll tries again
        try:
            _scale(ref, ref.get("replicas_before", 1))
            state = f"{ref['app']} was started again, on {_pointed_at(ref)}"
        except Exception:
            state = (f"{ref['app']} is stopped (0 replicas) and points at {_pointed_at(ref)}. "
                     "Point it at the right volume with Edit, then start it with Scale.")
        return ("failed", 100, f"On the restored volume {ref['app']} {why}, and swapping back did not work either: "
                f"{str(error)[:160]}. {state}. Its previous volume is {ref['old_claim']}.")
    to("back-starting", back_at=now)
    return "running", 95, f"Swapped back to {ref['old_claim']}. Waiting for {ref['app']} to be ready."


def resolve(item, checkpoint, now=None):
    """Move one restore-into-app job on a step: (status, progress, message)."""
    now = now or time.time()
    ref = item["ref"]
    ns, app, old, new = ref["namespace"], ref["app"], ref["old_claim"], ref["new_claim"]
    phase = ref.get("phase", "restoring")

    def to(next_phase, **changes):
        ref.update(changes, phase=next_phase, since=now)
        checkpoint(item)

    if phase == "restoring":
        sub = {"id": item.get("id"), "status": item.get("status"), "ref": ref["restore"], JOB_ERRORS.KEY: item.get(JOB_ERRORS.KEY)}
        try:
            status, progress, message = restore_resolver(sub)
        except ValueError as error:          # refused, not a passing error: the name is taken, CSI never ready
            return _put_back(item, f"The restore could not go on ({str(error)[:160]})", now)
        if status == "running":
            return "running", min(40, int(progress * 0.4)), f"Restoring into {new}: {message}"
        if status != "succeeded":
            return _put_back(item, f"The restore did not finish ({message})", now)
        pvc = kget(_pvc_path(ns, new))
        uid = (pvc.get("metadata") or {}).get("uid", "")
        if not _ours(pvc, ref) or (ref.get("new_uid") and uid != ref["new_uid"]):
            return _put_back(item, f"{new} was replaced by another claim of the same name", now)
        to("stopping", new_uid=uid)
        phase = "stopping"
    if phase == "stopping":
        if not ref.get("stopped"):
            if "replicas_before" not in ref:
                to("stopping", replicas_before=_replicas(kget(_dep_path(ns, app))))
            with fence(ref.get("old_volume")):
                scaled = _scale(ref, 0)
            ref.update(stopped=True, generation=(scaled.get("metadata") or {}).get("generation"))
            checkpoint(item)
        using = _pods_using(ns, old)
        if using:
            if now - float(ref.get("since") or now) > STOP_WAIT:
                return _put_back(item, f"{app} did not release {old} in time. "
                                       f"{using[0]['metadata']['name']} still uses it", now)
            return "running", 45, f"Stopping {app}"
        to("swapping")
        phase = "swapping"
    if phase == "swapping":
        dep = kget(_dep_path(ns, app))
        if _points_at(dep, ref) != new:
            for claim, uid in ((old, ref.get("old_uid")), (new, ref.get("new_uid"))):
                live = KUBE.get_optional(_pvc_path(ns, claim), read=kget)
                if not live or not uid or (live.get("metadata") or {}).get("uid") != uid:
                    return _put_back(item, f"{claim} was replaced by another claim of the same name, "
                                           f"so {app} was not swapped", now)
            try:
                with fence(ref.get("old_volume")):
                    _point(ref, ref.get("generation"), old, new)
            except urllib.error.HTTPError as error:
                if error.code not in (409, 422):
                    raise
                return _put_back(item, f"{app} was changed while it was stopped, so it was not swapped", now)
        _mark(item, old, ref.get("old_uid"), new, "restored" if ref["direction"] == "restore" else "swapped-back", now)
        if ref["direction"] == "restore":
            ksend("PATCH", _pvc_path(ns, new), {"metadata": {"annotations": {RESTORED_INTO: f"{ns}/{app}"}}},
                  ctype="application/merge-patch+json")
        else:
            _unmark(ns, new)
        to("starting", swapped=True)
        phase = "starting"
    if phase == "starting":
        if ref.get("replicas_before", 1) == 0:
            return "succeeded", 100, f"{app} now uses {new}. It stays stopped, as it was. {_kept(ref, now)}"
        with fence(ref.get("old_volume")):
            _scale(ref, ref.get("replicas_before", 1))
        to("watching", started_at=now)
        return "running", 70, f"{app} is starting on {new}"
    if phase == "watching":
        dep = kget(_dep_path(ns, app))
        if not ref.get("rolled_at"):
            if AUTOUPDATE._rolled_out(dep):
                to("watching", rolled_at=now)
                return "running", 80, f"{app} is ready. Waiting for it to answer."
            if now - float(ref.get("started_at") or now) > AUTOUPDATE.ROLLOUT_WAIT:
                return _swap_back(item, to, f"was not ready after {AUTOUPDATE.ROLLOUT_WAIT // 60} minutes", now)
            return "running", 75, f"Waiting for {app} to be ready"
        if not AUTOUPDATE._rolled_out(dep):
            if _still_bad(ref, now):
                return _swap_back(item, to, "stopped being ready", now)
            return "running", 82, f"{app} is not ready. Checking again before swapping back."
        state = uptime_report().get(f"{ns}/{app}") or {}
        last = state.get("last") or {}
        fresh = (last.get("at") or 0) > ref["rolled_at"]
        if fresh and state.get("state") == "down":
            if _still_bad(ref, now):
                return _swap_back(item, to, f"stopped answering ({last.get('error') or 'no answer'})", now)
            return "running", 86, f"{app} did not answer. Checking again before swapping back."
        ref.pop("bad_since", None)
        if state.get("state") == "off" or not state:
            if now - ref["rolled_at"] >= AUTOUPDATE.STEADY:
                return "succeeded", 100, f"{app} runs on {new}. {_kept(ref, now)}"
            return "running", 85, f"{app} is ready. Making sure it stays up."
        if fresh and state.get("state") in ("up", "slow"):
            return "succeeded", 100, f"{app} runs on {new}. {_kept(ref, now)}"
        if now - ref["rolled_at"] > AUTOUPDATE.ANSWER_WAIT:
            return _swap_back(item, to, f"did not answer within {AUTOUPDATE.ANSWER_WAIT // 60} minutes", now)
        return "running", 88, f"Waiting for {app} to answer"
    if phase == "swapping-back":
        return _swap_back(item, to, ref.get("why") or "did not come up", now)
    if phase == "put-back":
        return _put_back(item, ref.get("why") or "It was stopped", now)
    if phase == "back-starting":
        dep = kget(_dep_path(ns, app))
        said = (f"Swapped back: on the {'restored' if ref['direction'] == 'restore' else 'other'} volume {app} "
                f"{ref.get('why') or 'did not come up'}. It runs on its previous volume {old} again. "
                f"The {'restored' if ref['direction'] == 'restore' else 'other'} volume {new} is kept "
                f"{_until(float(ref.get('back_at') or now), _days())} so it can be checked.")
        if ref.get("replicas_before", 1) == 0 or AUTOUPDATE._rolled_out(dep):
            return "failed", 100, said
        if now - float(ref.get("back_at") or now) > AUTOUPDATE.ROLLOUT_WAIT:
            return "failed", 100, said + f" It is not ready on {old} yet either. Check its pods."
        return "running", 96, f"Swapped back to {old}. Waiting for {app} to be ready."
    return "failed", item.get("progress", 0), f"Unknown step {phase}"


def _still_bad(ref, now):
    """Whether a failed check has lasted FLAP_WAIT: one bad poll - a pod
    restarting, a scale by hand - is not a reason to move the app's data."""
    since = float(ref.setdefault("bad_since", now))
    return now - since >= FLAP_WAIT


def _kept(ref, now):
    return f"Its previous volume {ref['old_claim']} is kept {_until(now, _days())}."


def cancel_plan(item):
    ref = item["ref"]
    if ref.get("phase") in ("swapping-back", "back-starting"):
        return {"mode": "stop", "can": False, "why_not": "It is already swapping back. Let that finish."}
    if ref.get("swapped") or ref.get("phase") in ("starting", "watching"):
        return {"mode": "rollback", "needs": "admin", "severity": "low",
                "undo": [f"{ref['app']} is stopped, pointed back at {ref['old_claim']} and started again"],
                "keeps": [f"{ref['new_claim']} is kept as a previous volume, {_until(time.time(), _days())}"]}
    undo = ([f"The restore stops and {ref['new_claim']}, the volume it was making, is deleted"]
            if ref["direction"] == "restore" else [])
    return {"mode": "rollback", "needs": "admin", "severity": "low",
            "undo": undo + [f"{ref['app']} is started again, unchanged"],
            "keeps": ["The backup", f"{ref['old_claim']}, which has not been changed"]}


def cancel_run(item, _options):
    ref = item["ref"]
    now = time.time()
    swapped = ref.get("swapped")
    if not swapped and ref.get("phase") == "swapping":
        swapped = _points_at(kget(_dep_path(ref["namespace"], ref["app"])), ref) == ref["new_claim"]
    if swapped:
        _swap_back_writes(item, now)
        ref.update(phase="cancelled", since=now)
        return (f"Swapped back: {ref['app']} runs on {ref['old_claim']} again. "
                f"{ref['new_claim']} is kept {_until(now, _days())}.")
    made = KUBE.get_optional(_pvc_path(ref["namespace"], ref["new_claim"]), read=kget) if ref["direction"] == "restore" else None
    if made and _ours(made, ref) and (not ref.get("new_uid") or (made.get("metadata") or {}).get("uid") == ref["new_uid"]):
        restore_cancel({"ref": {"namespace": ref["namespace"], "name": ref["new_claim"], "backup": ref["backup"]}})
    if ref.get("stopped"):
        with fence(ref.get("old_volume")):
            _scale(ref, ref.get("replicas_before", 1))
    ref.update(phase="cancelled", since=now)
    return f"Cancelled. Nothing was changed, and {ref['app']} runs on {ref['old_claim']} as before."


# ---- previous volumes --------------------------------------------------------

def _label(app):
    """An app's name as it may be shown: a Kubernetes name, or nothing."""
    return app if isinstance(app, str) and NAME.fullmatch(app) else ""


def _due(record, days):
    """When it would be deleted under today's setting: None when kept."""
    if record.get("kept") or not days:
        return None
    expires = float(record.get("replaced_at") or 0) + days * DAY
    reminded = float(record.get("reminded_at") or 0)
    return max(expires, reminded + DAY) if reminded else expires


def _kept_claims():
    """[(ns, name, record, claim)] for each record whose claim is still the
    one recorded (same uid); claim is None when that is not so."""
    out = []
    for key, record in sorted(read_records().items()):
        ns, _, name = key.partition("/")
        if not (NAME.fullmatch(ns) and NAME.fullmatch(name)) or not isinstance(record, dict):
            continue
        pvc = KUBE.get_optional(_pvc_path(ns, name), read=kget)
        same = pvc and (pvc.get("metadata") or {}).get("uid") == record.get("uid")
        out.append((ns, name, record, pvc if same else None))
    return out


def _back_digest(ns, app, current, name, uid, dep):
    return _digest(ns, app, current, name, uid, ((dep or {}).get("metadata") or {}).get("generation"))


def _row(ns, name, record, pvc, days, pods=None, dep=None):
    spec = pvc.get("spec") or {}
    due, app, current = _due(record, days), _label(record.get("app")), record.get("replaced_by", "")
    return {"namespace": ns, "name": name, "app": app, "replaced_by": current,
            "replaced_at": int(record.get("replaced_at") or 0) or None,
            "expires_at": int(due) if due else None,
            "reminded_at": int(record["reminded_at"]) if record.get("reminded_at") else None,
            "kept": bool(record.get("kept")), "reason": record.get("reason", "restored"),
            "size": ((spec.get("resources") or {}).get("requests") or {}).get("storage", ""),
            "storage_class": spec.get("storageClassName", ""), "volume": spec.get("volumeName", ""),
            "uid": record.get("uid", ""),
            "in_use": any(name in RECLASS._claims_in(p.get("spec")) for p in pods or []),
            "can_swap_back": bool(dep and _indexes(dep, current)),
            "managed": (UPDATES.managed_by(dep) or {}).get("by", "") if dep else "",
            "digest": _back_digest(ns, app, current, name, record.get("uid", ""), dep) if dep else ""}


def previous_volumes(now=None):
    """The volumes apps were moved off, and when each goes."""
    days = keep_days()
    pods, deps, rows = {}, {}, []
    for ns, name, record, pvc in _kept_claims():
        if not pvc:
            continue
        app = _label(record.get("app"))
        if ns not in pods:
            pods[ns] = [p for p in kget(f"/api/v1/namespaces/{ns}/pods").get("items", [])
                        if (p.get("status") or {}).get("phase") not in ("Succeeded", "Failed")]
        if (ns, app) not in deps:
            deps[(ns, app)] = KUBE.get_optional(_dep_path(ns, app), read=kget) if app else None
        rows.append(_row(ns, name, record, pvc, days, pods[ns], deps[(ns, app)]))
    return {"keep_days": days, "volumes": rows}


def _previous_claim(body):
    """A claim Homestead itself recorded as a previous volume, still the same claim."""
    b = body or {}
    ns, name = _valid(b.get("namespace"), "namespace"), _valid(b.get("name"), "volume name")
    record = read_records().get(f"{ns}/{name}")
    pvc = KUBE.get_optional(_pvc_path(ns, name), read=kget) if record else None
    if not record or not pvc or (pvc.get("metadata") or {}).get("uid") != record.get("uid"):
        raise ValueError(f"{ns}/{name} is not a previous volume")
    return ns, name, record, pvc


def keep(body, role):
    """Keep stops the expiry (operator: it only prevents a deletion); un-keep
    starts it again (admin: it re-arms one). Either way a fresh reminder comes
    a day before it goes."""
    want = (body or {}).get("keep")
    if not isinstance(want, bool):
        raise ValueError("keep must be true or false")
    if not want and role != "admin":
        raise PermissionError("letting a previous volume expire again needs the admin role")
    ns, name, record, _ = _previous_claim(body)

    def change(records):
        now_record = records.get(f"{ns}/{name}")
        if not now_record or now_record.get("uid") != record.get("uid"):
            raise ValueError(f"{ns}/{name} is not a previous volume")    # dropped or replaced meanwhile
        now_record.update(kept=want, reminded_at=None, held=None)
    update_records(change)
    ksend("PATCH", _pvc_path(ns, name), {"metadata": {"annotations": {KEEP: "true" if want else None, REMINDED: None}}},
          ctype="application/merge-patch+json")
    record = read_records()[f"{ns}/{name}"]
    return {**_row(ns, name, record, kget(_pvc_path(ns, name)), keep_days()), "detail": (
        f"{name} is kept until someone deletes it" if want else f"{name} expires again, with a reminder the day before")}


def _swap_blockers(ns, app, claims, dep):
    """What stops pointing the app at another claim: as the review of a
    restore into the app says it."""
    blockers = []
    if ns in system_namespaces():
        blockers.append("system namespaces are changed with Kubernetes administration tools, not here")
    if is_self(ns, app) or "homestead-data" in claims:
        blockers.append("this is Homestead's own data: swapping it would stop Homestead part-way")
    for claim in claims:
        users, loose = _users(ns, claim)
        others = [u for u in users if (u["kind"], u["name"]) != ("Deployment", app)]
        if others:
            blockers.append(f"{claim} is also used by {', '.join(u['kind'] + ' ' + u['name'] for u in others[:3])}. "
                            "Only a volume that one app uses can be swapped.")
        if loose:
            blockers.append(f"Stop {', '.join(loose[:3])} first. It use{'s' if len(loose) == 1 else ''} {claim} and belong"
                            f"{'s' if len(loose) == 1 else ''} to nothing Homestead can stop and start again")
    return blockers


def swap_back(body):
    """Point the app back at a previous volume: the same job, the other way
    round; the volume it leaves becomes the previous one. The app and the
    claim it runs on now come from Homestead's record, not the claim's marks."""
    b = body or {}
    with OPS.transaction():
        ns, name, record, pvc = _previous_claim(b)
        app, current = _label(record.get("app")), str(record.get("replaced_by") or "")
        if not app or not NAME.fullmatch(current):
            raise ValueError(f"{ns}/{name} is not a previous volume")
        dep = KUBE.get_optional(_dep_path(ns, app), read=kget)
        if not dep:
            raise ValueError(f"{app} no longer exists in {ns}, so there is nothing to swap back")
        indexes = _indexes(dep, current)
        if not indexes:
            raise ValueError(f"{app} no longer uses {current}. Point it at {name} with Edit instead.")
        digest = _back_digest(ns, app, current, name, record.get("uid", ""), dep)
        if str(b.get("digest") or "") != digest:
            return MODULE_ROUTES.Reply(409, {"error": f"{app} or {name} changed since this was shown. Check it again.",
                                             "volume": _row(ns, name, record, pvc, keep_days(), dep=dep)})
        blockers = _swap_blockers(ns, app, (current, name), dep)
        if blockers:
            raise ValueError(blockers[0])
        managed = UPDATES.managed_by(dep)
        if managed and b.get("accept_gitops") is not True:
            raise ValueError(f"{app} is applied by {managed['by']}, whose next sync puts {current} back. "
                             "Confirm that this has been read to continue.")
        busy = _busy(OPS.snapshot(), ns, app, (current, name))
        if busy:
            raise ValueError(f"{app} already has a job running ({busy.get('title') or busy.get('kind')}). Let it finish first.")
        leaving = KUBE.get_optional(_pvc_path(ns, current), read=kget) or {}
        ref = {"namespace": ns, "app": app, "kind": "Deployment", "volume": _volumes(dep)[indexes[0]]["name"],
               "indexes": indexes, "old_claim": current, "new_claim": name,
               "old_uid": (leaving.get("metadata") or {}).get("uid", ""), "new_uid": record.get("uid", ""),
               "old_volume": _handle(leaving) if leaving else "", "backup": "",
               "generation": (dep.get("metadata") or {}).get("generation"),
               "new_kept": bool(record.get("kept")),
               "direction": "back", "phase": "stopping", "since": time.time()}
        operation = OPS.start(KIND, f"Swap {app} back to {name}", {"kind": "Deployment", "name": app, "namespace": ns},
                              "/data-protection", ref, f"Stopping {app}")
    return {"ok": True, "operation": operation, "detail": f"Swapping {app} back to {name}"}


def set_keep_days(value):
    if isinstance(value, bool) or isinstance(value, float) and not value.is_integer():
        raise ValueError("keep days must be a whole number")
    try:
        days = int(value)
    except (TypeError, ValueError):
        raise ValueError("keep days must be a whole number") from None
    if days < 0 or days > 90:
        raise ValueError("previous volumes are kept 1 to 90 days, or 0 to keep them until deleted")
    save_settings({"restore_swap": {"keep_days": days}})
    return {"ok": True, "keep_days": days,
            "detail": f"Previous volumes are kept {days} days" if days else "Previous volumes are kept until deleted"}


# ---- the expiry: on the leader, hourly ---------------------------------------------

def _held(item, ns, name):
    ref, resource = item.get("ref") or {}, item.get("resource") or {}
    return (ref.get("namespace") or resource.get("namespace")) == ns and name in (_names_in(ref) | {resource.get("name")})


def _expire_one(ns, name, record, pvc, days, active, now):
    if pvc is None:
        # Deleted by a person, or made again under the name: no longer ours to keep.
        _forget(ns, name)
        return ("forgotten", ns, name)
    app = _label(record.get("app"))
    who = f"{app}'s" if app else "an app's"
    review = deletion_plan(ns, name)
    users = [c for c in review.get("consumers") or [] if c.get("active") or c.get("kind") not in ("Job", "Pod")]
    if users:
        # Swapped back by hand, or put back by Flux - kept or not: no longer a copy nobody uses.
        _unmark(ns, name)
        other = _keep_the_other(ns, name, record, now)
        note({"key": f"previous-volume:{ns}/{name}:in-use:{int(now)}", "category": "degraded", "severity": "info",
              "event": True, "title": f"{app or 'An app'} uses {name} again, so it is no longer due for deletion",
              "body": f"{users[0]['kind']} {users[0]['name']} mounts it, so it is not a previous volume any more."
                      + (f" {other}, which it ran on, is kept as its previous volume instead." if other else ""),
              "href": "/data-protection"})
        return ("in-use", ns, name)
    if record.get("kept"):
        return None
    expires, reminded = float(record.get("replaced_at") or 0) + days * DAY, float(record.get("reminded_at") or 0)
    if not record.get("replaced_at"):
        return None
    if not reminded:
        if now >= expires - DAY:
            # Every deletion is announced a day ahead, even after a shorter
            # setting or a cluster that was down past the date.
            update_records(lambda records: records[f"{ns}/{name}"].update(reminded_at=int(now)))
            ksend("PATCH", _pvc_path(ns, name), {"metadata": {"annotations": {REMINDED: str(int(now))}}},
                  ctype="application/merge-patch+json")
            return ("reminded", ns, name)
        return None
    if now < expires or now - reminded < DAY:
        return None
    if any(_held(item, ns, name) for item in active):
        return None
    if review.get("uid") != record.get("uid"):
        return None                       # made again under the name: dropped at the next tick
    # Due, but not to be deleted: say why, once, in the reminder's place.
    held = ("system namespaces are changed with Kubernetes administration tools" if ns in system_namespaces()
            else "its volume is set to Retain, so Homestead leaves it and its data alone"
            if (review.get("pv") or {}).get("reclaim_policy") == "Retain"
            else (review.get("blocking_reasons") or [""])[0])
    if held:
        if record.get("held") != held:
            update_records(lambda records: records.get(f"{ns}/{name}", {}).update(held=held))
        return ("held", ns, name)
    # delete_data, the choice a person's Delete offers that frees the space:
    # the point of the expiry. The claim is proven Homestead's previous volume
    # by its own record and uid, so nothing else is taken with it.
    with OPS.transaction():
        # Again, under the jobs' lock, right before the delete: a Keep or a
        # Swap back made while this tick read the cluster stops it.
        now_record = read_records().get(f"{ns}/{name}") or {}
        if (now_record.get("uid") != record.get("uid") or now_record.get("kept")
                or now_record.get("reminded_at") != record.get("reminded_at")):
            return None
        if any(_held(item, ns, name) for item in OPS.stored() if item.get("status") not in TERMINAL):
            return None
        delete_volume({"namespace": ns, "name": name, "action": "delete_data", "uid": record["uid"], "confirmation": name})
        _forget(ns, name)
    note({"key": f"previous-volume:{ns}/{name}:deleted:{int(now)}", "category": "degraded", "severity": "info",
          "event": True, "title": f"Deleted {who} previous volume {name} (kept {days} days)",
          "body": f"It ran on {record.get('replaced_by') or 'its restored volume'} instead. "
                  "Backups of either volume are not touched.", "href": "/data-protection"})
    return ("deleted", ns, name)


def _keep_the_other(ns, name, record, now):
    """The app went back to a previous volume without a swap back (Flux, or
    Edit): the claim it left is kept as its previous volume instead, if it is
    still the one recorded and nothing else uses it. Its name, or ""."""
    other, uid = str(record.get("replaced_by") or ""), record.get("replaced_by_uid")
    if not NAME.fullmatch(other) or not uid or f"{ns}/{other}" in read_records():
        return ""
    users, loose = _users(ns, other)
    if users or loose:
        return ""
    item = {"id": record.get("job", ""), "ref": {"namespace": ns, "app": record.get("app", ""), "old_claim": name,
                                                  "new_claim": other, "old_uid": record.get("uid"), "new_uid": uid}}
    return other if _mark(item, other, uid, name, "not-used", now) else ""


def expire_tick(now=None):
    """Remind a day ahead, then delete, each previous volume past its time.
    Any doubt about one claim skips it until the next hour."""
    now = now or time.time()
    if not is_leader():
        return []
    try:
        days = keep_days(settings_strict())
    except Exception as error:
        print(f"previous volumes: settings could not be read, nothing deleted this hour; trying again next hour: "
              f"{str(error)[:160]}", flush=True)
        return []
    if not days:
        return []
    active = [item for item in OPS.snapshot() if item.get("status") not in TERMINAL]
    done = []
    for ns, name, record, pvc in _kept_claims():
        try:
            outcome = _expire_one(ns, name, record, pvc, days, active, now)
        except Exception as error:
            print(f"previous volumes: {ns}/{name}: {str(error)[:160]}", flush=True)
            continue
        if outcome:
            done.append(outcome)
    return done


def alert_facts(now=None):
    """A reminded previous volume no one chose to keep: one info alert each."""
    now = now or time.time()
    days = keep_days()
    if not days:
        return []
    facts = []
    for ns, name, record, pvc in _kept_claims():
        if not pvc or record.get("kept") or not record.get("reminded_at"):
            continue
        app, due = _label(record.get("app")), _due(record, days)
        who = f"{app}'s" if app else "An app's"
        if record.get("held"):
            facts.append({"key": f"previous-volume:{ns}/{name}", "category": "degraded", "severity": "info",
                          "title": f"{who} previous volume {name} could not be deleted",
                          "resolved": f"{who} previous volume {name} is no longer due for deletion",
                          "body": f"It is past its time, but {record['held']}. Delete it manually under Data protection, "
                                  "Previous volumes, or keep it.",
                          "href": "/data-protection?" + urllib.parse.urlencode({"previous": f"{ns}/{name}"})})
            continue
        when = ("soon" if due <= now else "tomorrow" if due - now <= 1.5 * DAY else
                "on " + _day(due))
        facts.append({"key": f"previous-volume:{ns}/{name}", "category": "degraded", "severity": "info",
                      "title": f"{who} previous volume {name} will be deleted {when}",
                      "resolved": f"{who} previous volume {name} is no longer due for deletion",
                      "body": "Restore into this app moved it off this volume. Keep it under Data protection, "
                              "Previous volumes, to stop the deletion.",
                      "href": "/data-protection?" + urllib.parse.urlencode({"previous": f"{ns}/{name}"})})
    return facts


def _plan_route(request):
    q = lambda name: (request.query.get(name) or [""])[0]
    return public(plan(q("backup"), q("ns"), q("app"), q("claim"), q("new_name"), q("storage_class")))


# Its routes and who may use them (homestead_routes.py).
ROUTES = {
    ("GET", "/api/restore-swap/plan"): ("viewer", _plan_route),
    ("POST", "/api/restore-swap"): ("admin", lambda request: start(request.body)),
    ("GET", "/api/volumes/previous"): ("viewer", lambda request: previous_volumes()),
    ("POST", "/api/volumes/previous/keep"): ("operator", lambda request: keep(request.body, request.role)),
    ("POST", "/api/volumes/previous/swap-back"): ("admin", lambda request: swap_back(request.body)),
    ("POST", "/api/volumes/previous/settings"): ("admin", lambda request: set_keep_days((request.body or {}).get("keep_days"))),
}


def register(ops):
    """The job store's entry for a restore into an app (#743); called from
    homestead_storage_server.register, so server.py does not change."""
    ops.RESOLVERS[KIND] = lambda item: resolve(item, ops.checkpoint)
    ops.CANCELLERS[KIND] = (cancel_plan, cancel_run)
    # No OPS.CONFLICTS rule: start and swap_back check _busy and start the
    # job inside one OPS.transaction, so two cannot pass the check together.
