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
import json
import time
import urllib.error

import homestead_names as NAMES

kget = ksend = None
NS = "lab"
SCRIPTS = os.environ.get("NODEPROBE_SCRIPTS",
                         os.path.join(os.path.dirname(os.path.abspath(__file__)), "probe"))
NAME = "homestead-nodeprobe"
PROBE_PORT = 9099
SMART_PORT = 9100
# Homestead's data claims, for the watchdog (probe/homestead_watchdog.py).
CONF = "homestead.json"
# The watchdog's record on each host: what it salvaged, and in which boot.
STATE_DIR = "/var/lib/homestead-nodeprobe"
LAST = {"state": "pending", "detail": "the node probe has not been checked yet"}
_helper_key = None


def bind(_kget, _ksend, namespace, helper_key=None):
    global kget, ksend, NS, _helper_key
    kget, ksend, NS = _kget, _ksend, namespace
    _helper_key = helper_key
    NAMES.bind(_kget)


def shipped_scripts(directory=None):
    """The probe scripts carried in this image."""
    found = {}
    for name in ("probe.py", "smart.py", "homestead_watchdog.py"):
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
    # Homestead's own image, as the SMART sidecar's: one image Homestead
    # builds, upgrades and scans, rather than a stock one that can lag
    # Alpine's security fixes (November 2026, krb5-libs).
    telemetry = {
        "name": "probe", "image": NAMES.IMAGE + ":" + version,
        "imagePullPolicy": "IfNotPresent",
        "command": ["python3", "/srv/probe.py"],
        "ports": [{"containerPort": PROBE_PORT, "name": "http"}],
        "env": [node_name],
        "resources": {"requests": {"cpu": "5m", "memory": "24Mi"},
                      "limits": {"memory": "64Mi"}},
        # Reads sensors and counters and nothing else: no capabilities, no
        # writable filesystem, and no privilege available to escalate to. Root,
        # as the stock image ran it: Homestead's image defaults to its own user.
        "securityContext": {"runAsUser": 0, "runAsGroup": 0, "runAsNonRoot": False,
                            "allowPrivilegeEscalation": False,
                            "readOnlyRootFilesystem": True,
                            "capabilities": {"drop": ["ALL"]}},
        "volumeMounts": [_mount("code", "/srv"), _mount("sys", "/host/sys"),
                         _mount("dev", "/host/dev"), _mount("proc", "/host/proc"),
                         # The watchdog's once-per-boot record, the only thing it writes.
                         _mount("state", "/state", read_only=False)],
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
                        {"name": "state", "hostPath": {"path": STATE_DIR, "type": "DirectoryOrCreate"}},
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


UNREAD = [""]    # why installed() last answered None, "" after an answer


WAY_OUT = "Check Homestead's permissions and try again."


def unread_why():
    """The error of the last failed read of the probe, "no answer" if none was kept."""
    return UNREAD[0] or "no answer"


def unread_reason():
    """For a person: the cause, then the error, then the way out."""
    return f"The node probe could not be checked ({unread_why()}). {WAY_OUT}"


def installed():
    """The probe's name if it is installed, "" only on the API's own 404, None if it could not be read
    (unread_reason() then says why)."""
    UNREAD[0] = ""
    for name in (NAMES.NODEPROBE,):
        try:
            found = kget("/apis/apps/v1/namespaces/" + NS + "/daemonsets/" + name)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                continue
            UNREAD[0] = f"HTTP {error.code}"
            return None
        except Exception as error:
            UNREAD[0] = (str(error) or type(error).__name__)[:80]
            return None
        if (found.get("metadata", {}) or {}).get("name"):
            return name
    return ""


def presence():
    """Whether the probe is there: True, False only on the API's own 404, None if unknown."""
    try:
        found = kget("/apis/apps/v1/namespaces/" + NS + "/daemonsets/" + NAMES.NODEPROBE)
    except urllib.error.HTTPError as error:
        return False if error.code == 404 else None
    except Exception:
        return None
    return bool((found.get("metadata", {}) or {}).get("name"))


def install(version="dev"):
    """Create the probe for automatic installation or an administrator's retry."""
    there = installed()
    if there is None:
        raise ValueError(unread_reason())
    if there:
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
                  "detail": NAME + " installed. Each host reports once its pod is ready."})


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


def own_data():
    """What the watchdog looks after: the claims Homestead's own pod mounts,
    and its Deployment, so that it can tell Homestead's pods from a data
    move's or a restore's. {} when this is not Homestead's pod."""
    pod_name = os.environ.get("HOSTNAME", "")
    if not pod_name:
        return {}
    pod = kget(f"/api/v1/namespaces/{NS}/pods/{pod_name}")
    owner = next((row.get("name", "") for row in (pod.get("metadata") or {}).get("ownerReferences") or []
                  if row.get("kind") == "ReplicaSet" and row.get("controller")), "")
    claims = sorted(v["persistentVolumeClaim"]["claimName"] for v in (pod.get("spec") or {}).get("volumes") or []
                    if (v.get("persistentVolumeClaim") or {}).get("claimName"))
    if not owner or "-" not in owner or not claims:
        return {}
    return {"namespace": NS, "deployment": owner.rsplit("-", 1)[0], "claims": claims}


def _within(want, have):
    """Whether everything in want is in have as well. Kubernetes adds its
    defaults to what it stores, so an exact comparison would never match."""
    if isinstance(want, dict):
        return isinstance(have, dict) and all(_within(value, have.get(key)) for key, value in want.items())
    if isinstance(want, list):
        return isinstance(have, list) and len(want) == len(have) and all(map(_within, want, have))
    return want == have


# Images an installed probe may run that are still Homestead's to replace:
# its own, and the stock one it ran before 2.8.336.
MANAGED_IMAGES = ("python:3.12-alpine",)


def _template(name, version):
    """Bring the installed DaemonSet's pod spec up to this release: its
    images, the watchdog's record folder, and whatever comes next. A probe
    someone runs a custom image in is left as it is. Returns a note, or ""
    when nothing changed."""
    path = "/apis/apps/v1/namespaces/" + NS + "/daemonsets/" + name
    ds = kget(path)
    have = ((ds.get("spec") or {}).get("template") or {}).get("spec") or {}
    for row in have.get("containers") or []:
        image = str(row.get("image") or "")
        if not image.startswith(NAMES.IMAGE + ":") and image not in MANAGED_IMAGES:
            return "custom image kept, so its pod spec was not updated"
    want = manifest(version)[1]["spec"]["template"]["spec"]
    for volume in want["volumes"]:
        if volume["name"] == "auth":
            volume["secret"]["secretName"] = NAMES.object_name("smart-key")
    if _within(want, have):
        return ""
    # What someone added to the pod spec (a node selector, a priority) stays.
    ksend("PATCH", path, [
        {"op": "test", "path": "/metadata/resourceVersion", "value": (ds.get("metadata") or {}).get("resourceVersion")},
        {"op": "replace", "path": "/spec/template/spec", "value": {**have, **want}}],
        ctype="application/json-patch+json")
    return "pod spec updated to this release"


def reconcile(version=""):
    """Bring the installed probe up to this release: its scripts, what the
    watchdog looks after, and its pod spec."""
    scripts = shipped_scripts()
    if not scripts:
        return _note({"state": "unknown", "detail": "this image carries no probe scripts"})
    name = installed()
    if name is None:
        return _note({"state": "unknown", "detail": unread_reason()})
    if not name:
        # Not installed, and not Homestead's to install unasked: the SMART
        # sidecar is privileged, which is the operator's decision to make.
        return _note({"state": "absent", "detail": "the node probe is not installed"})
    ensure_helper_key()
    try:
        conf = own_data()
    except Exception:
        conf = None          # unread: whatever the probe was told before stays
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
    stale = not all(data.get(key) == value for key, value in scripts.items())
    told = json.dumps(conf, sort_keys=True) if conf else data.get(CONF)
    try:
        spec_note = _template(name, version or "dev")
    except Exception as error:
        spec_note = f"pod spec not updated ({str(error)[:100]})"
    if not stale and data.get(CONF) == told:
        if spec_note:
            return _note({"state": "updated" if spec_note.startswith("pod spec updated") else "current",
                          "detail": f"{name} is running this release's scripts; {spec_note}"})
        return _note({"state": "current", "detail": name + " is running this release's scripts"})
    wanted = dict(data, **scripts)
    if told:
        wanted[CONF] = told
    ksend("PUT", "/api/v1/namespaces/" + NS + "/configmaps/" + name,
          {"apiVersion": "v1", "kind": "ConfigMap",
           "metadata": {"name": name, "namespace": NS,
                        "annotations": {NAMES.key("probe-scripts"): version or "unknown"}},
           "data": wanted})
    if not stale:
        # Only what the watchdog looks after changed: the probe reads it at
        # each look, and Kubernetes updates the mounted file within a minute.
        return _note({"state": "updated", "detail": f"{name} now looks after {', '.join(conf['claims'])}"
                      + (f"; {spec_note}" if spec_note else "")})
    # A ConfigMap change restarts nothing by itself, and the probe reads its
    # script once at start, so the pods have to be replaced.
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    ksend("PATCH", "/apis/apps/v1/namespaces/" + NS + "/daemonsets/" + name,
          {"spec": {"template": {"metadata": {"annotations": {
              NAMES.key("restartedAt"): stamp}}}}},
          ctype="application/strategic-merge-patch+json")
    return _note({"state": "updated", "detail": name + " updated to this release's scripts",
                  "restarted_at": stamp})


# How long a salvage stays raised after it happened, unless acknowledged.
SALVAGE_SHOWN = 7 * 86400


def watchdog_alert_facts(temps, now=None):
    """An alert for each salvage the watchdog made in the last week, once
    Homestead is back to read it from the probe (homestead_watchdog.py):
    what it did, why Longhorn could not, and how to keep the next restart
    from needing it. temps: each host's probe reading (NODES.node_temps)."""
    if not temps:
        return None          # no probe reading: unread, so a raised alert is held, not cleared
    now = time.time() if now is None else now
    facts = []
    for host, reading in sorted(temps.items()):
        for entry in ((reading or {}).get("watchdog") or {}).get("salvaged") or []:
            try:
                at = float(entry.get("at"))
            except (AttributeError, TypeError, ValueError):
                continue
            if not 0 <= now - at <= SALVAGE_SHOWN:
                continue
            node, path, result = entry.get("node") or host, entry.get("path") or "its disk", str(entry.get("result") or "")
            when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(at))
            available, maximum, minimal = int(entry.get("available") or 0), int(entry.get("maximum") or 0), entry.get("minimal")
            if maximum and isinstance(minimal, int) and available * 100 < maximum * minimal:
                why = (f"Disk {path} on {node} is below Longhorn's free space threshold: {available / 1024 ** 3:.1f} GiB free "
                       f"of {maximum / 1024 ** 3:.1f} GiB, and Longhorn keeps {minimal}% free")
            else:
                why = f"Longhorn does not place copies on disk {path} on {node}"
            fix = ("Free space on that disk, or move the copy with Move copies on Volumes, "
                   "so that the next unclean restart does not need a salvage.")
            if result == "accepted":
                title = "Homestead's data volume was salvaged after a restart"
                body = (f"{why}, so Longhorn could not bring the volume back by itself. "
                        f"The node probe on {host} salvaged it at {when}. {fix}")
            else:
                title = "Homestead's data volume needed a salvage after a restart, and it was not confirmed"
                body = (f"{why}, so Longhorn could not bring the volume back by itself. The node probe on {host} asked "
                        f"Longhorn to salvage it at {when}, and the request was {result or 'not answered'}. "
                        f"It is not tried again until that host restarts. {fix}")
            facts.append({"signals": {"salvaged": 1}, "key": f"watchdog-salvage:{entry.get('volume')}:{int(at)}",
                          "category": "degraded", "severity": "degraded", "title": title,
                          "resolved": f"The salvage of Homestead's data volume at {when} is over a week old",
                          "body": body, "href": "/volumes"})
    return facts


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
