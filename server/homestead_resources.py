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


def bind(_kget, _ksend, _table_get):
    global kget, ksend, table_get
    kget, ksend, table_get = _kget, _ksend, _table_get


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


def get_object(group, version, resource, namespace, name, reveal=False):
    kind = _kind(group, version, resource)
    obj = _clean(kget(_path(kind, namespace, _name(name))))
    if kind["kind"] == "Secret" and kind["group"] == "" and not reveal:
        obj["data"] = {key: "(hidden)" for key in (obj.get("data") or {})}
        obj.pop("stringData", None)
    return {"kind": kind, "object": obj, "yaml": YAML.dump(obj), "secret_hidden": kind["kind"] == "Secret" and not reveal}


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
