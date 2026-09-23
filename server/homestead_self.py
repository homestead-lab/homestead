"""Homestead's own permissions, kept in step with its release.

The in-app update replaces Homestead's image and nothing else, so a release
that needed a new permission used to need someone to re-apply the manifest by
hand. The manifest now travels inside the image, like the node probe's
scripts, and on start Homestead makes the ClusterRole it is bound to match the
one this release describes - adding what a new feature needs and dropping what
an old one no longer does - and keeps its binding pointed at itself alone.
That takes the right to edit its own role (escalate included), granted once by
deploy/rbac.yaml; without it, Settings shows the one command that grants it.
"""
import copy
import json
import os
import re
import time
import urllib.error

import homestead_names as NAMES
import homestead_yaml as YAML

kget = ksend = None
NS = "lab"                # Homestead's own namespace
DATA_DIR = "/data"
VERSION = ""
POD = os.environ.get("HOSTNAME", "")
BRAND = NAMES.BRAND
HERE = os.path.dirname(os.path.abspath(__file__))
MANIFESTS = (os.path.join(HERE, "deploy.yaml"), os.path.join(HERE, "..", "deploy", "deploy.yaml"))
RBAC_URL = "https://raw.githubusercontent.com/wjcloudy/homestead/{ref}/deploy/rbac.yaml"
LAST = {"state": "pending", "detail": "Homestead has not checked its permissions yet"}


def bind(_kget, _ksend, namespace, version="", data_dir="/data"):
    global kget, ksend, NS, VERSION, DATA_DIR
    kget, ksend, NS, VERSION, DATA_DIR = _kget, _ksend, namespace, version, data_dir


def _note(result):
    LAST.clear()
    LAST.update(result, checked_at=int(time.time()))
    return status()


def status():
    return dict(LAST, command=command())


def command():
    """The one command that lets Homestead look after its own permissions."""
    ref = f"v{VERSION}" if re.fullmatch(r"\d+\.\d+\.\d+", VERSION or "") else "main"
    return f"kubectl apply -f {RBAC_URL.format(ref=ref)}"


def _get(path):
    try:
        return kget(path)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


# ------------------------------------------------------------ the manifest
def manifest_documents(path=None):
    for candidate in ([path] if path else MANIFESTS):
        try:
            with open(candidate, encoding="utf-8") as handle:
                text = handle.read()
        except OSError:
            continue
        return [YAML.loads(doc) for doc in re.split(r"(?m)^---\s*$", text) if doc.strip()]
    return []


def desired_rules(path=None):
    for doc in manifest_documents(path):
        if doc.get("kind") == "ClusterRole":
            return json.loads(json.dumps(doc.get("rules") or []))
    return []


def _grants(rule):
    names = rule.get("resourceNames") or [None]
    for group in rule.get("apiGroups") or [""]:
        for resource in rule.get("resources") or []:
            for verb in rule.get("verbs") or []:
                for name in names:
                    yield group, resource, verb, name


def _covers(rule, grant):
    group, resource, verb, name = grant

    def has(values, value):
        return "*" in (values or []) or value in (values or [])
    if not (has(rule.get("apiGroups") or [""], group) and has(rule.get("resources"), resource)
            and has(rule.get("verbs"), verb)):
        return False
    names = rule.get("resourceNames") or []
    return not names or (name is not None and name in names)


def missing(live, wanted):
    """What `wanted` grants that `live` does not."""
    return [grant for rule in wanted for grant in _grants(rule)
            if not any(_covers(have, grant) for have in live)]


# ------------------------------------------------------------ who am I
def identity():
    """This pod's service account."""
    if not POD:
        raise ValueError("Homestead is not running in a pod")
    pod = kget(f"/api/v1/namespaces/{NS}/pods/{POD}")
    return {"account": (pod.get("spec") or {}).get("serviceAccountName") or "default"}


def _binding_path():
    return f"/apis/rbac.authorization.k8s.io/v1/clusterrolebindings/{BRAND}"


def _role_path(name):
    return f"/apis/rbac.authorization.k8s.io/v1/clusterroles/{name}"


def _subject(account):
    return {"kind": "ServiceAccount", "name": account, "namespace": NS}


def bound_role(account):
    """The ClusterRole Homestead's binding gives this account, if it does."""
    try:
        binding = _get(_binding_path())
    except urllib.error.HTTPError:
        return ""
    if not binding or (binding.get("roleRef") or {}).get("kind") != "ClusterRole":
        return ""
    if any(s.get("kind") == "ServiceAccount" and s.get("name") == account and s.get("namespace", NS) == NS
           for s in binding.get("subjects") or []):
        return binding["roleRef"]["name"]
    return ""


def _own_binding(account):
    """Homestead's binding names Homestead and nothing else."""
    try:
        binding = _get(_binding_path())
        subjects = [{k: s.get(k) for k in ("kind", "name", "namespace")} for s in (binding or {}).get("subjects") or []]
        if not binding or subjects == [_subject(account)]:
            return ""
        body = copy.deepcopy(binding)
        body["subjects"] = [_subject(account)]
        ksend("PUT", _binding_path(), body)
        dropped = [s["name"] for s in subjects if s.get("name") != account]
        return f"its binding no longer names {', '.join(dropped)}" if dropped else ""
    except Exception:
        return ""


# ------------------------------------------------------------ permissions
def _plural(count, word):
    return f"{count} {word}{'' if count == 1 else 's'}"


def reconcile():
    """Make Homestead's ClusterRole this release's, if it is allowed to."""
    wanted = desired_rules()
    if not wanted:
        return _note({"state": "unknown", "detail": "this image carries no permission list"})
    try:
        me = identity()
    except Exception as error:
        return _note({"state": "unknown", "detail": f"could not tell what Homestead runs as: {str(error)[:120]}"})
    role = bound_role(me["account"])
    if not role:
        return _note({"state": "manual", "account": me["account"],
                      "detail": "Homestead cannot see its own permissions; run the command once"})
    try:
        live = kget(_role_path(role))
    except Exception as error:
        return _note({"state": "manual", "account": me["account"], "role": role,
                      "detail": f"Homestead cannot read its role {role}: {str(error)[:100]}"})
    rules = live.get("rules") or []
    gaps, surplus = missing(rules, wanted), missing(wanted, rules)
    tidied = _own_binding(me["account"])
    if not gaps and not surplus:
        return _note({"state": "current", "account": me["account"], "role": role,
                      "detail": f"{role} matches this release" + (f"; {tidied}" if tidied else "")})
    body = {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRole",
            "metadata": {k: v for k, v in (live.get("metadata") or {}).items()
                         if k in ("name", "labels", "annotations", "resourceVersion")},
            "rules": wanted}
    try:
        ksend("PUT", _role_path(role), body)
    except urllib.error.HTTPError as error:
        return _note({"state": "manual", "account": me["account"], "role": role, "missing": len(gaps),
                      "detail": f"this release needs {_plural(len(gaps), 'permission')} {role} does not have, "
                                f"and Homestead may not change its role itself (HTTP {error.code}); run the command once"})
    changes = [f"added {_plural(len(gaps), 'permission')}"] if gaps else []
    changes += [f"removed {_plural(len(surplus), 'permission')} it no longer uses"] if surplus else []
    return _note({"state": "updated", "account": me["account"], "role": role, "added": len(gaps),
                  "removed": len(surplus), "detail": f"{role} updated: {' and '.join(changes)}"})


# ------------------------------------------------- the old annotation domain
# Homestead was once called harvUI and wrote harvui.io/* keys. This moves any
# still on an object's own metadata to homestead.io/*, once, and can go in a
# later release. Only metadata is touched - never a pod template or a selector -
# so nothing restarts; a harvui.io key left inside a pod template is inert.
OLD_DOMAIN = "harvui.io/"
ADOPT = (("", "v1", "nodes", False), ("", "v1", "services", True), ("", "v1", "persistentvolumeclaims", True),
         ("", "v1", "configmaps", True), ("", "v1", "secrets", True),
         ("apps", "v1", "deployments", True), ("apps", "v1", "statefulsets", True),
         ("apps", "v1", "daemonsets", True), ("kubevirt.io", "v1", "virtualmachines", True))


def _renamed(values):
    """The patch that moves each old key to its new name, keeping a newer value that is already there."""
    change = {}
    for key, value in (values or {}).items():
        if key.startswith(OLD_DOMAIN):
            new = NAMES.DOMAIN + "/" + key[len(OLD_DOMAIN):]
            if new not in values:
                change[new] = value
            change[key] = None
    return change


def adopt_old_keys():
    """Rewrites harvui.io/* labels and annotations as homestead.io/*, once."""
    marker = os.path.join(DATA_DIR, "old-keys-adopted")
    if os.path.exists(marker):
        return {"done": True, "changed": 0}
    changed, failed = 0, 0
    for group, version, plural, namespaced in ADOPT:
        base = f"/apis/{group}/{version}" if group else f"/api/{version}"
        try:
            items = kget(f"{base}/{plural}").get("items", [])
        except Exception:
            if group == "kubevirt.io":
                continue
            failed += 1
            continue
        for item in items:
            meta = item.get("metadata") or {}
            annotations = _renamed(meta.get("annotations"))
            labels = _renamed(meta.get("labels"))
            if not annotations and not labels:
                continue
            path = (f"{base}/namespaces/{meta.get('namespace')}/{plural}/{meta['name']}" if namespaced
                    else f"{base}/{plural}/{meta['name']}")
            try:
                if plural == "secrets":
                    # Homestead may update Secrets but not patch them.
                    body = copy.deepcopy(item)
                    for field, change in (("annotations", annotations), ("labels", labels)):
                        values = body["metadata"].setdefault(field, {})
                        for key, value in change.items():
                            if value is None:
                                values.pop(key, None)
                            else:
                                values[key] = value
                    ksend("PUT", path, body)
                else:
                    patch = {"metadata": {k: v for k, v in (("annotations", annotations), ("labels", labels)) if v}}
                    ksend("PATCH", path, patch, ctype="application/merge-patch+json")
                changed += 1
            except Exception:
                failed += 1
    if not failed:
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(marker, "w", encoding="utf-8") as handle:
                handle.write(str(int(time.time())))
        except OSError:
            pass
    return {"done": not failed, "changed": changed, "failed": failed}
