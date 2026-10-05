"""What the platform runs, what is newer, and moving it on.

The platform under the apps: the cluster itself (Harvester, k3s or RKE2),
Longhorn, KubeVirt and CDI beside it, and on k3s and RKE2 the two network
parts Homestead installs - kube-vip for VIPs, Multus for a VM's or
container's own LAN address. The last two are Helm charts, so their versions
are the charts', read from each chart repository's index. For each this reads the
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
import urllib.parse
import urllib.request

import homestead_helm as HELM

kget = ksend = None
platform = None        # force -> what the cluster has, from homestead_platform
helm_upgrade = None    # HELM.upgrade
addons = None          # homestead_addons: chart_archive, kubevirt_cr, cdi_cr, fetch
fetch_json = None      # url -> parsed JSON

RELEASES_TTL = 12 * 3600
_releases = {}
GITHUB = "https://api.github.com/repos/{}/releases?per_page=60"
REPOS = {"longhorn": "longhorn/longhorn", "kubevirt": "kubevirt/kubevirt",
         "cdi": "kubevirt/containerized-data-importer", "harvester": "harvester/harvester",
         "macvtap": "kubevirt/macvtap-cni"}
CHANNELS = {"k3s": "https://update.k3s.io/v1-release/channels", "rke2": "https://update.rke2.io/v1-release/channels"}
NOTES = {"longhorn": "https://github.com/longhorn/longhorn/releases/tag/{}",
         "kubevirt": "https://github.com/kubevirt/kubevirt/releases/tag/{}",
         "cdi": "https://github.com/kubevirt/containerized-data-importer/releases/tag/{}",
         "k3s": "https://github.com/k3s-io/k3s/releases/tag/{}",
         "rke2": "https://github.com/rancher/rke2/releases/tag/{}",
         "macvtap": "https://github.com/kubevirt/macvtap-cni/releases/tag/{}"}
HELM_NS = "kube-system"
CHARTS = {"longhorn": "longhorn", "kubevirt": "homestead-kubevirt", "cdi": "homestead-cdi",
          "kube-vip": "kube-vip", "multus": "multus", "macvtap": "homestead-macvtap"}
# Charts whose releases are read from their repository's index: (index, chart).
CHART_INDEX = {"kube-vip": ("https://kube-vip.github.io/helm-charts/index.yaml", "kube-vip"),
               "multus": ("https://rke2-charts.rancher.io/index.yaml", "rke2-multus")}
NOTES.update({"kube-vip": "https://github.com/kube-vip/helm-charts/releases/tag/kube-vip-{}",
              "multus": "https://github.com/rancher/rke2-charts/tree/main/packages/rke2-multus"})
SUC_CHART = "homestead-system-upgrade"
SUC_NS = "system-upgrade"
SUC = "https://github.com/rancher/system-upgrade-controller/releases"
PLANS = "/apis/upgrade.cattle.io/v1/namespaces/system-upgrade/plans"
PLAN_NAMES = ("homestead-server", "homestead-agent")
CONTROL_PLANE = "node-role.kubernetes.io/control-plane"
CONTROLLER_WAIT = 15 * 60
JOB_FINISH_WAIT = 10 * 60


def bind(_kget, _ksend, _platform, _helm_upgrade, _addons, _fetch_json=None):
    global kget, ksend, platform, helm_upgrade, addons, fetch_json
    kget, ksend, platform, helm_upgrade, addons = _kget, _ksend, _platform, _helm_upgrade, _addons
    fetch_json = _fetch_json or _get_json
    import homestead_lhv2_upgrade as V2
    V2.bind(_kget, _ksend, _platform, longhorn_version, parse)


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


def chart_index(text, chart):
    """[(chart version, app version)] for one chart in a Helm repository's
    index.yaml, as the index lists them. Read without a YAML library: an
    index maps each chart's name, two spaces in, to a list of entries."""
    start = text.find(f"\n  {chart}:\n")
    if start < 0:
        return []
    block = text[start + len(chart) + 5:]
    end = re.search(r"\n  [^\s-]", block)
    block = block[:end.start()] if end else block
    out = []
    for entry in re.split(r"\n  - ", "\n" + block)[1:]:
        version = re.search(r"(?m)^\s*version:\s*[\"']?([^\"'\s]+)", entry)
        app = re.search(r"(?m)^\s*appVersion:\s*[\"']?([^\"'\s]+)", entry)
        if version:
            out.append((version.group(1), app.group(1) if app else ""))
    return out


def releases(kind, force=False):
    """Published releases, stable only, cached for half a day: ([versions], error)."""
    cached = _releases.get(kind)
    if cached and not force and time.time() - cached["at"] < RELEASES_TTL:
        return cached["value"], cached["error"]
    value, error = [], ""
    try:
        if kind in CHART_INDEX:
            url, chart = CHART_INDEX[kind]
            pairs = chart_index(addons.fetch(url)[0], chart)
            _apps[kind] = dict(pairs)
            value = [version for version, _ in pairs]
        elif kind in CHANNELS:
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


_apps = {}


def app_version(kind, chart_version):
    """What a chart version installs: kube-vip chart 0.11.1 is kube-vip v1.2.3."""
    return _apps.get(kind, {}).get(chart_version, "")


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


def _image_tag(path):
    ds = _get(path) or {}
    containers = (((ds.get("spec") or {}).get("template") or {}).get("spec") or {}).get("containers") or []
    return _tag(containers[0].get("image")) if containers else ""


NETWORK_PARTS = (
    ("kube-vip", "kube-vip", ("/apis/apps/v1/namespaces/kube-system/daemonsets/kube-vip",
                              "/apis/apps/v1/namespaces/kube-system/daemonsets/kube-vip-ds",
                              "/apis/apps/v1/namespaces/harvester-system/daemonsets/kube-vip")),
    ("multus", "Multus", ("/apis/apps/v1/namespaces/kube-system/daemonsets/multus",
                          "/apis/apps/v1/namespaces/kube-system/daemonsets/rke2-multus",
                          "/apis/apps/v1/namespaces/kube-system/daemonsets/kube-multus-ds")),
)


def network_rows(p):
    """kube-vip and Multus: on k3s and RKE2 the charts Homestead installed,
    at their chart versions; on Harvester, its own."""
    rows = []
    harvester = bool(p.get("harvester"))
    present = {"kube-vip": p.get("load_balancer") == "kube-vip", "multus": bool(p.get("multus"))}
    for component, name, daemonsets in NETWORK_PARTS:
        chart = None if harvester else _helmchart(CHARTS[component])
        if not present[component] and not chart:
            continue
        running = next((tag for tag in (_image_tag(path) for path in daemonsets) if tag), "")
        if harvester:
            rows.append({"id": component, "name": name, "how": "harvester", "installed": running,
                         "note": "Comes with Harvester, and is upgraded with it."})
            continue
        spec = (chart or {}).get("spec") or {}
        if not chart or spec.get("chart") not in ("kube-vip", "rke2-multus"):
            rows.append({"id": component, "name": name, "how": "manual", "installed": running,
                         "note": "Installed outside Homestead: upgrade it the way it was installed."})
            continue
        installed = str(spec.get("version") or "")
        available, _ = releases(component)
        if not installed:
            # Installed before Homestead pinned a version: the chart whose
            # app is the image running now.
            bare = running.lstrip("v").split("-")[0]
            installed = next((v for v in available if app_version(component, v).lstrip("v") == bare), "")
        app = app_version(component, installed) or running
        rows.append(_row(component, name, installed, component, "helmchart",
                         f"{name} {app} · chart {installed}" if app and installed else "",
                         {"app": app, "chart": True}))
    return rows


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
    rows.extend(network_rows(p))
    if p.get("kubevirt") and not p.get("harvester"):
        import homestead_macvtap as MACVTAP
        state = MACVTAP.inspect()
        if state["installed"]:
            managed = bool(_helmchart(CHARTS["macvtap"]))
            rows.append(_row("macvtap", "macvtap", state["version"], "macvtap", "helmchart" if managed else "manual",
                             "" if managed else "Installed outside Homestead: upgrade it the way it was installed."))
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


def upgrade(component, target, options=None):
    """Start moving a component on to its next version. Only that version:
    skipping a minor is what these projects warn against."""
    found = _component(component)
    if target != found["next"]:
        raise ValueError(f"{found['name']} goes from {found['installed']} to {found['next']} next; "
                         f"{target} would skip a step")
    held = []
    if component == "cluster":
        held = cordoned()
        detail = _start_cluster(target)
    elif component == "longhorn":
        import homestead_lhv2_upgrade as V2
        mode = (options or {}).get("v2_mode", "offline")
        V2.require_upgrade(target, mode)
        if mode == "live" and (options or {}).get("confirm_backup") is not True:
            raise ValueError("Confirm that V2 volumes have been backed up before starting a live upgrade")
        if mode == "offline" and parse(found["installed"])[:3] >= (1, 13, 0):
            V2._set(V2.AUTO, "false")
        helm_upgrade({"namespace": "longhorn-system", "name": CHARTS["longhorn"], "version": target.lstrip("v")})
        detail = f"Upgrading Longhorn managers to {target}; V2 instance managers use the {mode} upgrade method"
    elif component == "macvtap":
        import homestead_macvtap as MACVTAP
        detail = MACVTAP.upgrade(target)
    elif component in CHART_INDEX:
        # The chart's own version, its values kept: kube-vip's settings and
        # Multus's CNI paths stay as Homestead set them.
        helm_upgrade({"namespace": HELM_NS, "name": CHARTS[component], "version": target})
        app = app_version(component, target)
        detail = f"Upgrading {found['name']} to chart {target}{f' ({app})' if app else ''}"
    else:
        detail = _start_operator(component, target)
    return {"ok": True, "component": component, "name": found["name"], "from": found["installed"],
            "to": target, "detail": detail,
            **({"v2_mode": mode} if component == "longhorn" else {}),
            **({"held": held} if component == "cluster" else {})}


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
        configuration = (current.get("spec") or {}).get("configuration") or {}
        emulation = bool((configuration.get("developerConfiguration") or {}).get("useEmulation"))
        switch_on = addons.kubevirt_cr(emulation, configuration.get("network"),
                                       (configuration.get("developerConfiguration") or {}).get("disabledFeatureGates"))
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
    if not _helmchart(SUC_CHART):
        raise ValueError("system-upgrade-controller is managed outside Homestead; use its owner to upgrade "
                         "k3s or RKE2 instead of creating competing Plans")
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
    if not _helmchart(SUC_CHART):
        raise ValueError("system-upgrade-controller is no longer managed by Homestead; no upgrade Plans were changed")
    distribution = (platform(False) or {}).get("distribution", "")
    bodies = plan_bodies(distribution, target)
    current_plans = {body["metadata"]["name"]: _get(f"{PLANS}/{body['metadata']['name']}") for body in bodies}
    for name, current in current_plans.items():
        if current:
            if ((current.get("metadata") or {}).get("labels") or {}).get("homestead.io/managed") != "true":
                raise ValueError(f"upgrade Plan {name} exists but is not owned by Homestead")
    for body in bodies:
        current = current_plans[body["metadata"]["name"]]
        if current:
            current["spec"] = body["spec"]
            current["metadata"].pop("managedFields", None)
            ksend("PUT", f"{PLANS}/{body['metadata']['name']}", current)
        else:
            ksend("POST", PLANS, body)


def remove_plans():
    for name in PLAN_NAMES:
        try:
            current = kget(f"{PLANS}/{name}")
            if ((current.get("metadata") or {}).get("labels") or {}).get("homestead.io/managed") == "true":
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


def cordoned():
    """Hosts someone had already cordoned: an upgrade leaves them so."""
    return sorted(n["metadata"]["name"] for n in _items("/api/v1/nodes") if (n.get("spec") or {}).get("unschedulable"))


def _upgrade_jobs_running():
    """Nodes whose upgrade job has not finished. A node reports its new
    version when k3s or RKE2 restarts, before its job ends - and it is the
    job's end that has the controller uncordon it."""
    out = []
    for job in _items(f"/apis/batch/v1/namespaces/{SUC_NS}/jobs"):
        labels = (job.get("metadata") or {}).get("labels") or {}
        status = job.get("status") or {}
        if labels.get("upgrade.cattle.io/plan") in PLAN_NAMES and not status.get("succeeded") and not status.get("failed"):
            out.append(labels.get("upgrade.cattle.io/node") or job["metadata"]["name"])
    return sorted(out)


def _uncordon_upgraded(held):
    """Removing the Plans removes their jobs, and a job removed before the
    controller saw it end leaves its node cordoned. Uncordon the nodes the
    Plans touched, except those someone had cordoned before the upgrade."""
    freed = []
    for node in _items("/api/v1/nodes"):
        name, labels = node["metadata"]["name"], node["metadata"].get("labels") or {}
        touched = any(f"plan.upgrade.cattle.io/{plan}" in labels for plan in PLAN_NAMES)
        if touched and (node.get("spec") or {}).get("unschedulable") and name not in held:
            ksend("PATCH", f"/api/v1/nodes/{name}", [{"op": "add", "path": "/spec/unschedulable", "value": False}],
                  ctype="application/json-patch+json")
            freed.append(name)
    return freed


def _elapsed(item):
    try:
        return time.time() - float(item["ref"].get("started") or 0)
    except (TypeError, ValueError):
        return 0


def _network_upgrade_status(item):
    ref = item["ref"]
    component, target = ref["component"], ref.get("to", "")
    name = ref.get("name") or next(label for key, label, _ in NETWORK_PARTS if key == component)
    chart = _helmchart(CHARTS[component])
    if not chart:
        return "running", 20, f"Waiting to read {name}'s HelmChart"
    namespace = (chart.get("spec") or {}).get("targetNamespace") or HELM_NS
    # spec.version is desired state. Only the newest Helm release proves
    # which chart was applied, including chart revisions with the same app.
    path = (f"/api/v1/namespaces/{namespace}/secrets?labelSelector="
            + urllib.parse.quote(f"owner=helm,name={CHARTS[component]}", safe=""))
    try:
        secrets = kget(path).get("items", [])
        latest = max(secrets, key=lambda secret: int((secret.get("metadata", {}).get("labels") or {}).get("version", 0)), default=None)
        release = HELM.decode(latest) if latest else None
        if release and (release.get("name") != CHARTS[component] or release.get("namespace") != namespace):
            raise ValueError("release identity mismatch")
        release_chart = ((release or {}).get("chart") or {}).get("metadata") or {}
        if release and release_chart.get("name") != (chart.get("spec") or {}).get("chart"):
            raise ValueError("release chart mismatch")
    except Exception:
        return "running", 20, f"Cannot verify {name}'s deployed Helm release; inspect the release and API access"
    metadata = ((release or {}).get("chart") or {}).get("metadata") or {}
    now = metadata.get("version") or "unknown"
    state = ((release or {}).get("info") or {}).get("status", "")
    if now.lstrip("v") != target.lstrip("v") or state != "deployed":
        job_name = (chart.get("status") or {}).get("jobName") or f"helm-install-{CHARTS[component]}"
        job = _get(f"/apis/batch/v1/namespaces/{HELM_NS}/jobs/{job_name}") or {}
        job_status = job.get("status") or {}
        if now.lstrip("v") == target.lstrip("v") and state == "failed" and not job_status.get("active"):
            return "failed", 50, f"Helm could not deploy {name} chart {target}; inspect its Helm release and install job"
        if job_status.get("failed") and not job_status.get("active") and _elapsed(item) > 120:
            return "failed", 50, f"Helm could not apply {name} chart {target}; inspect {job_name} in {HELM_NS}"
        return "running", 50 if job_status.get("active") else 20, f"{name} chart {now} → {target}; waiting for Helm deployment"
    paths = next(paths for key, _, paths in NETWORK_PARTS if key == component)
    agents = []
    try:
        for path in paths:
            # Network charts installed by Homestead target kube-system.
            # Keep their recognised agent names when a target namespace was set.
            if f"/namespaces/{HELM_NS}/" not in path:
                continue
            try:
                agents.append(kget(path.replace(f"/namespaces/{HELM_NS}/", f"/namespaces/{namespace}/")))
            except urllib.error.HTTPError as error:
                if error.code != 404:
                    raise
        if component == "multus":
            kget("/apis/k8s.cni.cncf.io/v1/network-attachment-definitions")
    except Exception:
        return "running", 70, f"{name} chart {target} deployed; waiting to verify network agents and APIs"
    desired = sum((ds.get("status") or {}).get("desiredNumberScheduled", 0) for ds in agents)
    available = sum((ds.get("status") or {}).get("numberAvailable", 0) for ds in agents)
    ready = bool(agents) and desired > 0 and all(
        not (ds.get("metadata") or {}).get("deletionTimestamp")
        and (ds.get("status") or {}).get("observedGeneration", 0) >= (ds.get("metadata") or {}).get("generation", 1)
        and (ds.get("status") or {}).get("updatedNumberScheduled", 0) == (ds.get("status") or {}).get("desiredNumberScheduled", 0)
        and (ds.get("status") or {}).get("numberAvailable", 0) == (ds.get("status") or {}).get("desiredNumberScheduled", 0)
        for ds in agents)
    app = metadata.get("appVersion") or ""
    version = f"{name} chart {target}" + (f" ({app})" if app else "")
    if ready:
        return "succeeded", 100, f"{version} deployed; available on all {desired} scheduled nodes"
    return "running", 75, f"{version} deployed; agents available on {available}/{desired} scheduled nodes; rollout pending"


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
            try:
                _write_plans(target)
            except ValueError as error:
                return "failed", 5, str(error)
            ref["phase"] = "nodes"
        versions = node_versions()
        done = sum(1 for v in versions.values() if v == target)
        if versions and done == len(versions):
            finishing = _upgrade_jobs_running()
            since = time.time() - ref.setdefault("versions_at", time.time())
            if finishing and since < JOB_FINISH_WAIT:
                return "running", 98, f"Every node runs {target}; waiting for the upgrade job on {', '.join(finishing[:3])} to finish"
            remove_plans()
            freed = _uncordon_upgraded(set(ref.get("held") or []))
            return "succeeded", 100, f"Every node runs {target}" + (f"; uncordoned {', '.join(freed)}" if freed else "")
        failed = _failed_upgrade_job()
        if failed:
            return "failed", int(100 * done / max(1, len(versions))), failed
        waiting = sorted(name for name, v in versions.items() if v != target)
        return ("running", 5 + int(90 * done / max(1, len(versions))),
                f"{done} of {len(versions)} nodes on {target}; next {', '.join(waiting[:3])}")
    if component in CHART_INDEX:
        return _network_upgrade_status(item)
    if component == "longhorn":
        if ref.get("v2_mode"):
            job = _get(f"/apis/batch/v1/namespaces/{HELM_NS}/jobs/helm-install-longhorn") or {}
            if (job.get("status") or {}).get("failed") and not (job.get("status") or {}).get("active") and _elapsed(item) > 120:
                return "failed", 20, "Helm could not apply Longhorn; inspect its install job in kube-system"
            import homestead_lhv2_upgrade as V2
            return V2.progress(item)
        now = longhorn_version()
    elif component == "kubevirt":
        now = kubevirt_version()[0]
    elif component == "macvtap":
        import homestead_macvtap as MACVTAP
        state = MACVTAP.inspect()
        now = state["version"] if state["ready"] else ""
    elif component == "cdi":
        now = cdi_version()[0]
    else:
        return "failed", 100, "Unknown platform component; inspect this upgrade before retrying"
    if now == target:
        return "succeeded", 100, f"{ref.get('name', component)} runs {target}"
    job = _get(f"/apis/batch/v1/namespaces/{HELM_NS}/jobs/helm-install-{CHARTS.get(component, component)}") or {}
    job_status = job.get("status") or {}
    if job_status.get("failed") and not job_status.get("active") and _elapsed(item) > 120:
        return "failed", 50, f"Helm could not apply it; the helm-install-{CHARTS.get(component)} job's log in kube-system says why"
    return "running", 50 if job_status.get("active") else 20, f"{ref.get('name', component)} {now or '…'} → {target}"


def cancel_plan(item):
    if item["ref"].get("component") == "longhorn" and item["ref"].get("v2_mode") == "live":
        return {"mode": "stop", "undo": ["Pause V2 live upgrades before the next host"],
                "keeps": ["The manager upgrade and current host upgrade continue. No version is rolled back."],
                "severity": "low", "needs": "admin"}
    if item["ref"].get("component") == "cluster":
        return {"mode": "stop", "undo": ["The upgrade Plans are removed, so no further node is upgraded"],
                "keeps": ["Nodes already upgraded stay on the new version; a node part-way through finishes"],
                "severity": "low", "needs": "admin"}
    return {"mode": "forget", "keeps": ["The new version is already being rolled out by its operator or Helm; "
                                        "it carries on and only stops showing here"]}


def cancel_run(item, _options):
    if item["ref"].get("component") == "longhorn" and item["ref"].get("v2_mode") == "live":
        import homestead_lhv2_upgrade as V2
        if parse(longhorn_version()) and parse(longhorn_version())[:3] >= (1, 13, 0):
            V2._set(V2.AUTO, "false")
        return "Paused before the next V2 host; the manager and current host upgrade can still finish"
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
