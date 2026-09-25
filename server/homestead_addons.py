"""Longhorn and KubeVirt for a cluster that has not got them.

Harvester brings both. On k3s or RKE2 they are an install away, and both
distributions run the Helm controller, so each is installed the way any chart
here is: a HelmChart object the controller acts on, with cluster-admin rights
Homestead itself does not hold, and that anyone can see with kubectl.

Longhorn publishes a chart. KubeVirt and CDI (which fills VM disks from
images) publish plain manifests with each release instead, so those are
wrapped in a chart of their own, sent inside the HelmChart: the release's
CustomResourceDefinitions in its crds folder, which Helm applies first, and
everything else, with the one custom resource that switches it on, as its
templates. A machine without hardware virtualisation (/dev/kvm) gets
KubeVirt's emulation instead - slow, but it runs.
"""
import base64
import io
import json
import re
import tarfile
import time
import urllib.request

import homestead_names as NAMES

kget = ksend = None
platform = None          # what the cluster has, from homestead_platform
probes = None            # each node's probe payload, for /dev/kvm
fetch = None             # url -> (text, final url)
CONTROLLER_NS = "kube-system"
KUBEVIRT_STABLE = "https://storage.googleapis.com/kubevirt-prow/release/kubevirt/kubevirt/stable.txt"
KUBEVIRT = "https://github.com/kubevirt/kubevirt/releases"
CDI = "https://github.com/kubevirt/containerized-data-importer/releases"
CDI_API = "https://api.github.com/repos/kubevirt/containerized-data-importer/releases/latest"
LONGHORN_REPO = "https://charts.longhorn.io"
RKE2_CHARTS = "https://rke2-charts.rancher.io"
CHARTS = {"longhorn": "longhorn", "kubevirt": "homestead-kubevirt", "cdi": "homestead-cdi", "multus": "multus"}
NAD_API = "/apis/k8s.cni.cncf.io/v1"


def _fetch(url):
    request = urllib.request.Request(url, headers={"User-Agent": "Homestead"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8"), response.geturl()


def bind(_kget, _ksend, _platform, _probes, _fetch_fn=None):
    global kget, ksend, platform, probes, fetch
    kget, ksend, platform, probes = _kget, _ksend, _platform, _probes
    fetch = _fetch_fn or _fetch


def _helmcharts():
    try:
        return {c["metadata"]["name"]: c for c in
                kget(f"/apis/helm.cattle.io/v1/namespaces/{CONTROLLER_NS}/helmcharts").get("items", [])}
    except Exception:
        return {}


def _kvm():
    """Which nodes have hardware virtualisation, by their probe; None where
    no probe has said."""
    try:
        found = probes() or {}
    except Exception:
        found = {}
    return {node: data.get("kvm") for node, data in found.items() if "kvm" in data}


def _multus():
    """Multus is there when its network attachments are: the definition
    appears with it."""
    try:
        kget(NAD_API)
        return True
    except Exception:
        return False


def status():
    p = platform(True)
    charts = _helmcharts()
    kvm = _kvm()
    multus = bool(p.get("harvester")) or _multus()
    return {
        "distribution": p.get("distribution", ""),
        "harvester": bool(p.get("harvester")),
        "helm_controller": bool(p.get("helm_controller")),
        "longhorn": {"installed": bool(p.get("longhorn")), "installing": CHARTS["longhorn"] in charts
                     and not p.get("longhorn")},
        "kubevirt": {"installed": bool(p.get("kubevirt")), "installing": CHARTS["kubevirt"] in charts
                     and not p.get("kubevirt"), "cdi": bool(p.get("cdi"))},
        "multus": {"installed": multus, "installing": CHARTS["multus"] in charts and not multus},
        # Each node's /dev/kvm, where its probe reports it.
        "kvm": kvm,
        "kvm_known": bool(kvm),
        "kvm_everywhere": bool(kvm) and all(kvm.values()),
        "kvm_nowhere": bool(kvm) and not any(kvm.values()),
    }


def _can_install(what):
    p = platform(True)
    if p.get("harvester"):
        raise ValueError(f"Harvester includes {what} already")
    if not p.get("helm_controller"):
        raise ValueError(f"this cluster has no Helm controller (k3s and RKE2 have one), so Homestead cannot "
                         f"install {what}; install it with its own instructions")
    return p


def _post_chart(name, spec):
    body = {"apiVersion": "helm.cattle.io/v1", "kind": "HelmChart",
            "metadata": {"name": name, "namespace": CONTROLLER_NS, "labels": {NAMES.key("managed"): "true"}},
            "spec": spec}
    if name in _helmcharts():
        raise ValueError(f"{name} is already being installed; follow it in the job tray or on the Helm page")
    ksend("POST", f"/apis/helm.cattle.io/v1/namespaces/{CONTROLLER_NS}/helmcharts", body)


# ------------------------------------------------------------------ Longhorn
def install_longhorn(cfg=None):
    """Longhorn from its chart, keeping as many copies as there are nodes, up
    to three - one node can hold only one."""
    p = _can_install("Longhorn")
    if p.get("longhorn"):
        raise ValueError("Longhorn is installed already")
    try:
        nodes = len(kget("/api/v1/nodes").get("items", []))
    except Exception:
        nodes = 1
    copies = max(1, min(3, nodes))
    values = "\n".join(["persistence:", f"  defaultClassReplicaCount: {copies}",
                        "defaultSettings:", f"  defaultReplicaCount: {copies}", ""])
    _post_chart(CHARTS["longhorn"], {"repo": LONGHORN_REPO, "chart": "longhorn", "targetNamespace": "longhorn-system",
                                     "createNamespace": True, "valuesContent": values})
    return {"ok": True, "name": CHARTS["longhorn"], "job": f"helm-install-{CHARTS['longhorn']}", "copies": copies,
            "detail": f"Longhorn is being installed, keeping {copies} cop{'y' if copies == 1 else 'ies'} of each volume. "
                      "Each node needs open-iscsi and an NFS client for it to mount volumes."}


# ------------------------------------------------------------------ Multus
# Where k3s keeps its CNI configuration and plugins, as its documentation
# gives them for Multus. RKE2 keeps them where the chart looks by default.
K3S_MULTUS_VALUES = "\n".join([
    "config:",
    "  fullnameOverride: multus",
    "  cni_conf:",
    "    confDir: /var/lib/rancher/k3s/agent/etc/cni/net.d",
    "    binDir: /var/lib/rancher/k3s/data/cni/",
    "    kubeconfig: /var/lib/rancher/k3s/agent/etc/cni/net.d/multus.d/multus.kubeconfig",
    ""])
RKE2_MULTUS_VALUES = "\n".join(["config:", "  fullnameOverride: multus", ""])


def install_multus(cfg=None):
    """Multus, which lets a pod join a second network - what a LAN network
    needs - from the chart RKE2 uses for it, set up for this distribution."""
    p = _can_install("Multus")
    if _multus():
        raise ValueError("Multus is installed already")
    distribution = p.get("distribution", "")
    if distribution not in ("k3s", "rke2"):
        raise ValueError("Homestead installs Multus on k3s and RKE2; elsewhere install it with its own instructions")
    _post_chart(CHARTS["multus"], {"repo": RKE2_CHARTS, "chart": "rke2-multus", "targetNamespace": CONTROLLER_NS,
                                   "valuesContent": K3S_MULTUS_VALUES if distribution == "k3s" else RKE2_MULTUS_VALUES})
    return {"ok": True, "name": CHARTS["multus"], "job": f"helm-install-{CHARTS['multus']}",
            "detail": "Multus is being installed on every node; pods already running are left as they are. "
                      "LAN networks can be made once it is up"}


# ------------------------------------------------------------------ KubeVirt
def _documents(text):
    return [doc for doc in re.split(r"(?m)^---\s*$", text) if doc.strip() and re.search(r"(?m)^kind:", doc)]


def _kind(doc):
    match = re.search(r"(?m)^kind:\s*(\S+)", doc)
    return match.group(1) if match else ""


def _escape(text):
    """Plain YAML as a Helm template: only "{{" means anything to Helm."""
    return text.replace("{{", '{{ "{{" }}')


def chart_archive(name, version, manifests, extra):
    """A chart, as the base64 tarball a HelmChart carries: every
    CustomResourceDefinition in crds, the rest - and extra - as templates."""
    crds, rest = [], []
    for doc in _documents(manifests):
        (crds if _kind(doc) == "CustomResourceDefinition" else rest).append(doc.strip())
    files = {
        f"{name}/Chart.yaml": "\n".join(["apiVersion: v2", f"name: {name}", f"version: {version.lstrip('v')}",
                                         f"appVersion: {json.dumps(version)}",
                                         "description: Made by Homestead from the project's release manifests", ""]),
        f"{name}/crds/crds.yaml": "\n---\n".join(crds) + "\n",
        f"{name}/templates/release.yaml": _escape("\n---\n".join(rest)) + "\n",
        f"{name}/templates/switch-on.yaml": _escape(extra),
    }
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as tar:
        for path, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(path)
            info.size, info.mtime, info.mode = len(data), int(time.time()), 0o644
            tar.addfile(info, io.BytesIO(data))
    return base64.b64encode(raw.getvalue()).decode()


def kubevirt_cr(emulation):
    developer = {"featureGates": []}
    if emulation:
        developer["useEmulation"] = True
    return json.dumps({"apiVersion": "kubevirt.io/v1", "kind": "KubeVirt",
                       "metadata": {"name": "kubevirt", "namespace": "kubevirt"},
                       "spec": {"certificateRotateStrategy": {}, "customizeComponents": {},
                                "imagePullPolicy": "IfNotPresent", "workloadUpdateStrategy": {},
                                "configuration": {"developerConfiguration": developer}}}, indent=1) + "\n"


def cdi_cr():
    return json.dumps({"apiVersion": "cdi.kubevirt.io/v1beta1", "kind": "CDI", "metadata": {"name": "cdi"},
                       "spec": {"imagePullPolicy": "IfNotPresent", "workload": {"nodeSelector": {"kubernetes.io/os": "linux"}},
                                "config": {"featureGates": ["HonorWaitForFirstConsumer"]}}}, indent=1) + "\n"


def latest_versions():
    kubevirt = fetch(KUBEVIRT_STABLE)[0].strip()
    # GitHub sends .../releases/latest on to the newest release's page; its
    # API says the same, for when the page is not reachable.
    try:
        _, where = fetch(f"{CDI}/latest")
        cdi = where.rstrip("/").rsplit("/", 1)[-1]
    except Exception:
        cdi = ""
    if not re.fullmatch(r"v\d+\.\d+\.\d+", cdi):
        cdi = str(json.loads(fetch(CDI_API)[0]).get("tag_name") or "")
    for label, version in (("KubeVirt", kubevirt), ("CDI", cdi)):
        if not re.fullmatch(r"v\d+\.\d+\.\d+", version):
            raise ValueError(f"could not tell {label}'s newest release (got {version!r})")
    return kubevirt, cdi


def install_kubevirt(cfg=None):
    """KubeVirt and CDI, from their newest releases, through the Helm controller."""
    cfg = cfg or {}
    p = _can_install("KubeVirt")
    if p.get("kubevirt"):
        raise ValueError("KubeVirt is installed already")
    known = status()
    emulation = bool(cfg.get("emulation")) if "emulation" in cfg else known["kvm_nowhere"]
    kubevirt, cdi = latest_versions()
    operator = fetch(f"{KUBEVIRT}/download/{kubevirt}/kubevirt-operator.yaml")[0]
    cdi_operator = fetch(f"{CDI}/download/{cdi}/cdi-operator.yaml")[0]
    # The release's own objects name their namespaces, so the Helm release
    # itself is kept in kube-system beside the controller's others.
    _post_chart(CHARTS["kubevirt"], {"chartContent": chart_archive("kubevirt", kubevirt, operator, kubevirt_cr(emulation)),
                                     "targetNamespace": CONTROLLER_NS})
    if not p.get("cdi"):
        _post_chart(CHARTS["cdi"], {"chartContent": chart_archive("cdi", cdi, cdi_operator, cdi_cr()),
                                    "targetNamespace": CONTROLLER_NS})
    return {"ok": True, "name": CHARTS["kubevirt"], "job": f"helm-install-{CHARTS['kubevirt']}",
            "kubevirt": kubevirt, "cdi": cdi, "emulation": emulation,
            "detail": f"KubeVirt {kubevirt} and CDI {cdi} are being installed"
                      + ("; with no hardware virtualisation found, VMs are emulated and run slowly" if emulation else "")}
