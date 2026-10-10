"""The node probe: what it is, and keeping the installed one in step.

The probe is a DaemonSet running two scripts from a ConfigMap. Those scripts
ship inside the Homestead image, so a release that reads a new field can carry
the script that produces it instead of asking anyone to re-apply a manifest.

The objects are described here in Python and deploy/nodeprobe.yaml is rendered
from that description, so the file someone applies by hand and the objects
Homestead installs itself cannot drift apart. A test regenerates the file and
compares it.
"""
import os
import base64
import time
import urllib.error

import homestead_names as NAMES

kget = ksend = None
NS = "lab"
VERSION = ""
SCRIPTS = os.environ.get("NODEPROBE_SCRIPTS",
                         os.path.join(os.path.dirname(os.path.abspath(__file__)), "probe"))
NAME = "homestead-nodeprobe"
PROBE_PORT = 9099
SMART_PORT = 9100
LAST = {"state": "pending", "detail": "the node probe has not been checked yet"}
_helper_key = None


def bind(_kget, _ksend, namespace, helper_key=None, version=""):
    global kget, ksend, NS, _helper_key, VERSION
    kget, ksend, NS = _kget, _ksend, namespace
    _helper_key = helper_key
    # The release the probe is installed at, for the install route.
    VERSION = version
    NAMES.bind(_kget)


def shipped_scripts(directory=None):
    """The probe scripts carried in this image."""
    found = {}
    for name in ("probe.py", "smart.py"):
        try:
            with open(os.path.join(directory or SCRIPTS, name), encoding="utf-8") as handle:
                found[name] = handle.read()
        except OSError:
            return {}
    try:
        with open(os.path.join(os.path.dirname(directory or SCRIPTS), "homestead_http.py"), encoding="utf-8") as handle:
            found["homestead_http.py"] = handle.read()
    except OSError:
        return {}
    return found


def _mount(name, path, read_only=True):
    row = {"name": name, "mountPath": path}
    if read_only:
        row["readOnly"] = True
    return row


def manifest(version="dev", namespace=None, scripts=None):
    """The ConfigMap and DaemonSet that together are the probe."""
    namespace = namespace or NS
    node_name = {"name": "NODE_NAME",
                 "valueFrom": {"fieldRef": {"fieldPath": "spec.nodeName"}}}
    configmap = {
        "apiVersion": "v1", "kind": "ConfigMap",
        "metadata": {"name": NAME, "namespace": namespace},
        "data": shipped_scripts() if scripts is None else scripts,
    }
    telemetry = {
        "name": "probe", "image": "python:3.12-alpine",
        "command": ["python3", "/srv/probe.py"],
        "ports": [{"containerPort": PROBE_PORT, "name": "http"}],
        "env": [node_name],
        "resources": {"requests": {"cpu": "5m", "memory": "24Mi"},
                      "limits": {"memory": "64Mi"}},
        # Reads sensors and counters and nothing else: no capabilities, no
        # writable filesystem, and no privilege available to escalate to.
        "securityContext": {"allowPrivilegeEscalation": False,
                            "readOnlyRootFilesystem": True,
                            "capabilities": {"drop": ["ALL"]}},
        "volumeMounts": [_mount("code", "/srv"), _mount("sys", "/host/sys"),
                         _mount("dev", "/host/dev"), _mount("proc", "/host/proc")],
    }
    smart = {
        "name": "smart", "image": NAMES.IMAGE + ":" + version,
        "imagePullPolicy": "IfNotPresent",
        "command": ["python3", "/srv/smart.py"],
        "ports": [{"containerPort": SMART_PORT, "name": "smart"}],
        "env": [node_name],
        "readinessProbe": {"httpGet": {"path": "/healthz", "port": "smart"},
                           "initialDelaySeconds": 5, "periodSeconds": 30,
                           "timeoutSeconds": 20},
        "resources": {"requests": {"cpu": "5m", "memory": "24Mi"},
                      "limits": {"cpu": "250m", "memory": "96Mi"}},
        # smartctl talks to block devices directly, which nothing short of
        # privileged allows for an unknown and changing set of drives. It still
        # has no host PID, IPC or network namespace, a read-only root, and it
        # answers only requests carrying a short-lived signature from Homestead.
        "securityContext": {"privileged": True, "runAsUser": 0,
                            "readOnlyRootFilesystem": True},
        "volumeMounts": [_mount("code", "/srv"), _mount("sys", "/host/sys"),
                         _mount("dev", "/host/dev", read_only=False),
                         _mount("auth", "/auth")],
    }
    daemonset = {
        "apiVersion": "apps/v1", "kind": "DaemonSet",
        "metadata": {"name": NAME, "namespace": namespace, "labels": {"app": NAME}},
        "spec": {
            "selector": {"matchLabels": {"app": NAME}},
            "template": {
                "metadata": {"labels": {"app": NAME}},
                "spec": {
                    "terminationGracePeriodSeconds": 2,
                    # Telemetry is wanted from every node, including cordoned
                    # and unwell ones - those are the interesting ones.
                    "tolerations": [{"operator": "Exists"}],
                    "containers": [telemetry, smart],
                    "volumes": [
                        {"name": "code", "configMap": {"name": NAME}},
                        {"name": "sys", "hostPath": {"path": "/sys", "type": "Directory"}},
                        {"name": "dev", "hostPath": {"path": "/dev", "type": "Directory"}},
                        {"name": "proc", "hostPath": {"path": "/proc", "type": "Directory"}},
                        {"name": "auth",
                         "secret": {"secretName": "homestead-smart-key"}},
                    ],
                },
            },
        },
    }
    return [configmap, daemonset]


def _note(result):
    LAST.clear()
    LAST.update(result)
    return result


def status():
    """What the last reconcile did, so an automatic action is not invisible."""
    return dict(LAST)


def installed():
    """The probe's name if it is installed, under either spelling."""
    for name in (NAMES.NODEPROBE,):
        try:
            found = kget("/apis/apps/v1/namespaces/" + NS + "/daemonsets/" + name)
        except Exception:
            continue
        if (found.get("metadata", {}) or {}).get("name"):
            return name
    return ""


def install(version="dev"):
    """Create the probe for automatic installation or an administrator's retry."""
    if installed():
        raise ValueError("the node probe is already installed")
    if not shipped_scripts():
        raise ValueError("this image carries no probe scripts")
    ensure_helper_key()
    configmap, daemonset = manifest(version)
    # The helper receives its own key, separate from the account store.
    daemonset["spec"]["template"]["spec"]["volumes"][-1]["secret"]["secretName"] = NAMES.object_name("smart-key", NS, kind="secrets")
    for body, path in ((configmap, "/api/v1/namespaces/" + NS + "/configmaps"),
                       (daemonset, "/apis/apps/v1/namespaces/" + NS + "/daemonsets")):
        try:
            ksend("POST", path, body)
        except urllib.error.HTTPError as error:
            if error.code != 409:
                raise
    return _note({"state": "installed",
                  "detail": NAME + " installed; each node reports once its pod is ready"})


def remove():
    """Take the probe away again, including one installed before the rename."""
    removed = []
    for name in (NAMES.NODEPROBE,):
        for path in ("/apis/apps/v1/namespaces/" + NS + "/daemonsets/" + name,
                     "/api/v1/namespaces/" + NS + "/configmaps/" + name):
            try:
                ksend("DELETE", path + "?propagationPolicy=Background")
                removed.append(name)
            except urllib.error.HTTPError as error:
                if error.code != 404:
                    raise
    return _note({"state": "absent", "detail": "the node probe was removed",
                  "removed": sorted(set(removed))})


def reconcile(version=""):
    """Bring the installed probe's scripts up to this release."""
    scripts = shipped_scripts()
    if not scripts:
        return _note({"state": "unknown", "detail": "this image carries no probe scripts"})
    name = installed()
    if not name:
        # Not installed, and not Homestead's to install unasked: the SMART
        # sidecar is privileged, which is the operator's decision to make.
        return _note({"state": "absent", "detail": "the node probe is not installed"})
    ensure_helper_key()
    if _helper_key:
        ds_path = "/apis/apps/v1/namespaces/" + NS + "/daemonsets/" + name
        ds = kget(ds_path)
        volumes = ds.get("spec", {}).get("template", {}).get("spec", {}).get("volumes", [])
        if any(v.get("name") == "auth" and v.get("secret", {}).get("secretName") != NAMES.object_name("smart-key") for v in volumes):
            ksend("PATCH", ds_path, {"spec": {"template": {"spec": {"volumes": [{"name": "auth", "secret": {"secretName": NAMES.object_name("smart-key"), "optional": False}}]}}}}, ctype="application/strategic-merge-patch+json")
    try:
        current = kget("/api/v1/namespaces/" + NS + "/configmaps/" + name)
    except Exception as error:
        return _note({"state": "error", "detail": str(error)[:180]})
    data = current.get("data", {}) or {}
    if all(data.get(key) == value for key, value in scripts.items()):
        return _note({"state": "current", "detail": name + " is running this release's scripts"})

    ksend("PUT", "/api/v1/namespaces/" + NS + "/configmaps/" + name,
          {"apiVersion": "v1", "kind": "ConfigMap",
           "metadata": {"name": name, "namespace": NS,
                        "annotations": {NAMES.key("probe-scripts"): version or "unknown"}},
           "data": dict(data, **scripts)})
    # A ConfigMap change restarts nothing by itself, and the probe reads its
    # script once at start, so the pods have to be replaced.
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    ksend("PATCH", "/apis/apps/v1/namespaces/" + NS + "/daemonsets/" + name,
          {"spec": {"template": {"metadata": {"annotations": {
              NAMES.key("restartedAt"): stamp}}}}},
          ctype="application/strategic-merge-patch+json")
    return _note({"state": "updated", "detail": name + " updated to this release's scripts",
                  "restarted_at": stamp})


def ensure_helper_key():
    if _helper_key is None:
        return
    data = {"key": base64.b64encode(_helper_key()).decode()}
    path = f"/api/v1/namespaces/{NS}/secrets/{NAMES.object_name('smart-key')}"
    try:
        current = kget(path)
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        current = {}
    if current.get("data") == data:
        return
    meta = {"name": NAMES.object_name("smart-key"), "namespace": NS}
    if current.get("metadata", {}).get("resourceVersion"):
        meta["resourceVersion"] = current["metadata"]["resourceVersion"]
    body = {"apiVersion": "v1", "kind": "Secret", "type": "Opaque", "metadata": meta, "data": data}
    ksend("PUT" if current else "POST", path if current else path.rsplit("/", 1)[0], body)


# Its routes and who may use them (homestead_routes.py).
ROUTES = {
    ("POST", "/api/node/probe/remove"): ("admin", lambda request: remove()),
    ("POST", "/api/node/probe/install"): ("admin", lambda request: install(VERSION)),
}
