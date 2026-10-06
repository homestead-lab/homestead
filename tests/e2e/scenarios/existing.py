"""Homestead added to a k3s or RKE2 cluster someone already runs - the
installer's "Install on this cluster" (HS_ROLE=addons) - rather than one it
built itself.

Each scenario has a single-host cluster of its own, from k3s's and RKE2's own
installers (install.build_bare), with a workload of its own already running. The installer is then run on its
first server, as a person would: Homestead must come up and see every host,
with Longhorn installed when the cluster has none and a Longhorn already
there used as it is, leave what was running alone, and a second run must
find Homestead there and change nothing."""
import os
import time

from harness import diagnostics, install, log
from harness.api import Homestead

# The Longhorn someone installed before Homestead: its own manifest, at the
# release Homestead's chart tests track unless E2E_LONGHORN_VERSION says.
LONGHORN = os.environ.get("E2E_LONGHORN_VERSION", "v1.13.0")
SENTINEL_NS = "before-homestead"
SENTINEL = {
    "apiVersion": "apps/v1", "kind": "Deployment",
    "metadata": {"name": "sentinel", "namespace": SENTINEL_NS},
    "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "sentinel"}},
             "template": {"metadata": {"labels": {"app": "sentinel"}},
                          "spec": {"containers": [{"name": "sleep", "image": "registry.k8s.io/e2e-test-images/busybox:1.36.1-1",
                                                   "command": ["sleep", "infinity"],
                                                   "resources": {"requests": {"cpu": "10m", "memory": "8Mi"}}}]}}},
}


def _sentinel(ctx):
    """A workload the cluster ran before Homestead; its pod, by uid."""
    ctx.kube.run("create", "namespace", SENTINEL_NS, check=False)
    ctx.kube.apply(SENTINEL)
    ctx.kube.deployment_ready(SENTINEL_NS, "sentinel", timeout=600)
    pods = ctx.kube.items("pods", "-n", SENTINEL_NS, "-l", "app=sentinel")
    assert len(pods) == 1, pods
    return pods[0]["metadata"]["uid"]


def _install(ctx):
    started = time.time()
    install.run_installer(ctx.nodes[0], ctx.distro, ctx.version, "addons", {"HS_VIP": ctx.vip})
    ctx.kube.deployment_ready("lab", "homestead", timeout=1800)
    log.info(f"Homestead added and ready in {int(time.time() - started)}s")
    ctx.api = Homestead(ctx.homestead_urls())
    ctx.api.sign_in()


def _homestead_sees_the_cluster(ctx, sentinel_uid):
    names = ctx.kube.node_names()
    listed = ctx.api.get("/api/nodes")
    seen = sorted(n.get("name") for n in (listed if isinstance(listed, list) else listed.get("nodes", [])))
    assert seen == names, f"Homestead lists {seen}, the cluster has {names}"
    probe = ctx.kube.wait("the node probe on every host", lambda: (
        lambda ds: ds["status"].get("numberReady") == len(names) == ds["status"].get("desiredNumberScheduled") and ds)(
            ctx.kube.get("daemonset", "-n", "lab", "homestead-nodeprobe")), timeout=600)
    log.info(f"node probe ready on {probe['status']['numberReady']} host(s)")
    claim = ctx.kube.get("pvc", "-n", "lab", "homestead-data")
    assert claim["status"].get("phase") == "Bound", f"Homestead's data claim is {claim['status']}"
    pods = ctx.kube.items("pods", "-n", SENTINEL_NS, "-l", "app=sentinel")
    assert [p["metadata"]["uid"] for p in pods] == [sentinel_uid], "what ran before Homestead was restarted or replaced"
    assert pods[0]["status"].get("phase") == "Running", pods[0]["status"]
    return claim


def _second_run_changes_nothing(ctx):
    before = ctx.kube.get("deployment", "-n", "lab", "homestead")["metadata"]["generation"]
    node = ctx.nodes[0]
    env = install.installer_env(ctx.distro, ctx.version, node, "addons", {"HS_VIP": ctx.vip})
    out = node.ssh(f"sudo env {env} sh /tmp/install.sh --install --text < /dev/null 2>&1 | tail -20")
    assert "Already Installed" in out, f"a second run did not find Homestead there: {out[-600:]}"
    after = ctx.kube.get("deployment", "-n", "lab", "homestead")["metadata"]["generation"]
    assert after == before, f"a second run changed Homestead's Deployment (generation {before} -> {after})"
    log.info("A second run found Homestead there and changed nothing")


def plain(ctx):
    """A cluster with no Longhorn: the installer installs Longhorn and then
    Homestead on it."""
    assert not ctx.kube.run("get", "crd", "volumes.longhorn.io", check=False).strip(), "this cluster should start without Longhorn"
    sentinel = _sentinel(ctx)
    _install(ctx)
    charts = {c["metadata"]["name"] for c in ctx.kube.items("helmcharts.helm.cattle.io", "-n", "kube-system")}
    assert "longhorn" in charts, f"Longhorn was not installed as a chart: {sorted(charts)}"
    ctx.kube.wait("Longhorn's manager on every host", lambda: (
        lambda ds: ds["status"].get("numberReady") == len(ctx.nodes) and ds)(
            ctx.kube.get("daemonset", "-n", "longhorn-system", "longhorn-manager")), timeout=900)
    claim = _homestead_sees_the_cluster(ctx, sentinel)
    log.info(f"Homestead's data on {claim['spec'].get('storageClassName')} ({claim['metadata']['name']})")
    _second_run_changes_nothing(ctx)


def longhorn_first(ctx):
    """A cluster that already runs Longhorn, from Longhorn's own manifest:
    Homestead uses it as it is - no chart of its own, the same images - and
    keeps its data on it."""
    log.info(f"Longhorn {LONGHORN} from its own manifest, before Homestead")
    ctx.kube.run("apply", "-f", f"https://raw.githubusercontent.com/longhorn/longhorn/{LONGHORN}/deploy/longhorn.yaml", timeout=300)
    ctx.kube.wait("Longhorn's manager on every host", lambda: (
        lambda ds: ds["status"].get("numberReady") == len(ctx.nodes) and ds)(
            ctx.kube.get("daemonset", "-n", "longhorn-system", "longhorn-manager")), timeout=1200)
    ctx.kube.wait("Longhorn's storage class", lambda: ctx.kube.run("get", "storageclass", "longhorn", check=False).strip(), timeout=300)
    images = sorted(c["image"] for c in ctx.kube.get("daemonset", "-n", "longhorn-system", "longhorn-manager")
                    ["spec"]["template"]["spec"]["containers"])
    sentinel = _sentinel(ctx)
    _install(ctx)
    charts = {c["metadata"]["name"] for c in ctx.kube.items("helmcharts.helm.cattle.io", "-n", "kube-system")}
    assert "longhorn" not in charts, "Homestead installed a second Longhorn over the one that was there"
    now = sorted(c["image"] for c in ctx.kube.get("daemonset", "-n", "longhorn-system", "longhorn-manager")
                 ["spec"]["template"]["spec"]["containers"])
    assert now == images, f"Longhorn was changed: {images} -> {now}"
    claim = _homestead_sees_the_cluster(ctx, sentinel)
    assert claim["spec"].get("storageClassName") == "longhorn", \
        f"Homestead's data is on {claim['spec'].get('storageClassName')}, not the Longhorn that was there"
    volumes = ctx.api.get("/api/volumes")
    rows = volumes if isinstance(volumes, list) else volumes.get("items") or volumes.get("volumes") or []
    assert any(claim["spec"].get("volumeName") in str(row) or claim["metadata"]["name"] in str(row) for row in rows), \
        "Homestead does not list its own Longhorn volume"
    _second_run_changes_nothing(ctx)


def longhorn_first_on_its_cluster(ctx):
    """The suite's second cluster is the one that starts with Longhorn; its
    own diagnostics are kept if it fails (the runner keeps the first's)."""
    try:
        longhorn_first(ctx.others[0])
    except Exception:
        diagnostics.collect(ctx.others[0], "longhorn-first-cluster")
        raise
