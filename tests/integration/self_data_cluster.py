"""Real API + local-path rehearsal in a disposable Docker k3s cluster.

Run: python tests/integration/self_data_cluster.py
Requires Docker (privileged containers), ~4 GiB for k3s, and network for images.
No kubeconfig or host ports are used. This is not a CSI detach/host-loss test.

k3s starts first and boots while the image builds. On failure it prints what
a person would look at - every pod, recent events, Homestead's log and each
move helper's - and every wait has a limit inside the CI job's own, so a
stall fails here with that picture rather than hanging the runner.
"""
import json
import os
import secrets
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
K3S = "rancher/k3s@sha256:627fc392e5e7992fb8aa738fb04bc0c9ae9a068999cb4cfbea5858661cd79d04"
IMAGE = "homestead:self-data-k3s-test"
# A preset setup code, fresh each run, in the format Homestead checks (#737).
SETUP_CODE = "".join(secrets.choice("0123456789ABCDEFGHJKMNPQRSTVWXYZ") for _ in range(10))


def run(*args, data=None, timeout=300):
    result = subprocess.run(args, input=data, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"{args[:4]} failed: {result.stderr[-3000:]} {result.stdout[-3000:]}")
    return result.stdout.strip()


def log(*words):
    print(f"[{time.monotonic() - STARTED:6.1f}s]", *words, flush=True)


STARTED = time.monotonic()
# The whole fixture, every docker and kubectl call together: each has its own
# timeout, but their sum could outlast the CI job, which then dies with no
# log. This stops it first, with the diagnostics and the cleanup.
WATCHDOG = int(os.environ.get("FIXTURE_WATCHDOG", "720"))


def _overdue(signum, frame):
    raise TimeoutError(f"the fixture ran past {WATCHDOG}s")


def revert_while_stopped(kube, image):
    """What going back after the switch (#1214) asks of the real Deployment controller,
    which tests/fakes/self_data_move.py can't show: at 0 replicas, the template moves to
    claim B and then, in one write with the replica count, back to claim A. That hash
    has an earlier ReplicaSet. The pod must mount A and never B, and status must reach
    what Coordinator._ready asks. On a throwaway Deployment: the moves come after this."""
    ns, name = "revert-test", "revert"
    def claim(which):
        return {"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": {"name": which, "namespace": ns},
                "spec": {"storageClassName": "local-path", "accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": "16Mi"}}}}
    workload = {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": name, "namespace": ns}, "spec": {
        "replicas": 1, "strategy": {"type": "Recreate"}, "selector": {"matchLabels": {"app": name}},
        "template": {"metadata": {"labels": {"app": name}}, "spec": {
            "terminationGracePeriodSeconds": 1,
            "containers": [{"name": "app", "image": image, "imagePullPolicy": "IfNotPresent", "command": ["python3", "-c", "import time; time.sleep(86400)"],
                            "volumeMounts": [{"name": "data", "mountPath": "/data"}]}],
            "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "claim-a"}}]}}}}
    def read(*what): return json.loads(kube("-n", ns, "get", *what, "-o", "json"))
    def pods(): return read("pods", "-l", "app=" + name)["items"]
    def mounts(pod): return {v["persistentVolumeClaim"]["claimName"] for v in pod["spec"]["volumes"] if "persistentVolumeClaim" in v}
    def write(**change):
        dep = read("deployment", name)
        # Unconditional: a controller's status write after the read above would fail a replace that kept it.
        dep.pop("status", None); dep["metadata"].pop("resourceVersion", None)
        if "replicas" in change: dep["spec"]["replicas"] = change["replicas"]
        if "claim" in change: dep["spec"]["template"]["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"] = change["claim"]
        kube("replace", "-f", "-", data=json.dumps(dep))
    def wait_for(what, done, limit=120):
        deadline = time.monotonic() + limit
        while True:
            found = done()
            if found: return found
            if time.monotonic() >= deadline: raise AssertionError(f"revert_while_stopped: {what} within {limit}s: {read('deployment', name).get('status')}")
            time.sleep(1)
    def stopped():
        dep = read("deployment", name)
        return dep["status"].get("observedGeneration", 0) >= dep["metadata"]["generation"] and not dep["status"].get("replicas") and not pods()
    def back():
        rows = pods()
        if any("claim-b" in mounts(pod) for pod in rows): raise AssertionError("revert_while_stopped: a pod mounted the claim the Deployment went back from")
        dep = read("deployment", name)
        status = dep["status"]
        return (status.get("observedGeneration", 0) >= dep["metadata"]["generation"]
                and all(status.get(key) == 1 for key in ("replicas", "updatedReplicas", "readyReplicas", "availableReplicas"))
                and len(rows) == 1 and mounts(rows[0]) == {"claim-a"} and rows[0]["status"].get("phase") == "Running")
    kube("create", "namespace", ns)
    kube("apply", "-f", "-", data=json.dumps({"apiVersion": "v1", "kind": "List", "items": [claim("claim-a"), claim("claim-b"), workload]}))
    kube("-n", ns, "wait", "--for=condition=available", "deployment/" + name, "--timeout=120s")
    write(replicas=0)
    wait_for("the stop", stopped)
    write(claim="claim-b")
    wait_for("the switch at 0 replicas", stopped)
    write(claim="claim-a", replicas=1)
    wait_for("back on the first claim", back)
    for rs in read("replicasets", "-l", "app=" + name)["items"]:
        claims = {v["persistentVolumeClaim"]["claimName"] for v in rs["spec"]["template"]["spec"]["volumes"]}
        assert rs["spec"]["replicas"] == (1 if claims == {"claim-a"} else 0), rs["metadata"]["name"]
    # Waits for its first consumer: Pending proves no pod ever tried it, which a 1 s poll can't.
    pending = read("pvc", "claim-b")
    assert pending["status"].get("phase") == "Pending" and not pending["spec"].get("volumeName"), pending["status"]
    log("Going back at 0 replicas: the controller started the first claim's template")
    # Only now: on a failure the caller prints pods and events, which need it still there.
    kube("delete", "namespace", ns, "--timeout=90s")


def main():
    if hasattr(signal, "SIGALRM"):
        signal.signal(signal.SIGALRM, _overdue)
        signal.alarm(WATCHDOG)
    with tempfile.TemporaryDirectory(prefix="homestead-release-") as directory:
        archive = Path(directory, "image.tar")
        # k3s boots (a minute or so) while the image builds and is saved.
        cid = run("docker", "run", "-d", "--privileged", "--memory", "4g", "--cpus", "3",
                  "--label", "homestead.release-test=true", "--mount", f"type=bind,source={ROOT},target=/repo,readonly",
                  "--mount", f"type=bind,source={directory},target=/fixture,readonly", K3S,
                  "server", "--disable", "traefik", "--disable", "servicelb", "--node-name", "release-test", timeout=180)
        log("Disposable k3s fixture:", cid)
        try:
            if not os.environ.get("HOMESTEAD_TEST_IMAGE_BUILT"):
                run("docker", "build", "-t", IMAGE, str(ROOT), timeout=600)
            run("docker", "save", "-o", str(archive), IMAGE)
            log("Image built and saved")
        except Exception:
            run("docker", "rm", "-f", "-v", cid)
            raise
        stop_watch = threading.Event()
        def kube(*args, data=None): return run("docker", "exec", "-i", cid, "kubectl", *args, data=data)
        try:
            deadline = time.monotonic() + 180
            while True:
                try:
                    kube("wait", "--for=condition=Ready", "node/release-test", "--timeout=5s")
                    break
                except RuntimeError:
                    if time.monotonic() >= deadline: raise
                    time.sleep(2)
            log("Node ready")
            run("docker", "exec", cid, "ctr", "images", "import", "/fixture/image.tar")
            ref = "docker.io/library/" + IMAGE
            rows = run("docker", "exec", cid, "ctr", "images", "list").splitlines()
            digest = next(row.split()[2] for row in rows if row.split()[0] == ref)
            assert digest.startswith("sha256:") and len(digest) == 71
            pinned = "docker.io/library/homestead@" + digest
            run("docker", "exec", cid, "ctr", "images", "tag", ref, pinned)
            stream = kube("create", "--dry-run=client", "--validate=false", "-f", "/repo/deploy/deploy.yaml", "-o", "json")
            decoder, items = json.JSONDecoder(), []
            while stream.strip():
                item, end = decoder.raw_decode(stream.lstrip()); stream = stream.lstrip()[end:]
                if item["kind"] not in {"Namespace", "ServiceAccount", "ClusterRole", "ClusterRoleBinding", "Role", "RoleBinding", "PersistentVolumeClaim", "Deployment", "Service"}: continue
                if item["kind"] == "PersistentVolumeClaim":
                    item["spec"].update(storageClassName="local-path", accessModes=["ReadWriteOnce"])
                    item["spec"]["resources"]["requests"]["storage"] = "256Mi"
                if item["kind"] == "Deployment":
                    item["spec"]["replicas"] = 1
                    spec = item["spec"]["template"]["spec"]
                    for container in spec["containers"] + spec.get("initContainers", []): container["image"] = pinned
                    for env in spec["containers"][0]["env"]:
                        if env["name"] == "STORAGE_CLASS": env["value"] = "local-path"
                        if env["name"] == "LB_IP": env["value"] = ""
                if item["kind"] == "Service":
                    item["spec"]["type"] = "ClusterIP"
                    for key in ("loadBalancerIP", "externalTrafficPolicy"): item["spec"].pop(key, None)
                    item["metadata"].pop("annotations", None)
                items.append(item)
            # The setup code set ahead, as the installer's HS_SETUP_CODE does (#737).
            items.append({"apiVersion": "v1", "kind": "Secret", "type": "Opaque", "metadata": {"name": "homestead-setup", "namespace": "lab"},
                          "stringData": {"code": SETUP_CODE}})
            items.append({"apiVersion": "storage.k8s.io/v1", "kind": "StorageClass", "metadata": {"name": "fixture-target"},
                          "provisioner": "rancher.io/local-path", "volumeBindingMode": "WaitForFirstConsumer", "reclaimPolicy": "Retain"})
            kube("apply", "-f", "-", data=json.dumps({"apiVersion": "v1", "kind": "List", "items": items}))
            kube("-n", "lab", "wait", "--for=condition=available", "deployment/homestead", "--timeout=180s")
            log("Homestead available")
            revert_while_stopped(kube, pinned)
            def watch_controllers():
                previous = {}
                def differences(a, b, path=""):
                    if isinstance(a, dict) and isinstance(b, dict):
                        return [d for key in set(a) | set(b) for d in differences(a.get(key), b.get(key), path + "/" + key)]
                    return [path] if a != b else []
                while not stop_watch.is_set():
                    try:
                        for rs in json.loads(kube("-n", "lab", "get", "replicasets", "-o", "json"))["items"]:
                            name, meta = rs["metadata"]["name"], rs["metadata"]
                            value = {"spec": rs["spec"], **{k: meta.get(k) for k in ("annotations", "labels", "ownerReferences")}}
                            if name in previous:
                                changed = differences(previous[name], value)
                                if changed: print("ReplicaSet changed fields:", sorted(changed), flush=True)
                            previous[name] = value
                    except Exception: pass
                    stop_watch.wait(1)
            threading.Thread(target=watch_controllers, daemon=True).start()
            # A coordinator lost in the middle of each move, once Homestead is stopped.
            # First its pod is deleted; in the second move its process is told to stop, so
            # the pod ends Failed (a stop must not exit 0, or the Job would be Complete). The
            # real Job controller has to start another each time, and each move can only
            # finish if that one carries on from the record. Pods of a move name its operation.
            lost = {}
            def lose_the_coordinator():
                while not stop_watch.is_set() and len(lost) < 2:
                    try:
                        stopped = json.loads(kube("-n", "lab", "get", "deployment", "homestead", "-o", "json"))["spec"]["replicas"] == 0
                        pods = json.loads(kube("-n", "lab", "get", "pods", "-l", "homestead.io/handoff-worker", "-o", "json"))["items"]
                        running = [p for p in pods if (p.get("status") or {}).get("phase") == "Running"
                                   and not p["metadata"].get("deletionTimestamp")
                                   and p["metadata"]["labels"]["homestead.io/self-data-handoff"] not in lost]
                        if stopped and running:
                            name, operation = running[0]["metadata"]["name"], running[0]["metadata"]["labels"]["homestead.io/self-data-handoff"]
                            if not lost:
                                kube("-n", "lab", "delete", "pod", name, "--wait=false")
                            else:
                                kube("-n", "lab", "exec", name, "--", "python3", "-c", "import os, signal; os.kill(1, signal.SIGTERM)")
                            lost[operation] = name
                            log("Lost the coordinator on purpose:", name, "(deleted)" if len(lost) == 1 else "(process stopped)")
                    except Exception: pass
                    stop_watch.wait(1)
            threading.Thread(target=lose_the_coordinator, daemon=True).start()
            service = json.loads(kube("-n", "lab", "get", "service", "homestead", "-o", "json"))
            url = "http://" + service["spec"]["clusterIP"] + ":8088"
            subprocess.run(["docker", "run", "--rm", "--network", "container:" + cid, "--read-only", "--tmpfs", "/tmp",
                         "--env", "FIXTURE_URL=" + url, "--env", "FIXTURE_SETUP_CODE=" + SETUP_CODE, "--env", "FIXTURE_BUDGET=600", "--mount", f"type=bind,source={ROOT},target=/repo,readonly",
                         "--entrypoint", "python3", IMAGE, "-u", "/repo/tests/integration/self-data-http.py"], timeout=660, check=True)
            assert len(lost) == 2, "A move never gave the fixture a coordinator to lose while Homestead was stopped: " + str(lost)
            pvcs = json.loads(kube("-n", "lab", "get", "pvc", "-o", "json"))["items"]
            assert len(pvcs) == 3 and all(p["status"]["phase"] == "Bound" for p in pvcs), "Both old data volumes must be retained"
            # Helpers are retired in the background once a move is done: they
            # must be gone within a minute, not at the instant the move ends.
            deadline = time.monotonic() + 60
            while True:
                helpers = json.loads(kube("-n", "lab", "get", "pods", "-l", "homestead.io/self-data-handoff", "-o", "json"))["items"]
                if not helpers or time.monotonic() >= deadline:
                    break
                time.sleep(2)
            assert not helpers, "Temporary move helpers must be retired: " + ", ".join(p["metadata"]["name"] for p in helpers)
            log("PASS: three retained PVCs; no move helper pods remain")
        except BaseException:
            log("FAILED: what the cluster looked like")
            diagnose(kube)
            raise
        finally:
            if hasattr(signal, "SIGALRM"):
                signal.alarm(0)         # the cleanup below must not be cut short
            stop_watch.set()
            # Only the exact container just created, including its disposable
            # anonymous storage. No host kubeconfig or user cluster is touched.
            run("docker", "rm", "-f", "-v", cid)


def diagnose(kube):
    """What a person would look at: pods, events, Homestead's log, and each
    move helper's and any pod that is stuck."""
    def show(title, *args):
        try:
            print(f"--- {title}", flush=True)
            print(kube(*args), flush=True)
        except Exception as error:
            print(f"(could not read: {str(error)[:200]})", flush=True)
    show("pods", "get", "pods", "-A", "-o", "wide")
    show("persistent volume claims", "-n", "lab", "get", "pvc", "-o", "wide")
    show("recent events", "get", "events", "-A", "--sort-by=.lastTimestamp")
    show("Homestead's log", "-n", "lab", "logs", "deployment/homestead", "--all-containers", "--tail=80")
    try:
        pods = json.loads(kube("-n", "lab", "get", "pods", "-o", "json"))["items"]
    except Exception:
        pods = []
    for pod in pods:
        name, phase = pod["metadata"]["name"], (pod.get("status") or {}).get("phase")
        labels = pod["metadata"].get("labels") or {}
        if "homestead.io/self-data-handoff" in labels or any("self-data" in str(v) for v in labels.values()):
            show(f"helper {name} log", "-n", "lab", "logs", name, "--all-containers", "--tail=60")
        if phase not in ("Running", "Succeeded"):
            show(f"{name} ({phase})", "-n", "lab", "describe", "pod", name)


if __name__ == "__main__": main()
