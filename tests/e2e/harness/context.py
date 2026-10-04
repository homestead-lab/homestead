"""What a scenario is given: the hosts, kubectl, Homestead's API, and test
apps that write to their volumes so their data can be checked across a
reboot or a shutdown."""
import json
import time

from . import log
from .vms import VIP

# From registry.k8s.io, not Docker Hub, whose rate limits a CI runner meets.
BUSYBOX = "registry.k8s.io/e2e-test-images/busybox:1.36.1-1"


class Context:
    def __init__(self, lab, kube, api, distro, version, artifacts, vip=VIP, nodes=None):
        self.lab, self.kube, self.api = lab, kube, api
        self.distro, self.version, self.artifacts = distro, version, artifacts
        self.vip, self.nodes = vip, nodes or lab.nodes
        self.others = []        # further clusters, for moves between them
        self._copies_checked = False

    def node(self, name):
        return next(n for n in self.lab.nodes if n.name == name)

    def homestead_urls(self):
        urls = [f"http://{self.vip}:8088"]
        try:
            port = next(p["nodePort"] for p in self.kube.get("service", "-n", "lab", "homestead")["spec"]["ports"] if p.get("nodePort"))
            urls += [f"http://{n.ip}:{port}" for n in self.nodes]
        except Exception:
            pass
        return urls

    # ------------------------------------------------------------ test apps
    def app(self, name, node=None, pinned=False, size="1Gi", storage_class="longhorn-r2", cpu_burn=False, ns="lab"):
        """A Deployment with a Longhorn volume, writing to it every few seconds.
        node: where it starts - pinned (nodeSelector) or preferred (affinity)."""
        placement = {}
        if node and pinned:
            placement["nodeSelector"] = {"kubernetes.io/hostname": node}
        elif node:
            placement["affinity"] = {"nodeAffinity": {"preferredDuringSchedulingIgnoredDuringExecution": [
                {"weight": 100, "preference": {"matchExpressions": [{"key": "kubernetes.io/hostname", "operator": "In", "values": [node]}]}}]}}
        loop = "while true; do :; done" if cpu_burn else "while true; do date > /data/now; sleep 5; done"
        classes = {c["metadata"]["name"]: c for c in self.kube.items("storageclass")}
        if storage_class not in classes and len(self.nodes) > 1 and not self._copies_checked:
            # The installer's Longhorn starts with one copy, on one host;
            # Homestead raises the default as hosts join, so a host can drain
            # with its apps' data whole elsewhere. Checked once, before any app.
            want = min(3, len(self.nodes))
            def raised():
                cls = {c["metadata"]["name"]: c for c in self.kube.items("storageclass")}.get("longhorn") or {}
                return int((cls.get("parameters") or {}).get("numberOfReplicas") or 0) >= want
            self.kube.wait(f"Homestead raising Longhorn's default copies to {want}", raised, timeout=900, every=15)
            self._copies_checked = True
            classes = {c["metadata"]["name"]: c for c in self.kube.items("storageclass")}
        if storage_class not in classes:
            # A Longhorn class the installer made - the default one first - never local-path.
            longhorn = sorted((n for n, c in classes.items() if c.get("provisioner") == "driver.longhorn.io"),
                              key=lambda n: (classes[n]["metadata"].get("annotations") or {}).get(
                                  "storageclass.kubernetes.io/is-default-class") != "true")
            assert longhorn, f"no Longhorn storage class among {sorted(classes)}"
            storage_class = longhorn[0]
        manifest = {"apiVersion": "v1", "kind": "List", "items": [
            {"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": {"name": f"{name}-data", "namespace": ns},
             "spec": {"accessModes": ["ReadWriteOnce"], "storageClassName": storage_class, "resources": {"requests": {"storage": size}}}},
            {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": name, "namespace": ns, "labels": {"app": name, "e2e": "true"}},
             "spec": {"replicas": 1, "strategy": {"type": "Recreate"}, "selector": {"matchLabels": {"app": name}},
                      "template": {"metadata": {"labels": {"app": name}},
                                   "spec": {**placement, "terminationGracePeriodSeconds": 5, "containers": [{
                                       "name": name, "image": BUSYBOX,
                                       "command": ["sh", "-c", f"echo started $(date) >> /data/starts; {loop}"],
                                       "resources": {"requests": {"cpu": "50m", "memory": "32Mi"}},
                                       "volumeMounts": [{"name": "data", "mountPath": "/data"}]}],
                                       "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": f"{name}-data"}}]}}}}]}
        self.kube.apply(json.dumps(manifest))
        log.info(f"Test app {ns}/{name}: volume on {storage_class}")
        self.kube.deployment_ready(ns, name, timeout=600)
        log.info(f"Test app {ns}/{name} running on {self.app_node(name, ns)}")

    def app_node(self, name, ns="lab"):
        pods = [p for p in self.kube.items("pods", "-n", ns, "-l", f"app={name}") if p["status"].get("phase") == "Running"]
        return pods[0]["spec"]["nodeName"] if pods else None

    def exec(self, name, command, ns="lab", timeout=120):
        return self.kube.run("exec", "-n", ns, f"deploy/{name}", "--", "sh", "-c", command, timeout=timeout).strip()

    def mark(self, name, ns="lab"):
        """A line written to the app's volume now, to be found again later."""
        marker = f"e2e-{name}-{int(time.time())}"
        self.exec(name, f"echo {marker} >> /data/markers && sync", ns)
        return marker

    def has_mark(self, name, marker, ns="lab"):
        return marker in self.exec(name, "cat /data/markers", ns)
