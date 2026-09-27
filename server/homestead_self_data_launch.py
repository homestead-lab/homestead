"""Isolated coordinator Pod/Service manifests and its executable entry point.

Builders do not apply resources. Reviewed setup must journal creation receipts,
observe the admitted Pod UID/spec, pin them in the anchor, and only then publish
the local pointer. A bare, non-restarting Pod deliberately has no controller that
could replace a lost coordinator. Both data claims stay unmounted here.
The progress Service is cluster-internal; external authenticated/TLS routing is
the setup gateway's responsibility, not an implicit public LoadBalancer.

Identity/projection contracts:
https://kubernetes.io/docs/concepts/workloads/pods/downward-api/
https://kubernetes.io/docs/concepts/storage/projected-volumes/
"""
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
PORT = 8081
MAX_CONFIG = 32768


def configuration(scope, anchor_uid, status_digest):
    value = {"protocol": 1, "namespace": scope.namespace, "deployment": scope.deployment,
             "operation": scope.operation, "anchor_uid": anchor_uid, "status_digest": status_digest,
             "claims": list(scope.claims), "volumes": list(scope.volumes), "nodes": list(scope.nodes)}
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
        if (not isinstance(value, dict) or set(value) != {"protocol", "namespace", "deployment", "operation",
                "anchor_uid", "status_digest", "claims", "volumes", "nodes"}
                or type(value["protocol"]) is not int or value["protocol"] != 1):
            raise ValueError("unsupported config")
        _uid(value["anchor_uid"])
        A._hash(value["status_digest"])
        scope = K.Scope(value["namespace"], value["deployment"], value["operation"],
                        value["claims"], value["volumes"], value["nodes"])
    except (KeyError, TypeError, ValueError, RecursionError):
        raise Held("The coordinator configuration cannot be verified") from None
    return value, scope


def _uid(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9-]{1,128}", value):
        raise Held("The coordinator resource identity is invalid")


def resources(scope, *, anchor_uid, image, node, status_digest):
    """Return scoped access, one independent Pod, and a private progress Service.

    No Pod UID can be supplied by setup: Kubernetes supplies it through the
    downward API. Never use this manifest's pre-admission shape as the worker
    receipt; scheduling/defaulting/webhooks can change the stored Pod spec.
    """
    raw = configuration(scope, anchor_uid, status_digest)
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
    pod = {"apiVersion": "v1", "kind": "Pod", "metadata": metadata(), "spec": {
        "serviceAccountName": account, "automountServiceAccountToken": False,
        "restartPolicy": "Never", "terminationGracePeriodSeconds": 60,
        "enableServiceLinks": False,
        "affinity": {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {
            "nodeSelectorTerms": [{"matchFields": [{"key": "metadata.name", "operator": "In", "values": [node]}]}]}}},
        "securityContext": {"runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001,
                            "fsGroup": 10001, "seccompProfile": {"type": "RuntimeDefault"}},
        "volumes": [K.token_volume()],
        "containers": [{"name": "coordinator", "image": image, "imagePullPolicy": "IfNotPresent",
            "command": ["python3", "/srv/homestead_self_data_launch.py"],
            "env": [{"name": CONFIG_ENV, "value": raw},
                    {"name": UID_ENV, "valueFrom": {"fieldRef": {"apiVersion": "v1", "fieldPath": "metadata.uid"}}}],
            "ports": [{"name": "progress", "containerPort": PORT, "protocol": "TCP"}],
            "resources": {"requests": {"cpu": "50m", "memory": "64Mi"}, "limits": {"cpu": "500m", "memory": "256Mi"}},
            "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]}},
            "volumeMounts": [{"name": "handoff-api", "mountPath": "/var/run/homestead-handoff", "readOnly": True}],
            # Readiness means only that progress can be queried. No liveness
            # restart: an unknown outcome must not create a replacement worker.
            "readinessProbe": {"httpGet": {"path": "/healthz", "port": "progress"},
                               "periodSeconds": 5, "timeoutSeconds": 2, "failureThreshold": 3}}]}}
    service = {"apiVersion": "v1", "kind": "Service", "metadata": metadata(), "spec": {
        "type": "ClusterIP", "selector": {"homestead.io/handoff-worker": account},
        "ports": [{"name": "progress", "port": PORT, "targetPort": "progress", "protocol": "TCP"}]}}
    return [*access, pod, service]


def serve(runner, token_digest, stop, *, address=("0.0.0.0", PORT)):
    """Bind progress before any mutation and keep it available during holds.

    run() catches per-step errors and durably holds. If the HTTP serving thread
    itself fails, stop at the next tick boundary. An in-flight request can still
    finish; its durable receipt, not process exit, determines recovery safety.
    """
    httpd = W.status_server(address, runner, token_digest)
    failed = threading.Event()
    def progress():
        try:
            httpd.serve_forever(poll_interval=0.2)
        except Exception:
            failed.set()
        finally:
            if not stop.is_set():
                failed.set()
            stop.set()
    thread = threading.Thread(target=progress, name="handoff-progress", daemon=True)
    started = False
    try:
        thread.start()
        started = True
        runner.run(stop)
    finally:
        stop.set()
        if thread.is_alive():
            httpd.shutdown()
        httpd.server_close()
        if started:
            thread.join(timeout=5)
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
        worker_uid = os.environ.get(UID_ENV)
        _uid(worker_uid)
        client = K.Client(scope)
        runner = W.Runner(client.read, client.send, client.logs, namespace=scope.namespace,
            deployment=scope.deployment, operation=scope.operation, anchor_uid=value["anchor_uid"], worker_uid=worker_uid)
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous[signum] = signal.signal(signum, lambda *_: stop.set())
        serve(runner, value["status_digest"], stop)
        return 0
    except Exception:
        # No raw environment/config/API exception, and no automatic replay.
        print("The data move coordinator could not start or stopped unexpectedly. Review the retained move record; no retry was requested.", file=sys.stderr)
        return 1
    finally:
        stop.set()
        for signum, handler in previous.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    raise SystemExit(main())
