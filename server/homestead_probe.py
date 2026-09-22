"""Keep the node probe's scripts in step with the Homestead that reads them.

The probe is a DaemonSet running a script from a ConfigMap, so a Homestead
release that reads a new field is useless until that script is replaced. Asking
people to re-apply a manifest to finish an upgrade is asking them to remember,
and a half-upgraded probe reports nothing new while looking perfectly healthy.

So the scripts ship inside the image and Homestead reconciles them: if the
ConfigMap it finds does not match the ones it carries, it writes them and
restarts the DaemonSet. It never installs the probe uninvited - the SMART
sidecar is privileged, and that is a decision for whoever runs the cluster.
"""
import os
import re
import time
import urllib.error

import homestead_names as NAMES

kget = ksend = None
NS = "lab"
MANIFEST = os.environ.get("NODEPROBE_MANIFEST", "/srv/nodeprobe.yaml")
VERSION_KEY = "probe-scripts"


def bind(_kget, _ksend, namespace):
    global kget, ksend, NS
    kget, ksend, NS = _kget, _ksend, namespace
    NAMES.bind(_kget)


def shipped_scripts(path=None):
    """The probe scripts carried in this image, read out of the manifest.

    Only the literal blocks under the ConfigMap's data are taken. That is a
    plain indentation rule rather than YAML in general, which keeps this free
    of a parser the server does not otherwise need.
    """
    try:
        with open(path or MANIFEST, encoding="utf-8") as handle:
            lines = handle.read().split("\n")
    except OSError:
        return {}
    scripts, key, body = {}, None, []
    for line in lines:
        header = re.fullmatch(r"  ([A-Za-z0-9_.-]+\.py): \|", line)
        if header:
            if key:
                scripts[key] = "\n".join(body).rstrip("\n") + "\n"
            key, body = header.group(1), []
            continue
        if key is None:
            continue
        if line.strip() and not line.startswith("    "):
            scripts[key] = "\n".join(body).rstrip("\n") + "\n"
            key, body = None, []
            continue
        body.append(line[4:] if line.startswith("    ") else line)
    if key:
        scripts[key] = "\n".join(body).rstrip("\n") + "\n"
    return scripts


def _installed():
    """The probe's ConfigMap and DaemonSet, under whichever name they carry."""
    for name in NAMES.NODEPROBE:
        try:
            daemonset = kget(f"/apis/apps/v1/namespaces/{NS}/daemonsets/{name}")
        except Exception:
            continue
        if (daemonset.get("metadata", {}) or {}).get("name"):
            return name
    return ""


LAST = {"state": "pending", "detail": "the node probe has not been checked yet"}


def _note(result):
    LAST.clear()
    LAST.update(result)
    return result


def status():
    """What the last reconcile did, so an automatic action is not invisible."""
    return dict(LAST)


def reconcile(version=""):
    """Bring the installed probe's scripts up to this release. Returns a note."""
    scripts = shipped_scripts()
    if not scripts:
        return _note({"state": "unknown", "detail": "this image carries no probe scripts"})
    name = _installed()
    if not name:
        # Not installed, and not Homestead's to install: the SMART sidecar is
        # privileged, so it stays an explicit choice.
        return _note({"state": "absent", "detail": "the node probe is not installed"})
    try:
        current = kget(f"/api/v1/namespaces/{NS}/configmaps/{name}")
    except Exception as error:
        return _note({"state": "error", "detail": str(error)[:180]})
    data = current.get("data", {}) or {}
    if all(data.get(key) == value for key, value in scripts.items()):
        return _note({"state": "current", "detail": f"{name} is running this release's scripts"})

    body = {"apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": name, "namespace": NS,
                         "annotations": {NAMES.key(VERSION_KEY): version or "unknown"}},
            "data": dict(data, **scripts)}
    ksend("PUT", f"/api/v1/namespaces/{NS}/configmaps/{name}", body)
    # A ConfigMap change does not restart anything by itself, and the probe
    # reads its script once at start, so the pods have to be replaced.
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    ksend("PATCH", f"/apis/apps/v1/namespaces/{NS}/daemonsets/{name}",
          {"spec": {"template": {"metadata": {"annotations": {
              NAMES.key("restartedAt"): stamp}}}}},
          ctype="application/strategic-merge-patch+json")
    return _note({"state": "updated", "detail": f"{name} updated to this release's scripts",
                  "restarted_at": stamp})
