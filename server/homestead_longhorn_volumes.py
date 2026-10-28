"""
Longhorn volumes and the groups and jobs they carry: listing them, putting a
volume in a group or job (one label each), saving and deleting groups, and a
volume's snapshots.

Split out of homestead_longhorn (#623); what bind() sets, the shared helpers
and the routes stay there.
"""
import homestead_names as NAMES
import homestead_snapshot_delete as SNAPSHOT_DELETE
import secrets
import time
import urllib.error


def _volumes():
    try:
        items = LH.kget(f"{LH.API}/namespaces/{LH.LHNS}/volumes").get("items", [])
    except Exception:
        return []
    out = []
    for v in items:
        md, st = v["metadata"], v.get("status", {}) or {}
        ks = st.get("kubernetesStatus", {}) or {}
        labels = md.get("labels", {}) or {}
        out.append({
            "name": md["name"],
            "pvc": ks.get("pvcName", ""),
            "namespace": ks.get("namespace", ""),
            "size_gb": round(int(v.get("spec", {}).get("size", 0) or 0) / 1024**3, 1),
            "robustness": st.get("robustness", ""),
            "state": st.get("state", ""),
            "labels": labels,
            "jobs": sorted(k[len(LH.JOB_LABEL):] for k in labels if k.startswith(LH.JOB_LABEL)),
            "groups": sorted(k[len(LH.GROUP_LABEL):] for k in labels if k.startswith(LH.GROUP_LABEL)),
            "last_backup": st.get("lastBackup", ""),
            "last_backup_at": st.get("lastBackupAt", ""),
        })
    return sorted(out, key=lambda x: x["pvc"] or x["name"])


def volumes():
    return _volumes()


def groups():
    """Every group referenced by a job or worn by a volume."""
    g = set()
    for j in JOBS.list_jobs():
        g.update(j["groups"])
    for v in _volumes():
        g.update(v["groups"])
    g.add("default")
    return sorted(g)


SOURCE_LABEL = "recurring-job.longhorn.io/source"


def _patch_labels(volume, changes):
    """Set or clear recurring-job labels on a volume - and on its PVC, when
    the PVC is where Longhorn reads them from.

    A PVC labelled recurring-job.longhorn.io/source=enabled has its own
    labels copied onto the volume, so a change made to the volume alone was
    undone on Longhorn's next sync."""
    patch = {"metadata": {"labels": changes}}
    LH.ksend("PATCH", f"{LH.API}/namespaces/{LH.LHNS}/volumes/{volume}", patch,
          ctype="application/merge-patch+json")
    try:
        status = LH.kget(f"{LH.API}/namespaces/{LH.LHNS}/volumes/{volume}").get("status", {}) or {}
        ks = status.get("kubernetesStatus", {}) or {}
        if ks.get("pvcName") and ks.get("namespace"):
            path = f"/api/v1/namespaces/{ks['namespace']}/persistentvolumeclaims/{ks['pvcName']}"
            if ((LH.kget(path).get("metadata", {}) or {}).get("labels", {}) or {}).get(SOURCE_LABEL) == "enabled":
                LH.ksend("PATCH", path, patch, ctype="application/merge-patch+json")
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise


def assign(volume, name, kind="group", enabled=True):
    """Add or remove a job/group label on one volume."""
    key = (LH.GROUP_LABEL if kind == "group" else LH.JOB_LABEL) + name
    _patch_labels(volume, {key: "enabled" if enabled else None})
    LH._bust("lhvols", "lhjobs")
    return {"ok": True, "volume": volume, "label": key, "enabled": enabled}


def bulk_assign(volumes_list, name, kind="group", enabled=True):
    done, failed = [], []
    for v in volumes_list:
        try:
            assign(v, name, kind, enabled)
            done.append(v)
        except Exception as e:
            failed.append(f"{v}: {e}")
    return {"ok": True, "updated": done, "failed": failed}


def _job_objects():
    try:
        return LH.kget(f"{LH.API}/namespaces/{LH.LHNS}/recurringjobs").get("items", [])
    except Exception:
        return []


def _set_job_groups(name, regroup):
    """Change a recurring job's groups, the rest unchanged. regroup(groups)
    works out the new list from the groups as stored when it is saved, so a
    group the job joined or left meanwhile is kept that way (#621). Returns
    the groups saved."""
    saved = []

    def change(job):
        spec = job.setdefault("spec", {})
        spec["groups"] = regroup(list(spec.get("groups") or []))
        saved[:] = [spec["groups"]]
        return job
    LH._update(f"{LH.API}/namespaces/{LH.LHNS}/recurringjobs/{name}", change)
    return saved[0]


def _only_default(labels):
    keys = [k for k in labels if k.startswith((LH.JOB_LABEL, LH.GROUP_LABEL)) and labels[k] == "enabled"]
    return keys == [LH.GROUP_LABEL + "default"]


def save_group(cfg):
    """Make or change a group: its name, the volumes in it and the jobs that
    cover it, in one go.

    A group is nothing but a label on volumes and a name in jobs' lists, so
    one with neither is kept nowhere; it needs a volume or a job. Longhorn
    puts every volume without a job or group label in default, and leaves it
    there when it joins another group - so it would get both groups' plans.
    Joining here takes it out of default unless asked not to."""
    name = str(cfg.get("name") or "").strip()
    original = str(cfg.get("original") or "").strip() or name
    if not LH.SAFE.match(name):
        raise ValueError("a group name is lowercase letters, numbers and dashes, up to 40")
    if "default" in (original, name) and original != name:
        raise ValueError("default is Longhorn's own group and keeps its name")
    wanted_volumes = set(cfg.get("volumes") or [])
    wanted_jobs = set(cfg.get("jobs") or [])
    if name != "default" and not wanted_volumes and not wanted_jobs:
        raise ValueError("choose a volume or a job: a group with neither is not kept anywhere")
    vols = {v["name"]: v for v in _volumes()}
    unknown = wanted_volumes - set(vols)
    if unknown:
        raise ValueError("no such volume: " + ", ".join(sorted(unknown)))
    jobs = {j["metadata"]["name"]: j for j in _job_objects()}
    unknown = wanted_jobs - set(jobs)
    if unknown:
        raise ValueError("no such job: " + ", ".join(sorted(unknown)))
    if original != name and name in groups():
        raise ValueError(f"there is already a group called {name}")
    old_key, key, default_key = LH.GROUP_LABEL + original, LH.GROUP_LABEL + name, LH.GROUP_LABEL + "default"
    added, removed, left_default, back_to_default, kept = [], [], [], [], []
    for vol, v in vols.items():
        labels = v["labels"]
        has = labels.get(old_key) == "enabled"
        want = vol in wanted_volumes
        changes = {}
        if original != name and has:
            changes[old_key] = None
        if want and (not has or original != name):
            changes[key] = "enabled"
        if want and not has:
            added.append(vol)
            if name != "default" and cfg.get("leave_default", True) and labels.get(default_key) == "enabled":
                changes[default_key] = None
                left_default.append(vol)
        elif has and not want:
            if name == "default" and _only_default(labels):
                # Longhorn would put it straight back.
                kept.append(vol)
                continue
            changes[old_key] = None
            removed.append(vol)
            rest = {k for k, value in labels.items() if value == "enabled"
                    and k.startswith((LH.JOB_LABEL, LH.GROUP_LABEL)) and k != old_key}
            if not rest:
                back_to_default.append(vol)
        if changes:
            _patch_labels(vol, changes)
    for job_name, job in jobs.items():
        def regroup(current, want=job_name in wanted_jobs):
            if original in current and (not want or original != name):
                current = [g for g in current if g != original]
            if want and name not in current:
                current = current + [name]
            return current
        stored = list((job.get("spec") or {}).get("groups") or [])
        if regroup(stored) != stored:
            _set_job_groups(job_name, regroup)
    LH._bust("lhvols", "lhjobs", "lhov")
    return {"ok": True, "name": name, "added": added, "removed": removed, "left_default": left_default,
            "back_to_default": back_to_default, "kept_in_default": kept}


def delete_group(name):
    """Take a group off every volume and out of every job.

    Snapshots and backups already taken are kept. A volume left with no
    group or job goes back to default, as Longhorn does; a job left with no
    group protects nothing until it is given one - both are said by name."""
    if name == "default":
        raise ValueError("default is Longhorn's own group; every volume without another comes back to it")
    if not LH.SAFE.match(str(name or "")):
        raise ValueError("unknown group")
    key = LH.GROUP_LABEL + name
    back, idle = [], []
    for v in _volumes():
        if v["labels"].get(key) != "enabled":
            continue
        rest = [k for k, value in v["labels"].items() if value == "enabled"
                and k.startswith((LH.JOB_LABEL, LH.GROUP_LABEL)) and k != key]
        _patch_labels(v["name"], {key: None})
        if not rest:
            back.append(v["pvc"] or v["name"])
    for job in _job_objects():
        current = list((job.get("spec") or {}).get("groups") or [])
        if name in current:
            left = _set_job_groups(job["metadata"]["name"], lambda groups: [g for g in groups if g != name])
            if not left:
                idle.append(job["metadata"]["name"])
    LH._bust("lhvols", "lhjobs", "lhov")
    return {"ok": True, "back_to_default": back, "idle_jobs": idle}


def snapshots(volume=None):
    listing = LH.kget(f"{LH.API}/namespaces/{LH.LHNS}/snapshots")
    if not isinstance(listing.get("items"), list) or listing.get("metadata", {}).get("continue"):
        raise ValueError("snapshot inventory is incomplete; refresh before changing recovery points")
    items = listing["items"]
    out = []
    for s in items:
        sp, st = s.get("spec", {}), s.get("status", {}) or {}
        vol = sp.get("volume", "")
        if volume and vol != volume:
            continue
        out.append({
            "name": s["metadata"]["name"], "volume": vol,
            "uid": s["metadata"].get("uid", ""),
            "created": st.get("creationTime", ""),
            "size_mb": round(int(st.get("size", 0) or 0) / 1048576, 1),
            "ready": bool(st.get("readyToUse")),
            "user_created": bool(st.get("userCreated")),
            "source": SNAPSHOT_DELETE.source(s),
            "removed": bool(st.get("markRemoved")),
            "deleting": bool(s["metadata"].get("deletionTimestamp")),
            "parent": st.get("parent", ""), "error": st.get("error", ""),
            "children": list((st.get("children") or {}).keys()),
        })
    return sorted(out, key=lambda x: x["created"], reverse=True)


def create_snapshot(volume, name=None, annotations=None):
    # The second alone is not enough: a workload with two volumes has both
    # snapshotted in the same one, and the second name was refused.
    name = name or f"homestead-{int(time.time())}-{secrets.token_hex(3)}"
    body = {"apiVersion": "longhorn.io/v1beta2", "kind": "Snapshot",
            "metadata": {"name": name, "namespace": LH.LHNS,
                         "labels": {NAMES.key("managed"): "true"}},
            "spec": {"volume": volume, "createSnapshot": True}}
    if annotations:
        body["metadata"]["annotations"] = dict(annotations)
    LH.ksend("POST", f"{LH.API}/namespaces/{LH.LHNS}/snapshots", body)
    LH._bust("lhsnaps")
    return {"ok": True, "snapshot": name, "volume": volume}


# Last, so that importing any of these modules first works: homestead_longhorn
# imports this one.
import homestead_longhorn_jobs as JOBS  # noqa: E402
import homestead_longhorn as LH  # noqa: E402
