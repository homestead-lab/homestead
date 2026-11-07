"""Isolated coordinator Job/Service manifests and its executable entry point.

Builders do not apply resources. Reviewed setup must journal creation receipts,
observe the admitted Job UID/spec, pin them in the anchor, and only then publish
the local pointer. The Job replaces a lost coordinator a limited number of times
(stuck-jobs C4): each try is a new Pod that continues from the durable record, in
which the Job, not the Pod, is the reviewed worker. Both data claims stay unmounted.
The private progress Service verifies setup. The optional maintenance listener
joins the existing app Service during the move; no new public address is created.

Identity/projection contracts:
https://kubernetes.io/docs/concepts/workloads/pods/downward-api/
https://kubernetes.io/docs/concepts/storage/projected-volumes/
"""
import copy
import json
import os
import re
import signal
import sys
import threading

import homestead_self_data_anchor as A
import homestead_self_data_kube as K
import homestead_self_data_worker as W
from homestead_storage_journal import Held


CONFIG_ENV = "HOMESTEAD_HANDOFF_CONFIG"
UID_ENV = "HOMESTEAD_HANDOFF_POD_UID"
JOB_UID_ENV = "HOMESTEAD_HANDOFF_JOB_UID"
PORT = 8081
MAX_CONFIG = 32768
# Tries after the first. After them the Job has failed and nothing looks after
# the move (docs/data-move-recovery.md says what then).
BACKOFF_LIMIT = 3


def configuration(scope, anchor_uid, status_digest, route=None):
    value = {"protocol": 1, "namespace": scope.namespace, "deployment": scope.deployment,
             "operation": scope.operation, "anchor_uid": anchor_uid, "status_digest": status_digest,
             "claims": list(scope.claims), "volumes": list(scope.volumes), "nodes": list(scope.nodes)}
    if route is not None: value["route"] = route
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    parse_configuration(raw)
    return raw


def parse_configuration(raw):
    if not isinstance(raw, str) or not 0 < len(raw.encode()) <= MAX_CONFIG:
        raise Held("The coordinator configuration is missing or too large")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise Held("The coordinator configuration has duplicate fields")
            result[key] = value
        return result
    try:
        value = json.loads(raw, object_pairs_hook=unique)
        if (not isinstance(value, dict) or set(value) - {"route"} != {"protocol", "namespace", "deployment", "operation",
                "anchor_uid", "status_digest", "claims", "volumes", "nodes"}
                or type(value["protocol"]) is not int or value["protocol"] != 1):
            raise ValueError("unsupported config")
        _uid(value["anchor_uid"])
        A._hash(value["status_digest"])
        if "route" in value:
            from homestead_self_data_route import validate
            validate(value["route"])
        scope = K.Scope(value["namespace"], value["deployment"], value["operation"],
                        value["claims"], value["volumes"], value["nodes"])
    except (KeyError, TypeError, ValueError, RecursionError):
        raise Held("The coordinator configuration cannot be verified") from None
    return value, scope


def _uid(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9-]{1,128}", value):
        raise Held("The coordinator resource identity is invalid")


def pod_of(job, name=None):
    """The Pod a Job's template describes, for checks that take a Pod."""
    template = job["spec"]["template"]
    return {"apiVersion": "v1", "kind": "Pod", "metadata": {**copy.deepcopy(template.get("metadata", {})),
            "name": name or job["metadata"]["name"], "namespace": job["metadata"]["namespace"]},
            "spec": copy.deepcopy(template["spec"])}


def resources(scope, *, anchor_uid, image, node, status_digest, route=None):
    """Return scoped access, one independent Job, and a private progress Service.

    No UID can be supplied by setup: Kubernetes supplies the Pod's and the Job's
    through the downward API. Never use this manifest's pre-admission shape as
    the worker receipt; defaulting/webhooks can change the stored Job spec.

    restartPolicy Never on purpose: each try is a new Pod, so it can go to another
    reviewed host and leaves the last one's log. OnFailure would restart in the
    same network namespace, where the status port (allow_reuse_address off, its
    probe connections in TIME_WAIT) refuses to bind and the tries run out in a
    minute. Replaced as soon as the old Pod is ending (TerminatingOrFailed): a dead
    host's Pod may never end. The worker's first tick fences it. No time limit: the
    coordinator outlives the copy, and cleanup removes it.
    """
    raw = configuration(scope, anchor_uid, status_digest, route)
    if not isinstance(image, str) or not re.fullmatch(r"[a-zA-Z0-9./:_-]+@sha256:[a-f0-9]{64}", image):
        raise Held("The coordinator image must be pinned to a published digest")
    if node not in scope.nodes:
        raise Held("The coordinator must use a reviewed host")
    access = K.access_resources(scope)
    account = access[0]["metadata"]["name"]
    labels = {"app.kubernetes.io/managed-by": "homestead", A.LABEL: scope.operation,
              "homestead.io/handoff-worker": account}
    def metadata():
        return {"name": account, "namespace": scope.namespace, "labels": dict(labels)}
    template = {"metadata": {"labels": dict(labels)}, "spec": {
        "serviceAccountName": account, "automountServiceAccountToken": False,
        "restartPolicy": "Never", "terminationGracePeriodSeconds": 60,
        "enableServiceLinks": False,
        # Any reviewed host, so a retry isn't Pending for ever on a dead one. One term
        # per host: the API allows a field selector only one value.
        "affinity": {"nodeAffinity": {
            "requiredDuringSchedulingIgnoredDuringExecution": {
                "nodeSelectorTerms": [{"matchFields": [{"key": "metadata.name", "operator": "In", "values": [host]}]} for host in scope.nodes]},
            "preferredDuringSchedulingIgnoredDuringExecution": [{"weight": 100, "preference": {
                "matchFields": [{"key": "metadata.name", "operator": "In", "values": [node]}]}}]}},
        "securityContext": {"runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001,
                            "fsGroup": 10001, "seccompProfile": {"type": "RuntimeDefault"}},
        "volumes": [K.token_volume()],
        "containers": [{"name": "coordinator", "image": image, "imagePullPolicy": "IfNotPresent",
            "command": ["python3", "/srv/homestead_self_data_launch.py"],
            "env": [{"name": CONFIG_ENV, "value": raw},
                    {"name": UID_ENV, "valueFrom": {"fieldRef": {"apiVersion": "v1", "fieldPath": "metadata.uid"}}},
                    {"name": JOB_UID_ENV, "valueFrom": {"fieldRef": {"apiVersion": "v1",
                        "fieldPath": "metadata.labels['batch.kubernetes.io/controller-uid']"}}}],
            "ports": [{"name": "progress", "containerPort": PORT, "protocol": "TCP"}],
            "resources": {"requests": {"cpu": "50m", "memory": "64Mi"}, "limits": {"cpu": "500m", "memory": "256Mi"}},
            "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]}},
            "volumeMounts": [{"name": "handoff-api", "mountPath": "/var/run/homestead-handoff", "readOnly": True}],
            # Readiness means only that progress can be queried. No liveness
            # restart: a slow tick must not use up the Job's tries.
            "readinessProbe": {"httpGet": {"path": "/healthz", "port": "progress"},
                               "periodSeconds": 5, "timeoutSeconds": 2, "failureThreshold": 3}}]}}
    job = {"apiVersion": "batch/v1", "kind": "Job", "metadata": metadata(), "spec": {
        "backoffLimit": BACKOFF_LIMIT, "parallelism": 1, "completions": 1,
        "podReplacementPolicy": "TerminatingOrFailed", "template": template}}
    service = {"apiVersion": "v1", "kind": "Service", "metadata": metadata(), "spec": {
        "type": "ClusterIP", "selector": {"homestead.io/handoff-worker": account},
        "ports": [{"name": "progress", "port": PORT, "targetPort": "progress", "protocol": "TCP"}]}}
    if route is not None:
        from homestead_self_data_route import apply
        apply(template, service, route)
    return [*access, job, service]


def serve(runner, token_digest, stop, *, address=("0.0.0.0", PORT), route=None):
    """Bind progress before any mutation and keep it available during holds.

    run() catches per-step errors and durably holds. If the HTTP serving thread
    itself fails, stop at the next tick boundary. An in-flight request can still
    finish; its durable receipt, not process exit, determines recovery safety.
    """
    httpd = W.status_server(address, runner, token_digest)
    maintenance = None
    if route is not None:
        try:
            maintenance = W.status_server((address[0], route["port"]), runner, token_digest, maintenance=True)
        except Exception:
            httpd.server_close()
            raise
    failed = threading.Event()
    def progress(server):
        try:
            server.serve_forever(poll_interval=0.2)
        except Exception:
            failed.set()
        finally:
            if not stop.is_set():
                failed.set()
            stop.set()
    thread = threading.Thread(target=progress, args=(httpd,), name="handoff-progress", daemon=True)
    extra = threading.Thread(target=progress, args=(maintenance,), name="handoff-page", daemon=True) if maintenance else None
    started = False
    try:
        thread.start()
        started = True
        if extra is not None: extra.start()
        runner.run(stop)
    finally:
        stop.set()
        if thread.is_alive():
            httpd.shutdown()
        httpd.server_close()
        if started:
            thread.join(timeout=5)
        if extra is not None:
            if extra.is_alive(): maintenance.shutdown()
            maintenance.server_close()
            if extra.ident is not None: extra.join(timeout=5)
    if failed.is_set():
        raise Held("The independent progress server stopped unexpectedly")


def main():
    stop = threading.Event()
    previous = {}
    try:
        # This image must never accidentally start the application server or
        # accept arbitrary CLI arguments/config paths containing credentials.
        if len(sys.argv) != 1:
            raise Held("The coordinator does not accept command-line overrides")
        value, scope = parse_configuration(os.environ.get(CONFIG_ENV))
        pod_uid, job_uid = os.environ.get(UID_ENV), os.environ.get(JOB_UID_ENV)
        _uid(pod_uid); _uid(job_uid)
        client = K.Client(scope)
        runner = W.Runner(client.read, client.send, client.logs, namespace=scope.namespace,
            deployment=scope.deployment, operation=scope.operation, anchor_uid=value["anchor_uid"], worker_uid=job_uid,
            pod_uid=pod_uid, config=os.environ.get(CONFIG_ENV), require_setup_receipts=True)
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous[signum] = signal.signal(signum, lambda *_: stop.set())
        serve(runner, value["status_digest"], stop, **({"route": value["route"]} if "route" in value else {}))
        # Never 0: that marks the Pod Succeeded and the Job Complete, so a helper
        # stopped by a drain would never be replaced. It only ever stops when told
        # to, and cleanup deletes the Job once the move is done.
        return 1
    except Exception:
        # No raw environment/config/API exception, and no automatic replay.
        print("The data move coordinator could not start or stopped unexpectedly. Its Job starts another while it has tries left; that one continues from the retained move record and repeats no request.", file=sys.stderr)
        return 1
    finally:
        stop.set()
        for signum, handler in previous.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    raise SystemExit(main())
