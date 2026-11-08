"""Ending a disk job whose host was removed: a reviewed, admin-only end.

A V2 disk preparation, a storage allocation or Longhorn runtime maintenance
pins its host's identity and refuses every step on another one, so once that
identity is gone from Kubernetes the job can never finish - yet it is never
forgotten, and keeps holding back Longhorn upgrades, shutdowns and OS rollouts.

Proof that the host is gone is identity only, read fresh under the job lock:
the Node is not there (404) or has another uid than the job pinned, and where
the job pinned Longhorn's record of the host, the same holds for that record.
A host that is only down, a name, a cache or a failed read is not proof.

The end records who ended the job and on what evidence (phase "host-gone",
status failed, ref kept), and deletes the job's helper Job only if it is still
the exact object the job created. Nothing on the machine is touched: what the
job may have left there is listed in the review, for a person to deal with.
"""
import copy
import time
import urllib.error
from urllib.parse import quote

import homestead_capacity_review as REVIEW
import homestead_disk_v2 as V2
import homestead_storage_conflicts as CONFLICTS

NODES = "/api/v1/nodes/"
LONGHORN_NODES = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system/nodes/"
SYSTEMD = "/etc/systemd/system/"
RUNTIME_FOLDER = "/var/lib/homestead/longhorn-runtime/"
GiB = 1024 ** 3


class Unreadable(Exception):
    pass


def _read(read, path):
    """The object, None when it is not there (404); anything else is Unreadable."""
    try:
        value = read(path)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise Unreadable(f"HTTP {error.code}") from error
    except Exception as error:
        raise Unreadable(type(error).__name__) from error
    if not isinstance(value, dict) or not (value.get("metadata") or {}).get("uid"):
        raise Unreadable("no identity in the answer")
    return value


def _identity(read, path, pinned):
    """("missing" | "replaced" | "present", the uid seen)."""
    found = _read(read, path)
    if found is None:
        return "missing", None
    uid = found["metadata"]["uid"]
    return ("present" if uid == pinned else "replaced"), uid


def _job(items, ident):
    item = next((row for row in items if row.get("id") == ident), None)
    if item is None or item.get("kind") not in CONFLICTS.HOST_GONE_KINDS or not CONFLICTS.unresolved(item):
        raise ValueError("There is no disk job left by a removed host with this id to end")
    return item


def _helper(ref, read, namespace):
    """The helper Job this job created, as it stands now (part of the review)."""
    name = ref.get("job")
    if not name:
        return None
    found = _read(read, f"/apis/batch/v1/namespaces/{namespace}/jobs/{quote(name, safe='')}")
    if found is None:
        return {"name": name, "state": "absent"}
    uid = found["metadata"]["uid"]
    if not ref.get("job_uid"):
        return {"name": name, "state": "unverified", "uid": uid}
    return {"name": name, "state": "own" if uid == ref["job_uid"] else "other", "uid": uid}


def _left_on_host(kind, ref):
    phase, node = ref.get("phase"), ref.get("node", "")
    if kind == "disk-v2-convert":
        hw = ref.get("hardware") or {}
        disk = f"{hw.get('device', 'The disk')} (serial {hw.get('serial') or 'unknown'}, {hw.get('by_id') or 'no stable name'})"
        if phase in ("evacuating", "awaiting-erase", "removing"):
            return [f"{disk} was not erased. It stays as it was: mounted, and disabled for new Longhorn replicas."]
        if phase in ("dispatching", "preparing"):
            return [f"{disk} may be partly erased: the erase helper may have started on it."]
        return [f"{disk} was erased and may hold a Longhorn V2 block disk."]
    if kind == "disk-allocation":
        if phase == "queued":
            return ["Nothing was started on the host: no helper ran."]
        area = ref.get("area") or {}
        return [f"The {area.get('name', '')} logical volume ({area.get('lv_name', '')}) may be partly grown towards "
                f"{(ref.get('target_bytes') or 0) / GiB:.1f} GiB, and its filesystem with it."]
    if phase == "queued":
        return ["Nothing was started on the host: runtime maintenance had not begun."]
    service = ref.get("service")
    dropin = (f"{SYSTEMD}{service}.service.d/homestead-runtime.conf, which makes {service} require that unit" if service else
              f"{SYSTEMD}k3s.service.d or k3s-agent.service.d/homestead-runtime.conf, which makes k3s require that unit")
    return [f"Boot unit {SYSTEMD}homestead-longhorn-runtime.service, which runs before k3s on every boot.",
            f"Drop-in {dropin}.",
            f"Folder {RUNTIME_FOLDER}{ref.get('request_id', '')} (the maintenance plan and its state).",
            f"The retained original Longhorn data at {ref.get('retained', 'its retained path')}.",
            f"If {node} comes back as the same machine without a reinstall, remove the unit and the drop-in before it boots into k3s."]


def _released(kind, ref):
    lines = ["Longhorn upgrades, cluster shutdown and OS rollouts.",
             f"Disk and host jobs on a host named {ref.get('node', '')}.",
             "Starting V2 cleanup (disabling Longhorn V2)."]
    if kind == "disk-v2-convert" and ref.get("volumes"):
        lines.append("Storage jobs on the volumes it moved to other hosts: "
                     + ", ".join(row.get("volume", "") for row in ref["volumes"]) + ".")
    if kind == "longhorn-runtime":
        lines.append("Runtime maintenance on other hosts.")
    return lines


def _left_in_cluster(helper, namespace):
    if not helper or helper["state"] == "absent":
        return []
    name = helper["name"]
    return [{"own": f"Helper Job {name} in {namespace}, pinned to the removed host: deleted when you end the job, "
                    "only if it is still the same Job.",
             "other": f"A Job named {name} with another identity: left alone.",
             "unverified": f"Helper Job {name}: this job never saved its identity, so it is left alone; "
                           "delete it by hand once you have checked it."}[helper["state"]]]


def _snapshot(item, read, actor, namespace):
    ref, kind = item["ref"], item["kind"]
    node = ref.get("node", "")
    blockers, evidence, observed = [], {}, {}
    if not ref.get("node_uid"):
        blockers.append(f"This job did not record {node}'s identity, so Homestead cannot prove the host is gone.")
    else:
        try:
            evidence["node"], observed["node"] = _identity(read, NODES + quote(node, safe=""), ref["node_uid"])
            if evidence["node"] == "present":
                blockers.append(f"{node} is still in the cluster with the same identity this job pinned. "
                                "Remove it first (Cluster, the host, What it takes), or open the job.")
            if ref.get("lh_uid") and kind in ("disk-v2-convert", "longhorn-runtime"):
                evidence["longhorn"], observed["longhorn"] = _identity(read, LONGHORN_NODES + quote(node, safe=""), ref["lh_uid"])
                if evidence["longhorn"] == "present":
                    blockers.append(f"Longhorn still has its record of {node} with the identity this job pinned. On the Cluster "
                                    f'page, under Hosts coming and going, "Longhorn still lists {node}": delete it, or force it '
                                    "if replicas are still recorded there. Then review again.")
        except Unreadable as error:
            blockers.append(f"{node} could not be read ({error}). A failed read is not proof that the host is gone.")
    try:
        helper = _helper(ref, read, namespace)
    except Unreadable as error:
        helper = None
        blockers.append(f"The helper Job {ref.get('job')} could not be read ({error}).")
    warnings = ["Homestead records the end, and deletes the helper Job below if it is still the same Job.",
                "The job's record stays in Jobs until you dismiss it."]
    if "replaced" in evidence.values():
        warnings.insert(0, f"A host named {node} is in the cluster with a new identity. If it is the same machine and it was "
                           "not reinstalled, what this job left there is still on it.")
    plan = {"id": item["id"], "kind": kind, "title": item.get("title", ""), "node": node, "phase": ref.get("phase"),
            "blocked": bool(blockers), "blockers": blockers, "evidence": evidence,
            "left_on_host": _left_on_host(kind, ref), "left_in_cluster": _left_in_cluster(helper, namespace),
            "released": _released(kind, ref), "warnings": warnings, "requires_confirmation": True, "confirm": node}
    context = {"action": "host-gone-end", "actor": actor, "job_id": item["id"], "ref": copy.deepcopy(ref),
               "evidence": evidence, "observed": observed, "helper": helper}
    return plan, context


def preview(ident, ops, read, actor, namespace):
    with ops.transaction():
        plan, context = _snapshot(_job(ops.stored(), ident), read, actor, namespace)
        return {"plan": plan, "capacity_token": None if plan["blocked"] else REVIEW.issue({"id": ident}, context)}


def _delete_helper(helper, send, namespace):
    if not helper or helper["state"] != "own":
        return helper["state"] if helper else "none"
    try:
        send("DELETE", f"/apis/batch/v1/namespaces/{namespace}/jobs/{quote(helper['name'], safe='')}",
             {"apiVersion": "v1", "kind": "DeleteOptions", "propagationPolicy": "Background",
              "preconditions": {"uid": helper["uid"]}})
        return "deleted"
    except urllib.error.HTTPError as error:
        return f"not deleted (HTTP {error.code})"
    except Exception as error:
        return f"not deleted ({type(error).__name__})"


def resolve(body, ops, read, send, actor, namespace):
    ident = str(body.get("id") or "")
    with ops.transaction() as t:
        items = t.items
        item = _job(items, ident)
        plan, context = _snapshot(item, read, actor, namespace)
        if plan["blocked"]:
            raise ValueError(" ".join(plan["blockers"]) + " Nothing was ended.")
        if body.get("confirm_capacity") is not True or not REVIEW.valid({"id": ident, "capacity_token": body.get("capacity_token")}, context):
            raise ValueError("What Homestead found has changed since the review, or the review expired. Review again; nothing was ended.")
        if body.get("acknowledge_host_gone") is not True or body.get("confirm") != plan["confirm"]:
            raise ValueError("Type the host's name and confirm that nothing on the host is changed. Nothing was ended.")
        ref, node = item["ref"], plan["node"]
        outcome = _delete_helper(context["helper"], send, namespace)
        ref["host_gone"] = {"by": actor, "at": time.time(), "previous_phase": ref.get("phase"),
                            "previous_status": item.get("status"), "evidence": context["evidence"],
                            "observed": context["observed"], "helper_job": outcome}
        ref["phase"] = CONFLICTS.HOST_GONE
        ops._finish(item, "failed", item.get("progress", 0),
                    f"Ended: its host {node} was removed. Nothing on the host was changed; the record lists what may remain there.")
        t.save(items)
        detail = f"Ended. {node}'s job no longer holds anything back; nothing on the host was changed."
        if outcome.startswith("not deleted"):
            detail += f" The helper Job {context['helper']['name']} was {outcome}; delete it by hand once you have checked it."
        return {"ok": True, "detail": detail, "operation": ops._public(item)}


def _ops():
    import homestead_operations as OPS
    return OPS


def _resolve_route(request):
    import homestead_routes as ROUTER
    result = resolve(request.body, _ops(), V2.kget, V2.ksend, request.user, V2.NS)
    ROUTER.forget("disks", "lhcap")
    return result


# Read and written through the disk module's bound client, as its own steps are.
ROUTES = {
    ("POST", "/api/operations/host-gone/preview"): ("admin", lambda request: preview(
        str(request.body.get("id") or ""), _ops(), V2.kget, request.user, V2.NS)),
    ("POST", "/api/operations/host-gone/resolve"): ("admin", _resolve_route),
}
