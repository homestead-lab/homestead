"""Networking as a person sets it up in Homestead: an app given a VIP of its
own and reached from outside the cluster, the VIP carried to another host
when its host fails, a firewall that lets one namespace in and keeps
another out, an app on the LAN itself through Multus, and the installer -
a worker joined, the node doctor's safe fixes, and a second run that leaves
an installed cluster alone.

The runner sits on the hosts' bridge (10.10.0.1), so it reaches the VIPs and
LAN addresses as a machine on the same LAN would."""
import json
import time
import urllib.error
import urllib.request

from harness import log
from harness.api import HomesteadError
from harness.install import RAW, installer_env
from harness.vms import NET

AGNHOST = "registry.k8s.io/e2e-test-images/agnhost:2.53"
APP_VIP = f"{NET}.120"
LAN_ADDRESS = f"{NET}.150"


def _http(url, timeout=5):
    """The body the address answers with, or None while it does not."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.read().decode(errors="replace")
    except (urllib.error.URLError, OSError, TimeoutError):
        return None


def _until(what, check, timeout=300, every=3):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = check()
        if value:
            return value
        time.sleep(every)
    raise AssertionError(f"{what}: not within {timeout}s")


def _echo(ctx, name, replicas=1, ns="lab", spread=False):
    """agnhost answering HTTP on 8080 with its own hostname - no volume, so it
    moves in seconds."""
    spec = {"replicas": replicas, "selector": {"matchLabels": {"app": name}},
            "template": {"metadata": {"labels": {"app": name}},
                         "spec": {"terminationGracePeriodSeconds": 2,
                                  "containers": [{"name": name, "image": AGNHOST, "args": ["netexec", "--http-port=8080"],
                                                  "ports": [{"containerPort": 8080}],
                                                  "readinessProbe": {"httpGet": {"path": "/hostname", "port": 8080}, "periodSeconds": 2},
                                                  "resources": {"requests": {"cpu": "10m", "memory": "16Mi"}}}]}}}
    if spread:
        spec["template"]["spec"]["topologySpreadConstraints"] = [
            {"maxSkew": 1, "topologyKey": "kubernetes.io/hostname", "whenUnsatisfiable": "DoNotSchedule",
             "labelSelector": {"matchLabels": {"app": name}}}]
    ctx.kube.apply(json.dumps({"apiVersion": "apps/v1", "kind": "Deployment",
                               "metadata": {"name": name, "namespace": ns, "labels": {"app": name, "e2e": "true"}}, "spec": spec}))
    return ctx.kube.deployment_ready(ns, name, timeout=600)


def _client(ctx, ns):
    """A pod to make requests from, in a namespace of its own choosing."""
    ctx.kube.run("create", "namespace", ns, check=False)
    ctx.kube.apply(json.dumps({"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "e2e-client", "namespace": ns},
                               "spec": {"terminationGracePeriodSeconds": 1, "containers": [{
                                   "name": "client", "image": AGNHOST, "args": ["pause"]}]}}))
    ctx.kube.run("wait", "-n", ns, "--for=condition=Ready", "pod/e2e-client", "--timeout=300s")


def _reaches(ctx, ns, url):
    """agnhost connect exits 0 once a TCP connection is made, 1 otherwise."""
    try:
        ctx.kube.run("exec", "-n", ns, "e2e-client", "--", "/agnhost", "connect", "--timeout=3s",
                     url.split("//", 1)[-1].split("/")[0], timeout=30)
        return True
    except RuntimeError:
        return False


def _holder(ctx, address):
    """The host the address is up on now."""
    for node in ctx.lab.nodes:
        if node.running() and address in node.ssh(f"ip -4 -o addr show | grep -F ' {address}/' || true", check=False, quiet=True):
            return node
    return None


def _until_api(ctx, call, what, timeout=900):
    """A call that Homestead refuses until something it installs after it
    starts (kube-vip, Multus) is ready."""
    deadline, last = time.time() + timeout, None
    while time.time() < deadline:
        try:
            return call()
        except HomesteadError as error:
            last = error
            time.sleep(10)
    raise AssertionError(f"{what}: {last}")


# ------------------------------------------------------------------- VIP
def vip(ctx):
    """Homestead's own VIP answers; an app given a VIP from the pool answers
    on it from outside the cluster; and the VIP moves when its host fails."""
    _until("Homestead on its VIP", lambda: _http(f"http://{ctx.vip}:8088/"), timeout=600)
    _echo(ctx, "e2e-web", replicas=2, spread=True)

    added = ctx.api.post("/api/network/vips/add", {"ip": APP_VIP, "label": "e2e"})
    assert APP_VIP in added.get("added", []), f"the address was not added: {added}"
    pool = ctx.api.get("/api/network")
    assert any(row.get("ip") == APP_VIP for row in pool.get("vips") or []), "the added VIP is not listed under Networking"

    service = _until_api(ctx, lambda: ctx.api.post("/api/network/services", {
        "namespace": "lab", "workload": "e2e-web", "name": "e2e-web", "type": "LoadBalancer",
        "vip_mode": "manual", "vip": APP_VIP, "ports": [{"port": 80, "target_port": 8080}]}), "expose e2e-web")
    assert service.get("vip") == APP_VIP, f"Homestead gave {service.get('vip')!r}, not {APP_VIP}"
    if service.get("operation"):
        ctx.api.wait_job(service["operation"]["id"], timeout=600)
    first = _until(f"e2e-web on {APP_VIP}", lambda: _http(f"http://{APP_VIP}/hostname"), timeout=300)
    log.info(f"{APP_VIP} answered from {first}")

    # The VIP's host fails. Never the first host: the runner's kubectl talks to it.
    holder = _until(f"a host with {APP_VIP}", lambda: _holder(ctx, APP_VIP), timeout=120)
    if holder is ctx.lab.nodes[0]:
        log.info(f"{APP_VIP} is on {holder.name}; its kube-vip is restarted to hand it on")
        pod = next(p["metadata"]["name"] for p in ctx.kube.items("pods", "-A", "-l", "app.kubernetes.io/name=kube-vip")
                   if p["spec"].get("nodeName") == holder.name)
        namespace = next(p["metadata"]["namespace"] for p in ctx.kube.items("pods", "-A", "-l", "app.kubernetes.io/name=kube-vip")
                         if p["metadata"]["name"] == pod)
        ctx.kube.run("delete", "pod", "-n", namespace, pod, "--wait=false")
        holder = _until(f"{APP_VIP} on another host", lambda: (h := _holder(ctx, APP_VIP)) and h is not ctx.lab.nodes[0] and h, timeout=180)
    log.info(f"{APP_VIP} is on {holder.name}; pulling its plug")
    holder.pull_plug()
    started = time.time()
    _until(f"{APP_VIP} answering again after {holder.name} failed", lambda: _http(f"http://{APP_VIP}/hostname"), timeout=240)
    log.info(f"{APP_VIP} answered again {int(time.time() - started)}s after its host failed, now on "
             f"{getattr(_holder(ctx, APP_VIP), 'name', '?')}")
    holder.power_on()
    holder.wait_ssh()
    ctx.kube.nodes_ready(len(ctx.lab.nodes))


# -------------------------------------------------------------- firewall
def firewall(ctx):
    """A firewall on an app that lets its own namespace in on 8080 and keeps
    every other namespace out; removing it lets everyone in again."""
    dep = _echo(ctx, "e2e-guarded")
    pod_ip = _until("e2e-guarded's address", lambda: next(
        (p["status"].get("podIP") for p in ctx.kube.items("pods", "-n", "lab", "-l", "app=e2e-guarded")
         if p["status"].get("phase") == "Running"), None), timeout=120)
    url = f"http://{pod_ip}:8080"
    _client(ctx, "lab")
    _client(ctx, "e2e-outside")
    assert _until("lab reaching e2e-guarded", lambda: _reaches(ctx, "lab", url), timeout=60)
    assert _until("another namespace reaching e2e-guarded", lambda: _reaches(ctx, "e2e-outside", url), timeout=60)

    policy = {"namespace": "lab", "name": "homestead-fw-e2e-guarded",
              "target": {"namespace": "lab", "name": "e2e-guarded", "kind": "Deployment", "uid": dep["metadata"]["uid"]},
              "ingress": "restricted", "egress": "unchanged", "allow_dns": False, "egress_rules": [],
              "ingress_rules": [{"peer": "namespace", "value": "lab", "protocol": "TCP", "ports": "8080"}]}
    review = ctx.api.post("/api/firewall/preview", policy)
    assert review.get("review"), f"no review for the firewall: {review}"
    ctx.api.post("/api/firewall/save", dict(policy, review=review["review"]))
    listed = ctx.api.get("/api/firewall")
    assert "homestead-fw-e2e-guarded" in json.dumps(listed), "the firewall is not listed"

    _until("the other namespace kept out", lambda: not _reaches(ctx, "e2e-outside", url), timeout=120)
    assert _reaches(ctx, "lab", url), "the firewall keeps out the namespace it lets in"
    log.info(f"Firewall on {ctx.distro}: lab let in, e2e-outside kept out")

    ctx.api.post("/api/firewall/delete", {"namespace": "lab", "name": "homestead-fw-e2e-guarded"})
    _until("the other namespace let in again", lambda: _reaches(ctx, "e2e-outside", url), timeout=120)
    ctx.kube.run("delete", "namespace", "e2e-outside", "--wait=false", check=False)


# ---------------------------------------------------------- LAN / Multus
def lan(ctx):
    """A LAN network on the hosts' eth0 (macvlan, through Multus), and an app
    deployed onto it with an address of its own, reached from the LAN."""
    made = _until_api(ctx, lambda: ctx.api.post("/api/network/vm-networks",
                                                {"name": "e2e-lan", "namespace": "lab", "interface": "eth0"}),
                      "make a LAN network (Multus ready)")
    log.info(made.get("detail", made))
    nad = ctx.kube.get("network-attachment-definitions.k8s.cni.cncf.io", "-n", "lab", "e2e-lan")
    assert json.loads(nad["spec"]["config"])["type"] == "macvlan", nad["spec"]["config"]

    config = {"name": "e2e-onlan", "namespace": "lab", "image": AGNHOST, "args": ["netexec", "--http-port=8080"],
              "ports": [{"container": 8080, "expose": True}], "network_mode": "lan", "volumes": [],
              "lan": {"network": "lab/e2e-lan", "address": LAN_ADDRESS, "prefix": 24, "gateway": f"{NET}.1"}}
    review = ctx.api.post("/api/preview", config)
    ctx.api.post("/api/deploy", dict(config, capacity_token=review.get("capacity_token"), confirm_capacity=True))
    ctx.kube.deployment_ready("lab", "e2e-onlan", timeout=600)
    name = _until(f"e2e-onlan on {LAN_ADDRESS}", lambda: _http(f"http://{LAN_ADDRESS}:8080/hostname"), timeout=300)
    log.info(f"{LAN_ADDRESS} answered from {name} on {ctx.app_node('e2e-onlan')}")
    pod = next(p for p in ctx.kube.items("pods", "-n", "lab", "-l", "app=e2e-onlan") if p["status"].get("phase") == "Running")
    status = json.loads((pod["metadata"].get("annotations") or {}).get("k8s.v1.cni.cncf.io/network-status", "[]"))
    assert any(LAN_ADDRESS in (row.get("ips") or []) for row in status), f"Multus reports {status}"


# ------------------------------------------------------------- installer
def installer(ctx):
    """The worker the installer joined runs apps; the node doctor's safe
    fixes leave every host healthy; and the installer run again on an
    installed cluster changes nothing."""
    agents = [n for n in ctx.lab.nodes if n.role == "agent"]
    assert agents, "this suite builds a worker host"
    for node in agents:
        k8s = ctx.kube.get("node", node.name)
        roles = [k for k in k8s["metadata"].get("labels", {}) if k.startswith("node-role.kubernetes.io/")]
        assert not any(r.endswith(("control-plane", "master", "etcd")) for r in roles), f"{node.name} joined as {roles}"
    listed = ctx.api.get("/api/nodes")
    names = {n.get("name") for n in (listed if isinstance(listed, list) else listed.get("nodes", []))}
    assert {n.name for n in agents} <= names, f"Homestead lists {sorted(names)}"
    ctx.app("e2e-worker", node=agents[0].name, pinned=True)
    mark = ctx.mark("e2e-worker")
    assert ctx.has_mark("e2e-worker", mark)

    script = f"curl -sfL {RAW}/v{ctx.version}/scripts/install.sh -o /tmp/install.sh"
    for node in ctx.lab.nodes:
        out = node.ssh(f"{script} && sudo sh /tmp/install.sh --fix-safe < /dev/null; "
                       f"sudo sh /tmp/install.sh --report < /dev/null; echo EXIT=$?", check=False, timeout=900)
        code = out.strip().rsplit("EXIT=", 1)[-1].strip()
        log.info(f"{node.name}: doctor exit {code} after its safe fixes")
        assert code in ("0", "1"), f"{node.name}: failures remain after the safe fixes:\n{out[-3000:]}"

    before = sorted(p["metadata"]["uid"] for p in ctx.kube.items("pods", "-n", "lab", "-l", "app=homestead"))
    first = ctx.lab.nodes[0]
    env = installer_env(ctx.distro, ctx.version, first, "addons")
    out = first.ssh(f"{script} && sudo env {env} sh /tmp/install.sh --install --text < /dev/null 2>&1; echo EXIT=$?",
                    check=False, timeout=900)
    assert out.strip().endswith("EXIT=0"), f"the installer run again failed:\n{out[-3000:]}"
    assert "Already Installed" in out, f"the installer did not see Homestead installed:\n{out[-2000:]}"
    time.sleep(20)
    after = sorted(p["metadata"]["uid"] for p in ctx.kube.items("pods", "-n", "lab", "-l", "app=homestead"))
    assert before == after, "running the installer again restarted Homestead"
    ctx.kube.deployment_ready("lab", "homestead")
    assert ctx.has_mark("e2e-worker", mark), "the app on the worker lost data"
