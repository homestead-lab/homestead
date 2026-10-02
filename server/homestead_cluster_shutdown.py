"""Reviewed cluster shutdown, coordinated independently of Homestead's data disk.

The ConfigMap is both a singleton lock and a durable journal. Power helpers only
act on a short-lived commit for their own boot, after every consumer has left.
Neither a missing API nor an expired worker is evidence that a host is off.
"""
import hashlib
import json
import os
import re
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

import homestead_maintenance as M

NAME = "homestead-cluster-shutdown"
ANNOTATION = "homestead.io/cluster-shutdown"
KIND = "cluster-shutdown"
CONFIRM = "SHUT DOWN CLUSTER"
LH = "/apis/longhorn.io/v1beta2/volumes"
INFRA = {"kube-system", "longhorn-system"}
LIFETIME = 1800
ADMISSION = "/apis/admissionregistration.k8s.io/v1/"


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def optional(get, path):
    try:
        return get(path)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


def inventory(get, path, optional_api=False):
    value = optional(get, path) if optional_api else get(path)
    if value is None:
        return []
    if not isinstance(value.get("items"), list) or value.get("metadata", {}).get("continue"):
        raise ValueError("Incomplete shutdown inventory: " + path)
    return value["items"]


def identity(obj):
    m = obj.get("metadata", {})
    return [m.get("namespace", ""), m["name"], m["uid"]]


def node_row(node):
    return {"name": node["metadata"]["name"], "uid": node["metadata"]["uid"],
            "boot_id": node.get("status", {}).get("nodeInfo", {}).get("bootID", ""),
            "cordoned": node.get("spec", {}).get("unschedulable", False),
            "ready": any(c.get("type") == "Ready" and c.get("status") == "True"
                         for c in node.get("status", {}).get("conditions", []))}


def helper(pod, uid):
    return bool(uid) and any(o.get("kind") == "ConfigMap" and o.get("uid") == uid
                            for o in pod.get("metadata", {}).get("ownerReferences", []))


def consumer(pod):
    return M.drainable(pod) and pod.get("metadata", {}).get("namespace") not in INFRA


def admission_pods(get, pods):
    """Keep webhook backends alive so eviction and recovery can still be admitted."""
    services = set()
    for kind in ("mutatingwebhookconfigurations", "validatingwebhookconfigurations"):
        for config in inventory(get, ADMISSION + kind):
            for webhook in config.get("webhooks", []):
                service = webhook.get("clientConfig", {}).get("service")
                if service:
                    services.add((service["namespace"], service["name"]))
    keep = []
    for ns, name in sorted(services):
        service = get(f"/api/v1/namespaces/{ns}/services/{name}")
        selector = service.get("spec", {}).get("selector")
        if not selector:
            raise ValueError(f"Admission service {ns}/{name} has no pod selector; its shutdown dependency cannot be verified")
        keep.extend(p for p in pods if p["metadata"].get("namespace") == ns and
                    all(p["metadata"].get("labels", {}).get(k) == v for k, v in selector.items()) and M.drainable(p))
    return {p["metadata"]["uid"]: p for p in keep}


class Shutdown:
    def __init__(self, get, send, namespace, pod, image, enabled=True, busy=lambda: [],
                 clock=time.time, sleep=time.sleep):
        self.get, self.send, self.ns, self.pod = get, send, namespace, pod
        self.image, self.enabled, self.busy = image, enabled, busy
        self.clock, self.sleep = clock, sleep
        self.path = f"/api/v1/namespaces/{namespace}/configmaps/{NAME}"

    def read(self):
        return optional(self.get, self.path)

    def state(self):
        cm = self.read()
        return json.loads(cm["data"]["state"]) if cm else None

    def change(self, fn, uid=None):
        # Compare-and-swap also serializes cancellation against final commit.
        for _ in range(8):
            cm = self.read()
            if not cm or (uid and cm["metadata"]["uid"] != uid):
                raise ValueError("Shutdown journal identity changed; power was not authorized")
            fn(cm["data"])
            try:
                return self.send("PUT", self.path, cm)
            except urllib.error.HTTPError as error:
                if error.code != 409:
                    raise
        raise ValueError("Shutdown journal is busy; inspect its progress")

    def review(self, journal_uid=None):
        nodes = sorted((node_row(n) for n in inventory(self.get, "/api/v1/nodes")), key=lambda n: n["name"])
        pods = inventory(self.get, "/api/v1/pods")
        own = self.get(f"/api/v1/namespaces/{self.ns}/pods/{self.pod}")
        admission = admission_pods(self.get, pods)
        targets = [p for p in pods if consumer(p) and not helper(p, journal_uid) and p["metadata"]["uid"] not in admission]
        problems = []
        if any(v.get("persistentVolumeClaim") for p in admission.values() for v in p.get("spec", {}).get("volumes", [])):
            problems.append("An admission webhook uses a persistent volume; its dependency needs a separate shutdown plan")
        if not self.enabled:
            problems.append("Host power control is disabled on this installation")
        if not nodes or any(not n["ready"] or not n["boot_id"] or not n["uid"] for n in nodes):
            problems.append("Every host must be Ready with a verified identity and boot ID")
        own_node = own.get("spec", {}).get("nodeName")
        if own_node not in {n["name"] for n in nodes} or not any(identity(p) == identity(own) for p in targets):
            problems.append("Homestead's running pod and host could not be verified")
        deployment = self.get(f"/apis/apps/v1/namespaces/{self.ns}/deployments/homestead")
        selector = deployment.get("spec", {}).get("selector")
        siblings = [p for p in pods if p.get("metadata", {}).get("namespace") == self.ns
                    and M.selected(selector, p.get("metadata", {}).get("labels", {}))
                    and p.get("status", {}).get("phase") not in ("Succeeded", "Failed")]
        if deployment.get("spec", {}).get("replicas", 1) != 1 or len(siblings) != 1 or identity(siblings[0]) != identity(own):
            problems.append("Shutdown requires exactly one running Homestead replica")
        vmis = inventory(self.get, "/apis/kubevirt.io/v1/virtualmachineinstances", True)
        live_vms = ["/".join(identity(v)[:2]) for v in vmis if v.get("status", {}).get("phase") not in ("Succeeded", "Failed")]
        if live_vms:
            problems.append("Gracefully stop these VMs first: " + ", ".join(live_vms[:12]))
        maintenance = M.inventory(self.get, targets)
        problems.extend(maintenance["blockers"])
        problems.extend(str(v) for v in self.busy())
        volumes = inventory(self.get, LH, True)
        pvs = inventory(self.get, "/api/v1/persistentvolumes")
        handles = {v["metadata"]["name"] for v in volumes}
        if any(p.get("spec", {}).get("csi", {}).get("driver") == "driver.longhorn.io" and
               p["spec"]["csi"].get("volumeHandle") not in handles for p in pvs):
            problems.append("A Longhorn persistent volume is missing from the volume inventory")
        if any(v.get("status", {}).get("robustness") in ("faulted", "unknown") for v in volumes):
            problems.append("Repair faulted or unknown Longhorn volumes before shutting down")
        old = self.state()
        if old and old["phase"] != "released" and not journal_uid:
            problems.append("An existing shutdown needs attention; open its progress")
        snapshot = {"nodes": nodes, "pods": M.pod_snapshot(targets), "own": identity(own),
                    "own_node": own_node, "image": self.image,
                    "service_account": own.get("spec", {}).get("serviceAccountName"),
                    "pull_secrets": own.get("spec", {}).get("imagePullSecrets", []),
                    "admission": M.pod_snapshot(list(admission.values())),
                    "volumes": sorted(identity(v) for v in volumes), "budgets": maintenance["budgets"]}
        if not snapshot["service_account"] or not re.search(r"@sha256:[a-f0-9]{64}$", self.image):
            problems.append("A verified Homestead image digest and service account are required")
        if len(encode(snapshot)) > 400000:
            problems.append("This cluster exceeds the shutdown journal size limit")
        return {"ready": not problems, "blockers": problems, "review_token": hashlib.sha256(encode(snapshot).encode()).hexdigest(),
                "confirm": CONFIRM, "nodes": nodes, "homestead_node": own_node, "pods": len(targets) - 1,
                "vms": live_vms, "volumes": len(volumes), "local_storage": maintenance["local_storage"],
                "snapshot": snapshot}

    def pod_body(self, state, uid, mode, node, index=None):
        suffix = "coordinator" if index is None else str(index)
        args = ["python3", "/srv/homestead_cluster_shutdown.py", mode, self.ns, uid, state["run"]]
        if index is not None:
            args.append(str(index))
        spec = {"nodeName": node, "restartPolicy": "Never", "activeDeadlineSeconds": LIFETIME + 120,
                "serviceAccountName": state["plan"]["service_account"],
                "imagePullSecrets": state["plan"]["pull_secrets"], "tolerations": [{"operator": "Exists"}],
                "containers": [{"name": "shutdown", "image": self.image, "command": args,
                                "resources": {"requests": {"cpu": "10m", "memory": "32Mi"}, "limits": {"memory": "128Mi"}}}]}
        if mode == "agent":
            spec.update(hostPID=True, hostNetwork=True)
            spec["containers"][0]["securityContext"] = {"privileged": True, "runAsUser": 0, "runAsGroup": 0}
        return {"apiVersion": "v1", "kind": "Pod",
                "metadata": {"name": f"homestead-shutdown-{state['run']}-{suffix}", "namespace": self.ns,
                             "labels": {"homestead.io/task": KIND},
                             "ownerReferences": [{"apiVersion": "v1", "kind": "ConfigMap", "name": NAME, "uid": uid}]},
                "spec": spec}

    def start(self, body, ops):
        if body.get("confirm") != CONFIRM:
            raise ValueError("Type " + CONFIRM + " to confirm the entire cluster outage")
        plan = self.review()
        if not plan["ready"]:
            raise ValueError("; ".join(plan["blockers"]))
        if body.get("review_token") != plan["review_token"]:
            raise ValueError("Cluster impact changed; review shutdown again")
        state = {"run": uuid.uuid4().hex[:16], "phase": "preparing", "progress": 0,
                 "message": "Preparing independent shutdown helpers; no host power sent",
                 "deadline": self.clock() + LIFETIME, "plan": plan["snapshot"], "review_token": plan["review_token"]}
        previous = self.read()
        cm = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": NAME, "namespace": self.ns},
              "data": {"state": encode(state)}}
        if previous:
            if json.loads(previous["data"]["state"])["phase"] != "released":
                raise ValueError("A shutdown already exists; inspect it before retrying")
            cm["metadata"]["resourceVersion"] = previous["metadata"]["resourceVersion"]
        cm = self.send("PUT" if previous else "POST", self.path if previous else self.path.rsplit("/", 1)[0], cm)
        uid = cm["metadata"]["uid"]
        op = ops.start(KIND, "Shut down cluster", {"kind": "Cluster", "name": "cluster"}, "/cluster",
                       {"run": state["run"], "namespace": self.ns}, state["message"])
        # The singleton journal is retained even if submission/its reply is lost.
        self.send("POST", f"/api/v1/namespaces/{self.ns}/pods",
                  self.pod_body(state, uid, "coordinator", plan["homestead_node"]))
        return {"operation": op, "state": state}

    def cancel(self):
        def update(data):
            state = json.loads(data["state"])
            if "commit" in data or state["phase"] in ("handoff", "released"):
                raise ValueError("The final power handoff cannot be cancelled")
            data["cancel"] = "true"
        self.change(update)
        return {"ok": True}

    def volumes_detached(self):
        volumes = inventory(self.get, LH, not bool(self.state()["plan"]["volumes"]))
        expected = self.state()["plan"]["volumes"]
        if sorted(identity(v) for v in volumes) != expected:
            raise ValueError("Longhorn volume inventory changed; power was not authorized")
        attached = [v["metadata"]["name"] for v in volumes
                    if v.get("status", {}).get("state") != "detached" or v.get("spec", {}).get("nodeID")]
        attached.extend("CSI attachment " + a["metadata"]["name"] for a in
                        inventory(self.get, "/apis/storage.k8s.io/v1/volumeattachments")
                        if a.get("status", {}).get("attached") is not False)
        return attached

    def restore(self, plan, run):
        for n in plan["nodes"]:
            obj = self.get("/api/v1/nodes/" + n["name"])
            current = node_row(obj)
            if current["uid"] != n["uid"]:
                raise ValueError("Host identity changed; inspect scheduling on " + n["name"])
            if obj["metadata"].get("annotations", {}).get(ANNOTATION) != run:
                continue
            changes = [{"op": "test", "path": "/metadata/resourceVersion", "value": obj["metadata"]["resourceVersion"]},
                       {"op": "remove", "path": "/metadata/annotations/homestead.io~1cluster-shutdown"}]
            if not n["cordoned"]:
                changes.append({"op": "add", "path": "/spec/unschedulable", "value": False})
            self.send("PATCH", "/api/v1/nodes/" + n["name"], changes, ctype="application/json-patch+json")

    def recover(self, run):
        cm = self.read()
        state = json.loads(cm["data"]["state"])
        if run != state["run"] or state["phase"] == "released":
            raise ValueError("Shutdown changed; refresh its progress")
        # A timed-out helper can never consume an old commit after recovery.
        if state["phase"] != "failed" and self.clock() <= state["deadline"] + 180:
            raise ValueError("Wait for the shutdown helper deadline before recovering scheduling")
        live = [node_row(self.get("/api/v1/nodes/" + n["name"])) for n in state["plan"]["nodes"]]
        if any(not n["ready"] for n in live):
            raise ValueError("Every original host must be Ready before recovering scheduling")
        if "commit" in cm["data"] and any(n["boot_id"] == old["boot_id"] for n, old in zip(live, state["plan"]["nodes"])):
            raise ValueError("A committed shutdown requires every host to return with a new boot ID. Inspect hosts that stayed on before recovery")
        pods = inventory(self.get, "/api/v1/pods")
        if any(helper(p, cm["metadata"]["uid"]) and self.run_pod(p, run)
               and p.get("status", {}).get("phase") not in ("Succeeded", "Failed") for p in pods):
            raise ValueError("Shutdown helpers have not all terminated; inspect their status before recovering scheduling")
        self.restore(state["plan"], run)
        def release(data):
            current = json.loads(data["state"])
            if current["run"] != run:
                raise ValueError("Shutdown identity changed")
            current.update(phase="released", message="Scheduling restored; inspect workload and storage health", progress=100)
            data["state"] = encode(current)
        self.change(release, cm["metadata"]["uid"])
        return {"ok": True}

    @staticmethod
    def run_pod(pod, run):
        return pod["metadata"]["name"].startswith("homestead-shutdown-" + run + "-")

    def public_state(self):
        cm = self.read()
        if not cm:
            return None
        state = json.loads(cm["data"]["state"])
        state["hosts"] = [{"name": n["name"], "state": "Power timer accepted" if cm["data"].get("sent-" + str(i)) else
                           "Helper ready" if cm["data"].get("ready-" + str(i)) == n["boot_id"] else "Waiting for helper"}
                          for i, n in enumerate(state["plan"]["nodes"])]
        return state

    def progress(self, item):
        state = self.state()
        if not state or state["run"] != item["ref"]["run"]:
            return "failed", 0, "Shutdown journal changed; inspect cluster and host power state"
        phase = state["phase"]
        status = "succeeded" if phase == "released" else "failed" if phase == "failed" or self.clock() > state["deadline"] else "running"
        message = state["message"]
        if self.clock() > state["deadline"] and phase not in ("released", "failed"):
            message = "Shutdown observation ended. Check physical host power; open Cluster → Shut down cluster for recovery"
        return status, state["progress"], message


class Coordinator:
    def __init__(self, shutdown, uid, run):
        self.s, self.uid, self.run = shutdown, uid, run
        self.plan = None

    def current(self):
        cm = self.s.read()
        if not cm or cm["metadata"]["uid"] != self.uid:
            raise ValueError("Shutdown journal was replaced")
        state = json.loads(cm["data"]["state"])
        if state["run"] != self.run or self.s.clock() >= state["deadline"]:
            raise ValueError("Shutdown expired; power was not authorized")
        if cm["data"].get("cancel"):
            raise ValueError("Shutdown cancelled before final power handoff")
        return cm, state

    def report(self, phase, percent, message, commit=False):
        def update(data):
            state = json.loads(data["state"])
            if state["run"] != self.run or data.get("cancel") or self.s.clock() >= state["deadline"]:
                raise ValueError("Shutdown cancelled or expired before power handoff")
            state.update(phase=phase, progress=percent, message=message)
            data["state"] = encode(state)
            if commit:
                data["commit"] = encode({"run": self.run, "until": min(state["deadline"], self.s.clock() + 30)})
        self.s.change(update, self.uid)

    def nodes_unchanged(self):
        live = sorted((node_row(n) for n in inventory(self.s.get, "/api/v1/nodes")), key=lambda n: n["name"])
        if [(n["name"], n["uid"], n["boot_id"], n["ready"]) for n in live] != [
                (n["name"], n["uid"], n["boot_id"], True) for n in self.plan["nodes"]]:
            raise ValueError("Host membership, readiness or boot identity changed; power was not sent")

    def consumers(self, pods):
        admission = admission_pods(self.s.get, pods)
        if M.pod_snapshot(list(admission.values())) != [tuple(row) for row in self.plan["admission"]]:
            raise ValueError("Admission webhook dependencies changed during shutdown")
        return [p for p in pods if consumer(p) and not helper(p, self.uid) and p["metadata"]["uid"] not in admission]

    def drain(self, own=False):
        deadline = min(self.s.clock() + 600, self.current()[1]["deadline"])
        reviewed = {row[2] for row in self.plan["pods"]}
        sent = set()
        while True:
            self.current()
            self.nodes_unchanged()
            pods = inventory(self.s.get, "/api/v1/pods")
            targets = [p for p in self.consumers(pods)
                       if bool(identity(p) == self.plan["own"]) == own
                       and p.get("spec", {}).get("nodeName")]
            if not targets:
                return
            if any(p["metadata"]["uid"] not in reviewed for p in targets):
                raise ValueError("A new application pod started during shutdown; review again")
            pending = []
            for pod in targets:
                ns, name, uid = identity(pod)
                pending.append(ns + "/" + name)
                if uid in sent or pod["metadata"].get("deletionTimestamp"):
                    continue
                try:
                    self.s.send("POST", f"/api/v1/namespaces/{ns}/pods/{name}/eviction",
                                {"apiVersion": "policy/v1", "kind": "Eviction", "metadata": {"name": name, "namespace": ns},
                                 "deleteOptions": {"preconditions": {"uid": uid}}})
                    sent.add(uid)
                except urllib.error.HTTPError as error:
                    if error.code not in (404, 429):
                        raise
            self.report("stopping-homestead" if own else "draining", 70 if own else 35,
                        "Waiting for graceful eviction (including disruption budgets): " + ", ".join(pending[:6]))
            if self.s.clock() >= deadline:
                raise ValueError("Drain timed out: " + ", ".join(pending[:6]) + ". No power sent")
            self.s.sleep(2)

    def execute(self):
        cm = self.s.read()
        if not cm or cm["metadata"]["uid"] != self.uid:
            raise ValueError("Shutdown journal was replaced")
        state = json.loads(cm["data"]["state"])
        if state["run"] != self.run:
            raise ValueError("Shutdown identity changed")
        self.plan = state["plan"]
        try:
            self.current()
            review = self.s.review(self.uid)
            if not review["ready"] or review["review_token"] != state["review_token"]:
                raise ValueError("Cluster changed before drain: " + "; ".join(review["blockers"]))
            for i, node in enumerate(self.plan["nodes"]):
                self.current()
                self.s.send("POST", f"/api/v1/namespaces/{self.s.ns}/pods",
                            self.s.pod_body(state, self.uid, "agent", node["name"], i))
            until = self.s.clock() + 180
            while True:
                cm, _ = self.current()
                waiting = [n["name"] for i, n in enumerate(self.plan["nodes"]) if cm["data"].get("ready-" + str(i)) != n["boot_id"]]
                if not waiting:
                    break
                self.report("preparing", 10, "Checking independent power helper on " + ", ".join(waiting))
                if self.s.clock() > until:
                    raise ValueError("Power helpers did not become ready; no hosts drained or powered off")
                self.s.sleep(2)
            for n in self.plan["nodes"]:
                self.current()
                self.nodes_unchanged()
                obj = self.s.get("/api/v1/nodes/" + n["name"])
                if bool(obj.get("spec", {}).get("unschedulable")) != n["cordoned"]:
                    raise ValueError("Host scheduling changed after review")
                self.s.send("PATCH", "/api/v1/nodes/" + n["name"],
                            [{"op": "test", "path": "/metadata/uid", "value": n["uid"]},
                             {"op": "test", "path": "/metadata/resourceVersion", "value": obj["metadata"]["resourceVersion"]},
                             {"op": "add", "path": "/metadata/annotations", "value": {**obj["metadata"].get("annotations", {}), ANNOTATION: self.run}},
                             {"op": "add", "path": "/spec/unschedulable", "value": True}], ctype="application/json-patch+json")
            self.report("draining", 25, "All hosts cordoned; draining application pods. Homestead stays online")
            self.drain()
            self.report("stopping-homestead", 65, "Applications drained. Independent coordinator will stop Homestead next; this page will disconnect")
            self.s.sleep(10)  # Give connected browsers a chance to display the handoff.
            self.drain(own=True)
            until = self.s.clock() + 300
            while True:
                self.current()
                self.nodes_unchanged()
                attached = self.s.volumes_detached()
                if not attached:
                    break
                self.report("detaching", 80, "Waiting for Longhorn volumes to detach: " + ", ".join(attached[:6]))
                if self.s.clock() > until:
                    raise ValueError("Longhorn volumes did not detach; no host power sent")
                self.s.sleep(2)
            # Check again immediately before publishing the one-way handoff.
            self.drain()
            self.drain(own=True)
            self.nodes_unchanged()
            if self.s.volumes_detached():
                raise ValueError("A Longhorn volume reattached before power handoff")
            self.report("handoff", 90, "Power handoff committed. Host timers request power-off in 30 seconds; Homestead's host in 90 seconds. Physical power is unverified", commit=True)
        except Exception as error:
            cm = self.s.read()
            if not cm or cm["metadata"]["uid"] != self.uid or "commit" in cm["data"]:
                raise  # A lost commit response must never trigger recovery.
            message = str(error)
            try:
                self.s.restore(self.plan, self.run)
                message += ". Original scheduling restored; check workloads before retrying"
            except Exception as recovery:
                message += ". Scheduling recovery needs attention: " + str(recovery)
            def failed(data):
                current = json.loads(data["state"])
                if current["run"] != self.run or "commit" in data:
                    raise ValueError("Shutdown handoff changed; recovery stopped")
                current.update(phase="failed", message=message[:1500])
                data["state"] = encode(current)
            self.s.change(failed, self.uid)


HOST = ["nsenter", "-t", "1", "-m", "-u", "-i", "-n", "-p", "--", "sh", "-c"]


def agent(shutdown, uid, run, index, execute=subprocess.check_output):
    coordinator = Coordinator(shutdown, uid, run)
    _, state = coordinator.current()
    node = state["plan"]["nodes"][index]
    boot = execute(HOST + ["set -e; command -v systemctl >/dev/null; command -v systemd-run >/dev/null; cat /proc/sys/kernel/random/boot_id"], timeout=15).decode().strip()
    if boot != node["boot_id"]:
        raise ValueError("Host boot changed; power helper disarmed")
    def ready(data):
        if json.loads(data["state"])["run"] != run or "commit" in data or data.get("cancel"):
            raise ValueError("Shutdown already changed; helper disarmed")
        data["ready-" + str(index)] = boot
    shutdown.change(ready, uid)
    while True:
        try:
            cm, state = coordinator.current()
        except (OSError, urllib.error.URLError):
            if shutdown.clock() >= state["deadline"]:
                return
            shutdown.sleep(2)
            continue
        if state["phase"] in ("failed", "released"):
            return
        commit = json.loads(cm["data"].get("commit", "null"))
        if commit:
            if commit["run"] != run or shutdown.clock() >= commit["until"]:
                return
            delay = 90 if node["name"] == state["plan"]["own_node"] else 30
            if not re.fullmatch(r"[a-f0-9]{16}", run):
                raise ValueError("Invalid shutdown identity")
            # systemd owns this timer after the pod/API stops. No shell input
            # comes from the request; the run ID and delay are checked here.
            execute(HOST + [f'systemd-run --unit=homestead-shutdown-{run} --on-active={delay}s --timer-property=AccuracySec=1s "$(command -v systemctl)" poweroff'], timeout=15)
            print(f"Power timer accepted on {node['name']}; request in {delay} seconds; physical power unverified", flush=True)
            def sent(data):
                if json.loads(data["state"])["run"] != run:
                    raise ValueError("Shutdown journal changed after power timer was accepted")
                data["sent-" + str(index)] = "true"
            shutdown.change(sent, uid)
            return
        shutdown.sleep(1)


def main():
    mode, namespace, uid, run = sys.argv[1:5]
    host = os.environ["KUBERNETES_SERVICE_HOST"]
    root = "https://" + ("[" + host + "]" if ":" in host else host) + ":" + os.environ.get("KUBERNETES_SERVICE_PORT", "443")
    sa = "/var/run/secrets/kubernetes.io/serviceaccount/"
    context = ssl.create_default_context(cafile=sa + "ca.crt")
    def request(method, path, body=None, ctype="application/json"):
        with open(sa + "token", encoding="utf-8") as handle:
            token = handle.read().strip()
        req = urllib.request.Request(root + path, method=method, data=encode(body).encode() if body is not None else None,
                                     headers={"Authorization": "Bearer " + token, "Content-Type": ctype})
        with urllib.request.urlopen(req, context=context, timeout=15) as response:
            raw = response.read()
            return json.loads(raw) if raw else {}
    s = Shutdown(lambda path: request("GET", path), request, namespace, "", "")
    state = s.state()
    s.pod, s.image = state["plan"]["own"][1], state["plan"]["image"]
    if mode == "agent":
        agent(s, uid, run, int(sys.argv[5]))
    elif mode == "coordinator":
        Coordinator(s, uid, run).execute()
    else:
        raise ValueError("Unknown shutdown helper mode")


if __name__ == "__main__":
    main()
