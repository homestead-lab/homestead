"""kubectl from the runner, for fixtures and for looking - never in place of
Homestead's own actions, which scenarios drive through its API."""
import json
import subprocess
import tempfile
import time

from . import log


class Kube:
    def __init__(self, kubeconfig):
        self.kubeconfig = kubeconfig

    def run(self, *args, data=None, check=True, timeout=120):
        cmd = ["kubectl", "--kubeconfig", self.kubeconfig, *args]
        log.debug("$ kubectl " + " ".join(args))
        result = subprocess.run(cmd, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)
        if check and result.returncode:
            raise RuntimeError(f"kubectl {' '.join(args[:4])} failed: {result.stderr[-2000:]}")
        return result.stdout

    def get(self, *args):
        return json.loads(self.run("get", *args, "-o", "json"))

    def items(self, *args):
        return self.get(*args).get("items", [])

    def apply(self, manifest):
        return self.run("apply", "-f", "-", data=manifest if isinstance(manifest, str) else json.dumps(manifest))

    def node_names(self):
        return sorted(n["metadata"]["name"] for n in self.items("nodes"))

    def wait(self, what, check, timeout=900, every=5):
        """Until check() is truthy; its last answer is in the error."""
        deadline, last = time.time() + timeout, None
        while time.time() < deadline:
            try:
                last = check()
                if last:
                    return last
            except Exception as error:     # the API itself may be away mid-reboot
                last = f"{type(error).__name__}: {str(error)[:200]}"
            time.sleep(every)
        raise TimeoutError(f"{what} did not happen within {timeout}s (last: {str(last)[:300]})")

    def nodes_ready(self, count):
        def ready():
            nodes = self.items("nodes")
            good = [n for n in nodes if any(c["type"] == "Ready" and c["status"] == "True" for c in n["status"]["conditions"])]
            return len(good) == count and good
        return self.wait(f"{count} Ready host(s)", ready)

    def deployment_ready(self, ns, name, timeout=900):
        def ready():
            d = self.get("deployment", "-n", ns, name)
            want = d["spec"].get("replicas", 1)
            s = d.get("status", {})
            return (s.get("readyReplicas", 0) == want and s.get("updatedReplicas", 0) == want
                    and s.get("observedGeneration", 0) >= d["metadata"]["generation"]) and d
        return self.wait(f"{ns}/{name} ready", ready, timeout)

    def volume(self, claim_ns, claim):
        pvc = self.get("pvc", "-n", claim_ns, claim)
        return self.get("volumes.longhorn.io", "-n", "longhorn-system", pvc["spec"]["volumeName"])
