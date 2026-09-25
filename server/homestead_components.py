"""What the platform runs, what is newer, and moving it on.

Four parts make the platform under the apps: the cluster itself (Harvester,
k3s or RKE2), Longhorn, KubeVirt, and CDI beside it. For each this reads the
version running and the releases published, and works out the next version
to go to - one minor version at a time, as each project supports: the newest
patch of the minor it is on, else the newest of the next minor. Going
further means doing that again.

How each moves on depends on who put it there:

* Harvester brings its own Longhorn and KubeVirt and upgrades them with
  itself; they are shown, never upgraded apart from it. Harvester itself is
  upgraded by an Upgrade object for a version Harvester offers - what its own
  dashboard's Upgrade button makes.
* k3s and RKE2 are upgraded by Rancher's system-upgrade-controller: Plans say
  the version, and it upgrades the servers one at a time, then the agents.
  Homestead installs the controller (a chart through the Helm controller, as
  its add-ons are) the first time.
* Longhorn, KubeVirt and CDI installed by Homestead (or any HelmChart) move
  on by changing that HelmChart. Installed some other way, they are shown
  with the release notes, and upgraded the way they were installed.
"""
import json
import re
import time
import urllib.error
import urllib.request

kget = ksend = None
platform = None        # force -> what the cluster has, from homestead_platform
helm_upgrade = None    # HELM.upgrade
addons = None          # homestead_addons: chart_archive, kubevirt_cr, cdi_cr, fetch
fetch_json = None      # url -> parsed JSON

RELEASES_TTL = 12 * 3600
_releases = {}
GITHUB = "https://api.github.com/repos/{}/releases?per_page=60"
REPOS = {"longhorn": "longhorn/longhorn", "kubevirt": "kubevirt/kubevirt",
         "cdi": "kubevirt/containerized-data-importer", "harvester": "harvester/harvester"}
CHANNELS = {"k3s": "https://update.k3s.io/v1-release/channels", "rke2": "https://update.rke2.io/v1-release/channels"}
NOTES = {"longhorn": "https://github.com/longhorn/longhorn/releases/tag/{}",
         "kubevirt": "https://github.com/kubevirt/kubevirt/releases/tag/{}",
         "cdi": "https://github.com/kubevirt/containerized-data-importer/releases/tag/{}",
         "k3s": "https://github.com/k3s-io/k3s/releases/tag/{}",
         "rke2": "https://github.com/rancher/rke2/releases/tag/{}"}
HELM_NS = "kube-system"
CHARTS = {"longhorn": "longhorn", "kubevirt": "homestead-kubevirt", "cdi": "homestead-cdi"}
SUC_CHART = "homestead-system-upgrade"
SUC_NS = "system-upgrade"
SUC = "https://github.com/rancher/system-upgrade-controller/releases"
PLANS = "/apis/upgrade.cattle.io/v1/namespaces/system-upgrade/plans"
PLAN_NAMES = ("homestead-server", "homestead-agent")
CONTROL_PLANE = "node-role.kubernetes.io/control-plane"
CONTROLLER_WAIT = 15 * 60


def bind(_kget, _ksend, _platform, _helm_upgrade, _addons, _fetch_json=None):
    global kget, ksend, platform, helm_upgrade, addons, fetch_json
    kget, ksend, platform, helm_upgrade, addons = _kget, _ksend, _platform, _helm_upgrade, _addons
    fetch_json = _fetch_json or _get_json


def _get_json(url):
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "Homestead"})
    with urllib.request.urlopen(request, timeout=12) as response:
        return json.loads(response.read().decode("utf-8"))


# ------------------------------------------------------------------ versions
def parse(version):
    """(major, minor, patch, build) of a release - v1.9.1, v1.31.4+k3s1,
    v1.31.4+rke2r1 - or None for anything else, test builds included."""
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)(?:\+(?:k3s|rke2r)(\d+))?", str(version or "").strip())
    return tuple(int(part or 0) for part in match.groups()) if match else None


def next_step(current, available):
    """The version to go to next: the newest patch of this minor, else the
    newest of the next minor. None when this is the newest there is."""
    here = parse(current)
    if not here:
        return None
    newer = [v for v in available if parse(v) and parse(v) > here]
    same = [v for v in newer if parse(v)[:2] == here[:2]]
    if same:
        return max(same, key=parse)
    following = [v for v in newer if parse(v)[:2] == (here[0], here[1] + 1)]
    return max(following, key=parse) if following else None


def releases(kind, force=False):
    """Published releases, stable only, cached for half a day: ([versions], error)."""
    cached = _releases.get(kind)
    if cached and not force and time.time() - cached["at"] < RELEASES_TTL:
        return cached["value"], cached["error"]
    value, error = [], ""
    try:
        if kind in CHANNELS:
            data = fetch_json(CHANNELS[kind]).get("data") or []
            # One channel per minor (v1.31, v1.32 ...), each naming its newest.
            value = [row.get("latest", "") for row in data
                     if re.fullmatch(r"v\d+\.\d+", str(row.get("name") or row.get("id") or ""))]
        else:
            value = [row.get("tag_name", "") for row in fetch_json(GITHUB.format(REPOS[kind]))
                     if not row.get("prerelease") and not row.get("draft")]
        value = sorted({v for v in value if parse(v)}, key=parse, reverse=True)
    except Exception as err:
        error = str(err)[:160]
        value = (cached or {}).get("value") or []
    _releases[kind] = {"at": time.time(), "value": value, "error": error}
    return value, error


# ------------------------------------------------------------------ installed
def _get(path):
    try:
        return kget(path)
    except Exception:
        return None


def _items(path):
    found = _get(path)
    return (found or {}).get("items", []) if isinstance(found, dict) else []


def _tag(image):
    return image.rsplit(":", 1)[-1] if ":" in str(image or "").rsplit("/", 1)[-1] else ""


def longhorn_version():
    setting = _get("/apis/longhorn.io/v1beta2/namespaces/longhorn-system/settings/current-longhorn-version")
    value = str((setting or {}).get("value") or "")
    if parse(value):
        return value if value.startswith("v") else f"v{value}"
    manager = _get("/apis/apps/v1/namespaces/longhorn-system/daemonsets/longhorn-manager") or {}
    containers = (((manager.get("spec") or {}).get("template") or {}).get("spec") or {}).get("containers") or []
    tag = _tag(containers[0].get("image")) if containers else ""
    return tag if parse(tag) else ""


def _operator_version(path, field):
    rows = _items(path)
    if not rows:
        return "", ""
    status = rows[0].get("status") or {}
    return str(status.get(field) or ""), str(status.get("phase") or "")


def kubevirt_version():
    return _operator_version("/apis/kubevirt.io/v1/kubevirts", "observedKubeVirtVersion")


def cdi_version():
    return _operator_version("/apis/cdi.kubevirt.io/v1beta1/cdis", "observedVersion")


def node_versions():
    """Each node's kubelet version, which is the k3s or RKE2 version it runs."""
    out = {}
    for node in _items("/api/v1/nodes"):
        out[node["metadata"]["name"]] = ((node.get("status") or {}).get("nodeInfo") or {}).get("kubeletVersion", "")
    return out


def _helmchart(name):
    return _get(f"/apis/helm.cattle.io/v1/namespaces/{HELM_NS}/helmcharts/{name}")


# ------------------------------------------------------------------ the report
def _row(component, name, installed, kind, how, note="", extra=None):
    available, error = releases(kind) if kind else ([], "")
    target = next_step(installed, available) if how in ("helmchart", "suc") else None
    newest = available[0] if available else ""
    ahead = bool(newest and parse(installed) and parse(newest) > parse(installed))
    row = {"id": component, "name": name, "installed": installed, "newest": newest,
           "next": target or "", "behind": ahead, "how": how, "note": note, "error": error,
           "notes_url": NOTES[kind].format(target or newest) if kind in NOTES and (target or newest) else "",
           # More than one step to the newest: say so, as each is its own upgrade.
           "steps_left": bool(target and newest and target != newest)}
    row.update(extra or {})
    return row


def report(force=False):
    p = platform(True) or {}
    if force:
        _releases.clear()
    distribution = p.get("distribution", "")
    rows = []
    if p.get("harvester"):
        rows.append({"id": "cluster", "name": "Harvester", "how": "harvester", "installed": "",
                     "note": "Harvester's own releases and upgrades are below."})
    elif distribution in ("k3s", "rke2"):
        versions = node_versions()
        oldest = min((v for v in versions.values() if parse(v)), key=parse, default=p.get("version", ""))
        installed = oldest if str(oldest).startswith("v") else f"v{oldest}" if oldest else ""
        mixed = len({v for v in versions.values() if v}) > 1
        rows.append(_row("cluster", "k3s" if distribution == "k3s" else "RKE2", installed, distribution, "suc",
                         "Upgraded by Rancher's system-upgrade-controller: servers one at a time, then agents.",
                         {"nodes": versions, "mixed": mixed}))
    else:
        rows.append({"id": "cluster", "name": "Kubernetes", "how": "manual", "installed": p.get("version", ""),
                     "note": "Upgraded with the tools this cluster was built with."})
    harvester_note = "Comes with Harvester, and is upgraded with it."
    if p.get("longhorn"):
        installed = longhorn_version()
        chart = None if p.get("harvester") else _helmchart(CHARTS["longhorn"])
        managed = bool(chart and (chart.get("spec") or {}).get("chart") == "longhorn")
        rows.append(_row("longhorn", "Longhorn", installed, "longhorn",
                         "harvester" if p.get("harvester") else "helmchart" if managed else "manual",
                         harvester_note if p.get("harvester") else
                         "" if managed else "Installed outside Homestead: upgrade it the way it was installed."))
    if p.get("kubevirt"):
        installed, phase = kubevirt_version()
        managed = not p.get("harvester") and bool(_helmchart(CHARTS["kubevirt"]))
        rows.append(_row("kubevirt", "KubeVirt", installed, "kubevirt",
                         "harvester" if p.get("harvester") else "helmchart" if managed else "manual",
                         harvester_note if p.get("harvester") else
                         "" if managed else "Installed outside Homestead: upgrade it the way it was installed.",
                         {"phase": phase}))
    if p.get("cdi"):
        installed, phase = cdi_version()
        managed = not p.get("harvester") and bool(_helmchart(CHARTS["cdi"]))
        rows.append(_row("cdi", "CDI", installed, "cdi",
                         "harvester" if p.get("harvester") else "helmchart" if managed else "manual",
                         harvester_note if p.get("harvester") else
                         "" if managed else "Installed outside Homestead: upgrade it the way it was installed.",
                         {"phase": phase}))
    return {"distribution": distribution, "harvester": bool(p.get("harvester")), "components": rows,
            "checked": max((entry["at"] for entry in _releases.values()), default=0)}


# ------------------------------------------------------------------ upgrading
def _component(component):
    found = next((row for row in report()["components"] if row["id"] == component), None)
    if not found:
        raise ValueError(f"this cluster has no {component} to upgrade")
    if found["how"] == "harvester":
        raise ValueError(f"{found['name']} comes with Harvester and is upgraded with it")
    if found["how"] not in ("helmchart", "suc"):
        raise ValueError(f"{found['name']} was installed outside Homestead; upgrade it the way it was installed")
    if not found["next"]:
        raise ValueError(f"{found['name']} {found['installed']} is the newest there is")
    return found


def upgrade(component, target):
    """Start moving a component on to its next version. Only that version:
    skipping a minor is what these projects warn against."""
    found = _component(component)
    if target != found["next"]:
        raise ValueError(f"{found['name']} goes from {found['installed']} to {found['next']} next; "
                         f"{target} would skip a step")
    if component == "cluster":
        detail = _start_cluster(target)
    elif component == "longhorn":
        helm_upgrade({"namespace": "longhorn-system", "name": CHARTS["longhorn"], "version": target.lstrip("v")})
        detail = f"Longhorn is moving to {target}; its volumes stay attached while its parts restart"
    else:
        detail = _start_operator(component, target)
    return {"ok": True, "component": component, "name": found["name"], "from": found["installed"],
            "to": target, "detail": detail}


def _start_operator(component, target):
    """KubeVirt or CDI: the release's manifests, wrapped again at the new
    version, in place of the old ones. Its operator does the rest."""
    chart = _helmchart(CHARTS[component])
    if not chart:
        raise ValueError(f"Homestead did not install {component}")
    base = addons.KUBEVIRT if component == "kubevirt" else addons.CDI
    manifest = addons.fetch(f"{base}/download/{target}/{'kubevirt' if component == 'kubevirt' else 'cdi'}-operator.yaml")[0]
    if component == "kubevirt":
        current = (_items("/apis/kubevirt.io/v1/kubevirts") or [{}])[0]
        emulation = bool((((current.get("spec") or {}).get("configuration") or {})
                          .get("developerConfiguration") or {}).get("useEmulation"))
        switch_on = addons.kubevirt_cr(emulation)
    else:
        switch_on = addons.cdi_cr()
    chart.setdefault("spec", {})["chartContent"] = addons.chart_archive(component, target, manifest, switch_on)
    chart["metadata"].pop("managedFields", None)
    ksend("PUT", f"/apis/helm.cattle.io/v1/namespaces/{HELM_NS}/helmcharts/{CHARTS[component]}", chart)
    label = "KubeVirt" if component == "kubevirt" else "CDI"
    return f"{label} is moving to {target}; running VMs carry on while its operator rolls the update out"


def _plans_ready():
    try:
        kget("/apis/upgrade.cattle.io/v1")
        return True
    except Exception:
        return False


def _start_cluster(target):
    if not _plans_ready():
        _install_controller()
        return (f"Installing Rancher's system-upgrade-controller first; then the servers move to {target} "
                "one at a time, and the agents after them")
    _write_plans(target)
    return f"The servers move to {target} one at a time, then the agents"


def _install_controller():
    if _helmchart(SUC_CHART):
        return
    try:
        _, where = addons.fetch(f"{SUC}/latest")
        version = where.rstrip("/").rsplit("/", 1)[-1]
    except Exception:
        version = ""
    if not re.fullmatch(r"v\d+\.\d+\.\d+", version):
        raise ValueError("could not tell the system-upgrade-controller's newest release")
    manifests = "\n---\n".join(addons.fetch(f"{SUC}/download/{version}/{name}")[0]
                               for name in ("crd.yaml", "system-upgrade-controller.yaml"))
    body = {"apiVersion": "helm.cattle.io/v1", "kind": "HelmChart",
            "metadata": {"name": SUC_CHART, "namespace": HELM_NS},
            "spec": {"chartContent": addons.chart_archive("system-upgrade-controller", version, manifests, ""),
                     "targetNamespace": HELM_NS}}
    ksend("POST", f"/apis/helm.cattle.io/v1/namespaces/{HELM_NS}/helmcharts", body)


def plan_bodies(distribution, target):
    """The two Plans k3s and RKE2 document: servers one at a time, then agents,
    which wait for the servers' Plan first."""
    image = "rancher/k3s-upgrade" if distribution == "k3s" else "rancher/rke2-upgrade"
    common = {"concurrency": 1, "cordon": True, "serviceAccountName": "system-upgrade",
              "upgrade": {"image": image}, "version": target}
    server = dict(common, nodeSelector={"matchExpressions": [
        {"key": CONTROL_PLANE, "operator": "In", "values": ["true"]}]})
    agent = dict(common, nodeSelector={"matchExpressions": [{"key": CONTROL_PLANE, "operator": "DoesNotExist"}]},
                 prepare={"image": image, "args": ["prepare", PLAN_NAMES[0]]})
    return [{"apiVersion": "upgrade.cattle.io/v1", "kind": "Plan",
             "metadata": {"name": name, "namespace": SUC_NS, "labels": {"homestead.io/managed": "true"}},
             "spec": spec} for name, spec in zip(PLAN_NAMES, (server, agent))]


def _write_plans(target):
    distribution = (platform(False) or {}).get("distribution", "")
    for body in plan_bodies(distribution, target):
        current = _get(f"{PLANS}/{body['metadata']['name']}")
        if current:
            current["spec"] = body["spec"]
            current["metadata"].pop("managedFields", None)
            ksend("PUT", f"{PLANS}/{body['metadata']['name']}", current)
        else:
            ksend("POST", PLANS, body)


def remove_plans():
    for name in PLAN_NAMES:
        try:
            ksend("DELETE", f"{PLANS}/{name}")
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise


def _failed_upgrade_job():
    for job in _items(f"/apis/batch/v1/namespaces/{SUC_NS}/jobs"):
        labels = (job.get("metadata") or {}).get("labels") or {}
        status = job.get("status") or {}
        if labels.get("upgrade.cattle.io/plan") in PLAN_NAMES and status.get("failed") and not status.get("active"):
            node = labels.get("upgrade.cattle.io/node") or job["metadata"]["name"]
            return f"the upgrade job on {node} failed; its log in the {SUC_NS} namespace says why"
    return ""


def _elapsed(item):
    try:
        return time.time() - float(item["ref"].get("started") or 0)
    except (TypeError, ValueError):
        return 0


def status(item):
    """The job tray's view of an upgrade: (status, progress, message)."""
    ref = item["ref"]
    component, target = ref.get("component"), ref.get("to", "")
    if component == "cluster":
        if ref.get("phase") == "controller":
            if not _plans_ready():
                if _elapsed(item) > CONTROLLER_WAIT:
                    return "failed", 5, "the system-upgrade-controller did not start; its Helm job in kube-system says why"
                return "running", 3, "Installing the system-upgrade-controller"
            _write_plans(target)
            ref["phase"] = "nodes"
        versions = node_versions()
        done = sum(1 for v in versions.values() if v == target)
        if versions and done == len(versions):
            remove_plans()
            return "succeeded", 100, f"Every node runs {target}"
        failed = _failed_upgrade_job()
        if failed:
            return "failed", int(100 * done / max(1, len(versions))), failed
        waiting = sorted(name for name, v in versions.items() if v != target)
        return ("running", 5 + int(90 * done / max(1, len(versions))),
                f"{done} of {len(versions)} nodes on {target}; next {', '.join(waiting[:3])}")
    if component == "longhorn":
        now = longhorn_version()
    elif component == "kubevirt":
        now = kubevirt_version()[0]
    else:
        now = cdi_version()[0]
    if now == target:
        return "succeeded", 100, f"{ref.get('name', component)} runs {target}"
    job = _get(f"/apis/batch/v1/namespaces/{HELM_NS}/jobs/helm-install-{CHARTS.get(component, component)}") or {}
    job_status = job.get("status") or {}
    if job_status.get("failed") and not job_status.get("active") and _elapsed(item) > 120:
        return "failed", 50, f"Helm could not apply it; the helm-install-{CHARTS.get(component)} job's log in kube-system says why"
    return "running", 50 if job_status.get("active") else 20, f"{ref.get('name', component)} {now or '…'} → {target}"


def cancel_plan(item):
    if item["ref"].get("component") == "cluster":
        return {"mode": "stop", "undo": ["The upgrade Plans are removed, so no further node is upgraded"],
                "keeps": ["Nodes already upgraded stay on the new version; a node part-way through finishes"],
                "severity": "low", "needs": "admin"}
    return {"mode": "forget", "keeps": ["The new version is already being rolled out by its operator or Helm; "
                                        "it carries on and only stops showing here"]}


def cancel_run(item, _options):
    if item["ref"].get("component") == "cluster":
        remove_plans()
        return "Stopped: no further node is upgraded"
    return ""


# ------------------------------------------------------------------ Harvester
def start_harvester(version, offered):
    """An Upgrade for a version Harvester offers - what its dashboard's
    Upgrade button makes. Harvester checks it before it begins."""
    names = {row["version"] for row in offered}
    if version not in names:
        raise ValueError(f"Harvester does not offer {version}; it offers {', '.join(sorted(names)) or 'nothing now'}")
    body = {"apiVersion": "harvesterhci.io/v1beta1", "kind": "Upgrade",
            "metadata": {"generateName": "hvst-upgrade-", "namespace": "harvester-system"},
            "spec": {"version": version}}
    made = ksend("POST", "/apis/harvesterhci.io/v1beta1/namespaces/harvester-system/upgrades", body) or {}
    return (made.get("metadata") or {}).get("name", "")
