"""macvtap: a VM's own address on the LAN, on k3s and RKE2 hosts that have
no bridge.

A VM bridged onto a macvlan network never hears the LAN: macvlan passes only
frames for the one address it made itself, and the VM has its own. macvtap is
the kind of interface a VM can use - its tap device carries the VM's frames,
with the VM's address, straight onto the host's NIC - and the hosts' network
is not changed to get it. (Harvester has bridges, and needs none of this.)

It is two parts, as KubeVirt documents them:
- macvtap-cni, a DaemonSet: it copies its CNI plugin onto every host (k3s
  keeps its CNI plugins in /var/lib/rancher/k3s/data/cni, RKE2 in
  /opt/cni/bin) and runs a device plugin that offers each host's physical
  links as macvtap.network.kubevirt.io/<nic>;
- KubeVirt's macvtap network binding, registered in the KubeVirt resource.

Homestead installs the first as a HelmChart it makes, like KubeVirt and CDI
(homestead_addons.chart_archive), at a version it has tested, and upgrades it
under Platform versions. A LAN network for VMs is then a macvtap network
attachment on the NIC, and a VM joins it with the macvtap binding.

What macvtap cannot do: a host cannot reach the VMs on its own NIC this way -
other machines on the LAN can. A host bridge (homestead_host_bridge) is the
answer where that matters.
"""
import re

VERSION = "v0.13.2"
IMAGE = "quay.io/kubevirt/macvtap-cni"
CHART = "homestead-macvtap"
NS = "kube-system"
DAEMONSET = f"/apis/apps/v1/namespaces/{NS}/daemonsets/macvtap-cni"
KUBEVIRTS = "/apis/kubevirt.io/v1/kubevirts"
RESOURCE_PREFIX = "macvtap.network.kubevirt.io/"
BINDING = {"macvtap": {"domainAttachmentType": "tap"}}
CNI_BIN = {"k3s": "/var/lib/rancher/k3s/data/cni", "rke2": "/opt/cni/bin"}
VERSION_WORD = re.compile(r"v\d+\.\d+\.\d+")

kget = ksend = addons = platform = None


def bind(_kget, _ksend, _addons, _platform):
    global kget, ksend, addons, platform
    kget, ksend, addons, platform = _kget, _ksend, _addons, _platform


def manifests(distribution, version=VERSION):
    """The ConfigMap and DaemonSet, as the project's manifest has them, with
    the image pinned and the CNI folder the distribution uses."""
    if not VERSION_WORD.fullmatch(version):
        raise ValueError("a macvtap-cni version is like v0.13.2")
    cni_bin = CNI_BIN.get(distribution, "/opt/cni/bin")
    image = f"{IMAGE}:{version}"
    # An empty list offers every physical link and bond, each by its name.
    return f"""apiVersion: v1
kind: ConfigMap
metadata:
  name: macvtap-deviceplugin-config
  namespace: {NS}
  labels:
    app.kubernetes.io/managed-by: homestead
data:
  DP_MACVTAP_CONF: "[]"
---
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: macvtap-cni
  namespace: {NS}
  labels:
    app.kubernetes.io/managed-by: homestead
spec:
  selector:
    matchLabels:
      name: macvtap-cni
  template:
    metadata:
      labels:
        name: macvtap-cni
    spec:
      hostNetwork: true
      hostPID: true
      priorityClassName: system-node-critical
      tolerations:
      - operator: Exists
      nodeSelector:
        kubernetes.io/os: linux
      containers:
      - name: macvtap-cni
        command: ["/macvtap-deviceplugin", "-v", "3", "-logtostderr"]
        envFrom:
        - configMapRef:
            name: macvtap-deviceplugin-config
        image: {image}
        imagePullPolicy: IfNotPresent
        resources:
          requests:
            cpu: 60m
            memory: 30Mi
        securityContext:
          privileged: true
        volumeMounts:
        - name: deviceplugin
          mountPath: /var/lib/kubelet/device-plugins
        terminationMessagePolicy: FallbackToLogsOnError
        readinessProbe:
          exec:
            command: ["sh", "-c", "ls /var/lib/kubelet/device-plugins/macvtap.network.kubevirt.io* >/dev/null 2>&1"]
          initialDelaySeconds: 5
          periodSeconds: 10
      initContainers:
      - name: install-cni
        command: ["cp", "/macvtap-cni", "/host/opt/cni/bin/macvtap"]
        image: {image}
        imagePullPolicy: IfNotPresent
        resources:
          requests:
            cpu: 10m
            memory: 15Mi
        securityContext:
          privileged: true
        volumeMounts:
        - name: cni
          mountPath: /host/opt/cni/bin
          mountPropagation: Bidirectional
      volumes:
      - name: deviceplugin
        hostPath:
          path: /var/lib/kubelet/device-plugins
      - name: cni
        hostPath:
          path: {cni_bin}
"""


def _kubevirt():
    try:
        return (kget(KUBEVIRTS).get("items") or [None])[0]
    except Exception:
        return None


def binding_registered(kv=None):
    kv = kv if kv is not None else _kubevirt()
    bindings = ((((kv or {}).get("spec") or {}).get("configuration") or {}).get("network") or {}).get("binding") or {}
    return "macvtap" in bindings


def _tag(image):
    return str(image or "").rsplit(":", 1)[-1] if ":" in str(image or "") else ""


def inspect():
    """Whether VMs can use macvtap here: the DaemonSet ready on every node it
    runs on, and the binding registered."""
    out = {"installed": False, "ready": False, "binding": False, "version": "", "desired": 0, "available": 0,
           "state": "absent", "detail": "macvtap is not installed"}
    try:
        ds = kget(DAEMONSET)
    except Exception as error:
        if getattr(error, "code", None) != 404:
            out.update(state="unknown", detail="macvtap's status could not be read")
        return out
    status = ds.get("status") or {}
    containers = (((ds.get("spec") or {}).get("template") or {}).get("spec") or {}).get("containers") or [{}]
    desired, available = status.get("desiredNumberScheduled", 0), status.get("numberAvailable", 0)
    current = (status.get("observedGeneration", 0) >= (ds.get("metadata") or {}).get("generation", 1)
               and status.get("updatedNumberScheduled", 0) == desired)
    kv = _kubevirt()
    binding = binding_registered(kv) and nad_lookup(kv)
    ready = desired > 0 and available == desired and current and binding
    out.update(installed=True, ready=ready, binding=binding, version=_tag(containers[0].get("image")),
               desired=desired, available=available, state="ready" if ready else "not-ready",
               detail=(f"macvtap ready on {available} of {desired} nodes" if binding
                       else "macvtap runs, but KubeVirt is not set up for it yet (its binding, or reading a network's device)"))
    return out


def _version(kv):
    match = re.match(r"v?(\d+)\.(\d+)", str(((kv or {}).get("status") or {}).get("observedKubeVirtVersion") or ""))
    return (int(match.group(1)), int(match.group(2))) if match else None


def nad_lookup(kv=None):
    """Whether KubeVirt reads a network attachment's device resource and asks
    for it in the VM's pod. From 1.8 its ExternalNetResourceInjection gate
    (Beta, so on unless disabled) leaves that to a webhook; without one the
    pod never gets its macvtap device and Multus fails: "deviceID is required"."""
    kv = kv if kv is not None else _kubevirt()
    version = _version(kv)
    developer = ((((kv or {}).get("spec") or {}).get("configuration") or {}).get("developerConfiguration") or {})
    if "ExternalNetResourceInjection" in (developer.get("featureGates") or []):
        return False
    return not version or version < (1, 8) or GATE in (developer.get("disabledFeatureGates") or [])


GATE = "ExternalNetResourceInjection"


def register_binding():
    """KubeVirt's macvtap binding, beside any others, and KubeVirt reading
    the devices macvtap networks name (nad_lookup). KubeVirt before 1.5 also
    needs its NetworkBindingPlugins feature switched on."""
    kv = _kubevirt()
    if not kv:
        raise ValueError("KubeVirt is not installed")
    meta = kv["metadata"]
    path = f"/apis/kubevirt.io/v1/namespaces/{meta['namespace']}/kubevirts/{meta['name']}"
    patch = {"spec": {"configuration": {"network": {"binding": BINDING}}}}
    version = _version(kv)
    developer = ((kv["spec"].get("configuration") or {}).get("developerConfiguration") or {})
    changes = {}
    if version and version < (1, 5):
        gates = list(developer.get("featureGates") or [])
        if "NetworkBindingPlugins" not in gates:
            changes["featureGates"] = gates + ["NetworkBindingPlugins"]
    if version and version >= (1, 8):
        gates = [g for g in developer.get("featureGates") or [] if g != GATE]
        if gates != list(developer.get("featureGates") or []):
            changes["featureGates"] = gates
        disabled = list(developer.get("disabledFeatureGates") or [])
        if GATE not in disabled:
            changes["disabledFeatureGates"] = disabled + [GATE]
    if changes:
        patch["spec"]["configuration"]["developerConfiguration"] = changes
    ksend("PATCH", path, patch, ctype="application/merge-patch+json")


def install(cfg=None):
    """The DaemonSet as a HelmChart, and the binding in KubeVirt."""
    cfg = cfg or {}
    p = platform(True) or {}
    if p.get("harvester"):
        raise ValueError("Harvester's VMs use its bridges; macvtap is not needed there")
    if not p.get("kubevirt"):
        raise ValueError("macvtap is for virtual machines: install KubeVirt first")
    if not p.get("helm_controller"):
        raise ValueError("this cluster has no Helm controller, so Homestead cannot install macvtap")
    version = str(cfg.get("version") or VERSION)
    if inspect()["installed"]:
        if not (binding_registered() and nad_lookup()):
            register_binding()
            return {"ok": True, "name": CHART, "detail": "KubeVirt's macvtap binding is registered"}
        raise ValueError("macvtap is installed already")
    content = addons.chart_archive("macvtap", version, manifests(p.get("distribution", ""), version), "")
    # Its DaemonSet runs at system-node-critical: on a full node it would
    # preempt the Longhorn instance manager serving the volumes there.
    addons.preemption_hold("macvtap")
    try:
        chart = kget(f"/apis/helm.cattle.io/v1/namespaces/{NS}/helmcharts/{CHART}")
    except Exception:
        chart = None
    if chart:
        # An earlier try that Helm could not apply: its chart is replaced,
        # which runs the install again.
        chart.setdefault("spec", {})["chartContent"] = content
        ksend("PUT", f"/apis/helm.cattle.io/v1/namespaces/{NS}/helmcharts/{CHART}", chart)
    else:
        addons._post_chart(CHART, {"chartContent": content, "targetNamespace": NS})
    register_binding()
    return {"ok": True, "name": CHART, "job": f"helm-install-{CHART}", "version": version,
            "detail": f"macvtap {version} is being installed, and KubeVirt's macvtap binding registered"}


def upgrade(target):
    """A new image version, in the same chart."""
    p = platform(True) or {}
    chart = kget(f"/apis/helm.cattle.io/v1/namespaces/{NS}/helmcharts/{CHART}")
    chart.setdefault("spec", {})["chartContent"] = addons.chart_archive(
        "macvtap", target, manifests(p.get("distribution", ""), target), "")
    ksend("PUT", f"/apis/helm.cattle.io/v1/namespaces/{NS}/helmcharts/{CHART}", chart)
    return f"macvtap is moving to {target}; running VMs keep their interfaces"


def nad_config(name, mtu=None):
    """A network attachment's config for macvtap on a NIC."""
    config = {"cniVersion": "0.3.1", "name": name, "type": "macvtap"}
    if mtu:
        config["mtu"] = int(mtu)
    return config


def resource(nic):
    return RESOURCE_PREFIX + nic
