"""Safe automatic updates: snapshot first, update, and roll back if it breaks.

An app can be set to update itself (homestead.io/update-mode: auto). Inside
the cluster's maintenance window - and never when the cluster policy is
notify only - the leading replica takes one such app at a time that has an
update waiting and starts an auto-update job for it. The job:

1. snapshots each of the app's Longhorn volumes, so its data can be put back;
2. installs the update the way Update does: pinned to the new digest, with the
   old one kept for Rollback;
3. waits for the rollout, then for the app to answer its uptime check again;
4. succeeds, or - if the rollout stalls or the app stops answering - rolls the
   image back and fails, which raises an alert and a push like any failed job.
   The snapshots stay, and the job says which, since an update can change data
   that rolling the image back does not change back.

Only an update that keeps the app's tag is taken: a newer digest of :latest or
:1.2, not 1.2 to 2.0. A new version always waits for someone to choose it.
An app that is not answering, or not fully running, is not updated: there would
be nothing to compare with afterwards.

The job's steps run in its resolver, under the operations lock every replica
shares, and each step is checkpointed before it writes, so a restart carries
it on rather than repeating it.
"""
import re
import time

KIND = "auto-update"
MODE = "update-mode"            # annotation suffix, homestead.io/update-mode
ROLLOUT_WAIT = 600              # the new pods have this long to become ready
ANSWER_WAIT = 300               # then the app this long to answer again
STEADY = 120                    # an app with no check: ready this long is enough
KEEP_SNAPSHOTS = 2              # before-update snapshots kept per volume
CHECKED_OFF = ("checks are off for this app", "no address to ask")
_tidied = set()                 # jobs whose snapshots have been tidied since start

# Bound by server.py.
kget = ksend = None
updates = None                  # homestead_updates
uptime_report = lambda: {}
longhorn_snapshot = None        # (volume, name, annotations) -> dict
snapshot_list = None            # (volume) -> [snapshot rows]
snapshot_prune = None           # (volume, name) -> None; best effort
names = None                    # homestead_names


def bind(_kget, _ksend, _updates, _uptime_report, _snapshot, _snapshots, _prune, _names):
    global kget, ksend, updates, uptime_report, longhorn_snapshot, snapshot_list, snapshot_prune, names
    kget, ksend, updates, uptime_report = _kget, _ksend, _updates, _uptime_report
    longhorn_snapshot, snapshot_list, snapshot_prune, names = _snapshot, _snapshots, _prune, _names


def mode(annotations):
    return "auto" if (annotations or {}).get(names.key(MODE)) == "auto" else "manual"


def same_tag_only(entry):
    """Whether every waiting update keeps its image's tag: a newer build of
    the same tag, never a newer version."""
    waiting = [i for i in (entry or {}).get("images") or [] if i.get("available")]
    if not waiting or any(i.get("error") for i in (entry or {}).get("images") or []):
        return False
    return all(i.get("candidate") == i.get("source") for i in waiting)


def answering(state):
    """Whether an app is in a state to compare with after an update."""
    if not state:
        return True                      # never seen by the checks yet: readiness decides
    if state.get("state") in ("up", "slow"):
        return True
    return state.get("state") == "off" and state.get("why") in CHECKED_OFF


def candidates(workloads, report, answers):
    """Apps set to update themselves that have a same-tag update waiting and
    are well enough to compare with afterwards, in name order."""
    waiting = {(e.get("ns"), e.get("name")): e for e in (report or {}).get("workloads") or []}
    out = []
    for w in workloads or []:
        if w.get("update_mode") != "auto" or w.get("platform") or w.get("self") or w.get("homestead") \
                or w.get("managed_smb") or w.get("managed_nfs") or w.get("site"):
            continue
        entry = waiting.get((w.get("ns"), w.get("name")))
        if not entry or entry.get("managed") or not same_tag_only(entry):
            continue
        if not w.get("desired") or w.get("ready") != w.get("desired"):
            continue
        if not answering(answers.get(f"{w['ns']}/{w['name']}")):
            continue
        out.append(w)
    return sorted(out, key=lambda w: (w["ns"], w["name"]))


def pick(workloads, report, answers, operations, window_open, policy):
    """The one app to update now, or None. Nothing while a job of this kind runs."""
    if not window_open or policy == "notify_only":
        return None
    if any(o.get("kind") == KIND and o.get("status") in ("queued", "running") for o in operations or []):
        return None
    found = candidates(workloads, report, answers)
    return found[0] if found else None


def _claims_volumes(ns, claims):
    """The Longhorn volumes behind a workload's claims: [(claim, volume)]."""
    out = []
    for claim in claims or []:
        pvc = kget(f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{claim}")
        pv_name = (pvc.get("spec") or {}).get("volumeName")
        if not pv_name:
            continue
        pv = kget(f"/api/v1/persistentvolumes/{pv_name}")
        if ((pv.get("spec") or {}).get("csi") or {}).get("driver") == "driver.longhorn.io":
            out.append((claim, pv["spec"]["csi"].get("volumeHandle") or pv_name))
    return out


def _snapshot_name(item, index):
    return f"before-update-{item['id'][:12]}-{index}"


def _rolled_out(dep):
    spec, st = dep.get("spec") or {}, dep.get("status") or {}
    want = int(spec.get("replicas", 1) or 0)
    return (want > 0 and int(st.get("observedGeneration") or 0) >= int((dep.get("metadata") or {}).get("generation") or 0)
            and int(st.get("updatedReplicas") or 0) == want and int(st.get("readyReplicas") or 0) == want
            and int(st.get("availableReplicas") or 0) == want and int(st.get("replicas") or 0) == want)


def resolve(item, checkpoint, now=None):
    """Move one auto-update job on a step: (status, progress, message)."""
    now = now or time.time()
    ref = item["ref"]
    ns, name = ref["namespace"], ref["name"]
    phase = ref.get("phase", "snapshot")

    def to(next_phase, **changes):
        ref.update(changes, phase=next_phase, phase_at=now)
        checkpoint(item)

    if phase == "snapshot":
        dep = kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")
        claims = sorted({(v.get("persistentVolumeClaim") or {}).get("claimName")
                         for v in dep["spec"]["template"]["spec"].get("volumes") or []} - {None, ""})
        volumes = _claims_volumes(ns, claims)
        planned = [{"claim": c, "volume": v, "snapshot": _snapshot_name(item, i)} for i, (c, v) in enumerate(volumes)]
        to("snapshotting", snapshots=planned, skipped=[c for c in claims if c not in {p["claim"] for p in planned}])
        phase = "snapshotting"
    if phase == "snapshotting":
        for row in ref.get("snapshots") or []:
            try:
                longhorn_snapshot(row["volume"], row["snapshot"],
                                  {names.key("before-update"): f"{ns}/{name}"})
            except Exception as error:
                if "409" not in str(error) and "already exists" not in str(error).lower():
                    return "failed", 10, f"Nothing was updated: the snapshot of {row['claim']} could not be made ({str(error)[:160]})"
        to("update")
        return "running", 25, "Snapshots made; installing the update"
    if phase == "update":
        to("updating")
        phase = "updating"
    if phase == "updating":
        try:
            prepared = updates.prepare_update(ns, name)
        except ValueError as error:
            if "no image update" in str(error) and ref.get("before"):
                to("watch", updated_at=ref.get("updated_at") or now)   # committed before a restart
                return "running", 50, "Update installed; waiting for the new pods"
            if "no image update" in str(error):
                return "succeeded", 100, f"{name} is already current; nothing was changed"
            return "failed", 25, f"Nothing was updated: {str(error)[:200]}"
        if any(i.get("candidate") != i.get("source") for i in _waiting(prepared)):
            return "failed", 25, "Nothing was updated: the waiting update is a new version, which waits for someone to choose it"
        to("updating", before=prepared["before"])
        updates.commit_prepared(prepared)
        _refresh(ns, name)
        to("watch", updated_at=now)
        return "running", 50, "Update installed; waiting for the new pods"
    if phase == "watch":
        dep = kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")
        if not ref.get("rolled_at"):
            if _rolled_out(dep):
                to("watch", rolled_at=now)
                return "running", 70, "New pods are ready; waiting for it to answer"
            if now - ref.get("updated_at", now) > ROLLOUT_WAIT:
                return _roll_back(item, to, f"its new pods were not ready after {ROLLOUT_WAIT // 60} minutes")
            return "running", 55, "Waiting for the new pods to be ready"
        if not _rolled_out(dep):
            return _roll_back(item, to, "its new pods stopped being ready")
        state = uptime_report().get(f"{ns}/{name}") or {}
        last = state.get("last") or {}
        fresh = (last.get("at") or 0) > ref["rolled_at"]
        if state.get("state") == "off" or not state:
            if now - ref["rolled_at"] >= STEADY:
                return _done(item, "it has stayed ready (it has no answer check)")
            return "running", 80, "Ready; making sure it stays up"
        if fresh and state.get("state") in ("up", "slow"):
            return _done(item, "it is answering again")
        if fresh and state.get("state") == "down":
            return _roll_back(item, to, f"it stopped answering ({last.get('error') or 'no answer'})")
        if now - ref["rolled_at"] > ANSWER_WAIT:
            return _roll_back(item, to, f"it did not answer within {ANSWER_WAIT // 60} minutes of starting")
        return "running", 85, "Waiting for it to answer"
    if phase == "rolling-back":
        return _roll_back(item, to, ref.get("why") or "it failed after the update")
    return "failed", item.get("progress", 0), f"Unknown step {phase}"


def _refresh(ns, name):
    """Containers shows the new image, not a waiting update."""
    refresh = getattr(updates, "refresh_soon", None)
    if refresh:
        refresh(ns, name)


def _waiting(prepared):
    """What prepare_update will install: [{candidate, source}] per changed container."""
    current = {c["name"]: c.get("image", "") for c in updates._pod_containers(prepared["current"])}
    tracked_before = updates._annotation_json(prepared["current"], updates.TRACKED)
    tracked_after = updates._annotation_json(prepared["proposed"], updates.TRACKED)
    out = []
    for c in updates._pod_containers(prepared["proposed"]):
        if c.get("image") == current.get(c["name"]):
            continue
        source = tracked_before.get(c["name"]) or current.get(c["name"], "")
        candidate = tracked_after.get(c["name"], "")
        out.append({"candidate": _tagged(candidate), "source": _tagged(source)})
    return out


def _tagged(ref):
    """An image reference without its digest: what tag it follows."""
    return re.sub(r"@sha256:[0-9a-f]{64}$", "", str(ref or ""))


def _snapshots_said(ref):
    snaps = ref.get("snapshots") or []
    if not snaps:
        return "It has no Longhorn volumes to snapshot." if not ref.get("skipped") else \
            f"{', '.join(ref['skipped'])} {'is' if len(ref['skipped']) == 1 else 'are'} not on Longhorn, so not snapshotted."
    return "Snapshots from before the update: " + ", ".join(f"{s['claim']} ({s['snapshot']})" for s in snaps) + "."


def _roll_back(item, to, why):
    ref = item["ref"]
    if ref.get("phase") != "rolling-back":
        to("rolling-back", why=why)
    try:
        updates.rollback(ref["namespace"], ref["name"])
        _refresh(ref["namespace"], ref["name"])
    except ValueError as error:
        if "does not change" not in str(error):
            return ("failed", 100, f"The update failed - {why} - and rolling back did not work either: "
                    f"{str(error)[:160]}. Roll it back from Containers. {_snapshots_said(ref)}")
    to("rolled-back")
    return ("failed", 100, f"Rolled back: after the update {why}. The previous image is running again. "
            f"{_snapshots_said(ref)} Restore them from Data Protection if the update changed its data.")


def _done(item, how):
    return "succeeded", 100, f"Updated; {how}. {_snapshots_said(item['ref'])}"


def tidy(operations):
    """After updates that worked, keep only the newest before-update snapshots
    of their volumes. The leader runs it outside any job, as removing a
    snapshot is a job of its own. Each job once: the list keeps finished jobs
    for a while, and asking Longhorn about them every minute is waste. After
    a restart each is tidied once more, which finds nothing to do."""
    for op in operations or []:
        summary = op.get("auto_update") or {}
        if op.get("kind") != KIND or op.get("status") != "succeeded" or op.get("id") in _tidied:
            continue
        if op.get("id"):
            _tidied.add(op["id"])
        for row in summary.get("snapshots") or []:
            try:
                _prune(row["volume"], row["snapshot"])
            except Exception:
                pass      # tidying never fails anything


def _prune(volume, keep_name):
    """Keep the newest KEEP_SNAPSHOTS before-update snapshots of a volume."""
    rows = [s for s in snapshot_list(volume) or [] if str(s.get("name", "")).startswith("before-update-")
            and not s.get("removed") and not s.get("deleting")]
    rows.sort(key=lambda s: s.get("created") or "", reverse=True)
    for old in rows[KEEP_SNAPSHOTS:]:
        if old["name"] != keep_name:
            snapshot_prune(volume, old["name"])
