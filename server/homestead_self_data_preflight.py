"""Non-persisting API validation before helper creation or application downtime.

https://kubernetes.io/docs/reference/using-api/api-concepts/#dry-run
Dry-run identities are not resource receipts and are never adopted. This checks
the source actor's current admission path, not the Job controller's identity or
future owner-dependent webhooks. It does not reserve quota/capacity, pull images,
schedule pods or attach storage. Actual creations must still be checked.
"""
import copy
import time
import urllib.error

import homestead_self_data_bootstrap as B
import homestead_self_data_kube as K
from homestead_storage_journal import Held, digest


LIMITATION = "Admission is a current source-account check, not a reservation or proof of future controller admission, image pulls, scheduling or storage attachment."


def copy_job(namespace, state):
    from homestead_self_data_copy import job
    plan = state["plan"]
    return job(namespace, "homestead-data-copy-" + state["operation"], state["source"]["name"], state["destination"],
               plan["copy_image"], state["operation"], plan["copy_node"])


def copy_pod(job):
    template = job["spec"]["template"]
    return {"apiVersion": "v1", "kind": "Pod", "metadata": {**copy.deepcopy(template.get("metadata", {})),
        "name": "homestead-copy-check-" + job["metadata"]["labels"]["homestead.io/self-data-copy"],
        "namespace": job["metadata"]["namespace"]}, "spec": copy.deepcopy(template["spec"])}


def validate_receipt(receipt, job, *, now=None):
    from homestead_self_data_anchor import _keys, _hash
    _keys(receipt, ("checked_at", "request", "pod_request", "admitted", "limitation"))
    for key in ("request", "pod_request", "admitted"):
        _hash(receipt[key])
    if (type(receipt["checked_at"]) is not int or receipt["checked_at"] < 0
            or receipt["request"] != digest(job) or receipt["pod_request"] != digest(copy_pod(job))
            or receipt["limitation"] != LIMITATION):
        raise Held("The copy admission receipt does not match this data move")
    if now is not None and not 0 <= now - receipt["checked_at"] <= 60:
        raise Held("Copy admission preflight expired before Homestead stopped; obtain a fresh review")


class Preflight:
    def __init__(self, scope, send, *, clock=time.time):
        self.scope, self.send, self.clock = K.PreviewScope(scope), send, clock

    def _request(self, kind, body):
        namespace = self.scope.namespace
        path = (f"/api/v1/namespaces/{namespace}/pods" if kind == "Pod" else
                f"/apis/batch/v1/namespaces/{namespace}/jobs") + "?dryRun=All&fieldValidation=Strict"
        self.scope.check("POST", path, body)
        try:
            return self.send("POST", path, copy.deepcopy(body))
        except urllib.error.HTTPError as error:
            raise Held(f"{kind} admission preflight was rejected (HTTP {error.code}). Nothing was created; review cluster policy and permissions before continuing") from None
        except Exception:
            raise Held(f"{kind} admission preflight is unavailable. No live request or retry was attempted") from None

    def _pod(self, body, result):
        target = {"apiVersion": "v1", "kind": "Pod", "namespace": self.scope.namespace, "name": body["metadata"]["name"]}
        try:
            B.admitted(body, result, target, dry_run=True)
        except Exception:
            raise Held("Pod admission changed a reviewed setting or returned an unverifiable result. Nothing was created") from None
        # No UID/resourceVersion/timestamp from the simulated API object.
        return digest({"spec": result["spec"], "labels": result["metadata"].get("labels", {}),
                       "annotations": result["metadata"].get("annotations", {})})

    def worker(self, pod):
        if pod.get("metadata", {}).get("name") != self.scope.worker_name:
            raise Held("The preflight does not describe this operation's coordinator")
        started = self.clock()
        result = self._request("Pod", pod)
        admitted = self._pod(pod, result)
        return self._receipt(started, {"request": digest(pod), "admitted": admitted})

    def copy(self, job):
        started = self.clock()
        result = self._request("Job", job)
        try:
            meta = result["metadata"]
            if (result.get("kind") != "Job" or result.get("apiVersion") != "batch/v1"
                    or meta.get("name") != self.scope.copy_name or meta.get("namespace") != self.scope.namespace
                    or meta.get("ownerReferences") or meta.get("finalizers") or meta.get("deletionTimestamp")
                    or meta.get("annotations", {}) != job["metadata"].get("annotations", {})
                    or meta.get("labels", {}) != job["metadata"].get("labels", {}) or not B._subset(job, result)):
                raise Held("changed job")
            spec, wanted = result["spec"], job["spec"]
            if (set(spec) - set(wanted) - {"selector", "manualSelector", "completionMode", "suspend"}
                    or spec.get("manualSelector", False) is not False or spec.get("suspend", False) is not False
                    or spec.get("completionMode", "NonIndexed") != "NonIndexed"):
                raise Held("changed job policy")
            template = copy.deepcopy(spec["template"])
            # Only Kubernetes' own Job selector/name labels may be generated.
            generated = {"controller-uid": meta.get("uid"), "batch.kubernetes.io/controller-uid": meta.get("uid"),
                         "job-name": meta["name"], "batch.kubernetes.io/job-name": meta["name"]}
            labels = template.get("metadata", {}).get("labels", {})
            original = wanted["template"].get("metadata", {}).get("labels", {})
            for key in set(labels) - set(original):
                if key not in generated or not generated[key] or labels[key] != generated[key]:
                    raise Held("unreviewed job label")
                labels.pop(key)
            selector = spec.get("selector", {})
            if (set(selector) - {"matchLabels"} or any(not generated.get(key) or value != generated[key]
                    for key, value in selector.get("matchLabels", {}).items())):
                raise Held("unreviewed job selector")
            if template.get("metadata", {}).get("annotations", {}) != wanted["template"].get("metadata", {}).get("annotations", {}):
                raise Held("unreviewed job annotations")
            expected = copy_pod(job)
            admitted_job = copy.deepcopy(job)
            admitted_job["spec"]["template"] = template
            admitted = copy_pod(admitted_job)
            # Validate Job-level template mutation before asking Pod admission.
            self._pod(expected, admitted)
        except Exception:
            raise Held("Job admission changed the copy plan or does not support its required replacement policy. Nothing was created") from None
        # A Job dry-run alone never validates the Pod's admission chain.
        response = self._request("Pod", expected)
        pod_hash = self._pod(expected, response)
        return self._receipt(started, {"request": digest(job), "pod_request": digest(expected), "admitted": pod_hash})

    def _receipt(self, started, facts):
        now = self.clock()
        if not 0 <= now - started <= 60:
            raise Held("Admission preflight took too long; obtain a fresh check before continuing")
        return {"checked_at": int(now), **facts, "limitation": LIMITATION}
