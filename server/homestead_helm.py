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
import collections
import gzip
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import homestead_fetch as FETCH
import homestead_names as NAMES
import homestead_value_masks as MASKS
import homestead_routes

kget = ksend = None
CONTROLLER_NS = "kube-system"
PREVIOUS_SPEC = NAMES.key("helm-previous")
HUB = "https://artifacthub.io/api/v1"
SYSTEM_PREFIXES = ("cattle-", "harvester-", "longhorn-", "kube-", "fleet-", "rancher-", "cis-operator")
# Searches kept, least recently used first: each distinct query was kept for
# ever, so a script (or a lot of typing) grew it without end.
_cache = collections.OrderedDict()
_cache_lock = threading.Lock()
SEARCH_MAX = 256
SEARCH_SECONDS = 600
QUERY_MAX = 100
# An Artifact Hub answer: a search page or a package is tens of KiB, a chart's
# values file rarely more than a few hundred.
HUB_MAX_BYTES = 4 << 20
HUB_DEADLINE = 30
fetch = None


def bind(_kget, _ksend, _fetch=None):
    global kget, ksend, fetch
    kget, ksend = _kget, _ksend
    fetch = _fetch or _hub_get


def _hub_get(url, raw=False):
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "homestead"})
    with urllib.request.urlopen(request, timeout=15) as response:
        body = FETCH.read(response, HUB_MAX_BYTES, time.monotonic() + HUB_DEADLINE,
                          "Artifact Hub's answer").decode("utf-8", "replace")
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
            # Shared by every role (cached): a private chart address keeps no login.
            rows.append({"name": name, "namespace": ns, "chart": MASKS.strip_url_secrets(spec.get("chart", "")),
                         "chart_version": spec.get("version", ""),
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


def release(namespace, name, include_sensitive=True):
    secrets = kget(f"/api/v1/namespaces/{urllib.parse.quote(namespace)}/secrets?labelSelector="
                   f"{urllib.parse.quote(f'owner=helm,name={name}')}").get("items", [])
    if not secrets:
        raise ValueError(f"no Helm release {namespace}/{name}")
    records = sorted((decode(s) for s in secrets), key=lambda r: int(r.get("version") or 0), reverse=True)
    latest = records[0]
    charts = _chart_index(helmcharts())
    row = _row(latest, {}, charts)
    helmchart = charts.get((namespace, name))
    result = {**row, "notes": (latest.get("info") or {}).get("notes", "")[:20000],
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
                        "values": (helmchart.get("spec") or {}).get("valuesContent", ""),
                        # Sent back with Upgrade, so it never lands on a chart changed since.
                        "generation": (helmchart.get("metadata") or {}).get("generation", "")} if helmchart else None)}
    if not include_sensitive:
        for key in ("notes", "values", "chart_values_keys"):
            result.pop(key, None)
        for item in result["history"]:
            item.pop("description", None)
        if result["source"]:
            result["source"].pop("values", None)
            # A private repository's address can carry its login.
            MASKS.strip_in_place(result["source"])
        result["sensitive_hidden"] = True
    return result


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
    earlier = _earlier_job(CONTROLLER_NS, f"helm-install-{name}")
    ksend("POST", f"/apis/helm.cattle.io/v1/namespaces/{CONTROLLER_NS}/helmcharts", body)
    return {"ok": True, "name": name, "namespace": namespace,
            "detail": f"{chart} is being installed as {name} in {namespace} by the Helm controller",
            "chart": {"earlier_job": earlier}}


def _earlier_job(namespace, job):
    """The uid of a Job already there before a write, or "". A chart's last Job sits
    there for weeks: the job must not read it as the one its write causes, and the
    controller replaces it, so the new one has another uid. A failed read raises,
    before anything is written."""
    try:
        return (kget(f"/apis/batch/v1/namespaces/{namespace}/jobs/{job}").get("metadata") or {}).get("uid", "")
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        return ""


def _helmchart(namespace, name):
    for item in helmcharts():
        meta, spec = item.get("metadata") or {}, item.get("spec") or {}
        if (spec.get("targetNamespace") or meta.get("namespace")) == namespace and meta.get("name") == name:
            return item
    raise ValueError(f"{namespace}/{name} was not installed through a HelmChart, so Homestead leaves it alone")


def upgrade(cfg):
    namespace, name = str(cfg.get("namespace") or ""), str(cfg.get("name") or "")
    item = _helmchart(namespace, name)
    # The dialog sends the generation it read, so an upgrade never puts back
    # a version or values changed since it opened (by another admin, kubectl
    # or GitOps). Not resourceVersion: the Helm controller's status writes
    # move that, and would refuse every upgrade while it works.
    read = cfg.get("generation")
    now = (item.get("metadata") or {}).get("generation", "")
    if read is not None and str(read) != str(now):
        raise ValueError(f"{name} changed since this dialog opened. Open it again to see the change, then upgrade.")
    spec = item.setdefault("spec", {})
    before = settings(item)
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
    home = item["metadata"]["namespace"]
    # The same settings again write nothing: the controller would make no new Job
    # for them, and keeping them as the earlier ones would lose the good settings
    # for the ones that failed.
    if settings(item) == before:
        return {"ok": True, "detail": no_change_words(name), "chart": {"namespace": home, "no_change": True}}
    # What it was, on the chart itself, so cancelling the upgrade can put it
    # back. It holds nothing the chart's own spec does not already show.
    item.setdefault("metadata", {}).setdefault("annotations", {})[PREVIOUS_SPEC] = json.dumps(before, separators=(",", ":"))
    earlier = _earlier_job(home, f"helm-install-{name}")
    written = (ksend("PUT", f"/apis/helm.cattle.io/v1/namespaces/{home}/helmcharts/{name}", item) or {}).get("metadata") or {}
    # Which chart this upgrade wrote, for the job: what it may put back later is
    # this one, as this upgrade left it. Only spec changes move the generation.
    return {"ok": True, "detail": f"{name} is being upgraded by the Helm controller",
            "chart": {"namespace": home, "chart_uid": written.get("uid", ""), "chart_generation": written.get("generation"),
                      "earlier_job": earlier}}


def no_change_words(name):
    return f"This upgrade changed none of {name}'s settings, so nothing ran. If its last upgrade failed, it still has"


def settings(item):
    """A chart's version and values: what an upgrade changes and a put-back returns.
    Empty counts as absent: the dialog sends values "" for a chart with none."""
    spec = item.get("spec") or {}
    return {key: spec[key] for key in ("version", "valuesContent") if spec.get(key)}


def previous_spec(item):
    """The version and values kept on a chart before its last upgrade, or None
    when none were kept or they cannot be read."""
    annotations = (item.get("metadata") or {}).get("annotations") or {}
    try:
        previous = json.loads(annotations.get(PREVIOUS_SPEC) or "")
    except ValueError:
        return None
    return previous if isinstance(previous, dict) else None


def restore_previous(item):
    """Put a chart's version and values back as they were before its last
    upgrade; False when none were kept."""
    previous = previous_spec(item)
    if previous is None:
        return False
    annotations = item["metadata"]["annotations"]
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
    home = item["metadata"]["namespace"]
    earlier = _earlier_job(home, f"helm-delete-{name}")
    ksend("DELETE", f"/apis/helm.cattle.io/v1/namespaces/{home}/helmcharts/{name}")
    # A finalizer means the controller took the chart up, and holds it until its
    # uninstall has run: then the chart going is proof that it did. The uid tells
    # that chart from one made again under its name.
    meta = item.get("metadata") or {}
    return {"ok": True, "detail": f"{name} is being uninstalled. Volumes its chart kept are left in place.",
            "chart": {"namespace": home, "chart_uid": meta.get("uid", ""), "uninstall_seen": bool(meta.get("finalizers")),
                      "earlier_job": earlier}}


# ------------------------------------------------------------------ finding charts
def search(query):
    query = str(query or "").strip()
    if not 2 <= len(query) <= QUERY_MAX:
        return []
    key = ("search", query.lower())
    with _cache_lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < SEARCH_SECONDS:
            _cache.move_to_end(key)
            return hit[1]
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
    with _cache_lock:
        _cache[key] = (time.time(), rows)
        _cache.move_to_end(key)
        while len(_cache) > SEARCH_MAX:
            _cache.popitem(last=False)
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


# How long a Helm job may have no Job to follow. The controller makes one within
# seconds and deletes one only to make it again, so this is a controller that is
# not there, or a Job someone removed. The clock is a seam for the tests.
JOB_MISSING_LIMIT = 15 * 60
_clock = time.time


def job_action(item):
    """What a Helm job does. Jobs from older releases have no action: the job's
    name and the title say."""
    ref = item["ref"]
    if ref.get("action") in ("install", "upgrade", "uninstall"):
        return ref["action"]
    if str(ref.get("name", "")).startswith("helm-delete-"):
        return "uninstall"
    return "upgrade" if str(item.get("title", "")).startswith("Helm upgrade ") else "install"


def _chart_gone(item, ref):
    """This job's chart, if a read shows no such chart, or an uninstall's chart has
    been made again: a failed read raises, and a 404 from no Helm API at all says
    nothing about the chart."""
    name = (item.get("resource") or {}).get("name") or ""
    charts = f"/apis/helm.cattle.io/v1/namespaces/{ref['namespace']}/helmcharts"
    try:
        chart = kget(f"{charts}/{name}")
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        try:
            kget(charts)
        except urllib.error.HTTPError as listing:
            if listing.code != 404:
                raise
            return ""
        return name
    meta = chart.get("metadata") or {}
    if job_action(item) == "uninstall":
        if ref.get("chart_uid") and meta.get("uid") != ref["chart_uid"]:
            ref["chart_made_again"] = True
            return name
        if meta.get("deletionTimestamp"):
            ref["uninstall_seen"] = True
    return ""


def _no_job(item, ref, earlier=False):
    """What a Helm job with no Job to follow comes to; earlier: the only Job there
    is from before its write. An uninstall that worked ends here: the controller
    removes its Job, then lets the chart go. Only what this job saw of that says
    it worked."""
    gone = _chart_gone(item, ref)
    if gone:
        action = job_action(item)
        if action == "uninstall":
            again = ref.get("chart_made_again")
            if ref.get("uninstall_seen"):
                return "succeeded", 100, f"Helm removed {gone}" + (". A chart with that name has been created since" if again else "")
            return "failed", 5, (f"The HelmChart {gone} {'was removed and made again' if again else 'is gone'}, but Homestead "
                                 "did not see its uninstall run. The Helm page shows whether its release is still there")
        ref["chart_gone"] = True   # the job list reads no chart: it offers nothing to put back on this
        return "failed", 5, f"The HelmChart {gone} is gone, so nothing will run this {action}"
    now = _clock()
    if now - ref.setdefault("job_missing_since", now) < JOB_MISSING_LIMIT:
        return "running", 5, "Waiting for the Helm controller"
    what = (f"did not replace its earlier job {ref['name']} in {ref['namespace']} within" if earlier
            else f"had no job {ref['name']} in {ref['namespace']} for")
    return "failed", 5, (f"The Helm controller {what} {JOB_MISSING_LIMIT // 60} minutes, so Homestead has stopped "
                         "following it. Check that the controller is running. The Helm page shows what is installed.")


def job_failed(job):
    """Whether the Job itself has ended failed. Its `failed` count does not say: it
    counts lost pods, and the Job goes on making new ones. The controller's Jobs
    restart a failing container in its own pod, so a chart that fails adds nothing to
    that count; only this condition covers both. Platform upgrades read it too."""
    return any(condition.get("type") == "Failed" and condition.get("status") == "True"
               for condition in (job.get("status") or {}).get("conditions") or [])


# Container restarts in the Job's own pod that read as a chart that keeps failing,
# not a single slip: the kubelet's back-off makes these minutes apart.
FAILING_RESTARTS = 3
# Failed tries after which a Helm job ends as failed, so its put-back is offered
# (Andreas, 10 Nov). The Job itself is left alone and Kubernetes keeps trying it.
GIVE_UP_RESTARTS = 5


def failing_words(tries, job_name, namespace):
    return (f"Helm keeps failing: {tries} tries so far, and Kubernetes keeps trying. "
            f"The log of the {job_name} job in {namespace} says why.")


def restarts(namespace, job_name, job, read=None):
    """How many times the Job's container has failed in its pod, or 0. The Job writes
    Failed only at its backoffLimit (1000 restarts, about 3.5 days at the kubelet's
    5-minute cap), so only the pod shows a chart that keeps failing. A pod of another
    Job of that name does not count, and a pod read that fails says nothing."""
    uid = (job.get("metadata") or {}).get("uid")
    selector = urllib.parse.quote(f"job-name={job_name}", safe="")
    try:
        pods = (read or kget)(f"/api/v1/namespaces/{namespace}/pods?labelSelector={selector}").get("items") or []
    except (urllib.error.HTTPError, OSError, ValueError):
        return 0
    most = 0
    for pod in pods:
        owners = (pod.get("metadata") or {}).get("ownerReferences") or []
        if not uid or not any(owner.get("uid") == uid for owner in owners):
            continue
        for container in (pod.get("status") or {}).get("containerStatuses") or []:
            most = max(most, _failures(container))
    return most


def _failures(container):
    """Failed tries of one container. Kubernetes counts a restart when the new container
    starts: while it runs, its restarts are its failures so far; while it waits in
    back-off after a failure, or has just failed, that failure is one more. Only a
    non-zero exit is a failure."""
    count = int(container.get("restartCount") or 0)
    state = container.get("state") or {}
    last = (container.get("lastState") or {}).get("terminated") or {}
    if (state.get("terminated") or {}).get("exitCode"):
        return count + 1
    if not last.get("exitCode"):
        return 0
    return count + 1 if state.get("waiting") else count


def job_status(item):
    """The Helm controller's install or upgrade job, for the job tray."""
    ref = item["ref"]
    if ref.get("no_change"):   # nothing was written, so no Job is this upgrade's
        return "succeeded", 100, no_change_words((item.get("resource") or {}).get("name") or "")
    try:
        job = kget(f"/apis/batch/v1/namespaces/{ref['namespace']}/jobs/{ref['name']}")
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return _no_job(item, ref)
        raise
    # A job from an older Homestead has no such record, and follows the Job it finds.
    if ref.get("earlier_job") and (job.get("metadata") or {}).get("uid") == ref["earlier_job"]:
        return _no_job(item, ref, earlier=True)
    ref.pop("job_missing_since", None)
    if job_action(item) == "uninstall":
        ref["uninstall_seen"] = True
    status = job.get("status") or {}
    if status.get("succeeded"):
        return "succeeded", 100, "Helm finished"
    if job_failed(job):
        return "failed", 100, f"Helm failed. The log of the {ref['name']} job in {ref['namespace']} says why."
    if status.get("active"):
        tries = restarts(ref["namespace"], ref["name"], job)
        if tries >= GIVE_UP_RESTARTS:
            return "failed", 100, (f"Helm failed {tries} times, so Homestead stopped waiting for it. Kubernetes keeps "
                                   f"trying it. The log of the {ref['name']} job in {ref['namespace']} says why.")
        if tries >= FAILING_RESTARTS:
            return "running", 40, failing_words(tries, ref["name"], ref["namespace"])
        return "running", 40, "Helm is running"
    if status.get("failed"):
        return "running", 10, "A Helm pod failed and Kubernetes is trying again"
    return "running", 10, "Starting Helm"


# Its routes and who may use them (homestead_routes.py).
ROUTES = {
    ("GET", "/api/helm/search"): ("viewer", lambda request: search((request.query.get("q") or [""])[0])),
    ("GET", "/api/helm/chart"): ("viewer", lambda request: chart((request.query.get("repo") or [""])[0], (request.query.get("name") or [""])[0])),
    ("GET", "/api/helm"): ("viewer", lambda request: homestead_routes.cached(
        "helm", homestead_routes.PAGE_SECONDS, releases)),
    ("GET", "/api/helm/release"): ("viewer", lambda request: release((request.query.get("ns") or [""])[0], (request.query.get("name") or [""])[0], include_sensitive=request.role == "admin")),
}


def register(ops):
    """The job store's entry for a Helm install or upgrade (#622)."""
    # The release has its new revision once the job ends: the Helm page,
    # kept PAGE_SECONDS, shows it on its next refresh (#978).
    ops.RESOLVERS["helm"] = homestead_routes.drops_when_ended(job_status, "helm")
