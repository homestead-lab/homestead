"""Real API + local-path rehearsal in a disposable Docker k3s cluster.

Run: python tests/integration/self_data_cluster.py
Requires Docker (privileged containers), ~4 GiB for k3s, and network for images.
No kubeconfig or host ports are used. This is not a CSI detach/host-loss test.
"""
import json
import subprocess
import tempfile
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


def main():
    run("docker", "build", "-t", IMAGE, str(ROOT), timeout=600)
    with tempfile.TemporaryDirectory(prefix="homestead-release-") as directory:
        archive = Path(directory, "image.tar")
        run("docker", "save", "-o", str(archive), IMAGE)
        cid = run("docker", "run", "-d", "--privileged", "--memory", "4g", "--cpus", "3",
                  "--label", "homestead.release-test=true", "--mount", f"type=bind,source={ROOT},target=/repo,readonly",
                  "--mount", f"type=bind,source={directory},target=/fixture,readonly", K3S,
                  "server", "--disable", "traefik", "--disable", "servicelb", "--node-name", "release-test")
        print("Disposable k3s fixture:", cid, flush=True)
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
            kube("-n", "lab", "wait", "--for=condition=available", "deployment/homestead", "--timeout=120s")
            service = json.loads(kube("-n", "lab", "get", "service", "homestead", "-o", "json"))
            url = "http://" + service["spec"]["clusterIP"] + ":8088"
            subprocess.run(["docker", "run", "--rm", "--network", "container:" + cid, "--read-only", "--tmpfs", "/tmp",
                         "--env", "FIXTURE_URL=" + url, "--mount", f"type=bind,source={ROOT},target=/repo,readonly",
                         "--entrypoint", "python3", IMAGE, "/repo/tests/integration/self-data-http.py"], timeout=900, check=True)
            pvcs = json.loads(kube("-n", "lab", "get", "pvc", "-o", "json"))["items"]
            assert len(pvcs) == 3 and all(p["status"]["phase"] == "Bound" for p in pvcs), "Both old data volumes must be retained"
            helpers = json.loads(kube("-n", "lab", "get", "pods", "-l", "homestead.io/self-data-handoff", "-o", "json"))
            assert not helpers["items"], "Temporary move helpers must be retired"
            print("PASS: three retained PVCs; no move helper pods remain", flush=True)
        except Exception:
            for args in (("-n", "lab", "get", "pods", "-o", "wide"), ("-n", "lab", "logs", "deployment/homestead", "--tail=35")):
                try: print(kube(*args), flush=True)
                except Exception: pass
            raise
        finally:
            # Only the exact container just created, including its disposable
            # anonymous storage. No host kubeconfig or user cluster is touched.
            run("docker", "rm", "-f", "-v", cid)


if __name__ == "__main__": main()
