"""Dedicated Kubernetes transport and RBAC for the self-data coordinator.

No kubeconfig, caller-supplied origin, proxy, redirect, token cache or retries.
The client policy is defense in depth, not a sandbox for compromised Python.
RBAC cannot name-restrict top-level Job creation; workload write permissions can
also indirectly access namespace credentials. Do not describe this as isolation
from a malicious coordinator. Setup/cleanup still belongs to the reviewed app.
"""
import hashlib
import json
import re
import ssl
import urllib.error
import urllib.request

import homestead_self_data_anchor as A
from homestead_storage_journal import Held


ORIGIN = "https://kubernetes.default.svc"
TOKEN = "/var/run/homestead-handoff/token"
CA = "/var/run/homestead-handoff/ca.crt"
NAME = r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?"


class Scope:
    def __init__(self, namespace, deployment, operation, claims, volumes, nodes):
        if any(not isinstance(values, (tuple, list)) or not 1 <= len(values) <= 256 for values in (claims, volumes, nodes)):
            raise Held("The data move API scope needs bounded resource-name lists")
        for name in (namespace, deployment, *claims, *volumes, *nodes):
            A._name(name)
        if (not re.fullmatch(r"[a-f0-9]{24}", operation or "") or not claims or not volumes or not nodes
                or any(len(set(values)) != len(values) for values in (claims, volumes, nodes))):
            raise Held("The data move API scope is incomplete or ambiguous")
        self.namespace, self.deployment, self.operation = namespace, deployment, operation
        self.claims, self.volumes, self.nodes = tuple(claims), tuple(volumes), tuple(nodes)
        self.anchor_name = deployment + "-data-handoff"
        A._name(self.anchor_name)
        self.copy_name = "homestead-data-copy-" + operation
        self.core = f"/api/v1/namespaces/{namespace}"
        self.anchor = self.core + "/configmaps/" + self.anchor_name
        self.dep = f"/apis/apps/v1/namespaces/{namespace}/deployments/{deployment}"
        self.jobs = f"/apis/batch/v1/namespaces/{namespace}/jobs"
        self.job = self.jobs + "/" + self.copy_name

    def check(self, method, path, body=None, *, logs=False):
        # Reject encoded traversal, arbitrary query parameters, subresources,
        # URL authorities and redirects before even reading a service token.
        if not isinstance(path, str) or len(path) > 2048 or any(c in path for c in ("%", "#", "\\", "..", "//")):
            raise Held("The data move request is outside its reviewed API scope")
        if body is not None and (not isinstance(body, dict) or not isinstance(body.get("metadata", {}), dict)
                                 or not isinstance(body.get("preconditions", {}), dict)):
            raise Held("The data move request body cannot be verified")
        allowed = False
        if logs:
            allowed = method == "GET" and re.fullmatch(re.escape(self.core) + rf"/pods/{NAME}/log\?container=copy&tailLines=20", path)
        elif method == "GET":
            allowed = path in {self.anchor, self.dep, self.job, self.core + "/pods",
                f"/apis/apps/v1/namespaces/{self.namespace}/replicasets",
                f"/apis/autoscaling/v2/namespaces/{self.namespace}/horizontalpodautoscalers",
                "/api/v1/nodes", "/api/v1/pods", "/api/v1/namespaces", "/apis/metrics.k8s.io/v1beta1/nodes"}
            allowed |= any(path == self.core + "/persistentvolumeclaims/" + name for name in self.claims)
            allowed |= any(path == "/api/v1/persistentvolumes/" + name for name in self.volumes)
            allowed |= any(path == "/api/v1/nodes/" + name or path == "/apis/coordination.k8s.io/v1/namespaces/kube-node-lease/leases/" + name for name in self.nodes)
            allowed |= bool(re.fullmatch(re.escape(self.core) + rf"/pods/{NAME}", path))
            allowed |= bool(re.fullmatch(r"/api/v1/namespaces/" + NAME, path))
            allowed |= bool(re.fullmatch(r"/apis/storage.k8s.io/v1/storageclasses/" + NAME, path))
        elif method == "PUT" and path in (self.anchor, self.dep):
            meta = (body or {}).get("metadata", {})
            kind, api, name = ("ConfigMap", "v1", self.anchor_name) if path == self.anchor else ("Deployment", "apps/v1", self.deployment)
            allowed = (isinstance(body, dict) and body.get("kind") == kind and body.get("apiVersion") == api
                and meta.get("namespace") == self.namespace and meta.get("name") == name
                and bool(meta.get("uid")) and bool(meta.get("resourceVersion")))
        elif method == "POST" and path == self.jobs:
            meta = (body or {}).get("metadata", {})
            allowed = (isinstance(body, dict) and body.get("apiVersion") == "batch/v1" and body.get("kind") == "Job"
                and meta.get("namespace") == self.namespace and meta.get("name") == self.copy_name
                and not meta.get("generateName")
                and meta.get("labels", {}).get("homestead.io/self-data-copy") == self.operation)
        elif method == "DELETE" and path == self.job:
            pre = (body or {}).get("preconditions", {})
            allowed = (isinstance(body, dict) and bool(pre.get("uid")) and bool(pre.get("resourceVersion"))
                and body.get("propagationPolicy") == "Foreground" and "gracePeriodSeconds" not in body)
        if not allowed or method == "GET" and body is not None:
            raise Held("The data move request is outside its reviewed API scope")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class PreviewScope:
    """Source-side preflight only; never grants a worker real Pod creation.

    Kubernetes RBAC uses the same permission for dry-run and real creation.
    Use the source app's reviewed existing credentials, not an enlarged worker
    Role. The transport refuses requests that omit the literal dry-run flag.
    """
    def __init__(self, scope):
        self.namespace = scope.namespace
        self.anchor_name = scope.deployment + "-data-handoff"
        self.worker_name = access_name(scope.namespace, scope.deployment, scope.operation)
        self.copy_name = scope.copy_name
        self.pod_name = "homestead-copy-check-" + scope.operation

    def check(self, method, path, body=None, *, logs=False):
        meta = body.get("metadata", {}) if isinstance(body, dict) else {}
        suffix = "?dryRun=All&fieldValidation=Strict"
        wanted = (("v1", "Pod", {self.worker_name, self.pod_name}) if path == f"/api/v1/namespaces/{self.namespace}/pods" + suffix else
                  ("batch/v1", "Job", {self.copy_name}) if path == f"/apis/batch/v1/namespaces/{self.namespace}/jobs" + suffix else None)
        if (logs or method != "POST" or wanted is None or not isinstance(body, dict) or not isinstance(meta, dict)
                or body.get("apiVersion") != wanted[0] or body.get("kind") != wanted[1]
                or meta.get("namespace") != self.namespace or meta.get("name") not in wanted[2]
                or any(key in meta for key in ("uid", "resourceVersion", "generateName"))):
            raise Held("This preflight client only permits the reviewed dry-run Pod and Job requests")
        if "ownerReferences" in meta:
            owners = meta["ownerReferences"]
            if (meta["name"] != self.worker_name or body["kind"] != "Pod" or not isinstance(owners, list) or len(owners) != 1
                    or not isinstance(owners[0], dict) or not isinstance(owners[0].get("uid"), str) or not owners[0]["uid"]
                    or owners[0] != {"apiVersion": "v1", "kind": "ConfigMap", "name": self.anchor_name,
                                      "uid": owners[0]["uid"], "controller": True, "blockOwnerDeletion": False}):
                raise Held("Only the maintenance worker may reference its control-record owner during preflight")


class Client:
    def __init__(self, scope, *, token_path=TOKEN, ca_path=CA):
        self.scope, self.token_path = scope, token_path
        # No unverified fallback, environment proxies or arbitrary API origin.
        context = ssl.create_default_context(cafile=ca_path)
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=context), _NoRedirect())

    def _request(self, method, path, body=None, *, logs=False):
        self.scope.check(method, path, body, logs=logs)
        try:
            # Kubelet rotates projected tokens. Reopen the path for every call,
            # never keep an fd or copy the bearer into a persistent file.
            with open(self.token_path, encoding="ascii") as handle:
                token = handle.read(16385).strip()
            if not token or len(token) > 16384 or not re.fullmatch(r"[A-Za-z0-9._-]+", token):
                raise Held("The data move service token is unavailable")
            data = None if body is None else json.dumps(body, separators=(",", ":"), allow_nan=False).encode()
            request = urllib.request.Request(ORIGIN + path, data=data, method=method,
                # The Kubernetes log subresource negotiates through the API
                # serializer even though its successful body is plain text.
                # A text/plain-only Accept receives 406 on supported k3s APIs.
                headers={"Authorization": "Bearer " + token, "Accept": "*/*" if logs else "application/json",
                         "Content-Type": "application/json"})
            limit = 65536 if logs else 16 * 1024 * 1024
            with self.opener.open(request, timeout=15) as response:
                if response.geturl() != ORIGIN + path or not 200 <= response.status < 300:
                    raise Held("The data move API response could not be verified")
                raw = response.read(limit + 1)
            if len(raw) > limit:
                raise Held("The data move API response exceeds its safe read limit")
            if logs:
                return raw.decode("utf-8", errors="strict")
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise Held("The data move API returned an invalid object")
            return result
        except urllib.error.HTTPError as error:
            # Keep the code for 404/409/422 journal semantics, not the request,
            # Authorization header, upstream body or raw diagnostic.
            code = error.code
            error.close()
            raise urllib.error.HTTPError(path, code, "Kubernetes request returned an error", {}, None) from None
        except Held:
            raise
        except Exception:
            raise Held("The Kubernetes reply could not be verified. No request was retried") from None

    def read(self, path):
        return self._request("GET", path)

    def send(self, method, path, body=None, **kwargs):
        if kwargs and kwargs != {"ctype": "application/json"}:
            raise Held("The data move API transport does not accept mutation overrides")
        return self._request(method, path, body)

    def logs(self, path):
        return self._request("GET", path, logs=True)


def access_name(namespace, deployment, operation):
    suffix = hashlib.sha256((namespace + "/" + deployment + "/" + operation).encode()).hexdigest()[:24]
    return "homestead-handoff-" + suffix


def access_resources(scope):
    """Return operation-specific RBAC; this function does not apply anything.

    RBAC: https://kubernetes.io/docs/reference/access-authn-authz/rbac/
    Top-level Job create cannot use resourceNames. It is namespace-scoped,
    while the trusted transport additionally validates the exact copy Job name.
    """
    name = access_name(scope.namespace, scope.deployment, scope.operation)
    labels = {"app.kubernetes.io/managed-by": "homestead", A.LABEL: scope.operation}
    def resource(kind, ns=scope.namespace):
        return {"apiVersion": "v1" if kind == "ServiceAccount" else "rbac.authorization.k8s.io/v1", "kind": kind,
                "metadata": {"name": name, "labels": dict(labels), **({"namespace": ns} if ns else {})}}
    def rule(group, resources, verbs, names=None):
        return {"apiGroups": [group], "resources": resources, "verbs": verbs,
                **({"resourceNames": list(names)} if names is not None else {})}
    def binding(kind, role_kind, ns):
        return {**resource(kind, ns), "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": role_kind, "name": name},
                "subjects": [{"kind": "ServiceAccount", "name": name, "namespace": scope.namespace}]}
    account = {**resource("ServiceAccount"), "automountServiceAccountToken": False}
    role = {**resource("Role"), "rules": [
        rule("", ["configmaps"], ["get", "update"], [scope.anchor_name]),
        rule("apps", ["deployments"], ["get", "update"], [scope.deployment]),
        rule("", ["persistentvolumeclaims"], ["get"], scope.claims),
        rule("", ["pods"], ["get"]), rule("", ["pods/log"], ["get"]),
        rule("apps", ["replicasets"], ["list"]), rule("autoscaling", ["horizontalpodautoscalers"], ["list"]),
        rule("batch", ["jobs"], ["create"]), rule("batch", ["jobs"], ["get", "delete"], [scope.copy_name]),
    ]}
    cluster = {**resource("ClusterRole", None), "rules": [
        rule("", ["nodes", "pods", "namespaces"], ["list"]), rule("", ["nodes"], ["get"], scope.nodes),
        rule("", ["namespaces"], ["get"]),
        rule("", ["persistentvolumes"], ["get"], scope.volumes),
        rule("storage.k8s.io", ["storageclasses"], ["get"]), rule("metrics.k8s.io", ["nodes"], ["list"]),
    ]}
    leases = {**resource("Role", "kube-node-lease"), "rules": [rule("coordination.k8s.io", ["leases"], ["get"], scope.nodes)]}
    leases["metadata"]["name"] += "-leases"
    lease_binding = binding("RoleBinding", "Role", "kube-node-lease")
    lease_binding["metadata"]["name"] += "-leases"
    lease_binding["roleRef"]["name"] += "-leases"
    return [account, role, binding("RoleBinding", "Role", scope.namespace), cluster,
            binding("ClusterRoleBinding", "ClusterRole", None), leases, lease_binding]


def token_volume():
    """Explicit rotating API token; never mount either application's data PVC.

    Kubernetes requires at least 600 seconds for a projected token. No audience
    override: the token is for this cluster's API, not a guessed audience string.
    A non-root worker must use an explicit fsGroup to read the 0440 projection.
    """
    return {"name": "handoff-api", "projected": {"defaultMode": 0o440, "sources": [
        {"serviceAccountToken": {"path": "token", "expirationSeconds": 600}},
        {"configMap": {"name": "kube-root-ca.crt", "items": [{"key": "ca.crt", "path": "ca.crt"}]}},
    ]}}
