"""Fresh, read-only admission for the independent self-data worker.

No server import, DATA_DIR, cache, hardware reconciliation or cluster writes.
Setup must present review() to the user before persisting its receipt. Receipts
are not signatures: their authority comes from the reviewed, UID/CAS-pinned
anchor. This is a bounded scheduler preview, never a capacity reservation.
Host-local paths and unmodelled device allocation require a separate workflow;
they must not disappear behind an acknowledgement of memory pressure.
"""
import copy
import datetime
import hashlib
import json
import re
import time

import homestead_batch_capacity as BATCH
import homestead_place as PLACE
import homestead_pod_resources as RESOURCES
import homestead_self_data_copy as COPY
from homestead_storage_journal import Held


BASE_WARNINGS = {
    "This is a snapshot, not a reservation. Kubernetes chooses placement; the example below is not enforced.",
    "Other controllers' pending replicas and concurrent admissions are not fully simulated.",
    "Admission webhooks may change the final pod spec; provisioning and application readiness are not guaranteed.",
    "in-place resizing uses the higher observed resource reservation",
    "replicas sharing a ReadWriteOnce claim must fit together on one host",
    "live memory usage is unavailable", "no memory estimate is configured",
}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def warning_key(message):
    """A capacity warning as approved, its live figures in bands: "projected
    RAM reaches 83.2%" at review and 84.9% a minute later is the warning that
    was accepted - memory use on a running cluster never holds still - while
    pressure that rises a band (5 points, or half a GiB less reserve), any
    rise once memory is projected full (100% and over), a new kind of
    warning, or one about another host needs a new review."""
    def band(match):
        value, unit = float(match.group(1)), match.group(2)
        if unit == "%" and value >= 100:
            return match.group(0)
        step = 0.5 if unit == " GiB" else 5
        return f"{int(value // step)}~{unit}"
    return digest(re.sub(r"(\d+(?:\.\d+)?)(%| GiB)", band, message))


def validate_policy(policy):
    if (not isinstance(policy, dict) or set(policy) != {"threshold", "reviews"}
            or type(policy["threshold"]) is not int or not 1 <= policy["threshold"] <= 100
            or not isinstance(policy["reviews"], dict) or set(policy["reviews"]) != {"copy", "restart"}):
        raise Held("The data move needs a complete capacity review")
    for receipt in policy["reviews"].values():
        if (not isinstance(receipt, dict) or set(receipt) != {"proposal", "warnings"}
                or not isinstance(receipt["warnings"], list) or len(receipt["warnings"]) > 512
                or any(not isinstance(v, str) or not re.fullmatch(r"[a-f0-9]{64}", v)
                       for v in [receipt["proposal"], *receipt["warnings"]])
                or len(set(receipt["warnings"])) != len(receipt["warnings"])):
            raise Held("The data move capacity approval is invalid")


def validate_worker_approval(approval):
    import homestead_self_data_anchor as A
    A._keys(approval, ("threshold", "nodes", "receipt"))
    validate_policy({"threshold": approval["threshold"], "reviews": {"copy": approval["receipt"], "restart": approval["receipt"]}})
    if not isinstance(approval["nodes"], list) or not 1 <= len(approval["nodes"]) <= 256:
        raise Held("The coordinator capacity review needs a bounded host inventory")
    names = set()
    for node in approval["nodes"]:
        A._keys(node, ("name", "uid", "boot_id"))
        A._name(node["name"]); A._identity({"uid": node["uid"], "resourceVersion": node["boot_id"]})
        if node["name"] in names:
            raise Held("The coordinator capacity review has duplicate hosts")
        names.add(node["name"])


def _capacity_warning(message):
    return (message in BASE_WARNINGS or message.startswith("memory is not limited for ")
            or re.fullmatch(r"\d+ unscheduled pod\(s\) also compete for capacity", message)
            or re.fullmatch(r"projected RAM reaches [\d.]+% \(warning at \d+%\)", message)
            or re.fullmatch(r"less than [\d.]+ GiB host reserve remains", message)
            or re.fullmatch(r"[a-z0-9.-]+: conservative batch RAM upper estimate [\d.]+% exceeds warning threshold \d+%", message))


def _inventory(read, path, *, metric=False):
    result = read(path)
    if (not isinstance(result, dict) or not isinstance(result.get("items"), list)
            or result.get("metadata", {}).get("continue")):
        raise Held("The data move capacity inventory is incomplete")
    seen = set()
    for item in result["items"]:
        meta = item.get("metadata", {}) if isinstance(item, dict) else {}
        key = (meta.get("namespace"), meta.get("name"))
        if not meta.get("name") or not metric and not meta.get("uid") or key in seen:
            raise Held("The data move capacity inventory has ambiguous identities")
        seen.add(key)
    return result["items"]


def _nodes(read, pinned, now):
    raw = _inventory(read, "/api/v1/nodes")
    expected = {n["name"]: n for n in pinned}
    if len(expected) != len(pinned) or {n["metadata"]["name"] for n in raw} != set(expected):
        raise Held("Cluster membership changed; review the data move placement again")
    try:
        metrics = {n["metadata"]["name"]: n for n in _inventory(read, "/apis/metrics.k8s.io/v1beta1/nodes", metric=True)}
    except Exception:
        metrics = {}  # Missing telemetry is an explicit, separately approved warning.
    nodes = []
    for node in raw:
        meta, status, spec = node["metadata"], node.get("status", {}), node.get("spec", {})
        pin = expected[meta["name"]]
        if (meta["uid"] != pin["uid"] or status.get("nodeInfo", {}).get("bootID") != pin["boot_id"]
                or meta.get("deletionTimestamp")):
            raise Held("A reviewed capacity host was replaced, rebooted or removed")
        alloc = status.get("allocatable", {})
        if any(RESOURCES.quantity(alloc.get(key), key) <= 0 for key in ("memory", "cpu", "pods")):
            raise Held("Node allocatable resources are unavailable; capacity cannot be verified")
        cap = RESOURCES.quantity(status.get("capacity", {}).get("memory"))
        if cap <= 0:
            raise Held("Node memory capacity is unavailable")
        used, known = 0, False
        try:
            metric = metrics[meta["name"]]
            stamp = datetime.datetime.fromisoformat(metric["timestamp"].replace("Z", "+00:00"))
            if stamp.tzinfo is None or not -5 <= now - stamp.timestamp() <= 120:
                raise ValueError("stale")
            if metric["usage"]["memory"] in (None, ""):
                raise ValueError("missing")
            used = RESOURCES.quantity(metric["usage"]["memory"])
            known = True
        except (KeyError, TypeError, ValueError):
            pass
        nodes.append({"name": meta["name"], "uid": meta["uid"], "labels": meta.get("labels", {}),
            "status": "Ready" if any(c.get("type") == "Ready" and c.get("status") == "True" for c in status.get("conditions", [])) else "NotReady",
            "schedulable": not spec.get("unschedulable", False), "taints": spec.get("taints", []),
            "conditions": status.get("conditions", []), "allocatable": alloc,
            "mem_cap_gb": cap / 1024**3, "mem_used_gb": used / 1024**3, "mem_metrics_available": known})
    return nodes


def _proposal(purpose, proposal, namespace):
    kind = {"copy": "Job", "restart": "Deployment", "worker": "Pod"}.get(purpose)
    if kind is None or proposal.get("kind") != kind or proposal.get("metadata", {}).get("namespace") != namespace:
        raise Held("The capacity proposal does not match this data move")
    template = ({"metadata": copy.deepcopy(proposal["metadata"]), "spec": copy.deepcopy(proposal["spec"])}
                if purpose == "worker" else proposal["spec"]["template"])
    if purpose == "worker":
        # A read-only preview cannot know a future ConfigMap's Kubernetes UID.
        # Normalize ONLY that runtime identity for capacity acknowledgement.
        # Bootstrap's separately journalled manifest still binds the real UID,
        # and the launcher cannot override it. Scope/image/resources stay bound.
        import homestead_self_data_launch as L
        env = [e for c in template["spec"].get("containers", []) for e in c.get("env", []) if e.get("name") == L.CONFIG_ENV]
        if len(env) != 1 or set(env[0]) != {"name", "value"}:
            raise Held("The coordinator capacity template has ambiguous runtime configuration")
        config, scope = L.parse_configuration(env[0]["value"])
        if scope.namespace != namespace:
            raise Held("The coordinator capacity template belongs to a different namespace")
        config["anchor_uid"] = "pending"
        env[0]["value"] = json.dumps(config, sort_keys=True, separators=(",", ":"))
        if "route" in config:
            owners = template.get("metadata", {}).get("ownerReferences", [])
            if len(owners) != 1 or owners[0].get("kind") != "ConfigMap" or owners[0].get("name") != scope.deployment + "-data-handoff":
                raise Held("The maintenance helper needs its exact control-record owner")
            owners[0]["uid"] = "pending"
    spec = template["spec"]
    replicas = 1 if purpose in ("copy", "worker") else proposal["spec"]["replicas"]
    if type(replicas) is not int or not 1 <= replicas <= 64:
        raise Held("The data move replica count needs a bounded placement review")
    if (spec.get("resourceClaims") or spec.get("runtimeClassName") or spec.get("schedulingGates")
            or spec.get("schedulerName", "default-scheduler") != "default-scheduler"
            or any(v.get("hostPath") or v.get("ephemeral") or v.get("csi") for v in spec.get("volumes", []))
            or any("/" in r for r in RESOURCES.resource_names(spec))):
        raise Held("Host-local data, devices or custom scheduling need a separate placement review")
    # Preserve template labels, annotations, affinity and every init/sidecar.
    dep = {"kind": "Deployment", "apiVersion": "apps/v1", "metadata": copy.deepcopy(proposal["metadata"]),
           "spec": {"replicas": replicas, "template": copy.deepcopy(template)}}
    fingerprint = digest({"purpose": purpose, "namespace": namespace, "name": proposal["metadata"]["name"],
                          "replicas": replicas, "template": template})
    return dep, replicas, fingerprint


def _review_key(fingerprint, threshold, nodes):
    return digest({"workload": fingerprint, "threshold": threshold, "nodes": sorted(nodes, key=lambda n: n["name"])})


def review(read, namespace, purpose, proposal, pinned_nodes, threshold, *, clock=time.time):
    """Return displayable fresh evidence plus an unsigned receipt for approval.

    Never filters old or terminating pods out of the inventory. The phase engine
    separately proves mounts are released before copy or restart admission.
    """
    started = clock()
    if type(threshold) is not int or not 1 <= threshold <= 100:
        raise Held("The data move memory warning threshold is invalid")
    dep, count, fingerprint = _proposal(purpose, proposal, namespace)
    cache = {}
    def current(path):
        if path not in cache:
            cache[path] = copy.deepcopy(read(path))
        return copy.deepcopy(cache[path])
    nodes = _nodes(current, pinned_nodes, started)
    pods = _inventory(current, "/api/v1/pods")
    # Do not let a planner's unknown-storage caution become a RAM override.
    for volume in dep["spec"]["template"]["spec"].get("volumes", []):
        claim = volume.get("persistentVolumeClaim", {}).get("claimName")
        if not claim:
            continue
        pvc = current(f"/api/v1/namespaces/{namespace}/persistentvolumeclaims/{claim}")
        pv_name = pvc.get("spec", {}).get("volumeName")
        if pvc.get("status", {}).get("phase") != "Bound" or not pv_name or not pvc.get("metadata", {}).get("uid"):
            raise Held("A data move claim is not verifiably bound")
        pv = current("/api/v1/persistentvolumes/" + pv_name)
        ref = pv.get("spec", {}).get("claimRef", {})
        if (ref.get("uid"), ref.get("namespace"), ref.get("name")) != (pvc["metadata"]["uid"], namespace, claim):
            raise Held("A data move volume binding changed")
    single = PLACE.manifest_plan(dep, namespace, dep["metadata"]["name"], count, threshold,
        pod_snapshot=pods, nodes_snapshot=nodes, read=current, features=[])
    if single["blocked"] or not single["reservations_known"] or single["topology_status"] == "unknown":
        raise Held("Current storage, topology or scheduler capacity prevents the data move")
    report = BATCH.plan([{"deployment": dep, "name": dep["metadata"]["name"], "replicas": count}],
        namespace, pods, nodes, {}, threshold, read=current, features=[])
    warnings = sorted(set(report["warnings"]) | set(single["warnings"]))
    if report["blocked"] or any(not _capacity_warning(w) for w in warnings):
        raise Held("Placement has an unresolved storage or scheduler constraint; a memory override cannot bypass it")
    if not 0 <= clock() - started <= 30:
        raise Held("The data move capacity check took too long; obtain a fresh review")
    report["warnings"] = warnings
    report["receipt"] = {"proposal": _review_key(fingerprint, threshold, pinned_nodes),
                         "warnings": sorted({warning_key(w) for w in warnings})}
    return report


class WorkerAdmitter:
    """Initial helper starts alongside the existing apps, not in freed space.

    Caller must validate the user's signed approval before pinning this receipt
    in the setup journal. Thereafter that immutable record is its authority.
    """
    def __init__(self, read, namespace, approval, *, clock=time.time):
        validate_worker_approval(approval)
        self.read, self.namespace, self.clock = read, namespace, clock
        self.approval = copy.deepcopy(approval)

    def __call__(self, pod):
        report = review(self.read, self.namespace, "worker", pod, self.approval["nodes"], self.approval["threshold"], clock=self.clock)
        accepted = self.approval["receipt"]
        if (report["receipt"]["proposal"] != accepted["proposal"]
                or not set(report["receipt"]["warnings"]) <= set(accepted["warnings"])):
            raise Held("The coordinator workload or capacity warnings changed; review before starting it")
        return True


class Admitter:
    def __init__(self, read, namespace, pinned_nodes, policy, *, handoff=None, clock=time.time):
        validate_policy(policy)
        self.read, self.namespace, self.clock = read, namespace, clock
        self.nodes, self.policy = copy.deepcopy(pinned_nodes), copy.deepcopy(policy)
        self.handoff = copy.deepcopy(handoff)

    def __call__(self, purpose, proposal):
        if purpose == "stop":
            # Bind both future-stage approvals before any downtime. Setup's
            # projected post-stop review does not release live reservations;
            # copy/switch/start each obtain their own fresh real inventory.
            if not self.handoff:
                raise Held("The data move has no reviewed stop and restart context")
            state, starting = self.handoff, copy.deepcopy(proposal)
            plan = state["plan"]
            from homestead_self_data_fence import pin_app_image
            pin_app_image(starting, plan["copy_image"])
            volumes = [v for v in starting["spec"]["template"]["spec"].get("volumes", []) if v.get("name") == plan["data_volume"]]
            if len(volumes) != 1 or volumes[0].get("persistentVolumeClaim", {}).get("claimName") != state["source"]["name"]:
                raise Held("The source data mount changed before the capacity review")
            volumes[0]["persistentVolumeClaim"]["claimName"] = state["destination"]
            starting["spec"]["replicas"] = state["replicas"]
            copying = COPY.job(self.namespace, "homestead-data-copy-" + state["operation"], state["source"]["name"],
                               state["destination"], plan["copy_image"], state["operation"], plan["copy_node"])
            for stage, proposed in (("copy", copying), ("restart", starting)):
                _, _, fingerprint = _proposal(stage, proposed, self.namespace)
                if _review_key(fingerprint, self.policy["threshold"], self.nodes) != self.policy["reviews"][stage]["proposal"]:
                    raise Held("Copy or restart approval no longer matches; Homestead has not been stopped")
            return True
        stage = "restart" if purpose in ("switch", "start", "recover-start") else purpose
        report = review(self.read, self.namespace, stage, proposal, self.nodes, self.policy["threshold"], clock=self.clock)
        approved = self.policy["reviews"][stage]
        if purpose == "recover-start":
            approved = (self.handoff or {}).get("recovery", {}).get("restart")
            if not approved:
                raise Held("Original-volume restart needs its own reviewed capacity receipt")
        if (report["receipt"]["proposal"] != approved["proposal"]
                or not set(report["receipt"]["warnings"]) <= set(approved["warnings"])):
            raise Held("Capacity or the workload changed; review the new warnings before continuing")
        return True
