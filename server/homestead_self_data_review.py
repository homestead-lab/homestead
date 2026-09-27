"""Source-side review and signed confirmation for a prepared data destination.

No writes or permission changes. Destination provisioning precedes this final
review; runtime admission still uses real inventories after writer shutdown.
The returned private execution facts belong on the server, never in a browser
request. Source mount/protocol proof and writer drain remain separate gates.
"""
import copy
import hashlib
import re
import time

import homestead_capacity_review as SIGN
import homestead_rollout_capacity as ROLLOUT
import homestead_self_data_admission as D
import homestead_self_data_copy as COPY
import homestead_self_data_kube as K
import homestead_self_data_launch as L
from homestead_self_data_fence import require_app_readiness, pin_app_image
from homestead_storage_journal import Held, digest, identity, shape


FIELDS = {"operation", "destination", "worker_node", "copy_node"}
CONTROLS = {"capacity_token", "confirm_move", "confirm_capacity"}
CONDITIONAL = "After Homestead stops and releases its mounts. Current RAM usage is not subtracted; placement is checked again before each start."


def _config(body):
    import homestead_self_data_anchor as A
    if not isinstance(body, dict) or not FIELDS <= set(body) <= FIELDS | CONTROLS:
        raise Held("Choose the prepared destination and helper hosts before reviewing the move")
    cfg = {key: body[key] for key in FIELDS}
    if not isinstance(cfg["operation"], str) or not re.fullmatch(r"[a-f0-9]{24}", cfg["operation"]):
        raise Held("The move review identity is invalid")
    for key in FIELDS - {"operation"}: A._name(cfg[key])
    return cfg


def _fact(obj):
    return {"name": obj["metadata"]["name"], "uid": identity(obj)["uid"], "shape": shape(obj)}


def recheck_binding(approved, current):
    """Fresh checks may remove a warning, never add one or change the work."""
    changed = sorted(k for k in set(approved) | set(current) if k != "approvals" and approved.get(k) != current.get(k))
    old, new = approved.get("approvals", {}), current.get("approvals", {})
    def receipt(a, b):
        return a.get("proposal") == b.get("proposal") and set(b.get("warnings", [])) <= set(a.get("warnings", []))
    try:
        worker_ok = (old["worker"]["threshold"] == new["worker"]["threshold"] and old["worker"]["nodes"] == new["worker"]["nodes"]
                     and receipt(old["worker"]["receipt"], new["worker"]["receipt"]))
        policy_ok = (old["policy"]["threshold"] == new["policy"]["threshold"] and all(
            receipt(old["policy"]["reviews"][s], new["policy"]["reviews"][s]) for s in ("copy", "restart")))
        if not worker_ok or not policy_ok: changed.append("capacity")
    except (KeyError, TypeError):
        changed.append("capacity")
    if changed:
        # Field names only: no configuration, secret, or API response values.
        raise Held("Move review changed during preparation (" + ", ".join(changed) + "). Homestead has not been stopped. Keep the original volume, then review a new move.")
    return True


class Review:
    def __init__(self, read, namespace, deployment, *, actor, image, threshold, source_pod=None, data_dir="/data", runtime_check=None, clock=time.time, route_check=None):
        import homestead_self_data_anchor as A
        A._name(namespace); A._name(deployment)
        if not isinstance(actor, str) or not actor or len(actor) > 256:
            raise Held("A signed-in administrator is required to review this move")
        self.read, self.namespace, self.deployment = read, namespace, deployment
        self.actor, self.image, self.threshold, self.clock = actor, image, threshold, clock
        self.source_pod, self.data_dir = copy.deepcopy(source_pod), data_dir
        self.runtime_check = runtime_check
        self.route_check = route_check

    def _snapshot(self, body):
        started = self.clock()
        cfg, ns = _config(body), self.namespace
        cache = {}
        def read(path):
            if path not in cache: cache[path] = copy.deepcopy(self.read(path))
            return copy.deepcopy(cache[path])
        namespace = read(f"/api/v1/namespaces/{ns}")
        cluster = read("/api/v1/namespaces/kube-system")
        if namespace["metadata"]["name"] != ns or cluster["metadata"]["name"] != "kube-system":
            raise Held("The cluster namespace identity could not be verified")
        dep = read(f"/apis/apps/v1/namespaces/{ns}/deployments/{self.deployment}")
        if (dep.get("kind") != "Deployment" or dep["metadata"].get("namespace") != ns
                or dep["metadata"].get("name") != self.deployment or dep["metadata"].get("deletionTimestamp")
                or dep["spec"].get("paused")):
            raise Held("Homestead's Deployment cannot be reviewed while missing, deleting or paused")
        volumes = [v for v in dep["spec"]["template"]["spec"].get("volumes", []) if v.get("name") == "data"]
        require_app_readiness(dep["spec"]["template"]["spec"], self.deployment)
        if len(volumes) != 1 or set(volumes[0]) != {"name", "persistentVolumeClaim"} or volumes[0]["persistentVolumeClaim"].get("readOnly"):
            raise Held("Homestead needs one writable data claim before it can be moved")
        source = volumes[0]["persistentVolumeClaim"]["claimName"]
        if source == cfg["destination"]:
            raise Held("Choose a different data volume")
        claims, pvs = [], []
        dependencies = sorted({v.get("persistentVolumeClaim", {}).get("claimName")
            for v in dep["spec"]["template"]["spec"].get("volumes", [])} - {None, source, cfg["destination"]})
        for name in (source, cfg["destination"], *dependencies):
            pvc = read(f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{name}")
            if (pvc["metadata"].get("name") != name or pvc["metadata"].get("namespace") != ns
                    or pvc["metadata"].get("deletionTimestamp") or pvc.get("status", {}).get("phase") != "Bound"
                    or pvc["spec"].get("volumeMode", "Filesystem") != "Filesystem"):
                raise Held("Prepare and bind both filesystem volumes before the final move review")
            pv = read("/api/v1/persistentvolumes/" + pvc["spec"]["volumeName"])
            ref = pv["spec"].get("claimRef", {})
            if (pv["metadata"].get("deletionTimestamp") or pv["metadata"].get("name") != pvc["spec"]["volumeName"]
                    or (ref.get("namespace"), ref.get("name"), ref.get("uid")) != (ns, name, identity(pvc)["uid"])):
                raise Held("A data volume no longer has its expected claim binding")
            claims.append(pvc); pvs.append(pv)
        if identity(pvs[0])["uid"] == identity(pvs[1])["uid"]:
            raise Held("Source and destination must be separate volumes")
        nodes = D._inventory(read, "/api/v1/nodes")
        pins = [{"name": n["metadata"]["name"], "uid": identity(n)["uid"], "boot_id": n.get("status", {}).get("nodeInfo", {}).get("bootID")} for n in nodes]
        scope = K.Scope(ns, self.deployment, cfg["operation"], [p["metadata"]["name"] for p in claims],
                        [p["metadata"]["name"] for p in pvs], [n["name"] for n in pins])
        if cfg["copy_node"] not in scope.nodes:
            raise Held("Choose a current cluster host for the copy")
        pods = D._inventory(read, "/api/v1/pods")
        # Do not infer ownership from app labels or a Deployment's name.
        sets = D._inventory(read, f"/apis/apps/v1/namespaces/{ns}/replicasets")
        owned, known = ROLLOUT.owned_pods(dep, pods, read, ns)
        replicas = dep["spec"].get("replicas", 1)
        if not known or type(replicas) is not int or not 1 <= replicas <= 64 or not ROLLOUT.stable(dep, owned, replicas):
            raise Held("Wait for every Homestead replica to be ready and its current rollout to finish")
        owned_uids = {identity(p)["uid"] for p in owned}
        if len(owned_uids) != len(owned):
            raise Held("Homestead pod ownership is ambiguous")
        if self.source_pod is not None:
            current = next((p for p in owned if identity(p)["uid"] == identity(self.source_pod)["uid"]), None)
            if current is None or _fact(current) != _fact(self.source_pod):
                raise Held("This Homestead pod is not one of the reviewed Deployment's replicas")
            containers = [c for c in current["spec"].get("containers", []) if c.get("name") == self.deployment]
            mounts = [m for c in containers for m in c.get("volumeMounts", []) if m.get("mountPath", "").rstrip("/") == self.data_dir.rstrip("/")]
            if (len(containers) != 1 or len(mounts) != 1 or mounts[0].get("name") != "data"
                    or any(mounts[0].get(k) for k in ("subPath", "subPathExpr", "readOnly"))):
                raise Held("The current Homestead process must use the whole writable data volume")
            data = [v for v in current["spec"].get("volumes", []) if v.get("name") == "data"]
            if len(data) != 1 or data[0].get("persistentVolumeClaim", {}).get("claimName") != source or data[0]["persistentVolumeClaim"].get("readOnly"):
                raise Held("The current Homestead pod does not mount the reviewed source claim")
        for pod in pods:
            if pod.get("metadata", {}).get("namespace") != ns or pod.get("status", {}).get("phase") in ("Succeeded", "Failed"):
                continue
            used = {v.get("persistentVolumeClaim", {}).get("claimName") for v in pod.get("spec", {}).get("volumes", [])}
            if cfg["destination"] in used or source in used and identity(pod)["uid"] not in owned_uids:
                raise Held("Another pod uses a data volume; stop or move that consumer before reviewing")
        autoscalers = D._inventory(read, f"/apis/autoscaling/v2/namespaces/{ns}/horizontalpodautoscalers")
        if any(h.get("spec", {}).get("scaleTargetRef", {}).get("kind") == "Deployment"
               and h["spec"]["scaleTargetRef"].get("name") == self.deployment for h in autoscalers):
            raise Held("Disable Homestead's autoscaler before moving its data")
        binding = {"action": "self-data-handoff", "actor": self.actor, "cluster_uid": identity(cluster)["uid"],
                   "namespace_uid": identity(namespace)["uid"], "deployment": _fact(dep),
                   "claims": [_fact(p) for p in claims], "volumes": [_fact(p) for p in pvs],
                   "pods": sorted((_fact(p) for p in owned), key=lambda p: p["uid"]),
                   "controllers": sorted((_fact(s) for s in sets if ROLLOUT.controller(s).get("uid") == identity(dep)["uid"]), key=lambda s: s["uid"]),
                   "nodes": sorted(pins, key=lambda n: n["name"]), "image": self.image, "threshold": self.threshold}
        if self.runtime_check is not None:
            binding["source_runtime"] = self.runtime_check(copy.deepcopy(owned), source)
        # Derive only on the server; browser gets neither this capability nor
        # the worker's raw configuration in a review response.
        secret = SIGN.derive_secret({**cfg, "cluster_uid": binding["cluster_uid"], "namespace": ns, "deployment": self.deployment}, "self-data-progress")
        status_digest = hashlib.sha256(secret.encode()).hexdigest()
        route, services = self.route_check(read, dep) if self.route_check else (None, [])
        binding["services"] = services
        worker = L.resources(scope, anchor_uid="pending", image=self.image, node=cfg["worker_node"], status_digest=status_digest, route=route)[-2]
        reports = {"worker": D.review(read, ns, "worker", worker, pins, self.threshold, clock=self.clock)}
        # This is conditional planning only. Real phase admission never sees
        # this inventory and never subtracts live/terminating writers.
        synthetic = copy.deepcopy(worker)
        synthetic["metadata"]["uid"] = "review-worker-" + cfg["operation"]
        synthetic["spec"]["nodeName"] = cfg["worker_node"]
        synthetic["status"] = {"phase": "Running"}
        after = [copy.deepcopy(p) for p in pods if identity(p)["uid"] not in owned_uids] + [synthetic]
        def conditional(path):
            return {"items": copy.deepcopy(after)} if path == "/api/v1/pods" else read(path)
        copying = COPY.job(ns, scope.copy_name, source, cfg["destination"], self.image, cfg["operation"], cfg["copy_node"])
        restart = copy.deepcopy(dep)
        pin_app_image(restart, self.image)
        next(v for v in restart["spec"]["template"]["spec"]["volumes"] if v["name"] == "data")["persistentVolumeClaim"]["claimName"] = cfg["destination"]
        restart["spec"]["strategy"] = {"type": "Recreate"}
        for stage, proposed in (("copy", copying), ("restart", restart)):
            reports[stage] = D.review(conditional, ns, stage, proposed, pins, self.threshold, clock=self.clock)
        approval = {"threshold": self.threshold, "nodes": pins, "receipt": reports["worker"]["receipt"]}
        D.validate_worker_approval(approval)
        policy = {"threshold": self.threshold, "reviews": {s: reports[s]["receipt"] for s in ("copy", "restart")}}
        D.validate_policy(policy)
        binding["approvals"] = {"worker": approval, "policy": policy}
        if not 0 <= self.clock() - started <= 30:
            raise Held("The data move review took too long; check again")
        public = {"source": source, "destination": cfg["destination"], "replicas": replicas, "requires_confirmation": True,
                  "downtime": "Homestead will be unavailable while data is copied and checked. Both volumes are retained.",
                  "stages": [{"id": s, "label": label, "conditional": s != "worker", "detail": CONDITIONAL if s != "worker" else "Starts alongside the running Homestead pods.",
                              "capacity": {k: v for k, v in reports[s].items() if k != "receipt"}}
                             for s, label in (("worker", "Start move coordinator"), ("copy", "Copy and verify"), ("restart", "Restart Homestead"))]}
        execution = {"scope": scope, "deployment": dep, "source": claims[0], "destination": claims[1], "source_pv": pvs[0],
                     "destination_pv": pvs[1], "nodes": pins, "approval": approval, "policy": policy,
                     "status_digest": status_digest, "status_token": secret, "config": cfg,
                     "binding": binding, "image": self.image, "route": route}
        return cfg, public, binding, execution

    def preview(self, body):
        cfg, public, binding, _ = self._snapshot(body)
        return {**public, "capacity_token": SIGN.issue(cfg, binding)}

    def approve(self, body):
        cfg, public, binding, execution = self._snapshot(body)
        if (body.get("confirm_move") is not True or body.get("confirm_capacity") is not True
                or not SIGN.valid({**cfg, "capacity_token": body.get("capacity_token")}, binding)):
            raise Held("Review the current move and confirm its downtime and capacity warnings before continuing")
        return execution
