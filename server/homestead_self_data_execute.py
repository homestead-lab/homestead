"""One source-side flow: saved intent → helpers → drain → independent mover.

Only this live invocation may prepare the handoff. Polling/restarting the app
never resumes setup or repeats a lost creation. After publication the existing
Kubernetes-resident coordinator owns the move; source files are no longer written.
"""
import copy
import json
import time
import urllib.request

import homestead_self_data_anchor as A
import homestead_self_data_bootstrap as B
import homestead_self_data_setup as P
import homestead_self_data_worker as W
from homestead_storage_journal import Held, identity, shape

KIND = "self-data-handoff"


def idle(ops, own_id=None):
    if any(i["id"] != own_id and (i.get("status") not in ops.TERMINAL or i.get("ref", {}).get("retain_resources")) for i in ops._read()):
        raise Held("Finish running jobs and review retained recovery jobs before moving Homestead's data")
    return True


def enqueue(ops, execution):
    """Called under the shared operations lock, after server-side approval."""
    idle(ops)
    cfg, scope = execution["config"], execution["scope"]
    return ops.start(KIND, "Move Homestead data", {"kind": "PersistentVolumeClaim", "name": cfg["destination"], "namespace": scope.namespace},
        "/settings", {"namespace": scope.namespace, "deployment": scope.deployment, "operation": scope.operation,
        "destination": cfg["destination"], "source": execution["source"]["metadata"]["name"],
        "retain_resources": True, "setup_state": "intent"}, "Preparing the move; Homestead is still online")


def handshake(pod, execution, anchor_uid):
    # Fixed cluster-private DNS, no redirects/proxies, no user-supplied URL.
    scope = execution["scope"]
    name = pod["metadata"]["name"]
    expected = {"operation": scope.operation, "anchor_uid": anchor_uid, "worker_uid": identity(pod)["uid"]}
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *_): return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    req = urllib.request.Request(f"http://{name}.{scope.namespace}.svc:8081/identity",
        headers={"Authorization": "Bearer " + execution["status_token"]})
    try:
        with opener.open(req, timeout=5) as response:
            return response.status == 200 and json.loads(response.read(4097)) == expected
    except Exception:
        return False


def resolve(item, read, *, clock=time.time):
    """Existing Jobs UI reports only; it cannot execute or replay the move."""
    ref = item["ref"]
    if ref.get("setup_error"):
        return "failed", 0, ref["setup_error"]
    if not ref.get("anchor_uid"):
        return "running", 0, "Preparing the move. If Homestead restarted, inspect this job; setup will not be replayed."
    try:
        anchor = A.Anchor(read, None, ref["namespace"], ref["deployment"]).load(operation=ref["operation"], uid=ref["anchor_uid"])
        view = W.progress(anchor, clock())
        if view["status"] == "done":
            ref["retain_resources"] = False
            return "succeeded", 100, view["message"]
        return ("failed" if view["requires_review"] else "running"), 0, view["message"]
    except Exception:
        return "running", 0, "Cannot verify move progress. Both volumes are retained; no action has been repeated."


class Execution:
    def __init__(self, read, send, ops, execution, job, *, directory, barrier, recheck, clock=time.time, wait=time.sleep, probe=handshake):
        self.read, self.send, self.ops, self.execution = read, send, ops, execution
        self.job_id, self.directory, self.barrier = job["id"], directory, barrier
        self.recheck, self.clock, self.wait, self.probe = recheck, clock, wait, probe

    def checkpoint(self, **fields):
        with self.ops._lock:
            item = next(i for i in self.ops._read() if i["id"] == self.job_id)
            item["ref"].update(fields)
            self.ops.checkpoint(item)

    def run(self):
        e, ops = self.execution, self.ops
        scope, cfg = e["scope"], e["config"]
        anchor = A.Anchor(self.read, self.send, scope.namespace, scope.deployment)
        try:
            # The HTTP request has returned/released its shared activity lock.
            # A live source remains usable throughout helper preparation.
            with self.barrier.activity():
                with ops._lock:
                    idle(ops, self.job_id)
                    handle = anchor.create(operation=scope.operation,
                        deployment={"name": scope.deployment, **identity(e["deployment"])},
                        source={"name": e["source"]["metadata"]["name"], **identity(e["source"])},
                        destination=cfg["destination"], replicas=e["deployment"]["spec"].get("replicas", 1))
                    self.checkpoint(anchor_uid=handle["uid"], setup_state="helpers")
                setup = B.Setup(anchor, scope, image=e["image"], node=cfg["worker_node"], status_digest=e["status_digest"],
                    approval=e["approval"], clock=self.clock, route=e.get("route"),
                    handshake=lambda pod: self.probe(pod, e, handle["uid"]))
                while not setup.step()["complete"]: pass
            deadline = self.clock() + 300
            while True:
                try:
                    with self.barrier.activity(): worker = setup.worker_fact()
                    break
                except Held as error:
                    if not str(error).startswith("Waiting for") or self.clock() >= deadline: raise
                    self.wait(2)
            with self.barrier.activity():
                fact = lambda obj: {"name": obj["metadata"]["name"], "uid": identity(obj)["uid"], "shape": shape(obj)}
                plan = {"deployment_shape": shape(e["deployment"]), "source_pvc_shape": shape(e["source"]),
                    "source_pv": fact(e["source_pv"]), "destination_pvc": fact(e["destination"]),
                    "destination_pv": fact(e["destination_pv"]), "worker": worker, "nodes": e["nodes"],
                    "data_volume": "data", "target_shareable": "ReadWriteMany" in e["destination"]["spec"]["accessModes"],
                    "copy_image": e["image"], "copy_node": cfg["copy_node"], "admission": e["policy"]}
                plan["copy_preflight"] = setup.preflight_copy(plan)
                anchor.configure(plan)
                self.checkpoint(setup_state="handoff")
            def final_check():
                idle(ops, self.job_id)
                if setup.worker_fact() != worker: raise Held("The move helper changed before shutdown")
                return self.recheck(e, worker)
            P.publish_after_drain(self.directory, anchor, self.barrier, final_check)
            # No source file writes after this point, even a 'job succeeded'.
            return handle
        except Exception as error:
            # A published marker refuses this write too: its external anchor is
            # then the authority. Never remove a fence to save an error message.
            message = str(error) if isinstance(error, Held) else "Move preparation stopped. Inspect the retained job and helpers before trying again."
            try: self.checkpoint(setup_error=message[:500])
            except Exception: pass
            return None
