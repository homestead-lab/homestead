"""Homestead's own objects: the permissions it runs with, and the names they carry.

Permissions. The in-app update replaces Homestead's image and nothing else, so a
release that needed a new permission used to need someone to re-apply the
manifest by hand. The manifest now travels inside the image, like the node
probe's scripts, and on start Homestead compares the ClusterRole it is bound to
with the one this release describes and brings it up to date itself. That takes
the right to edit its own role (escalate included), granted once by
deploy/rbac.yaml; without it, Settings shows the one command that grants it.

Names. Homestead was called harvUI, and an install from then still runs as the
harvui service account, keeps its settings in harvui-* ConfigMaps and Secrets,
and its data on harvui-data. Everything still works - Homestead reads either
name - but Settings can move such an install to the new names:

  1. copy each harvui-* ConfigMap and Secret to its homestead-* name, which
     Homestead prefers from that moment; the old ones stay as they were;
  2. reinstall the node probe under its new name;
  3. give the Service its new name, keeping its address;
  4. switch the Deployment to the homestead service account and a new
     homestead-data volume. The copy of the data runs in the new pod before
     Homestead starts - the old pod has stopped by then, so nothing is writing -
     and the old volume is mounted read-only, untouched, so rolling the
     Deployment back returns exactly what was there.

Removing what is left under the old names is a separate step, taken once the
new ones have been seen working.
"""
import copy
import os
import re
import threading
import time
import urllib.error

import homestead_names as NAMES
import homestead_yaml as YAML

kget = ksend = None
NS = "lab"                # Homestead's own namespace
DATA_NS = "lab"           # where its settings live (DEFAULT_NS)
SHARES_NS = "lab"         # where the share definitions live (SMB_NAMESPACE)
VERSION = ""
POD = os.environ.get("HOSTNAME", "")
BRAND, LEGACY = NAMES.BRAND, NAMES.LEGACY
HERE = os.path.dirname(os.path.abspath(__file__))
MANIFESTS = (os.path.join(HERE, "deploy.yaml"), os.path.join(HERE, "..", "deploy", "deploy.yaml"))
RBAC_URL = "https://raw.githubusercontent.com/wjcloudy/homestead/{ref}/deploy/rbac.yaml"
LAST = {"state": "pending", "detail": "Homestead has not checked its permissions yet"}
# What went wrong in the part of a move that runs after the reply has gone.
MOVE = {"error": ""}

CONFIGMAPS = ("settings", "sources", "hardware")
SECRETS = ("auth",)
SHARE_PAIR = (("configmaps", "shares"), ("secrets", "share-credentials"))
# harvUI's first releases shipped their code in ConfigMaps; nothing reads them.
RELICS = ("server", "web")
ADOPT = "adopt-data"
PREVIOUS = "previous-data"
MARKER = ".homestead-adopted"
ADOPT_SCRIPT = (f"[ -e /data/{MARKER} ] && exit 0; "
                f"cp -a /previous/. /data/ && touch /data/{MARKER}")


def bind(_kget, _ksend, namespace, data_namespace, shares_namespace, version=""):
    global kget, ksend, NS, DATA_NS, SHARES_NS, VERSION
    kget, ksend, NS, DATA_NS, SHARES_NS, VERSION = (
        _kget, _ksend, namespace, data_namespace, shares_namespace, version)


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


def _exists(path):
    try:
        return bool(_get(path))
    except urllib.error.HTTPError:
        return False


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
            return [dict(rule) for rule in doc.get("rules") or []]
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
    """This pod's service account and Deployment."""
    if not POD:
        raise ValueError("Homestead is not running in a pod")
    pod = kget(f"/api/v1/namespaces/{NS}/pods/{POD}")
    account = (pod.get("spec") or {}).get("serviceAccountName") or "default"
    deployment = ""
    for owner in (pod.get("metadata") or {}).get("ownerReferences") or []:
        if owner.get("kind") == "ReplicaSet":
            rs = kget(f"/apis/apps/v1/namespaces/{NS}/replicasets/{owner['name']}")
            for parent in (rs.get("metadata") or {}).get("ownerReferences") or []:
                if parent.get("kind") == "Deployment":
                    deployment = parent["name"]
    return {"account": account, "deployment": deployment}


def _binding_path(name):
    return f"/apis/rbac.authorization.k8s.io/v1/clusterrolebindings/{name}"


def _role_path(name):
    return f"/apis/rbac.authorization.k8s.io/v1/clusterroles/{name}"


def _binds(binding, account):
    return any(s.get("kind") == "ServiceAccount" and s.get("name") == account
               and s.get("namespace", NS) == NS for s in binding.get("subjects") or [])


def bound_role(account):
    """The ClusterRole this account holds through Homestead's own bindings."""
    for name in (BRAND, LEGACY):
        try:
            binding = _get(_binding_path(name))
        except urllib.error.HTTPError:
            binding = None
        if binding and _binds(binding, account) and (binding.get("roleRef") or {}).get("kind") == "ClusterRole":
            return binding["roleRef"]["name"]
    return ""


# ------------------------------------------------------------ permissions
def reconcile():
    """Bring Homestead's ClusterRole up to this release, if it is allowed to."""
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
    gaps = missing(live.get("rules") or [], wanted)
    tidied = _tidy_binding()
    if not gaps:
        return _note({"state": "current", "account": me["account"], "role": role,
                      "detail": f"{role} has everything this release uses" + (f"; {tidied}" if tidied else "")})
    # Keep anything someone added by hand; only add what this release needs.
    extra = [rule for rule in live.get("rules") or [] if missing(wanted, [rule])]
    body = {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRole",
            "metadata": {k: v for k, v in (live.get("metadata") or {}).items()
                         if k in ("name", "labels", "annotations", "resourceVersion")},
            "rules": wanted + extra}
    try:
        ksend("PUT", _role_path(role), body)
    except urllib.error.HTTPError as error:
        return _note({"state": "manual", "account": me["account"], "role": role, "missing": len(gaps),
                      "detail": f"this release needs {len(gaps)} permission{'s' if len(gaps) != 1 else ''} "
                                f"that {role} does not have, and Homestead may not add them itself "
                                f"(HTTP {error.code}); run the command once"})
    return _note({"state": "updated", "account": me["account"], "role": role, "added": len(gaps),
                  "detail": f"{role} updated with {len(gaps)} permission{'s' if len(gaps) != 1 else ''} "
                            "this release uses"})


def _tidy_binding():
    """Drop the harvui account from Homestead's binding once there is no such account."""
    try:
        binding = _get(_binding_path(BRAND))
        if not binding or not _binds(binding, LEGACY) or _exists(f"/api/v1/namespaces/{NS}/serviceaccounts/{LEGACY}"):
            return ""
        body = copy.deepcopy(binding)
        body["subjects"] = [s for s in body.get("subjects") or []
                            if not (s.get("kind") == "ServiceAccount" and s.get("name") == LEGACY)]
        ksend("PUT", _binding_path(BRAND), body)
        return "the old harvui account was taken off its binding"
    except Exception:
        return ""


# ------------------------------------------------------------ the names
def _cm(ns, name):
    return f"/api/v1/namespaces/{ns}/configmaps/{name}"


def _secret(ns, name):
    return f"/api/v1/namespaces/{ns}/secrets/{name}"


def _objects():
    """(kind, namespace, old name, new name) for every settings object with an old name."""
    rows = [("configmaps", DATA_NS, f"{LEGACY}-{s}", f"{BRAND}-{s}") for s in CONFIGMAPS]
    rows += [("secrets", DATA_NS, f"{LEGACY}-{s}", f"{BRAND}-{s}") for s in SECRETS]
    rows += [(kind, SHARES_NS, f"{LEGACY}-{s}", f"{BRAND}-{s}") for kind, s in SHARE_PAIR]
    try:
        secrets = kget(f"/api/v1/namespaces/{DATA_NS}/secrets").get("items", [])
    except Exception:
        secrets = []
    for item in secrets:
        name = item["metadata"]["name"]
        if name.startswith(f"{LEGACY}-src-"):
            rows.append(("secrets", DATA_NS, name, BRAND + name[len(LEGACY):]))
    return rows


def _path(kind, ns, name):
    return (_cm if kind == "configmaps" else _secret)(ns, name)


def _deployment(name):
    return kget(f"/apis/apps/v1/namespaces/{NS}/deployments/{name}")


def _data_volume(spec):
    """The volume holding DATA_DIR: whichever the app container mounts at /data."""
    containers = spec.get("containers") or []
    mounts = (containers[0].get("volumeMounts") or []) if containers else []
    name = next((m["name"] for m in mounts if m.get("mountPath") == "/data"), "data")
    return next((v for v in spec.get("volumes") or [] if v.get("name") == name), None)


def plan():
    """What moving to the Homestead names would do here, and what stops it."""
    blockers, steps = [], []
    try:
        me = identity()
    except Exception as error:
        return {"blockers": [f"could not tell what Homestead runs as: {str(error)[:120]}"], "steps": [],
                "pending": 0, "leftovers": []}
    if not _exists(f"/api/v1/namespaces/{NS}/serviceaccounts/{BRAND}") or not bound_role(BRAND):
        blockers.append("the homestead service account and its permissions are not there yet - "
                        "run the command below once")
    for kind, ns, old, new in _objects():
        if not _exists(_path(kind, ns, old)):
            continue
        done = _exists(_path(kind, ns, new))
        steps.append({"id": f"copy:{kind}:{ns}:{old}", "what": f"{'ConfigMap' if kind == 'configmaps' else 'Secret'} "
                      f"{old} → {new}", "done": done})
    probe = _exists(f"/apis/apps/v1/namespaces/{DATA_NS}/daemonsets/{LEGACY}-nodeprobe")
    if probe or _exists(f"/apis/apps/v1/namespaces/{DATA_NS}/daemonsets/{BRAND}-nodeprobe"):
        steps.append({"id": "probe", "what": f"Node probe {LEGACY}-nodeprobe → {BRAND}-nodeprobe",
                      "done": not probe})
    old_svc = _get(f"/api/v1/namespaces/{NS}/services/{LEGACY}")
    if old_svc or _exists(f"/api/v1/namespaces/{NS}/services/{BRAND}"):
        steps.append({"id": "service", "what": f"Service {LEGACY} → {BRAND}", "done": not old_svc,
                      "note": "same LAN address and port; a Cloudflare Tunnel or proxy pointed at "
                              f"{LEGACY}.{NS}.svc must point at {BRAND}.{NS}.svc afterwards" if old_svc else ""})
    if me["deployment"]:
        spec = _deployment(me["deployment"])["spec"]["template"]["spec"]
        volume = _data_volume(spec) or {}
        claim = (volume.get("persistentVolumeClaim") or {}).get("claimName", "")
        if claim == f"{LEGACY}-data" or claim == f"{BRAND}-data":
            steps.append({"id": "data", "what": f"Data volume {LEGACY}-data → {BRAND}-data, copied before "
                          "Homestead starts", "done": claim == f"{BRAND}-data" or not _exists(
                              f"/api/v1/namespaces/{NS}/persistentvolumeclaims/{LEGACY}-data")})
        account = spec.get("serviceAccountName") or spec.get("serviceAccount") or "default"
        steps.append({"id": "account", "what": f"Service account {account} → {BRAND}", "done": account == BRAND})
        if me["deployment"] == LEGACY:
            steps.append({"id": "deployment-name", "what": "The Deployment itself keeps the name harvui",
                          "done": True, "note": "a Deployment cannot be renamed in place; nothing refers to it"})
    else:
        blockers.append("could not find the Deployment Homestead runs in")
    pending = [s for s in steps if not s["done"]]
    return {"blockers": blockers, "steps": steps, "pending": len(pending), "account": me["account"],
            "deployment": me["deployment"], "command": command(), "error": MOVE["error"],
            "leftovers": leftovers(me) if not pending else []}


def _copy(kind, ns, old, new):
    source = kget(_path(kind, ns, old))
    meta = source.get("metadata") or {}
    annotations = {k: v for k, v in (meta.get("annotations") or {}).items()
                   if k != "kubectl.kubernetes.io/last-applied-configuration"}
    body = {"apiVersion": "v1", "kind": "ConfigMap" if kind == "configmaps" else "Secret",
            "metadata": {"name": new, "namespace": ns, "labels": meta.get("labels") or {},
                         "annotations": annotations}}
    for field in ("data", "binaryData", "type"):
        if field in source:
            body[field] = source[field]
    try:
        ksend("POST", f"/api/v1/namespaces/{ns}/{kind}", body)
    except urllib.error.HTTPError as error:
        if error.code != 409:
            raise


def _service_body(old):
    meta, spec = old.get("metadata") or {}, old.get("spec") or {}
    keep = ("type", "selector", "loadBalancerIP", "externalTrafficPolicy", "sessionAffinity",
            "loadBalancerClass", "ipFamilies", "ipFamilyPolicy", "allocateLoadBalancerNodePorts",
            "internalTrafficPolicy", "publishNotReadyAddresses")
    body_spec = {k: copy.deepcopy(v) for k, v in spec.items() if k in keep}
    body_spec["ports"] = [{k: v for k, v in port.items() if k != "nodePort"} for port in spec.get("ports") or []]
    return {"apiVersion": "v1", "kind": "Service",
            "metadata": {"name": BRAND, "namespace": NS, "labels": meta.get("labels") or {},
                         "annotations": {k: v for k, v in (meta.get("annotations") or {}).items()
                                         if k != "kubectl.kubernetes.io/last-applied-configuration"}},
            "spec": body_spec}


def swap_service():
    """Give the Service its new name. Its address can only be held by one at a time."""
    old = _get(f"/api/v1/namespaces/{NS}/services/{LEGACY}")
    if not old or _exists(f"/api/v1/namespaces/{NS}/services/{BRAND}"):
        return
    body = _service_body(old)
    ksend("DELETE", f"/api/v1/namespaces/{NS}/services/{LEGACY}")
    try:
        ksend("POST", f"/api/v1/namespaces/{NS}/services", body)
    except Exception:
        # Put the old one back rather than leave Homestead with no address.
        back = copy.deepcopy(body)
        back["metadata"]["name"] = LEGACY
        ksend("POST", f"/api/v1/namespaces/{NS}/services", back)
        raise


def _new_claim(old):
    spec = old.get("spec") or {}
    body_spec = {k: copy.deepcopy(spec[k]) for k in ("accessModes", "storageClassName", "volumeMode") if k in spec}
    body_spec["resources"] = {"requests": dict((spec.get("resources") or {}).get("requests") or {})}
    return {"apiVersion": "v1", "kind": "PersistentVolumeClaim",
            "metadata": {"name": f"{BRAND}-data", "namespace": NS, "labels": {"app": BRAND}},
            "spec": body_spec}


def adopted_spec(deployment, account=BRAND):
    """The Deployment moved to the new account and volume, copying the old data in first."""
    body = copy.deepcopy(deployment)
    for field in ("status",):
        body.pop(field, None)
    spec = body["spec"]["template"]["spec"]
    spec["serviceAccountName"] = account
    spec.pop("serviceAccount", None)
    volume = _data_volume(spec)
    claim = (volume or {}).get("persistentVolumeClaim") or {}
    if claim.get("claimName") == f"{LEGACY}-data":
        claim["claimName"] = f"{BRAND}-data"
        volumes = [v for v in spec.get("volumes") or [] if v.get("name") != PREVIOUS]
        volumes.append({"name": PREVIOUS, "persistentVolumeClaim": {"claimName": f"{LEGACY}-data", "readOnly": True}})
        spec["volumes"] = volumes
        app = (spec.get("containers") or [{}])[0]
        init = [c for c in spec.get("initContainers") or [] if c.get("name") != ADOPT]
        init.insert(0, {
            "name": ADOPT, "image": app.get("image", ""), "imagePullPolicy": "IfNotPresent",
            "command": ["sh", "-c", ADOPT_SCRIPT],
            # Root, to keep every file's owner as it was; nothing else.
            "securityContext": {"runAsUser": 0, "runAsGroup": 0, "runAsNonRoot": False,
                                "allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"], "add": ["CHOWN", "FOWNER", "DAC_OVERRIDE"]}},
            "volumeMounts": [{"name": volume["name"], "mountPath": "/data"},
                             {"name": PREVIOUS, "mountPath": "/previous", "readOnly": True}],
        })
        spec["initContainers"] = init
    return body


def released_spec(deployment):
    """The Deployment without the copy step and the old volume, once they are done with."""
    body = copy.deepcopy(deployment)
    body.pop("status", None)
    spec = body["spec"]["template"]["spec"]
    spec["initContainers"] = [c for c in spec.get("initContainers") or [] if c.get("name") != ADOPT]
    spec["volumes"] = [v for v in spec.get("volumes") or [] if v.get("name") != PREVIOUS]
    return body


def move(after=None):
    """Takes every step of the plan. The last ones restart Homestead."""
    current = plan()
    if current["blockers"]:
        raise ValueError(current["blockers"][0])
    todo = {s["id"] for s in current["steps"] if not s["done"]}
    MOVE["error"] = ""
    for kind, ns, old, new in _objects():
        if f"copy:{kind}:{ns}:{old}" in todo:
            _copy(kind, ns, old, new)
    if "probe" in todo:
        import homestead_probe as PROBE
        PROBE.remove()
        PROBE.install(VERSION or "dev")
    restart = bool(todo & {"service", "data", "account"})
    if "data" in todo and not _exists(f"/api/v1/namespaces/{NS}/persistentvolumeclaims/{BRAND}-data"):
        old = kget(f"/api/v1/namespaces/{NS}/persistentvolumeclaims/{LEGACY}-data")
        ksend("POST", f"/api/v1/namespaces/{NS}/persistentvolumeclaims", _new_claim(old))

    def finish():
        # The reply goes out first: the Service is how it reaches the browser.
        time.sleep(1.5)
        try:
            if "service" in todo:
                swap_service()
            if todo & {"data", "account"}:
                deployment = _deployment(current["deployment"])
                ksend("PUT", f"/apis/apps/v1/namespaces/{NS}/deployments/{current['deployment']}",
                      adopted_spec(deployment))
        except Exception as error:
            MOVE["error"] = str(error)[:300]

    if restart:
        (after or (lambda fn: threading.Thread(target=fn, daemon=True).start()))(finish)
    return {"ok": True, "restarting": restart, "copied": len([t for t in todo if t.startswith("copy:")])}


def leftovers(me=None):
    """What still carries the old name once everything has moved."""
    me = me or identity()
    rows = []
    for kind, ns, old, new in _objects():
        if _exists(_path(kind, ns, old)) and _exists(_path(kind, ns, new)):
            rows.append({"kind": kind[:-1], "namespace": ns, "name": old})
    for relic in RELICS:
        if _exists(_cm(DATA_NS, f"{LEGACY}-{relic}")):
            rows.append({"kind": "configmap", "namespace": DATA_NS, "name": f"{LEGACY}-{relic}"})
    if _exists(f"/api/v1/namespaces/{NS}/persistentvolumeclaims/{LEGACY}-data"):
        rows.append({"kind": "persistentvolumeclaim", "namespace": NS, "name": f"{LEGACY}-data",
                     "data": True})
    if me["account"] != LEGACY:
        rbac = "/apis/rbac.authorization.k8s.io/v1"
        for kind, path in (("rolebinding", f"{rbac}/namespaces/{NS}/rolebindings/{LEGACY}-console"),
                           ("role", f"{rbac}/namespaces/{NS}/roles/{LEGACY}-console"),
                           ("clusterrolebinding", _binding_path(LEGACY)),
                           ("clusterrole", _role_path(LEGACY)),
                           ("serviceaccount", f"/api/v1/namespaces/{NS}/serviceaccounts/{LEGACY}")):
            if _exists(path):
                rows.append({"kind": kind, "namespace": NS if "namespaces" in path else "", "name": path.rsplit("/", 1)[1]})
    return rows


def _leftover_path(row):
    rbac = "/apis/rbac.authorization.k8s.io/v1"
    kind, ns, name = row["kind"], row["namespace"], row["name"]
    return {"configmap": _cm(ns, name), "secret": _secret(ns, name),
            "persistentvolumeclaim": f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{name}",
            "rolebinding": f"{rbac}/namespaces/{ns}/rolebindings/{name}",
            "role": f"{rbac}/namespaces/{ns}/roles/{name}",
            "clusterrolebinding": _binding_path(name), "clusterrole": _role_path(name),
            "serviceaccount": f"/api/v1/namespaces/{ns}/serviceaccounts/{name}"}[kind]


def clean(confirm=""):
    """Removes what the old names left behind. The old data volume goes too."""
    me = identity()
    current = plan()
    if current["pending"]:
        raise ValueError("move to the Homestead names first")
    if me["account"] == LEGACY:
        raise ValueError("Homestead still runs as harvui; it has not restarted under the new account yet")
    rows = leftovers(me)
    if any(r.get("data") for r in rows) and confirm != f"{LEGACY}-data":
        raise ValueError(f"type {LEGACY}-data to remove the old data volume")
    removed, failed = [], []
    for row in rows:
        try:
            ksend("DELETE", _leftover_path(row))
            removed.append(row["name"])
        except urllib.error.HTTPError as error:
            if error.code != 404:
                failed.append(f"{row['kind']} {row['name']}: HTTP {error.code}")
    _tidy_binding()
    restart = False
    if me["deployment"]:
        deployment = _deployment(me["deployment"])
        spec = deployment["spec"]["template"]["spec"]
        if any(c.get("name") == ADOPT for c in spec.get("initContainers") or []) or \
                any(v.get("name") == PREVIOUS for v in spec.get("volumes") or []):
            ksend("PUT", f"/apis/apps/v1/namespaces/{NS}/deployments/{me['deployment']}", released_spec(deployment))
            restart = True
    return {"ok": not failed, "removed": removed, "failed": failed, "restarting": restart}
