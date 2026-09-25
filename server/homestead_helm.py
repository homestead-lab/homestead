"""Helm: every release in the cluster, and charts installed the RKE2 way.

Reading. Helm keeps each revision of each release in a Secret labelled
owner=helm, gzipped JSON inside base64 inside base64. That record is the
same whoever ran Helm - a person, Rancher, Fleet, RKE2's Helm controller - so
reading it shows every release: its chart and version, status, the values it
was installed with, its notes, its history, and the objects it made.

Writing. Harvester runs on RKE2, which ships the Helm controller: a HelmChart
object (helm.cattle.io/v1) names a repository, chart, version and values, and
the controller installs it, upgrades it when the object changes, and
uninstalls it when the object goes. Homestead installs charts that way rather
than carrying Helm itself, so a chart it installed is an ordinary HelmChart
anyone can see with kubectl. Releases made some other way are shown, not
changed: Homestead does not know what else would fight over them.

Charts are found through Artifact Hub's public API.
"""
import base64
import gzip
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

import homestead_names as NAMES

kget = ksend = None
CONTROLLER_NS = "kube-system"
PREVIOUS_SPEC = NAMES.key("helm-previous")
HUB = "https://artifacthub.io/api/v1"
SYSTEM_PREFIXES = ("cattle-", "harvester-", "longhorn-", "kube-", "fleet-", "rancher-", "cis-operator")
_cache = {}
fetch = None


def bind(_kget, _ksend, _fetch=None):
    global kget, ksend, fetch
    kget, ksend = _kget, _ksend
    fetch = _fetch or _hub_get


def _hub_get(url, raw=False):
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "homestead"})
    with urllib.request.urlopen(request, timeout=15) as response:
        body = response.read().decode("utf-8", "replace")
    return body if raw else json.loads(body or "{}")


def decode(secret):
    """A Helm release record from its Secret."""
    raw = base64.b64decode((secret.get("data") or {}).get("release", ""))
    inner = base64.b64decode(raw)
    if inner[:2] == b"\x1f\x8b":
        inner = gzip.decompress(inner)
    return json.loads(inner.decode("utf-8"))


def _system(namespace, name):
    return namespace.startswith(SYSTEM_PREFIXES) or namespace in ("kube-system", "cdi", "local") or \
        name.startswith(("harvester", "rancher", "fleet", "longhorn", "rke2-", "kubevirt"))


def helmcharts():
    try:
        return kget("/apis/helm.cattle.io/v1/helmcharts").get("items", [])
    except Exception:
        return []


def _chart_index(charts):
    """HelmChart objects by the release they make: (target namespace, name)."""
    index = {}
    for item in charts:
        meta, spec = item.get("metadata") or {}, item.get("spec") or {}
        index[(spec.get("targetNamespace") or meta.get("namespace", ""), meta.get("name", ""))] = item
    return index


def _row(record, secret_meta, charts):
    chart = (record.get("chart") or {}).get("metadata") or {}
    info = record.get("info") or {}
    ns, name = record.get("namespace", ""), record.get("name", "")
    helmchart = charts.get((ns, name))
    labels = (helmchart or {}).get("metadata", {}).get("labels") or {}
    managed = ("homestead" if helmchart and labels.get(NAMES.key("managed")) == "true" else
               "helmchart" if helmchart else "")
    return {"name": name, "namespace": ns, "chart": chart.get("name", ""), "chart_version": chart.get("version", ""),
            "app_version": chart.get("appVersion", ""), "icon": chart.get("icon", ""),
            "description": chart.get("description", ""), "status": info.get("status", ""),
            "revision": int(record.get("version") or 0), "updated": info.get("last_deployed", ""),
            "managed": managed, "helmchart": (helmchart or {}).get("metadata", {}).get("namespace", ""),
            "system": _system(ns, name)}


def releases():
    """The newest revision of every release."""
    secrets = kget("/api/v1/secrets?labelSelector=owner%3Dhelm").get("items", [])
    newest = {}
    for secret in secrets:
        labels = (secret.get("metadata") or {}).get("labels") or {}
        key = (secret["metadata"].get("namespace", ""), labels.get("name", ""))
        version = int(labels.get("version") or 0)
        if key not in newest or version > newest[key][0]:
            newest[key] = (version, secret)
    charts = _chart_index(helmcharts())
    rows = []
    for (ns, name), (_, secret) in newest.items():
        try:
            rows.append(_row(decode(secret), secret["metadata"], charts))
        except (ValueError, OSError, KeyError):
            rows.append({"name": name, "namespace": ns, "status": "unreadable", "revision": 0, "chart": "",
                         "chart_version": "", "app_version": "", "managed": "", "system": _system(ns, name)})
    # Charts asked for whose release does not exist yet: installing, or failing.
    for (ns, name), item in charts.items():
        if (ns, name) not in newest:
            spec = item.get("spec") or {}
            labels = item.get("metadata", {}).get("labels") or {}
            rows.append({"name": name, "namespace": ns, "chart": spec.get("chart", ""), "chart_version": spec.get("version", ""),
                         "app_version": "", "status": "pending-install", "revision": 0, "updated": "",
                         "managed": "homestead" if labels.get(NAMES.key("managed")) == "true" else "helmchart",
                         "helmchart": item["metadata"].get("namespace", ""), "system": _system(ns, name),
                         "job": (item.get("status") or {}).get("jobName", "")})
    return sorted(rows, key=lambda r: (r["system"], r["namespace"], r["name"]))


def _objects(manifest):
    """The objects a release made, from its rendered manifest."""
    out = []
    for doc in re.split(r"^---\s*$", manifest or "", flags=re.M):
        kind = re.search(r"^kind:\s*(\S+)", doc, re.M)
        name = re.search(r"^metadata:\s*\n(?:[ \t]+.*\n)*?[ \t]+name:\s*[\"']?([^\"'\s]+)", doc, re.M)
        namespace = re.search(r"^metadata:\s*\n(?:[ \t]+.*\n)*?[ \t]+namespace:\s*[\"']?([^\"'\s]+)", doc, re.M)
        source = re.search(r"^# Source:\s*(\S+)", doc, re.M)
        if kind and name:
            out.append({"kind": kind.group(1), "name": name.group(1),
                        "namespace": namespace.group(1) if namespace else "", "source": source.group(1) if source else ""})
    return out


def release(namespace, name):
    secrets = kget(f"/api/v1/namespaces/{urllib.parse.quote(namespace)}/secrets?labelSelector="
                   f"{urllib.parse.quote(f'owner=helm,name={name}')}").get("items", [])
    if not secrets:
        raise ValueError(f"no Helm release {namespace}/{name}")
    records = sorted((decode(s) for s in secrets), key=lambda r: int(r.get("version") or 0), reverse=True)
    latest = records[0]
    charts = _chart_index(helmcharts())
    row = _row(latest, {}, charts)
    helmchart = charts.get((namespace, name))
    return {**row, "notes": (latest.get("info") or {}).get("notes", "")[:20000],
            "values": _yaml_dump(latest.get("config") or {}),
            "chart_values_keys": sorted((latest.get("chart") or {}).get("values", {}) or {})[:60],
            "history": [{"revision": int(r.get("version") or 0), "status": (r.get("info") or {}).get("status", ""),
                         "updated": (r.get("info") or {}).get("last_deployed", ""),
                         "chart_version": ((r.get("chart") or {}).get("metadata") or {}).get("version", ""),
                         "app_version": ((r.get("chart") or {}).get("metadata") or {}).get("appVersion", ""),
                         "description": (r.get("info") or {}).get("description", "")} for r in records],
            "objects": _objects(latest.get("manifest", ""))[:500],
            "source": ({"repo": (helmchart.get("spec") or {}).get("repo", ""),
                        "chart": (helmchart.get("spec") or {}).get("chart", ""),
                        "version": (helmchart.get("spec") or {}).get("version", ""),
                        "values": (helmchart.get("spec") or {}).get("valuesContent", "")} if helmchart else None)}


def _yaml_dump(value, indent=0):
    """Values as YAML, enough to read and to edit back: Helm's own values are
    plain maps, lists and scalars."""
    pad = "  " * indent
    if isinstance(value, dict):
        if not value:
            return "{}" if indent == 0 else " {}"
        lines = []
        for key, item in value.items():
            if isinstance(item, (dict, list)) and item:
                lines.append(f"{pad}{_scalar(key)}:\n{_yaml_dump(item, indent + 1)}")
            else:
                lines.append(f"{pad}{_scalar(key)}: {_scalar(item) if not isinstance(item, (dict, list)) else ('{}' if isinstance(item, dict) else '[]')}")
        return "\n".join(lines)
    if isinstance(value, list):
        lines = []
        for item in value:
            if isinstance(item, (dict, list)) and item:
                body = _yaml_dump(item, indent + 1).lstrip()
                lines.append(f"{pad}- {body}")
            else:
                lines.append(f"{pad}- {_scalar(item)}")
        return "\n".join(lines)
    return f"{pad}{_scalar(value)}"


def _scalar(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return json.dumps(value)
    text = str(value)
    if text == "" or re.search(r"[:#\[\]{},&*!|>'\"%@`\n]|^[\s-]|\s$", text) or \
            text.lower() in ("true", "false", "null", "yes", "no", "on", "off", "~") or re.fullmatch(r"[-+]?[\d.]+(e[-+]?\d+)?", text):
        return json.dumps(text)
    return text


# ------------------------------------------------------------------ installing
def _dns(value, label):
    value = str(value or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,51}[a-z0-9])?", value):
        raise ValueError(f"the {label} must be lowercase letters, numbers and dashes, at most 53 long")
    return value


def install(cfg):
    name = _dns(cfg.get("name"), "release name")
    namespace = _dns(cfg.get("namespace"), "namespace")
    repo = str(cfg.get("repo") or "").strip()
    chart = str(cfg.get("chart") or "").strip()
    if not re.fullmatch(r"https?://\S+", repo) and not repo.startswith("oci://"):
        raise ValueError("the repository is an https:// chart repository address, or oci://")
    if not re.fullmatch(r"[A-Za-z0-9._/-]{1,120}", chart):
        raise ValueError("name the chart, e.g. grafana")
    version = str(cfg.get("version") or "").strip()
    if version and not re.fullmatch(r"[A-Za-z0-9.+_-]{1,60}", version):
        raise ValueError("that is not a chart version")
    values = str(cfg.get("values") or "")
    if len(values) > 200000:
        raise ValueError("the values are too long")
    if any((spec.get("targetNamespace"), meta.get("name")) == (namespace, name)
           for meta, spec in ((i.get("metadata") or {}, i.get("spec") or {}) for i in helmcharts())):
        raise ValueError(f"{name} is already a chart in {namespace}")
    spec = {"repo": repo, "chart": chart, "targetNamespace": namespace, "createNamespace": True,
            "valuesContent": values}
    if repo.startswith("oci://"):
        spec.pop("repo")
        spec["chart"] = f"{repo.rstrip('/')}/{chart}"
    if version:
        spec["version"] = version
    body = {"apiVersion": "helm.cattle.io/v1", "kind": "HelmChart",
            "metadata": {"name": name, "namespace": CONTROLLER_NS, "labels": {NAMES.key("managed"): "true"}},
            "spec": spec}
    ksend("POST", f"/apis/helm.cattle.io/v1/namespaces/{CONTROLLER_NS}/helmcharts", body)
    return {"ok": True, "name": name, "namespace": namespace,
            "detail": f"{chart} is being installed as {name} in {namespace} by the Helm controller"}


def _helmchart(namespace, name):
    for item in helmcharts():
        meta, spec = item.get("metadata") or {}, item.get("spec") or {}
        if (spec.get("targetNamespace") or meta.get("namespace")) == namespace and meta.get("name") == name:
            return item
    raise ValueError(f"{namespace}/{name} was not installed through a HelmChart, so Homestead leaves it alone")


def upgrade(cfg):
    namespace, name = str(cfg.get("namespace") or ""), str(cfg.get("name") or "")
    item = _helmchart(namespace, name)
    spec = item.setdefault("spec", {})
    # What it was, on the chart itself, so cancelling the upgrade can put it
    # back. It holds nothing the chart's own spec does not already show.
    item.setdefault("metadata", {}).setdefault("annotations", {})[PREVIOUS_SPEC] = json.dumps(
        {key: spec[key] for key in ("version", "valuesContent") if key in spec}, separators=(",", ":"))
    if "version" in cfg:
        version = str(cfg.get("version") or "").strip()
        if version and not re.fullmatch(r"[A-Za-z0-9.+_-]{1,60}", version):
            raise ValueError("that is not a chart version")
        if version:
            spec["version"] = version
        else:
            spec.pop("version", None)
    if "values" in cfg:
        spec["valuesContent"] = str(cfg.get("values") or "")
    ksend("PUT", f"/apis/helm.cattle.io/v1/namespaces/{item['metadata']['namespace']}/helmcharts/{name}", item)
    return {"ok": True, "detail": f"{name} is being upgraded by the Helm controller"}


def restore_previous(item):
    """Put a chart's version and values back as they were before its last
    upgrade; False when none were kept."""
    annotations = (item.get("metadata") or {}).get("annotations") or {}
    try:
        previous = json.loads(annotations.get(PREVIOUS_SPEC) or "")
    except ValueError:
        return False
    if not isinstance(previous, dict):
        return False
    spec = item.setdefault("spec", {})
    for key in ("version", "valuesContent"):
        if key in previous:
            spec[key] = previous[key]
        else:
            spec.pop(key, None)
    annotations.pop(PREVIOUS_SPEC, None)
    item["metadata"].pop("managedFields", None)
    ksend("PUT", f"/apis/helm.cattle.io/v1/namespaces/{item['metadata']['namespace']}/helmcharts/"
                 f"{item['metadata']['name']}", item)
    return True


def uninstall(namespace, name):
    item = _helmchart(namespace, name)
    ksend("DELETE", f"/apis/helm.cattle.io/v1/namespaces/{item['metadata']['namespace']}/helmcharts/{name}")
    return {"ok": True, "detail": f"{name} is being uninstalled; volumes its chart kept are left in place"}


# ------------------------------------------------------------------ finding charts
def search(query):
    query = str(query or "").strip()
    if len(query) < 2:
        return []
    key = ("search", query.lower())
    if key in _cache and time.time() - _cache[key][0] < 600:
        return _cache[key][1]
    found = fetch(f"{HUB}/packages/search?{urllib.parse.urlencode({'ts_query_web': query, 'kind': 0, 'limit': 20, 'offset': 0})}")
    rows = []
    for pkg in found.get("packages") or []:
        repo = pkg.get("repository") or {}
        rows.append({"name": pkg.get("name", ""), "version": pkg.get("version", ""), "app_version": pkg.get("app_version", ""),
                     "description": (pkg.get("description") or "")[:240], "repo": repo.get("url", ""),
                     "repo_name": repo.get("name", ""), "publisher": repo.get("display_name") or repo.get("organization_display_name") or "",
                     "verified": bool(repo.get("verified_publisher")), "official": bool(pkg.get("official") or repo.get("official")),
                     "stars": pkg.get("stars", 0), "package_id": pkg.get("package_id", ""),
                     "logo": f"https://artifacthub.io/image/{pkg['logo_image_id']}" if pkg.get("logo_image_id") else ""})
    _cache[key] = (time.time(), rows)
    return rows


def chart(repo_name, name):
    """A chart's versions and its default values, for the install form."""
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", str(repo_name)) or not re.fullmatch(r"[A-Za-z0-9._-]{1,120}", str(name)):
        raise ValueError("no such chart")
    pkg = fetch(f"{HUB}/packages/helm/{repo_name}/{name}")
    versions = [v.get("version") for v in pkg.get("available_versions") or [] if v.get("version")]
    values = ""
    try:
        values = fetch(f"{HUB}/packages/{pkg['package_id']}/{pkg['version']}/values", raw=True)
    except Exception:
        pass
    return {"name": pkg.get("name", name), "version": pkg.get("version", ""), "versions": versions[:60],
            "repo": (pkg.get("repository") or {}).get("url", ""), "values": values[:200000],
            "readme_url": f"https://artifacthub.io/packages/helm/{repo_name}/{name}"}


def job_status(item):
    """The Helm controller's install or upgrade job, for the job tray."""
    ref = item["ref"]
    try:
        job = kget(f"/apis/batch/v1/namespaces/{ref['namespace']}/jobs/{ref['name']}")
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return "running", 5, "Waiting for the Helm controller"
        raise
    status = job.get("status") or {}
    if status.get("succeeded"):
        return "succeeded", 100, "Helm finished"
    if status.get("failed") and not status.get("active"):
        return "failed", 100, f"Helm failed; the {ref['name']} job's log in {ref['namespace']} says why"
    return "running", 40 if status.get("active") else 10, "Helm is running" if status.get("active") else "Starting Helm"
