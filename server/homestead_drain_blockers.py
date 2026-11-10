"""What keeps a host from draining, named before (and while) it drains.

A host restart's drain stalls on things the drain itself never reads. Longhorn
keeps a host's instance-manager pod, and its disruption budget at 0, while the
instance-manager still lists engine processes; when those engines in fact run
on another host now, the processes left here are leftovers, and Longhorn
records some of them as orphans. A pod stuck terminating keeps its volume
attached to the host. for_host() reads these for one host, joins them in
memory and says each one as a row; it changes nothing.

Each row is {id, kind, what, why, fix, can_remove, records}:
- "engine-orphans": engine-instance orphan records on this host whose engine
  is confirmed running on another host. Removable (records carries each
  record's name, uid and resourceVersion, the engine and the engine's host, so
  a removal can be fenced on them).
- "other-orphans": every other orphan record on this host - replica-instance,
  replica-data, and engine-instance ones whose engine is not confirmed
  running elsewhere. Explained only.
- "engines-elsewhere": engines this host's instance-manager still lists whose
  engine record says they run on another host, or that have no engine
  record at all, and no orphan record names.
- "removing-orphans": orphan records on this host that are being removed
  already (deletionTimestamp set; Longhorn's finalizer keeps them until it
  has cleaned up). Explained only.
- "stuck-pod": a pod on this host still terminating STUCK_AFTER seconds past
  the end of its grace period, with the volumes it keeps attached here.
- "stuck-attachment": a VolumeAttachment to this host whose volume no pod on
  this host uses any more.
- "unread": a list that could not be read, so its kind was not checked.

Longhorn's field and parameter names are those of Longhorn v1.13.0
(longhorn-manager k8s/pkg/apis/longhorn/v1beta2/orphan.go,
instancemanager.go, engine.go; controller/instance_manager_controller.go
createOrphan and controller/node_controller.go createOrphan)."""
import calendar
import concurrent.futures
import json
import re
import time
import urllib.error
import urllib.parse

import homestead_kube as KUBE

kget = KUBE.kget

LH = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system"
ORPHANS = LH + "/orphans"
INSTANCE_MANAGERS = LH + "/instancemanagers"
ENGINES = LH + "/engines"
CLAIMS = "/api/v1/persistentvolumeclaims"
ATTACHMENTS = "/apis/storage.k8s.io/v1/volumeattachments"

# A pod still terminating this long after its grace period ended is stuck.
STUCK_AFTER = 900

# Longhorn v1.13.0 orphan types (orphan.go). The replica-data type's value is
# "replica"; "replica-data" is accepted as well, as its documented name.
ENGINE_INSTANCE = "engine-instance"
REPLICA_INSTANCE = "replica-instance"
REPLICA_DATA = ("replica", "replica-data")
# spec.parameters keys (orphan.go: OrphanInstanceName, OrphanDataName).
INSTANCE_NAME = "InstanceName"
DATA_NAME = "DataName"
RUNNING = "running"

UNREAD_WHAT = {
    "orphans": "Longhorn orphan records",
    "instancemanagers": "Longhorn instance-managers",
    "engines": "Longhorn engines",
    "pods": "the pods",
    "claims": "the volume claims",
    "attachments": "the volume attachments",
}


def _pods_path(node):
    return "/api/v1/pods?" + urllib.parse.urlencode({"fieldSelector": f"spec.nodeName={node}"})


def _read_all(paths, read=None):
    """Each name's items, None when Longhorn is not there, or the exception
    reading it raised; read at the same time, through read (kget)."""
    read = read or kget

    def one(path):
        try:
            return (read(path) or {}).get("items") or []
        except urllib.error.HTTPError as error:
            # No Longhorn: nothing of its kinds holds the host. A 404 on a
            # core list is no such answer, so it is unread like any failure.
            if error.code == 404 and path.startswith(LH + "/"):
                return None
            return error
        except Exception as error:  # said as unread by the caller, never skipped
            return error
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(paths)) as pool:
        return dict(zip(paths, pool.map(one, paths.values())))


def _seconds(stamp):
    try:
        return calendar.timegm(time.strptime(str(stamp), "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        return None


def _span(seconds):
    minutes = int(seconds // 60)
    if minutes < 120:
        return f"{minutes} min"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} h"
    return f"{hours // 24} days"


def _names(items):
    return ", ".join(items)


# What can be gone in a few minutes with nobody doing anything: a read that failed
# can answer next time, and Longhorn records engines that moved and finishes
# removing orphans by itself. The others stay until a person acts.
CLEARS_BY_ITSELF = ("unread", "engines-elsewhere", "removing-orphans")


class MayClear(ValueError):
    """A restart with nobody there refused for something that may clear by itself:
    worth waiting a while before leaving the host for a person."""


def _row(id, kind, what, why, fix, can_remove=False, records=None):
    return {"id": id, "kind": kind, "what": what, "why": why, "fix": fix,
            "can_remove": can_remove, "records": records or []}


def _engines_by_name(engines):
    return {(e.get("metadata") or {}).get("name"): e for e in engines or []}


def _engine_node(engine):
    """The host an engine runs on, when its record says it is running there."""
    if not engine:
        return None
    if (engine.get("status") or {}).get("currentState") != RUNNING:
        return None
    return (engine.get("spec") or {}).get("nodeID") or None


def _terminating(obj):
    return bool((obj.get("metadata") or {}).get("deletionTimestamp"))


def _removing_engines(node, orphans):
    """The engines named by node's engine-instance orphan records that are
    being removed already."""
    return {((o.get("spec") or {}).get("parameters") or {}).get(INSTANCE_NAME)
            for o in orphans or [] if _terminating(o) and (o.get("spec") or {}).get("nodeID") == node
            and (o.get("spec") or {}).get("orphanType") == ENGINE_INSTANCE} - {None, ""}


def _orphan_rows(node, orphans, engines):
    mine = [o for o in orphans if (o.get("spec") or {}).get("nodeID") == node]
    removable, other, removing = [], [], []
    for orphan in sorted(mine, key=lambda o: o["metadata"]["name"]):
        spec = orphan.get("spec") or {}
        params = spec.get("parameters") or {}
        kind = spec.get("orphanType") or ""
        if _terminating(orphan):
            # Longhorn's finalizer keeps a removed record until it has
            # stopped the process (or deleted the data) it names.
            removing.append(f"{kind or 'unknown'} {params.get(INSTANCE_NAME) or params.get(DATA_NAME) or orphan['metadata']['name']}")
            continue
        if kind == ENGINE_INSTANCE and engines is not None:
            name = params.get(INSTANCE_NAME) or ""
            engine = engines.get(name)
            where = _engine_node(engine)
            if where and where != node:
                removable.append((orphan, name, engine, where))
                continue
        other.append((orphan, kind, params))
    rows = []
    if removable:
        volumes = [(e.get("spec") or {}).get("volumeName") or name for _, name, e, _ in removable]
        hosts = sorted({where for *_, where in removable})
        records = [{"name": o["metadata"]["name"], "uid": o["metadata"].get("uid", ""),
                    "resourceVersion": o["metadata"].get("resourceVersion", ""),
                    "engine": name, "engine_node": where} for o, name, _, where in removable]
        count = len(removable)
        rows.append(_row(
            f"engine-orphans:{node}", "engine-orphans",
            f"{count} Longhorn orphan record{'s' if count != 1 else ''} on {node} for "
            f"volume{'s' if count != 1 else ''} {_names(volumes)}, now running on {_names(hosts)}",
            f"Each record keeps a leftover engine process on {node}'s instance-manager. Longhorn keeps the "
            f"instance-manager, and its disruption budget at 0, while it still runs engines, so the drain "
            f"cannot evict it.",
            "Remove these orphan records in the restart review: Longhorn then stops the leftover processes. "
            f"The volumes keep running on {_names(hosts)}.",
            can_remove=True, records=records))
    if other:
        said = []
        data = False
        for orphan, kind, params in other:
            if kind in REPLICA_DATA:
                data = True
                said.append(f"replica-data {params.get(DATA_NAME) or orphan['metadata']['name']}")
            else:
                said.append(f"{kind or 'unknown'} {params.get(INSTANCE_NAME) or orphan['metadata']['name']}")
        fix = ("Check each in Longhorn before removing it. An instance record whose volume runs elsewhere and "
               "is healthy can be removed. ")
        fix += ("Removing a replica-data record deletes an on-disk copy of the volume's data, which may be its "
                "last copy, so remove one only once the volume is healthy on other hosts."
                if data else "Homestead does not remove these.")
        count = len(other)
        rows.append(_row(
            f"other-orphans:{node}", "other-orphans",
            f"{count} other Longhorn orphan record{'s' if count != 1 else ''} on {node}: {_names(said)}",
            f"Longhorn keeps what an orphan record names on {node} until the record is removed. An instance "
            f"record can keep {node}'s instance-manager from stopping.",
            fix))
    if removing:
        count = len(removing)
        rows.append(_row(
            f"removing-orphans:{node}", "removing-orphans",
            f"{count} Longhorn orphan record{'s' if count != 1 else ''} on {node} being removed: {_names(removing)}",
            f"Longhorn keeps a removed orphan record until it has cleaned up what the record names on {node}. "
            f"Until then, an engine record's leftover process can still hold {node}'s instance-manager.",
            "Longhorn is cleaning up. Review the restart again in a minute. If a record stays, check Longhorn on "
            f"{node} (its instance-manager may not be reachable)."))
    return rows


def _listed(node, managers, engines, named):
    """The engines node's instance-manager lists: (those that still run on
    node or are stopping there, those that no longer do and no orphan record
    in named names, said)."""
    live, stale = [], []
    for im in managers:
        if (im.get("spec") or {}).get("nodeID") != node:
            continue
        for name in sorted((im.get("status") or {}).get("instanceEngines") or {}):
            if name in named:
                continue
            if name not in engines:
                stale.append(f"{name} (no engine record)")
                continue
            where = _engine_node(engines[name])
            if where and where != node:
                stale.append(f"{name} (now on {where})")
            else:
                live.append(name)
    return live, stale


def _elsewhere_row(node, managers, engines, named):
    _, stale = _listed(node, managers, engines, named)
    if not stale:
        return []
    return [_row(
        f"engines-elsewhere:{node}", "engines-elsewhere",
        f"{node}'s Longhorn instance-manager still lists engine{'s' if len(stale) != 1 else ''} "
        f"{_names(stale)}, which no longer run{'s' if len(stale) == 1 else ''} on {node}",
        f"Longhorn keeps {node}'s instance-manager, and its disruption budget at 0, while it lists engines, "
        "so the drain cannot evict it.",
        "Longhorn normally records these as orphans within minutes. Review the restart again then. "
        "If they stay, check the instance-manager in Longhorn.")]


def _claims_of(pod):
    """The claims pod's volumes use; a generic ephemeral volume's claim is
    named "<pod name>-<volume name>" (Kubernetes ephemeral.VolumeClaimName)."""
    claims = []
    for v in (pod.get("spec") or {}).get("volumes") or []:
        if v.get("persistentVolumeClaim"):
            claims.append((v.get("persistentVolumeClaim") or {}).get("claimName"))
        elif v.get("ephemeral") and v.get("name"):
            claims.append(f"{(pod.get('metadata') or {}).get('name', '')}-{v['name']}")
    return claims


def _attached_here(node, attachments, detaching=True):
    """{volume name: attachment name} of the attachments to node; without
    those already being detached (deletionTimestamp set) when not detaching."""
    found = {}
    for a in attachments:
        if not detaching and _terminating(a):
            continue
        spec = a.get("spec") or {}
        volume = (spec.get("source") or {}).get("persistentVolumeName")
        if spec.get("nodeName") == node and volume:
            found[volume] = (a.get("metadata") or {}).get("name") or volume
    return found


def _volume_of(claims):
    return {((c.get("metadata") or {}).get("namespace"), (c.get("metadata") or {}).get("name")):
            (c.get("spec") or {}).get("volumeName") for c in claims}


def _unused_attachment_rows(node, pods, claims, attachments):
    """Attachments to node whose volume no pod on node uses: the pod is gone
    and the attachment stayed."""
    volume_of = _volume_of(claims)
    used = {volume_of.get(((p.get("metadata") or {}).get("namespace"), c))
            for p in pods if (p.get("spec") or {}).get("nodeName") == node for c in _claims_of(p)}
    rows = []
    # An attachment already being detached is a normal pod move, not stuck.
    for volume, name in sorted(_attached_here(node, attachments, detaching=False).items()):
        if volume in used:
            continue
        rows.append(_row(
            f"stuck-attachment:{name}", "stuck-attachment",
            f"Volume {volume} is still attached to {node} (attachment {name}), but no pod on {node} uses it",
            f"While it is attached its storage stays on {node}, so Longhorn keeps its engine there and the "
            "drain cannot finish.",
            "Check why it was not detached: a pod that was force-deleted, or the host still has it mounted. "
            "Homestead does not detach it."))
    return rows


def _stuck_rows(node, pods, claims, attachments, now):
    volume_of = None if claims is None else _volume_of(claims)
    attached = None if attachments is None else set(_attached_here(node, attachments))
    rows = []
    for pod in pods:
        meta = pod.get("metadata") or {}
        if (pod.get("spec") or {}).get("nodeName") != node:
            continue
        deadline = _seconds(meta.get("deletionTimestamp"))
        # deletionTimestamp is when the grace period ends, so a pod inside it
        # carries one in the future.
        if deadline is None or now - deadline < STUCK_AFTER:
            continue
        ns, name = meta.get("namespace", ""), meta.get("name", "")
        phase = (pod.get("status") or {}).get("phase") or "Unknown"
        held = []
        if volume_of is not None and attached is not None:
            held = sorted({volume_of.get((ns, c)) for c in _claims_of(pod)} & attached - {None})
        what = f"Pod {ns}/{name} on {node} has been terminating for {_span(now - deadline)} past its grace period"
        if held:
            what += f" and still holds volume{'s' if len(held) != 1 else ''} {_names(held)} attached there"
        elif volume_of is None or attached is None:
            what += ", and its volumes could not be checked"
        finalizers = meta.get("finalizers") or []
        why = (f"Its phase is {phase}"
               + (f" and it waits on finalizer{'s' if len(finalizers) != 1 else ''} {_names(finalizers)}"
                  if finalizers else "")
               + f". While it exists its volumes stay attached to {node}, so their storage cannot leave the host.")
        fix = ("Its containers have stopped, so force-deleting the pod is safe. If a volume stays attached "
               "afterwards, the host still has it mounted." if phase in ("Succeeded", "Failed") else
               "Find out why it does not stop (its events, the host's kubelet) before force-deleting it. A "
               "force delete of a pod whose containers still run can leave two writers on one volume.")
        rows.append(_row(f"stuck-pod:{ns}/{name}", "stuck-pod", what, why, fix))
    return sorted(rows, key=lambda row: row["id"])


def for_host(node, now=None, read=None):
    """What on node would keep its drain from finishing, as rows (see the
    module docstring); [] when nothing does. read: the caller's own reader
    (kget), so a module bound to one keeps reading through it."""
    return _for_host(node, now, read)


def failed(error):
    """The one row for a check that failed as a whole: unread, never skipped."""
    return _row("unread:drain-blockers", "unread",
                f"Homestead could not check what would keep this host from draining: {_reason(error)}",
                "What it would show was not checked, so something there may still hold this host.",
                "Review the restart again. If this stays, check Homestead's access to the cluster.")


# How long a pod has been stuck is said, not compared: the same pod a minute
# later is the same thing to acknowledge.
_STUCK_FOR = re.compile(r" for \d+ (?:min|h|days) past its grace period")


def review(rows):
    """rows as the restart review's token sees them: each row's id and what,
    without the time a pod has been stuck."""
    return [(row["id"], _STUCK_FOR.sub(" past its grace period", row["what"])) for row in rows]


def _for_host(node, now=None, read=None):
    now = time.time() if now is None else now
    got = _read_all({"orphans": ORPHANS, "instancemanagers": INSTANCE_MANAGERS, "engines": ENGINES,
                     "pods": _pods_path(node), "claims": CLAIMS, "attachments": ATTACHMENTS}, read)
    unread = [key for key, value in got.items() if isinstance(value, Exception)]

    def items(key, absent=()):
        value = got[key]
        if isinstance(value, Exception):
            return None
        return list(absent) if value is None else value

    orphans, managers, engine_list = items("orphans"), items("instancemanagers"), items("engines")
    engines = None if engine_list is None else _engines_by_name(engine_list)
    rows = []
    if orphans is not None:
        rows += _orphan_rows(node, orphans, engines)
    named = {r["engine"] for row in rows if row["kind"] == "engine-orphans" for r in row["records"]}
    named |= _removing_engines(node, orphans)
    if managers is not None and engines is not None:
        rows += _elsewhere_row(node, managers, engines, named)
    pods = items("pods")
    if pods is not None:
        rows += _stuck_rows(node, pods, items("claims"), items("attachments"), now)
        if items("claims") is not None and items("attachments") is not None:
            rows += _unused_attachment_rows(node, pods, items("claims"), items("attachments"))
    for key in unread:
        rows.append(_row(
            f"unread:{key}", "unread", f"Homestead could not check {UNREAD_WHAT[key]}: {_reason(got[key])}",
            "What it would show was not checked, so something there may still hold this host.",
            "Review the restart again. If this stays, check Homestead's access to the cluster."))
    return rows


def _reason(error):
    if isinstance(error, urllib.error.HTTPError):
        return f"HTTP {error.code}"
    return type(error).__name__


# ------------------------------------------------------- while it drains
# How long the drain waits on a disruption budget whose copies are still
# starting, from its first refusal.
BUDGET_WAIT = 300
# How long node's instance-manager may be held by nothing but engines that
# run elsewhere (and their orphan records) before the drain stops waiting:
# an engine that just moved can be listed here for a moment.
STALE_GRACE = 30
# How often the drain reads Longhorn's orphans, instance-managers and engines
# while it waits on an instance-manager: often enough for STALE_GRACE.
ENGINES_EVERY = 10
# Longhorn's setting for the orphan records it removes by itself; "instance"
# among its values (separated by ";") covers engine-instance records.
AUTO_DELETION = LH + "/settings/orphan-resource-auto-deletion"
# How long Longhorn waits before it removes them (longhorn-manager
# types/setting.go: SettingNameOrphanResourceAutoDeletionGracePeriod, an int
# in seconds, default "300"); the drain waits that much longer, at most
# GRACE_MOST.
AUTO_DELETION_GRACE = AUTO_DELETION + "-grace-period"
GRACE_DEFAULT, GRACE_MOST = 300, 1800

PDB = "/apis/policy/v1/namespaces/{ns}/poddisruptionbudgets/{name}"
# The cause Kubernetes gives an eviction a budget refuses
# (pkg/registry/core/pod/storage/eviction.go: checkAndDecrement and
# createTooManyRequestsError, cause type policyv1.DisruptionBudgetCause).
BUDGET_CAUSE = "DisruptionBudget"
_BUDGET_NAMED = re.compile(r"^The disruption budget (\S+) (?:needs \d+ healthy pods|is still being processed)")
UNREAD_WHY = "could not read why"
# The condition the disruption controller sets when it cannot work a budget
# out (pkg/controller/disruption/disruption.go failSafe; policy/v1
# DisruptionAllowedCondition and SyncFailedReason).
SYNC_CONDITION, SYNC_FAILED = "DisruptionAllowed", "SyncFailed"
SYNC_SAID = 200     # characters of the controller's SyncFailed message a refusal quotes
THROTTLED = "the API server asked Homestead to slow down"


def _retry_after(error, status):
    """The seconds a 429 asks to wait: its Retry-After header in seconds, else
    the Status body's details.retryAfterSeconds; 0 for neither."""
    for value in ((getattr(error, "headers", None) or {}).get("Retry-After"),
                  ((status or {}).get("details") or {}).get("retryAfterSeconds")):
        try:
            seconds = int(str(value).strip())
        except (TypeError, ValueError):
            continue
        if seconds > 0:
            return seconds
    return 0


def refusal(error, pod, read=None):
    """Why an eviction of pod was refused with 429, from the refusal's own
    Status body and the budget it names: {budget, passing, starting,
    expected, why}. passing: the budget has copies not yet healthy
    (expectedPods > currentHealthy), Kubernetes has not processed its latest
    change yet, or its status is behind an eviction just made (disruptedPods
    set, or currentHealthy above desiredHealthy with no disruption allowed),
    so waiting can let the eviction through. A body or
    budget that can't be read is not passing, with why "couldn't read why".
    A budget Kubernetes cannot update (SyncFailed) is not passing either.
    plain: the 429 is the API server's own Status with no budget cause, and
    retry_after the seconds it asks to wait (0 when it names none)."""
    unknown = {"budget": None, "passing": False, "starting": 0, "expected": 0, "why": UNREAD_WHY}
    try:
        status = json.loads(error.read() or b"null")
        causes = ((status or {}).get("details") or {}).get("causes") or []
    except Exception:  # no body, not JSON, not a Status: why is unknown
        return unknown
    budget_causes = [c for c in causes if isinstance(c, dict) and c.get("reason") == BUDGET_CAUSE]
    if isinstance(status, dict) and status.get("kind") == "Status" and not budget_causes:
        # The API server's own 429 (such as API priority and fairness): no
        # budget refused it, so the drain tries it again in its normal wait,
        # after the time the answer asks for (Retry-After, else its body's
        # retryAfterSeconds).
        return {**unknown, "plain": True, "why": THROTTLED, "retry_after": _retry_after(error, status)}
    named = [_BUDGET_NAMED.match(c.get("message") or "") for c in budget_causes]
    named = [m.group(1) for m in named if m]
    ns = (pod.get("metadata") or {}).get("namespace") or ""
    if not named or not ns:
        return unknown
    try:
        pdb = (read or kget)(PDB.format(ns=ns, name=named[0])) or {}
        st, meta = pdb.get("status") or {}, pdb.get("metadata") or {}
        expected, healthy = int(st.get("expectedPods") or 0), int(st.get("currentHealthy") or 0)
        desired = int(st.get("desiredHealthy") or 0)
        processing = int(st.get("observedGeneration") or 0) < int(meta.get("generation") or 0)
        # Right after an eviction Kubernetes (eviction.go checkAndDecrement)
        # sets disruptionsAllowed to 0 and names the pod in disruptedPods;
        # currentHealthy stays as it was until the budget controller resyncs.
        behind = bool(st.get("disruptedPods")) or (
            healthy > desired and int(st.get("disruptionsAllowed") or 0) == 0)
        # The disruption controller could not work the budget out (its
        # failSafe): its counts are the last ones written, so they promise
        # nothing, and it allows no disruption until the cause is fixed.
        failed_sync = next((c for c in st.get("conditions") or [] if isinstance(c, dict)
                            and c.get("type") == SYNC_CONDITION and c.get("reason") == SYNC_FAILED), None)
    except Exception:
        return unknown
    budget = f"{ns}/{named[0]}"
    if failed_sync:
        # The controller's own error text, which can be long: the start of it.
        said = str(failed_sync.get("message") or "no reason given")
        said = said if len(said) <= SYNC_SAID else said[:SYNC_SAID] + "…"
        return {"budget": budget, "passing": False, "starting": 0, "expected": expected,
                "why": f"disruption budget {budget}, which allows no disruption because Kubernetes cannot update it: {said}"}
    starting = max(0, expected - healthy)
    if starting:
        why = (f"disruption budget {budget}, which has {healthy} of {expected} copies healthy and needs "
               f"{desired} ({starting} not yet healthy)")
    elif processing:
        why = f"disruption budget {budget}, which Kubernetes has not caught up with since it was changed"
    elif behind:
        why = (f"disruption budget {budget}, which Kubernetes has not caught up with since a copy was evicted "
               f"({healthy} of {expected} copies counted healthy, it needs {desired})")
    else:
        why = (f"disruption budget {budget} allows no disruption: {healthy} of {expected} copies are healthy, "
               f"it needs {desired}, and none is starting")
    return {"budget": budget, "passing": bool(starting or processing or behind), "starting": starting,
            "expected": expected, "why": why}


def engines_here(node, read=None):
    """What holds node's instance-manager: (how many engines it lists that
    still run on node or are stopping there, the rows - engine-orphans and
    engines-elsewhere - naming the ones that no longer do). None when Longhorn
    could not be read."""
    got = _read_all({"orphans": ORPHANS, "instancemanagers": INSTANCE_MANAGERS, "engines": ENGINES}, read)
    if any(isinstance(value, Exception) for value in got.values()):
        return None
    try:
        orphans, managers = got["orphans"] or [], got["instancemanagers"] or []
        engines = _engines_by_name(got["engines"] or [])
        # Only a record whose engine the instance-manager still lists holds
        # it; one whose process has gone already holds nothing.
        listed = {name for im in managers if (im.get("spec") or {}).get("nodeID") == node
                  for name in (im.get("status") or {}).get("instanceEngines") or {}}
        holding = [o for o in orphans if ((o.get("spec") or {}).get("parameters") or {}).get(INSTANCE_NAME) in listed]
        rows = [row for row in _orphan_rows(node, holding, engines) if row["kind"] == "engine-orphans"]
        # Records being removed are Longhorn's to finish: not counted as what
        # nothing more will let go, and named when the drain times out.
        named = {r["engine"] for row in rows for r in row["records"]} | _removing_engines(node, orphans)
        rows += _elsewhere_row(node, managers, engines, named)
        live, _ = _listed(node, managers, engines, named)
    except Exception:  # data not shaped as Longhorn writes it: couldn't check, as unread
        return None
    return len(live), rows


def removes_instance_orphans(read=None):
    """The seconds Longhorn waits before it removes engine-instance orphan
    records by itself (orphan-resource-auto-deletion includes "instance"),
    or None when it does not. Then the drain does not stop early on them and
    waits that much longer: Longhorn lets the instance-manager go once they
    are removed. A setting that cannot be read counts as off, as before; a
    grace period that cannot be read is Longhorn's default, and at most
    GRACE_MOST counts."""
    try:
        value = str(((read or kget)(AUTO_DELETION) or {}).get("value") or "")
    except Exception:
        return None
    if "instance" not in [part.strip() for part in value.split(";")]:
        return None
    try:
        grace = int(str(((read or kget)(AUTO_DELETION_GRACE) or {}).get("value")).strip())
    except Exception:
        grace = GRACE_DEFAULT
    return min(max(grace, 0), GRACE_MOST)


GENERAL_ADVICE = "Check the Longhorn volumes, disruption budgets and pod events"


def say(node, rows):
    """rows (for_host's) as the end of one sentence for a stalled drain's
    message: what holds node and what couldn't be checked, never left out;
    only what couldn't be checked, with the general advice; "" for no rows."""
    found = [row["what"] for row in rows if row["kind"] != "unread"]
    unread = [row["what"] for row in rows if row["kind"] == "unread"]
    if found:
        return f"what holds {node}: " + "; ".join(found + unread) + ". Review the restart again to see what to do"
    if unread:
        return "; ".join(unread) + ". " + GENERAL_ADVICE
    return ""


def stall_reason(node, read=None):
    """What holds node, read now, for the message of a drain that stalled;
    "" when nothing is found."""
    try:
        return say(node, _for_host(node, read=read))
    except Exception:  # the message falls back to its general advice
        return ""


# ------------------------------------------------- removing engine orphan records
# The restart review offers one fix: removing Longhorn's engine-instance orphan
# records on the host whose engine already runs on another host. Longhorn then
# stops the leftover engine process, the instance-manager can go, and the drain
# can finish. Nothing else is removed here: a replica-data record ("replica" in
# v1.13.0) names an on-disk copy of a volume, which removing it deletes.
REMOVE_AT_ONCE = 50
_NODE = re.compile(r"[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?")
_NAME = re.compile(r"[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?")


def remove_request(body):
    """The checked request body: (node, records), or ValueError."""
    if not isinstance(body, dict):
        raise ValueError("the request must be an object")
    node, records = body.get("node"), body.get("records")
    if not isinstance(node, str) or not _NODE.fullmatch(node):
        raise ValueError("node must be a host name")
    if not isinstance(records, list) or not records or len(records) > REMOVE_AT_ONCE:
        raise ValueError(f"records must list 1 to {REMOVE_AT_ONCE} orphan records")
    checked = []
    for item in records:
        if not isinstance(item, dict) or not all(isinstance(item.get(k), str) and item.get(k)
                                                 for k in ("name", "uid", "resourceVersion")):
            raise ValueError("each record names its name, uid and resourceVersion")
        if not _NAME.fullmatch(item["name"]):
            raise ValueError("a record name is not a Kubernetes name")
        checked.append({k: item[k] for k in ("name", "uid", "resourceVersion")})
    return node, checked


def _remove_one(node, item, read, send):
    """Remove one engine-instance orphan record, or why not."""
    name = item["name"]
    try:
        orphan = read(f"{ORPHANS}/{urllib.parse.quote(name, safe='')}")
    except urllib.error.HTTPError as error:
        return "it has already been removed" if error.code == 404 else f"it could not be read ({_reason(error)})"
    except Exception as error:
        return f"it could not be read ({_reason(error)})"
    meta, spec = orphan.get("metadata") or {}, orphan.get("spec") or {}
    if meta.get("uid") == item["uid"] and meta.get("deletionTimestamp"):
        return "it is already being removed"
    if meta.get("uid") != item["uid"] or meta.get("resourceVersion") != item["resourceVersion"]:
        return "it changed since the review, so review the restart again"
    if spec.get("orphanType") != ENGINE_INSTANCE:
        return "it is not an engine instance record, and Homestead removes only those"
    if spec.get("nodeID") != node:
        return f"it is not on {node}"
    engine_name = (spec.get("parameters") or {}).get(INSTANCE_NAME) or ""
    if not engine_name:
        return "it names no engine"
    try:
        engine = read(f"{ENGINES}/{urllib.parse.quote(engine_name, safe='')}")
    except Exception as error:
        return f"its engine {engine_name} could not be read ({_reason(error)})"
    where = _engine_node(engine)
    if not where:
        return f"its engine {engine_name} is not running anywhere Homestead can see"
    if where == node:
        return f"its engine {engine_name} runs on {node} now"
    # spec.nodeID is where Longhorn wants the engine; status.instanceManagerName
    # (v1.13.0 InstanceStatus) is the instance-manager its process runs in.
    # While that is still node's, the move has not finished.
    manager = ((engine or {}).get("status") or {}).get("instanceManagerName") or ""
    if not manager:
        return f"its engine {engine_name} names no instance-manager"
    try:
        im = read(f"{INSTANCE_MANAGERS}/{urllib.parse.quote(manager, safe='')}") or {}
    except urllib.error.HTTPError as error:
        im = {} if error.code == 404 else error
    except Exception as error:
        im = error
    if isinstance(im, Exception):
        return f"its engine's instance-manager {manager} could not be read ({_reason(im)})"
    if (im.get("spec") or {}).get("nodeID") == node:
        return f"its engine {engine_name} still runs in {node}'s instance-manager"
    try:
        send("DELETE", f"{ORPHANS}/{urllib.parse.quote(name, safe='')}",
             {"apiVersion": "v1", "kind": "DeleteOptions",
              "preconditions": {"uid": item["uid"], "resourceVersion": item["resourceVersion"]}})
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return "it has already been removed"
        if error.code == 409:
            return "it changed since the review, so review the restart again"
        return f"Kubernetes refused to remove it ({_reason(error)})"
    except Exception as error:
        return f"it could not be removed ({_reason(error)})"
    return None


def remove_engine_orphans(node, records, read=None, send=None):
    """Remove the reviewed engine-instance orphan records on node whose engine
    runs on another host: {"removed": [name], "refused": [{"name", "why"}]}.
    Each is read again first, and deleted only with its uid and resourceVersion
    as preconditions."""
    read, send = read or kget, send or KUBE.ksend
    removed, refused = [], []
    for item in records:
        why = _remove_one(node, item, read, send)
        if why is None:
            removed.append(item["name"])
        else:
            refused.append({"name": item["name"], "why": why})
    return {"removed": removed, "refused": refused}


def _remove_route(request):
    node, records = remove_request(request.body)
    return remove_engine_orphans(node, records)


# Its routes and who may use them (homestead_routes.py).
ROUTES = {
    ("POST", "/api/node/orphans/remove"): ("admin", _remove_route),
}
