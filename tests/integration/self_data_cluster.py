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
import subprocess
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
K3S = "rancher/k3s@sha256:627fc392e5e7992fb8aa738fb04bc0c9ae9a068999cb4cfbea5858661cd79d04"
IMAGE = "homestead:self-data-k3s-test"


def run(*args, data=None, timeout=300):
    result = subprocess.run(args, input=data, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"{args[:4]} failed: {result.stderr[-3000:]} {result.stdout[-3000:]}")
    return result.stdout.strip()


def log(*words):
    print(f"[{time.monotonic() - STARTED:6.1f}s]", *words, flush=True)


STARTED = time.monotonic()


def main():
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
            items.append({"apiVersion": "storage.k8s.io/v1", "kind": "StorageClass", "metadata": {"name": "fixture-target"},
                          "provisioner": "rancher.io/local-path", "volumeBindingMode": "WaitForFirstConsumer", "reclaimPolicy": "Retain"})
            kube("apply", "-f", "-", data=json.dumps({"apiVersion": "v1", "kind": "List", "items": items}))
            kube("-n", "lab", "wait", "--for=condition=available", "deployment/homestead", "--timeout=180s")
            log("Homestead available")
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
            service = json.loads(kube("-n", "lab", "get", "service", "homestead", "-o", "json"))
            url = "http://" + service["spec"]["clusterIP"] + ":8088"
            subprocess.run(["docker", "run", "--rm", "--network", "container:" + cid, "--read-only", "--tmpfs", "/tmp",
                         "--env", "FIXTURE_URL=" + url, "--env", "FIXTURE_BUDGET=600", "--mount", f"type=bind,source={ROOT},target=/repo,readonly",
                         "--entrypoint", "python3", IMAGE, "-u", "/repo/tests/integration/self-data-http.py"], timeout=660, check=True)
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
