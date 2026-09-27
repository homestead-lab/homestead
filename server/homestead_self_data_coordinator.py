"""Resumable self-data phase engine, independent of both data volumes.

Not yet exposed by the legacy endpoint. The production wrapper must supply
reviewed setup, replica write/startup fencing, narrow credentials and admission.
This engine never force-deletes writers, retries unknown writes or rolls back.
"""
import copy
import datetime
import json
import time

import homestead_self_data_copy as COPY
import homestead_self_data_anchor as A
from homestead_storage_journal import Held, Journal, identity, shape


def owner(obj, kind, uid):
    return any(o.get("controller") is True and o.get("kind") == kind and o.get("uid") == uid
               for o in obj.get("metadata", {}).get("ownerReferences", []))


def claims(pod):
    return {v.get("persistentVolumeClaim", {}).get("claimName") for v in pod.get("spec", {}).get("volumes", [])} - {None}


def zero(obj):
    generation = obj.get("metadata", {}).get("generation")
    status = obj.get("status", {})
    observed = status.get("observedGeneration")
    return (type(generation) is int and type(observed) is int and observed >= generation > 0
            and obj.get("spec", {}).get("replicas") == 0
            and all(status.get(k, 0) == 0 for k in ("replicas", "readyReplicas", "availableReplicas", "terminatingReplicas")))


class Coordinator:
    def __init__(self, anchor, read, send, logs, admit, *, worker_uid, clock=time.time):
        self.anchor, self.read, self.send, self.logs, self.admit = anchor, read, send, logs, admit
        self.state, self.ns, self.clock = anchor.state, anchor.namespace, clock
        self.plan = self.state.get("plan", {})
        if self.plan.get("worker", {}).get("uid") != worker_uid or not self.plan.get("copy_image"):
            raise Held("This coordinator does not match the reviewed worker and copy plan")
        if self.state.get("pointer_receipt") != A.pointer_digest(self.ns, self.state, anchor.handle()["uid"]):
            raise Held("The local data handoff receipt is not durably published; Homestead was not stopped")
        if self.state.get("runtime", {}).get("state") == "held":
            raise Held("This data move needs a recovery review before its coordinator can continue")
        self.dep_path = f"/apis/apps/v1/namespaces/{self.ns}/deployments/{self.state['deployment']['name']}"
        self.job_name = "homestead-data-copy-" + self.state["operation"]
        self.job_path = f"/apis/batch/v1/namespaces/{self.ns}/jobs/{self.job_name}"
        self.writer = Journal(anchor.item(), read, send, anchor.checkpoint)

    def _entry(self, step):
        return next((e for e in self.writer.entries if e["step"] == step), None)

    def _list(self, path):
        result = self.read(path)
        if not isinstance(result, dict) or not isinstance(result.get("items"), list) or result.get("metadata", {}).get("continue"):
            raise Held("The data handoff inventory is incomplete; nothing more was changed")
        if any(not isinstance(o, dict) or not o.get("metadata", {}).get("uid") for o in result["items"]):
            raise Held("The data handoff inventory has no reliable identities")
        return result["items"]

    def _fact(self, obj, fact, *, namespace=None):
        if (obj.get("metadata", {}).get("name") != fact["name"] or identity(obj)["uid"] != fact["uid"]
                or obj["metadata"].get("namespace") != namespace or obj["metadata"].get("deletionTimestamp")
                or shape(obj) != fact["shape"]):
            raise Held("A reviewed data handoff resource changed or was replaced")
        return obj

    def _deployment(self):
        entries = [e for e in self.writer.entries if e["target"]["path"] == self.dep_path]
        if entries:
            return self.writer.observe(entries[-1])
        dep = self.read(self.dep_path)
        return self._fact(dep, {**self.state["deployment"], "shape": self.plan["deployment_shape"]}, namespace=self.ns)

    def _bound(self, source):
        name = self.state["source"]["name"] if source else self.state["destination"]
        fact = ({**self.state["source"], "shape": self.plan["source_pvc_shape"]} if source else self.plan["destination_pvc"])
        pvc = self._fact(self.read(f"/api/v1/namespaces/{self.ns}/persistentvolumeclaims/{name}"), fact, namespace=self.ns)
        pv_fact = self.plan["source_pv" if source else "destination_pv"]
        pv = self._fact(self.read("/api/v1/persistentvolumes/" + pv_fact["name"]), pv_fact)
        ref = pv.get("spec", {}).get("claimRef", {})
        if (pvc.get("status", {}).get("phase") != "Bound" or pvc.get("spec", {}).get("volumeName") != pv_fact["name"]
                or pvc["spec"].get("volumeMode", "Filesystem") != "Filesystem"
                or (ref.get("namespace"), ref.get("name"), ref.get("uid")) != (self.ns, name, fact["uid"])):
            raise Held("A data volume is no longer bound to its reviewed claim")
        return pvc

    def _hosts(self):
        for pinned in self.plan["nodes"]:
            node = self.read("/api/v1/nodes/" + pinned["name"])
            if (identity(node)["uid"] != pinned["uid"] or node["metadata"].get("deletionTimestamp")
                    or node.get("status", {}).get("nodeInfo", {}).get("bootID") != pinned["boot_id"]
                    or not any(c.get("type") == "Ready" and c.get("status") == "True" for c in node.get("status", {}).get("conditions", []))):
                raise Held("A data handoff host is unavailable, replaced or rebooted; prove writer fencing before recovery")
            lease = self.read("/apis/coordination.k8s.io/v1/namespaces/kube-node-lease/leases/" + pinned["name"])
            spec = lease.get("spec", {})
            try:
                age = self.clock() - datetime.datetime.fromisoformat(spec["renewTime"].replace("Z", "+00:00")).timestamp()
                duration = spec["leaseDurationSeconds"]
                fresh = type(duration) is int and 0 < duration <= 120 and -5 <= age <= duration
            except (KeyError, ValueError, TypeError):
                fresh = False
            if spec.get("holderIdentity") != pinned["name"] or not fresh:
                raise Held("A host heartbeat is stale; a missing pod does not prove its writer stopped")

    def _environment(self):
        self.writer.check()
        # A second process must load its own handle. Reusing a stale in-memory
        # plan cannot silently become authority after another worker advances.
        current = self.read(self.anchor.path)
        if identity(current) != identity(self.anchor.obj):
            raise Held("The data handoff control record advanced elsewhere; refresh its current state")
        self._hosts()
        dep = self._deployment()
        self._bound(True); self._bound(False)
        worker = self._fact(self.read(f"/api/v1/namespaces/{self.ns}/pods/{self.plan['worker']['name']}"),
                            self.plan["worker"], namespace=self.ns)
        if (worker.get("status", {}).get("phase") != "Running"
                or claims(worker) & {self.state["source"]["name"], self.state["destination"]}
                or worker.get("spec", {}).get("nodeName") not in {n["name"] for n in self.plan["nodes"]}):
            raise Held("The coordinator must remain running independently of both data volumes")
        for hpa in self._list(f"/apis/autoscaling/v2/namespaces/{self.ns}/horizontalpodautoscalers"):
            target = hpa.get("spec", {}).get("scaleTargetRef", {})
            if target.get("kind") == "Deployment" and target.get("name") == self.state["deployment"]["name"]:
                raise Held("An autoscaler could restart Homestead during the copy; remove it before moving data")
        if dep.get("spec", {}).get("paused"):
            raise Held("Homestead's Deployment is paused; controllers cannot complete the data handoff")
        if self.state["phase"] == "prepare":
            known_sets = {identity(r)["uid"] for r in self._sets()}
            for pod in self._pods():
                mounted = claims(pod)
                if (self.state["destination"] in mounted or self.state["source"]["name"] in mounted
                        and not any(owner(pod, "ReplicaSet", uid) for uid in known_sets)):
                    raise Held("A data volume has an unreviewed consumer; Homestead was not stopped")
        return dep

    def _pods(self):
        return self._list(f"/api/v1/namespaces/{self.ns}/pods")

    def _sets(self):
        return [r for r in self._list(f"/apis/apps/v1/namespaces/{self.ns}/replicasets")
                if owner(r, "Deployment", self.state["deployment"]["uid"])]

    def _quiet(self, dep, *, helper_uid=None):
        if dep["spec"].get("replicas") != 0:
            raise Held("Homestead was restarted while its data was held for copying")
        sets = self._sets()
        own = {identity(s)["uid"] for s in sets}
        waiting = not zero(dep) or any(not zero(s) for s in sets)
        for pod in self._pods():
            own_pod = any(owner(pod, "ReplicaSet", uid) for uid in own)
            using = claims(pod) & {self.state["source"]["name"], self.state["destination"]}
            if helper_uid and owner(pod, "Job", helper_uid):
                continue
            if using and not own_pod:
                raise Held("Another pod still has a data volume mounted; it will not be deleted automatically")
            # Even a completed/terminating pod can retain its mounts. Wait for
            # normal removal instead of inferring release from its phase.
            if own_pod or using:
                waiting = True
        return not waiting

    def _admission(self, purpose, proposal):
        if self.admit(purpose, copy.deepcopy(proposal)) is not True:
            raise Held("Current placement or capacity needs review before the data handoff can continue")
        return self._environment()  # admission may take time; recheck afterwards

    def _update_dep(self, step, dep, proposed):
        proposed = copy.deepcopy(proposed)
        proposed["metadata"].update(identity(dep))
        self.writer.write(step, "PUT", self.dep_path, proposed, expected={**identity(dep), "shape": shape(dep)})

    def _result(self, message):
        return {"phase": self.anchor.state["phase"], "message": message,
                "progress": {"prepare": 5, "quiesce": 10, "copy": 30, "verify": 70, "switch": 80, "start": 90, "done": 100}[self.anchor.state["phase"]]}

    def _copy_manifest(self):
        return COPY.job(self.ns, self.job_name, self.state["source"]["name"], self.state["destination"],
                        self.plan["copy_image"], self.state["operation"], self.plan["copy_node"])

    def _copy_receipt(self, job, pods):
        if any(c.get("type") == "Failed" and c.get("status") == "True" for c in job.get("status", {}).get("conditions", [])):
            raise Held("The data copy Job failed; both volumes are retained")
        if len(pods) > 1:
            raise Held("The data copy Job has multiple pods; inspect the retained copy before continuing")
        complete = any(c.get("type") == "Complete" and c.get("status") == "True" for c in job.get("status", {}).get("conditions", []))
        if not complete:
            return None
        if len(pods) != 1 or pods[0].get("status", {}).get("phase") != "Succeeded":
            raise Held("The completed data copy has no unique successful pod")
        pod = pods[0]
        spec = pod.get("spec", {})
        wanted = self._copy_manifest()["spec"]["template"]["spec"]
        if (spec.get("nodeName") != self.plan["copy_node"] or spec.get("initContainers")
                or len(spec.get("containers", [])) != 1 or spec.get("volumes") != wanted["volumes"]
                or any(spec["containers"][0].get(k) != wanted["containers"][0].get(k)
                       for k in ("name", "image", "command", "volumeMounts", "env", "envFrom"))):
            raise Held("The copy pod differs from its reviewed image, mounts or command")
        statuses = pod.get("status", {}).get("containerStatuses", [])
        if (len(statuses) != 1 or statuses[0].get("name") != "copy"
                or not statuses[0].get("containerID") or statuses[0].get("restartCount") != 0
                or statuses[0].get("state", {}).get("terminated", {}).get("exitCode") != 0):
            raise Held("The copy container has no verified successful exit")
        path = f"/api/v1/namespaces/{self.ns}/pods/{pod['metadata']['name']}"
        before = identity(pod)["uid"]
        output = self.logs(path + "/log?container=copy&tailLines=20")
        after = self.read(path)
        if (identity(after)["uid"] != before or not owner(after, "Job", identity(job)["uid"])
                or shape(after) != shape(pod) or after.get("status", {}).get("phase") != "Succeeded"
                or after.get("status", {}).get("containerStatuses") != statuses):
            raise Held("The data copy log's pod was replaced during verification")
        prefix = "HOMESTEAD_SELF_DATA_COPY " + self.state["operation"] + " "
        lines = [line[len(prefix):] for line in str(output).splitlines() if line.startswith(prefix)]
        if len(lines) != 1:
            raise Held("The copy completed without one verified data-manifest receipt")
        try:
            return json.loads(lines[0])
        except (TypeError, ValueError):
            raise Held("The data copy completion receipt is invalid") from None

    def step(self):
        """One bounded phase; create a fresh instance from the anchor next poll."""
        dep = self._environment()
        phase = self.state["phase"]
        if phase == "prepare":
            if not self._entry("stop"):
                if dep["spec"].get("replicas", 1) != self.state["replicas"]:
                    raise Held("Homestead replica count changed after review")
                dep = self._admission("stop", dep)
                proposed = copy.deepcopy(dep); proposed["spec"]["replicas"] = 0
                self._update_dep("stop", dep, proposed)
            self.anchor.advance("quiesce")
            return self._result("Waiting for Homestead and its volume mounts to stop")
        if phase == "quiesce":
            if self._quiet(dep):
                self.anchor.advance("copy")
            return self._result("Homestead is stopped; preparing the verified data copy" if self.anchor.state["phase"] == "copy" else "Waiting for all Homestead pods to release the source")
        if phase == "copy":
            known = self._entry("copy-job")
            job_uid = known.get("after", {}).get("uid") if known else None
            if not self._quiet(dep, helper_uid=job_uid):
                return self._result("Waiting for source writers to stop")
            receipt = self.state.get("copy_receipt")
            if receipt and receipt["state"] == "verified":
                self.anchor.advance("verify")
                return self._result("Copy verified; releasing the copy pod's volume mounts")
            if receipt and receipt["state"] == "uncertain":
                raise Held("The data copy outcome is uncertain; it will not be repeated")
            if receipt is None:
                self.anchor.copy_started()
            if not known:
                body = self._copy_manifest()
                dep = self._admission("copy", body)
                if not self._quiet(dep):
                    return self._result("Waiting for source writers to stop")
                self.writer.write("copy-job", "POST", self.job_path.rsplit("/", 1)[0], body)
                return self._result("Copy Job started; waiting for verified data and metadata")
            job = self.writer.observe(known)
            copy_pods = [p for p in self._pods() if owner(p, "Job", job_uid)]
            receipt = self._copy_receipt(job, copy_pods)
            if receipt is None:
                return self._result("Copy is running; verified progress is not available yet")
            dep = self._environment()
            if not self._quiet(dep, helper_uid=job_uid):
                raise Held("A source writer returned during copy verification")
            self.anchor.copy_finished(receipt)
            self.anchor.advance("verify")
            return self._result("Copy verified; releasing the copy pod's volume mounts")
        if phase == "verify":
            if self.state.get("copy_receipt", {}).get("state") != "verified":
                raise Held("The data copy has no verified completion receipt")
            deletion = self._entry("release-copy")
            known = self._entry("copy-job")
            if not known:
                raise Held("The verified copy Job has no creation receipt")
            if not self._quiet(dep, helper_uid=known["after"]["uid"]):
                return self._result("Waiting for Homestead to remain stopped")
            if not deletion:
                job = self.writer.observe(known)
                self.writer.write("release-copy", "DELETE", self.job_path,
                                  {"propagationPolicy": "Foreground"}, expected=identity(job))
                return self._result("Waiting for the copy pod to release both volumes")
            if self.writer.observe(deletion) is not None or any(owner(p, "Job", known["after"]["uid"]) for p in self._pods()):
                return self._result("Waiting for the copy pod to release both volumes")
            if self._quiet(dep):
                self.anchor.advance("switch")
            return self._result("Copy pod released; ready to select the new data volume")
        if phase == "switch":
            if not self._quiet(dep):
                return self._result("Waiting for volume mounts to be released before switching")
            if not self._entry("switch"):
                proposed = copy.deepcopy(dep)
                volumes = [v for v in proposed["spec"]["template"]["spec"].get("volumes", []) if v.get("name") == self.plan["data_volume"]]
                if len(volumes) != 1 or volumes[0].get("persistentVolumeClaim", {}).get("claimName") != self.state["source"]["name"]:
                    raise Held("Homestead's data mount no longer names the reviewed source")
                volumes[0]["persistentVolumeClaim"]["claimName"] = self.state["destination"]
                proposed["spec"]["strategy"] = {"type": "Recreate"}
                # Place the reviewed restart, not the current zero replicas.
                starting = copy.deepcopy(proposed); starting["spec"]["replicas"] = self.state["replicas"]
                dep = self._admission("switch", starting)
                if not self._quiet(dep):
                    raise Held("A writer returned during cutover admission")
                self._update_dep("switch", dep, proposed)
            self.anchor.advance("start")
            return self._result("New data volume selected; checking restart placement")
        if phase == "start":
            if not self._entry("start"):
                if not self._quiet(dep):
                    return self._result("Waiting for volume mounts to be released before starting")
                proposed = copy.deepcopy(dep); proposed["spec"]["replicas"] = self.state["replicas"]
                dep = self._admission("start", proposed)
                if not self._quiet(dep):
                    raise Held("A writer returned during restart admission")
                self._update_dep("start", dep, proposed)
                return self._result("Homestead is starting on the new data volume")
            if not self._ready(dep):
                return self._result("Waiting for every Homestead replica to become ready on the new volume")
            self.anchor.advance("done")
            return self._result("Homestead is ready on the new data volume; the original is retained")
        return self._result("Homestead's data move is complete; the original volume is retained")

    def _ready(self, dep):
        status = dep.get("status", {})
        generation = dep.get("metadata", {}).get("generation")
        if (type(generation) is not int or status.get("observedGeneration", 0) < generation
                or any(status.get(k) != self.state["replicas"] for k in ("replicas", "updatedReplicas", "readyReplicas", "availableReplicas"))
                or status.get("terminatingReplicas", 0)):
            return False
        sets = self._sets()
        own = {identity(s)["uid"] for s in sets}
        pods = self._pods()
        members = [p for p in pods if any(owner(p, "ReplicaSet", uid) for uid in own)]
        if len(members) != self.state["replicas"]:
            return False
        for pod in pods:
            mounted = claims(pod)
            if self.state["source"]["name"] in mounted or self.state["destination"] in mounted and pod not in members:
                raise Held("A pod outside the restarted Homestead still uses one of the retained data volumes")
        return all(self.state["destination"] in claims(p) and not p["metadata"].get("deletionTimestamp")
                   and p.get("status", {}).get("phase") == "Running"
                   and any(c.get("type") == "Ready" and c.get("status") == "True" for c in p.get("status", {}).get("conditions", []))
                   for p in members)
