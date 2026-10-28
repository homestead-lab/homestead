"""Every resource in the cluster, the way Headlamp or kubectl shows it.

Homestead's own pages cover what a homelab mostly does. This covers the rest,
so someone used to a general Kubernetes UI is never stuck:

* every kind the API server serves - built in, from Harvester and Longhorn,
  and custom resources - found by asking the server, grouped as Headlamp
  groups them;
* each kind listed with the columns the API server itself prints for it (the
  columns `kubectl get` shows, custom resources' own included);
* any object as YAML, its events, edited and saved back, or deleted;
* new objects from pasted YAML, several documents at once.

Secrets are listed with their keys; their values are only sent to an admin
who asks for one.
"""
import json
import re
import time
import urllib.error
import urllib.parse

import homestead_outage as OUTAGE
import homestead_yaml as YAML

kget = ksend = table_get = None
_discovery = {"at": 0.0, "value": None}
TTL = 600

CATEGORIES = [
    ("Workloads", {("", "pods"), ("apps", "deployments"), ("apps", "statefulsets"), ("apps", "daemonsets"),
                   ("apps", "replicasets"), ("batch", "jobs"), ("batch", "cronjobs"),
                   ("autoscaling", "horizontalpodautoscalers"), ("policy", "poddisruptionbudgets")}),
    ("Network", {("", "services"), ("", "endpoints"), ("networking.k8s.io", "ingresses"),
                 ("networking.k8s.io", "networkpolicies"), ("networking.k8s.io", "ingressclasses"),
                 ("discovery.k8s.io", "endpointslices")}),
    ("Storage", {("", "persistentvolumeclaims"), ("", "persistentvolumes"), ("storage.k8s.io", "storageclasses"),
                 ("storage.k8s.io", "volumeattachments"), ("storage.k8s.io", "csidrivers"), ("snapshot.storage.k8s.io", "volumesnapshots")}),
    ("Configuration", {("", "configmaps"), ("", "secrets"), ("", "resourcequotas"), ("", "limitranges"),
                       ("scheduling.k8s.io", "priorityclasses"), ("admissionregistration.k8s.io", "validatingwebhookconfigurations"),
                       ("admissionregistration.k8s.io", "mutatingwebhookconfigurations")}),
    ("Access", {("", "serviceaccounts"), ("rbac.authorization.k8s.io", "roles"), ("rbac.authorization.k8s.io", "rolebindings"),
                ("rbac.authorization.k8s.io", "clusterroles"), ("rbac.authorization.k8s.io", "clusterrolebindings")}),
    ("Cluster", {("", "nodes"), ("", "namespaces"), ("", "events"), ("apiextensions.k8s.io", "customresourcedefinitions"),
                 ("coordination.k8s.io", "leases"), ("certificates.k8s.io", "certificatesigningrequests")}),
]


# server.py's guard that keeps this editor from going round Network Shares,
# and the namespace a manifest without one goes in (bind).
_guard = [lambda kind, ns, name: None]
_default_ns = ["lab"]


def bind(_kget, _ksend, _table_get, guard=None, default_ns=None):
    global kget, ksend, table_get
    kget, ksend, table_get = _kget, _ksend, _table_get
    if guard:
        _guard[0] = guard
    if default_ns:
        _default_ns[0] = default_ns


def _category(group, resource):
    for name, members in CATEGORIES:
        if (group, resource) in members:
            return name
    return "Custom resources"


def discover(force=False):
    """Every listable kind the API server serves, newest version of each group."""
    if not force and _discovery["value"] is not None and time.time() - _discovery["at"] < TTL:
        return _discovery["value"]
    lists = [("", "v1", kget("/api/v1"))]
    for group in kget("/apis").get("groups", []):
        version = (group.get("preferredVersion") or {}).get("version") or (group.get("versions") or [{}])[0].get("version")
        try:
            lists.append((group["name"], version, kget(f"/apis/{group['name']}/{version}")))
        except Exception:
            continue          # an aggregated API that is down: skip it, not the page
    kinds = []
    for group, version, listing in lists:
        for r in listing.get("resources", []):
            if "/" in r["name"] or "list" not in (r.get("verbs") or []):
                continue
            kinds.append({"group": group, "version": version, "resource": r["name"], "kind": r.get("kind", ""),
                          "namespaced": bool(r.get("namespaced")), "verbs": r.get("verbs") or [],
                          "short": r.get("shortNames") or [], "category": _category(group, r["name"])})
    kinds.sort(key=lambda k: ([c for c, _ in CATEGORIES] + ["Custom resources"]).index(k["category"]) * 1000
               + (0 if k["group"] == "" else 1))
    _discovery.update(at=time.time(), value=kinds)
    return kinds


def _kind(group, version, resource):
    for k in discover():
        if (k["group"], k["resource"]) == (group, resource) and (not version or k["version"] == version):
            return k
    raise ValueError(f"the cluster serves no {resource}" + (f" in {group}" if group else ""))


def _path(kind, namespace="", name=""):
    base = f"/api/{kind['version']}" if kind["group"] == "" else f"/apis/{kind['group']}/{kind['version']}"
    if kind["namespaced"] and namespace:
        base += f"/namespaces/{urllib.parse.quote(namespace)}"
    base += f"/{kind['resource']}"
    return base + (f"/{urllib.parse.quote(name)}" if name else "")


def _name(value, label="name"):
    value = str(value or "")
    if not re.fullmatch(r"[A-Za-z0-9]([A-Za-z0-9._:-]{0,251}[A-Za-z0-9])?", value):
        raise ValueError(f"that is not a {label}")
    return value


def list_objects(group, version, resource, namespace=""):
    kind = _kind(group, version, resource)
    if namespace:
        _name(namespace, "namespace")
    table = table_get(_path(kind, namespace) + "?limit=500")
    columns = [c for c in table.get("columnDefinitions", []) if int(c.get("priority", 0) or 0) == 0]
    keep = [i for i, c in enumerate(table.get("columnDefinitions", [])) if int(c.get("priority", 0) or 0) == 0]
    rows = []
    for row in table.get("rows", []):
        meta = ((row.get("object") or {}).get("metadata") or {})
        cells = row.get("cells") or []
        rows.append({"name": meta.get("name", ""), "namespace": meta.get("namespace", ""),
                     "created": meta.get("creationTimestamp", ""),
                     "cells": [cells[i] if i < len(cells) else "" for i in keep]})
    return {"kind": kind, "columns": [{"name": c.get("name", ""), "type": c.get("type", "string"),
                                       "description": c.get("description", "")[:160]} for c in columns],
            "rows": rows, "more": bool((table.get("metadata") or {}).get("continue"))}


def _clean(obj):
    """An object as a person edits it: without the server's bookkeeping."""
    meta = obj.get("metadata") or {}
    meta.pop("managedFields", None)
    annotations = meta.get("annotations") or {}
    annotations.pop("kubectl.kubernetes.io/last-applied-configuration", None)
    if not annotations:
        meta.pop("annotations", None)
    return obj


# Fields that hold what a Secret would - Helm values, inline cloud-init, and
# the copies controllers keep of them - outside a Secret. Homestead's own
# pages show them to admins only (/api/helm/release, /api/vm); here everyone
# below the role that may reveal a Secret sees each field's name and size,
# never its value.
HIDDEN = "•••"
SENSITIVE = {
    ("helm.cattle.io", "HelmChart"): (("spec", "valuesContent"), ("spec", "set"), ("spec", "chartContent")),
    ("helm.cattle.io", "HelmChartConfig"): (("spec", "valuesContent"),),
    ("helm.toolkit.fluxcd.io", "HelmRelease"): (("spec", "values"),),
    # A snapshot of a whole object: KubeVirt keeps each started VM's spec,
    # cloud-init included, and StatefulSets and DaemonSets their pod template.
    ("apps", "ControllerRevision"): (("data",),),
    ("catalog.cattle.io", "App"): (("spec", "values"), ("spec", "chart", "values")),
}
# Found wherever they sit in the object, outside metadata:
# - a KubeVirt cloud-init volume (VM, VMI, a template, a pool, a backup's or
#   snapshot's copy);
CLOUD_INIT = ("cloudInitNoCloud", "cloudInitConfigDrive")
CLOUD_INIT_FIELDS = ("userData", "networkData", "userDataBase64", "networkDataBase64")
# - a "helm" block's values (Argo CD's Application, Fleet's Bundle,
#   BundleDeployment and HelmApp);
HELM_FIELDS = ("values", "valuesObject", "parameters")
# - a Harvester backup's copies of the VM's Secrets, and CDI's download
#   headers (often "Authorization: Bearer ...").
LISTS = {"secretBackups": "data", "extraHeaders": None}
# An annotation that holds a copy of the whole object (k3s'
# objectset.rio.cattle.io/applied, gzipped; kubectl's
# last-applied-configuration; kapp's original): it carries every field masked
# here, a Secret's data included.
APPLIED = re.compile(r"applied|original", re.I)
# What the k3s Helm controller makes from a HelmChart: chart-values-* and
# chart-content-* ConfigMaps and the helm-install Job and its Pods, whose
# arguments and environment carry the values.
CHART_LABEL = "helmcharts.helm.cattle.io/chart"
CHART_CONFIGMAPS = ("chart-values-", "chart-content-")
CONTAINER_LISTS = ("containers", "initContainers", "ephemeralContainers")
# Env a controller copies from a masked field into any Pod: CDI's importer
# gets a DataVolume's extraHeaders.
SECRET_ENV = re.compile(r"IMPORTER_EXTRA_HEADER_")
# ConfigMaps whose whole data is values or cloud-init: a Helm release stored
# with HELM_DRIVER=configmap, and a Harvester cloud-init template.
VALUE_CONFIGMAP_LABELS = (("owner", "helm"), ("harvesterhci.io/cloud-init-template", None))

# The first layer, an allowlist: a role below reveal sees the whole object
# only for the kinds this page groups as built in (CATEGORIES; a Secret's
# data is hidden on its own) and the k3s HelmChart and HelmChartConfig, whose
# every field is known. Any other kind - a custom resource can keep anything
# anywhere - shows its metadata and status, with each other field replaced
# by a note. The masks above apply on top, to status as well.
SAFE_RESOURCES = set().union(*(members for _, members in CATEGORIES)) | {
    ("helm.cattle.io", "helmcharts"), ("helm.cattle.io", "helmchartconfigs")}
SHOWN = ("apiVersion", "kind", "metadata", "status")
# Of such a kind's status, only these generic fields: what the page and a
# person need to see whether it is healthy.
STATUS_FIELDS = ("conditions", "phase", "state", "observedGeneration", "ready", "replicas", "readyReplicas",
                 "availableReplicas", "updatedReplicas", "currentReplicas", "unavailableReplicas")
CONDITION_FIELDS = ("type", "status", "reason", "message", "lastTransitionTime", "lastUpdateTime",
                    "lastProbeTime", "observedGeneration")
# Annotations a role below reveal sees in full, at every depth: those under
# these domains, unless they hold a copy of the object (APPLIED). Any other
# annotation keeps its name and shows its size.
SAFE_ANNOTATION_DOMAINS = ("homestead.io", "kubernetes.io", "k8s.io", "x-k8s.io", "kubevirt.io", "longhorn.io",
                           "harvesterhci.io", "helm.sh", "cattle.io", "cni.cncf.io", "projectcalico.org",
                           "kube-vip.io", "fluxcd.io", "argoproj.io")


def _safe_annotation(key):
    domain = key.split("/", 1)[0] if "/" in key else ""
    return not APPLIED.search(key) and any(domain == d or domain.endswith("." + d) for d in SAFE_ANNOTATION_DOMAINS)


def _note(value, role):
    lines = len(YAML.dump(value).splitlines()) if isinstance(value, (dict, list)) else len(str(value).splitlines() or [""])
    return f"{HIDDEN} (hidden: needs the {role} role, {lines} line{'' if lines == 1 else 's'})"


def _leaves(value):
    """A map's keys and a list's length, with every leaf as HIDDEN."""
    if isinstance(value, dict):
        return {key: _leaves(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_leaves(item) for item in value]
    return HIDDEN


def _masked(value):
    """A field's shape without its content: a text's lines or length, a
    map's or list's structure. Masking twice changes nothing."""
    if isinstance(value, (dict, list)):
        return _leaves(value)
    text = str(value)
    if text.startswith(HIDDEN):
        return text
    if "\n" in text:
        lines = len(text.splitlines())
        return f"{HIDDEN} (hidden, {lines} line{'' if lines == 1 else 's'})"
    return f"{HIDDEN} (hidden, {len(text)} characters)"


def _annotation_fields(metadata, at, keep):
    annotations = metadata.get("annotations") if isinstance(metadata, dict) else None
    if not isinstance(annotations, dict):
        return []
    return [(annotations, key, f"{at}.annotations.{key}", _masked) for key, value in annotations.items()
            if not keep(key) and value not in (None, "")]


def _applied_annotations(obj):
    return _annotation_fields(obj.get("metadata"), "metadata", lambda key: not APPLIED.search(key))


def _from_helm_controller(obj):
    meta = obj.get("metadata") or {}
    spec = obj.get("spec") if isinstance(obj.get("spec"), dict) else {}
    template = spec.get("template") if isinstance(spec.get("template"), dict) else {}
    template = template.get("metadata") if isinstance(template.get("metadata"), dict) else {}
    return (any(str(o.get("apiVersion", "")).startswith("helm.cattle.io/") for o in meta.get("ownerReferences") or [])
            or CHART_LABEL in (meta.get("labels") or {}) or CHART_LABEL in (template.get("labels") or {})
            or (obj.get("kind") == "ConfigMap" and str(meta.get("name", "")).startswith(CHART_CONFIGMAPS)))


def _values_configmap(obj, group):
    if obj.get("kind") != "ConfigMap" or group != "":
        return False
    labels = (obj.get("metadata") or {}).get("labels") or {}
    return _from_helm_controller(obj) or any(key in labels and (value is None or labels[key] == value)
                                             for key, value in VALUE_CONFIGMAP_LABELS)


def _present(value):
    return value not in (None, "", {}, [])


def _sensitive_fields(obj, group):
    """(parent, key, path, mask) of every secret-bearing field the object
    holds; mask(value) is what a role that may not see it gets instead.
    Annotations count unless they are on the safe list, at every depth."""
    found = _annotation_fields(obj.get("metadata"), "metadata", _safe_annotation)
    for path in SENSITIVE.get((group, obj.get("kind")), ()):
        parent = obj
        for step in path[:-1]:
            parent = parent.get(step) if isinstance(parent, dict) else None
        if isinstance(parent, dict) and _present(parent.get(path[-1])):
            found.append((parent, path[-1], ".".join(path), _masked))
    if _values_configmap(obj, group):
        for field in ("data", "binaryData"):
            data = obj.get(field)
            if isinstance(data, dict):
                found.extend((data, key, f"{field}.{key}", _masked) for key, value in data.items() if _present(value))
    helm = _from_helm_controller(obj)
    taken = {(id(parent), key) for parent, key, _, _ in found}

    def fields(parent, names, at, mask=_masked):
        found.extend((parent, name, f"{at}.{name}", mask) for name in names if _present(parent.get(name)))

    def walk(node, where):
        if isinstance(node, dict):
            for key, item in node.items():
                if (id(node), key) in taken or (node is obj and key in ("metadata", "apiVersion", "kind")):
                    continue        # masked whole already, or the object's own name and labels
                if key == "metadata" and isinstance(item, dict):
                    found.extend(_annotation_fields(item, f"{where}{key}", _safe_annotation))
                elif key in CLOUD_INIT and isinstance(item, dict):
                    fields(item, CLOUD_INIT_FIELDS, f"{where}{key}")
                elif key == "helm" and isinstance(item, dict):
                    fields(item, HELM_FIELDS, f"{where}{key}")
                elif key in LISTS and isinstance(item, list):
                    inner = LISTS[key]
                    if inner is None:
                        found.append((node, key, f"{where}{key}", _masked))
                    for index, entry in enumerate(item if inner else ()):
                        if isinstance(entry, dict):
                            fields(entry, (inner,), f"{where}{key}[{index}]")
                elif key in CONTAINER_LISTS and isinstance(item, list):
                    for index, container in enumerate(item):
                        if not isinstance(container, dict):
                            continue
                        at = f"{where}{key}[{index}]"
                        if helm:
                            fields(container, ("command", "args"), at)
                        found.extend((env, "value", f"{at}.env[{i}].value", _leaves)
                                     for i, env in enumerate(container.get("env") or [])
                                     if isinstance(env, dict) and _present(env.get("value"))
                                     and (helm or SECRET_ENV.match(str(env.get("name") or ""))))
                else:
                    walk(item, f"{where}{key}.")
        elif isinstance(node, list):
            for index, item in enumerate(node):
                walk(item, f"{where[:-1]}[{index}].")
    # Everywhere but metadata: a copy can sit in data or status, not only spec.
    walk(obj, "")
    return found


def _hide(fields):
    hidden = []
    for parent, key, path, mask in fields:
        parent[key] = mask(parent[key])
        hidden.append(path)
    return hidden


def hide_sensitive(obj, group):
    """The object with every secret-bearing field masked, in place; returns
    the paths it masked. The second layer, under the safe list."""
    return _hide(_sensitive_fields(obj, group))


def hide_for_low_role(obj, kind, role):
    """What a role below reveal sees, in place; returns the masked paths.
    A kind off the safe list keeps its metadata and status only."""
    hidden = []
    if (kind["group"], kind["resource"]) not in SAFE_RESOURCES:
        for key in [k for k in obj if k not in SHOWN]:
            obj[key] = _note(obj[key], role)
            hidden.append(key)
        status = obj.get("status")
        if isinstance(status, dict):
            # Status can carry credentials too (Rancher's registration
            # token and import command): only generic fields stay.
            for key in [k for k in status if k not in STATUS_FIELDS]:
                status[key] = _note(status[key], role)
                hidden.append(f"status.{key}")
            if isinstance(status.get("conditions"), list):
                status["conditions"] = [{k: v for k, v in c.items() if k in CONDITION_FIELDS} if isinstance(c, dict) else c
                                        for c in status["conditions"]]
        elif status is not None:
            obj["status"] = _note(status, role)
            hidden.append("status")
    return hidden + [path for path in hide_sensitive(obj, kind["group"]) if path not in hidden]


def _has_mask(value):
    """Whether a value holds one of the mask's own forms (not any "•••")."""
    if isinstance(value, dict):
        return any(_has_mask(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_mask(item) for item in value)
    return isinstance(value, str) and (value == HIDDEN or value.startswith(f"{HIDDEN} (hidden"))


def _refuse_masked(obj, group):
    """A masked copy saved back would overwrite the real values with the mask."""
    masked = [key for key, value in obj.items() if key not in ("apiVersion", "kind", "metadata") and _has_mask(value)]
    masked += [path for parent, key, path, _ in _sensitive_fields(obj, group) if _has_mask(parent[key])]
    if masked:
        raise ValueError(f"{masked[0]} is hidden in this copy; open the object as an admin (and reveal a "
                         "Secret) and edit that copy, or the mask would be saved as its value")


def get_object(group, version, resource, namespace, name, reveal=False, see_values=False, reveal_role="admin"):
    """One object as YAML. A Secret's data is masked unless revealed. Unless
    see_values (a role that may reveal) or revealed, a kind off the safe list
    shows only metadata and status, and every secret-bearing field (SENSITIVE,
    cloud-init, annotations off the safe list) is masked."""
    kind = _kind(group, version, resource)
    obj = _clean(kget(_path(kind, namespace, _name(name))))
    if kind["kind"] == "Secret" and kind["group"] == "" and not reveal:
        obj["data"] = {key: "(hidden)" for key in (obj.get("data") or {})}
        obj.pop("stringData", None)
    if not reveal and (obj.get("metadata") or {}).get("annotations"):
        # An outage webhook's address can carry a token (#741).
        obj["metadata"]["annotations"] = OUTAGE.mask_annotations(obj["metadata"]["annotations"])
    hidden = []
    if kind["kind"] == "Secret" and kind["group"] == "" and not reveal:
        # Its applied copy holds the data just hidden: for every role.
        hidden = _hide(_applied_annotations(obj))
    if not reveal and not see_values:
        hidden += [path for path in hide_for_low_role(obj, kind, reveal_role) if path not in hidden]
    return {"kind": kind, "object": obj, "yaml": YAML.dump(obj), "secret_hidden": kind["kind"] == "Secret" and not reveal,
            "values_hidden": bool(hidden), "hidden": hidden}


def events_for(namespace, name, uid=""):
    selector = f"involvedObject.name={name}" + (f",involvedObject.uid={uid}" if uid else "")
    path = (f"/api/v1/namespaces/{urllib.parse.quote(namespace)}/events" if namespace else "/api/v1/events")
    items = kget(f"{path}?fieldSelector={urllib.parse.quote(selector)}").get("items", [])
    rows = [{"type": e.get("type", ""), "reason": e.get("reason", ""), "message": (e.get("message") or "")[:400],
             "count": e.get("count") or 1, "last": e.get("lastTimestamp") or e.get("eventTime") or ""} for e in items]
    return sorted(rows, key=lambda r: r["last"] or "", reverse=True)[:100]


def _parse(text):
    try:
        obj, _ = YAML.loads_untabbed(text)
    except YAML.YamlError as error:
        raise ValueError(f"YAML: {error}")
    obj = json.loads(json.dumps(obj))
    if not isinstance(obj, dict) or not obj.get("apiVersion") or not obj.get("kind"):
        raise ValueError("an object needs apiVersion, kind and metadata.name")
    return obj


def save_object(group, version, resource, namespace, name, text):
    """Replaces the object with the edited YAML. The resourceVersion it was
    read at is kept, so an edit made meanwhile elsewhere is refused, not lost."""
    kind = _kind(group, version, resource)
    obj = _parse(text)
    meta = obj.setdefault("metadata", {})
    if meta.get("name") != name or (kind["namespaced"] and (meta.get("namespace") or namespace) != namespace):
        raise ValueError("the name and namespace cannot change here; create a new object instead")
    if kind["kind"] == "Secret" and any(v == "(hidden)" for v in (obj.get("data") or {}).values()):
        raise ValueError("reveal the secret's values before editing it, or they would be saved as '(hidden)'")
    _refuse_masked(obj, kind["group"])
    if OUTAGE.HIDDEN in str((meta.get("annotations") or {}).get(OUTAGE.NAMES.key(OUTAGE.KEY)) or ""):
        current = (kget(_path(kind, namespace, name)).get("metadata") or {}).get("annotations") or {}
        meta["annotations"] = OUTAGE.unmask_annotations(meta["annotations"], current)
    if not meta.get("resourceVersion"):
        raise ValueError("keep metadata.resourceVersion: it is how a change made elsewhere meanwhile is caught")
    try:
        saved = ksend("PUT", _path(kind, namespace, name), obj)
    except urllib.error.HTTPError as error:
        raise ValueError(_refusal(error))
    return {"ok": True, "detail": f"{kind['kind']} {name} saved", "resourceVersion": (saved.get("metadata") or {}).get("resourceVersion", "")}


def delete_object(group, version, resource, namespace, name):
    kind = _kind(group, version, resource)
    try:
        ksend("DELETE", _path(kind, namespace, _name(name)))
    except urllib.error.HTTPError as error:
        raise ValueError(_refusal(error))
    return {"ok": True, "detail": f"{kind['kind']} {name} deleted"}


def create_objects(text, default_namespace="default"):
    """Creates every object in the YAML, in order, stopping at the first refusal."""
    docs = [d for d in re.split(r"^---[ \t]*$", text or "", flags=re.M) if d.strip() and not re.fullmatch(r"(\s*#.*\n?)*\s*", d)]
    if not docs:
        raise ValueError("paste one or more objects")
    for doc in docs:
        # Before anything is created: a masked copy would plant the mask.
        obj = _parse(doc)
        _refuse_masked(obj, obj["apiVersion"].rpartition("/")[0])
    kinds = discover()
    made = []
    for doc in docs:
        obj = _parse(doc)
        api_version = obj["apiVersion"]
        group, _, version = api_version.rpartition("/")
        kind = next((k for k in kinds if k["group"] == group and k["kind"] == obj["kind"]), None)
        if not kind:
            raise ValueError(f"the cluster serves no {obj['kind']} in {api_version}" + (f" (created: {', '.join(made)})" if made else ""))
        kind = {**kind, "version": version}
        meta = obj.setdefault("metadata", {})
        namespace = meta.get("namespace") or (default_namespace if kind["namespaced"] else "")
        if kind["namespaced"]:
            meta["namespace"] = namespace
        try:
            ksend("POST", _path(kind, namespace), obj)
        except urllib.error.HTTPError as error:
            raise ValueError(f"{obj['kind']} {meta.get('name', '')}: {_refusal(error)}" + (f" (created: {', '.join(made)})" if made else ""))
        made.append(f"{obj['kind']} {meta.get('name') or meta.get('generateName', '')}")
    return {"ok": True, "created": made, "detail": f"created {', '.join(made)}"}


def _refusal(error):
    try:
        body = json.loads(error.read().decode("utf-8", "replace"))
        return body.get("message") or f"the API server refused ({error.code})"
    except Exception:
        return f"the API server refused ({error.code})"


# Its routes and who may use them (homestead_routes.py).
def _save(b):
    _guard[0](b.get("resource"), b.get("ns"), b.get("name"))
    return save_object(b.get("group", ""), b.get("version", ""), b.get("resource", ""),
                       b.get("ns", ""), b.get("name", ""), b.get("yaml", ""))


def _delete(b):
    _guard[0](b.get("resource"), b.get("ns"), b.get("name"))
    return delete_object(b.get("group", ""), b.get("version", ""), b.get("resource", ""), b.get("ns", ""), b.get("name", ""))


def _create(b):
    for document in re.split(r"^---[ \t]*$", b.get("yaml", ""), flags=re.M):
        if document.strip():
            obj = _parse(document)
            meta = obj.get("metadata") or {}
            _guard[0](obj.get("kind"), meta.get("namespace") or b.get("ns") or _default_ns[0], meta.get("name"))
    return create_objects(b.get("yaml", ""), b.get("ns") or _default_ns[0])


ROUTES = {
    ("POST", "/api/resources/save"): ("admin", lambda request: _save(request.body)),
    ("POST", "/api/resources/delete"): ("admin", lambda request: _delete(request.body)),
    ("POST", "/api/resources/create"): ("admin", lambda request: _create(request.body)),
    ("GET", "/api/resources/kinds"): ("viewer", lambda request: discover(force=(request.query.get("force") or [""])[0] == "1")),
    ("GET", "/api/resources/events"): ("viewer", lambda request: events_for((request.query.get("ns") or [""])[0], (request.query.get("name") or [""])[0], (request.query.get("uid") or [""])[0])),
}
