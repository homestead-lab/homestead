"""Host power jobs: a reviewed reboot or shutdown, run as a job.

Host actions review a host's power change (homestead_power.plan); once the
review is accepted the job cordons and drains the host, stops what waits for
it, rechecks and sends the command, recording each phase so another replica
can carry it on if the drain evicts this one. An OS update of every host
(homestead_os_rollout) restarts each host the same way, with nobody there to
accept a warning.

server.py binds the cluster reads and writes and Homestead's own pod; its
background loop asks the job tray, whose resolver resumes a job here.
"""
import os
import threading
import time
import urllib.error
import urllib.parse

import homestead_leader as LEADER
import homestead_lifecycle as LC
import homestead_names as NAMES
import homestead_operations as OPS
import homestead_power as POWER
import homestead_power_hold as HOLD
import homestead_routes as ROUTER
import homestead_self as SELF

kget = ksend = None
_self_data_helper_image = homestead_running_on = None
# The Homestead replica a host power job is running in. A drain can evict
# that very replica; the job then says which pod to look for, and the leader
# carries it on from another replica (resume_power_job).
POD_NAME = os.environ.get("HOSTNAME", "")
_POWER_RESUMING = set()
_POWER_RESUME_LOCK = threading.Lock()


def bind(_kget, _ksend, helper_image, running_on, pod_name):
    """The cluster, the image and data claim Homestead runs with, and its pod."""
    global kget, ksend, _self_data_helper_image, homestead_running_on, POD_NAME
    kget, ksend, POD_NAME = _kget, _ksend, pod_name
    _self_data_helper_image, homestead_running_on = helper_image, running_on


class PowerNotSent(Exception):
    """A reviewed power request that stopped; the job it made says how far it got."""

    def __init__(self, message, operation):
        super().__init__(message)
        self.operation = operation


def active_power_job(node):
    """A reboot or shutdown job for this host that has not finished."""
    return next((item for item in OPS.list_operations()
                 if item.get("kind") == "node-power" and item.get("resource", {}).get("name") == node
                 and item.get("status") not in ("succeeded", "failed", "cancelled")), None)


def power_plan_with_job(power_plan):
    """A host whose power job is still going cannot be reviewed again: a
    second review during its drain is how the same drain started twice."""
    running = active_power_job(power_plan.get("node"))
    if running:
        power_plan = {**power_plan, "ready": False, "operation": running,
                      "blockers": ["A power job for this host is still running. Follow it in its progress view."]
                                  + list(power_plan.get("blockers") or [])}
    return power_plan


def run_power_job(operation_id, power_plan, force=False, resumed=False):
    """Cordon, drain, recheck and send a reviewed reboot or shutdown, recording
    each phase on the job. Cordon and drain may be repeated safely; the send is
    guarded by the job's own receipts."""
    node, action = power_plan["node"], power_plan["action"]
    planned_outage = bool(power_plan.get("planned_outage")) and not force
    phase_state = {"phase": "reviewed"}

    def power_progress(phase, percent, message, **details):
        updated = OPS.record_phase(operation_id, phase, percent, message, owner=POD_NAME or None, **details)
        phase_state["phase"] = phase
        return updated

    def hold():
        return hold_for_power(operation_id, power_plan, power_progress)

    def handoff(node, action, steps, rep, report):
        return send_handoff(operation_id, power_plan, steps, rep, report)
    try:
        result = LC.node_power(node, action, True,
                               before_send=(lambda: POWER.recheck_planned_outage(power_plan)) if planned_outage else
                               (lambda: POWER.recheck_forced(power_plan)) if force
                               else (lambda: POWER.recheck_after_drain(power_plan)),
                               reviewed_pods=power_plan["drain_pods"], progress=power_progress, force=force, planned_outage=planned_outage,
                               resumed=resumed, hold=None if force else hold,
                               send=handoff if planned_outage else None)
        result["operation"] = {"id": operation_id}
        return result
    except OPS.Superseded:
        raise  # another replica owns the job; nothing to record or send here
    except Exception as e:
        uncertain = phase_state["phase"] in ("sending", "observing")
        message = ("Power submission outcome is uncertain; inspect the existing job/helper before retrying" if uncertain else
                   "Power was not sent. Inspect the host's cordon state: " + str(e))
        if uncertain:
            power_progress("observing", 20, message)
        else:
            # Power was not sent: what was stopped to wait for the host starts
            # again now. The host stays cordoned, so an app tied to it waits
            # for scheduling to be allowed.
            restored = {}
            held = next((((i.get("ref") or {}).get("held") or []) for i in OPS._read() if i.get("id") == operation_id), [])
            if held:
                try:
                    started, left = HOLD.restore(held, operation_id)
                    restored = {"restored": {"started": started, "left": left, "at": time.time()}}
                    message += (". Started again: " + ", ".join(started)) if started else ""
                    message += (". Left as they are: " + ", ".join(left)) if left else ""
                except Exception as error:
                    message += f". What was stopped could not be started again ({str(error)[:120]}); start it by hand"
            power_progress("failed", 10, message, failed_phase=phase_state["phase"], **restored)
        raise PowerNotSent(message, {"id": operation_id}) from e


def send_handoff(operation_id, power_plan, steps, rep, report):
    """The last step on a cluster's only host. Homestead's data volume is on
    that host: going down with Homestead still writing to it left the volume
    faulted. So a helper on the host takes over - Homestead's own image and
    account - and Homestead stops itself; the helper waits for its volume to
    detach, then asks systemd for the reboot or power-off, and starts
    Homestead again on the new boot (homestead_power_handoff)."""
    node, action = power_plan["node"], power_plan["action"]
    path = f"/apis/apps/v1/namespaces/{SELF.NS}/deployments/{NAMES.BRAND}"
    try:
        own, image = _self_data_helper_image(kget, SELF.NS)
        claim, _ = homestead_running_on()
        replicas = int((kget(path).get("spec") or {}).get("replicas", 1) or 1)
    except Exception as error:
        report("verifying", 15, f"Homestead cannot hand the last step to the host ({str(error)[:160]}); sending the {action} directly")
        return LC._send_power(node, action, steps, rep, report)
    spec = own.get("spec") or {}
    pod_name = f"homestead-handoff-{action}-{int(time.time()) % 100000}"
    body = {"apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": pod_name, "namespace": SELF.NS, "labels": NAMES.labels("node-power")},
            "spec": {"nodeName": node, "hostPID": True, "restartPolicy": "OnFailure",
                     "serviceAccountName": spec.get("serviceAccountName") or "default",
                     "imagePullSecrets": spec.get("imagePullSecrets") or [],
                     "tolerations": [{"operator": "Exists"}],
                     "terminationGracePeriodSeconds": 1,
                     "containers": [{"name": "handoff", "image": image,
                                     "command": ["python3", "/srv/homestead_power_handoff.py", SELF.NS, NAMES.BRAND,
                                                 operation_id, action, power_plan["boot_id"], claim],
                                     "securityContext": {"privileged": True, "runAsUser": 0, "runAsGroup": 0},
                                     "resources": {"requests": {"cpu": "10m", "memory": "32Mi"}, "limits": {"memory": "128Mi"}}}]}}
    # The helper's identity is on the job before it exists, as for any power helper.
    report("sending", 20, "Handing the last step to a helper on the host; power has not been sent",
           helper_pod=pod_name, helper_namespace=SELF.NS, handoff=True, started_epoch=time.time())
    receipt = ksend("POST", f"/api/v1/namespaces/{SELF.NS}/pods", body)
    meta = (receipt or {}).get("metadata") or {}
    if not meta.get("uid") or meta.get("name") != pod_name:
        raise ValueError("The host helper was not confirmed; inspect it before retrying")
    report("observing", 20, f"Homestead is stopping so its data volume detaches; the helper on the host then sends the {action}. "
           "This page is offline until the host is back", helper_uid=meta["uid"])
    # Homestead stops last, marked like anything else held for the host, so
    # the helper starts it again - and only while that mark is the job's.
    ksend("PATCH", path, {"metadata": {"annotations": {HOLD.HELD_BY: operation_id, HOLD.HELD_AS: str(replicas)}},
                          "spec": {"replicas": 0}}, ctype="application/merge-patch+json")
    steps.append(f"handed the {action} to {pod_name}; Homestead stopped so its data volume detaches")
    return {"ok": True, "node": node, "action": action, "steps": steps, "helper_pod": pod_name, "quorum": rep}


def hold_for_power(operation_id, power_plan, progress):
    """Stop what waits for the host and live-migrate the VMs that move, then
    wait for them to leave it. Safe to repeat: a resumed job finds its own
    marks. Returns whether there was anything to do."""
    node, picks = power_plan["node"], power_plan.get("choices") or {}
    items = power_plan.get("hold") or []
    waiting = [i for i in items if picks.get(i["id"]) == "wait"]
    moving = [i for i in items if i["kind"] == "VirtualMachine" and picks.get(i["id"]) == "move"]
    if not waiting and not moving:
        return False
    progress("holding", 8, f"Stopping {len(waiting)} app(s) and VM(s) to wait for the host"
             + (f", moving {len(moving)} VM(s)" if moving else "") + "; power has not been sent")
    held = [HOLD.stop(item, operation_id) for item in waiting]
    progress("holding", 8, "Waiting for them to stop; power has not been sent", held=held)
    if moving:
        # A resumed job finds some already moved: only what is still here moves.
        here = {((v.get("metadata") or {}).get("namespace"), (v.get("metadata") or {}).get("name"))
                for v in kget("/apis/kubevirt.io/v1/virtualmachineinstances").get("items", [])
                if (v.get("status") or {}).get("nodeName") == node}
        for vm in moving:
            if (vm["ns"], vm["name"]) in here:
                HOLD.migrate(vm)
    deadline = time.monotonic() + 600
    while True:
        pods = [p for p in kget("/api/v1/pods").get("items", []) if (p.get("spec") or {}).get("nodeName") == node]
        vmis = [v for v in (kget("/apis/kubevirt.io/v1/virtualmachineinstances").get("items", []) if items and any(
            i["kind"] == "VirtualMachine" for i in items) else []) if (v.get("status") or {}).get("nodeName") == node]
        pending = [f"{i['ns']}/{i['name']}" for i in waiting + moving if not HOLD.gone(i, node, pods, vmis)]
        if not pending:
            return True
        if time.monotonic() >= deadline:
            raise ValueError("These did not stop or move within 10 minutes: " + ", ".join(pending[:6])
                             + ". What was stopped stays stopped until this job is released or the host is back")
        progress("holding", 8, "Waiting to stop or move: " + ", ".join(pending[:6]) + "; power has not been sent")
        time.sleep(3)


def restore_held(item, uncordon=True):
    """Start again what a power job stopped to wait for its host. Leader only,
    once; the job's resolver calls it and records what happened."""
    ref = item.get("ref") or {}
    if not LEADER.is_leader() or ref.get("restored") is not None:
        return ref.get("restored") is not None
    if uncordon and not ref.get("planned_outage") and not ref.get("cordoned_before"):
        # What waited is for this host; it cannot start here while cordoned.
        node = kget(f"/api/v1/nodes/{urllib.parse.quote(ref['node'], safe='')}")
        if ref.get("node_uid") and (node.get("metadata") or {}).get("uid") == ref["node_uid"]:
            LC.set_cordon(ref["node"], False)
            ref["uncordoned"] = True
    started, left = HOLD.restore(ref.get("held") or [], item["id"])
    ref["restored"] = {"started": started, "left": left, "at": time.time()}
    return True


def allow_scheduling(item):
    """Uncordon a host its power job cordoned, now it is back. Leader only,
    once; only the host the job was for, by identity."""
    ref = item.get("ref") or {}
    if not LEADER.is_leader():
        return False
    if ref.get("uncordoned"):
        return True
    node = kget(f"/api/v1/nodes/{urllib.parse.quote(ref['node'], safe='')}")
    if ref.get("node_uid") and (node.get("metadata") or {}).get("uid") != ref["node_uid"]:
        return False
    if (node.get("spec") or {}).get("unschedulable"):
        LC.set_cordon(ref["node"], False)
    ref["uncordoned"] = True
    return True


def release_held_power(operation_id):
    """Start what waits for a host on other hosts now, without waiting for it."""
    with OPS._lock:
        items = OPS._read()
        item = next((i for i in items if i.get("id") == operation_id), None)
        if not item or item.get("kind") != "node-power":
            raise ValueError("host power job not found")
        ref = item.get("ref") or {}
        if not ref.get("held") or ref.get("restored") is not None:
            raise ValueError("nothing is waiting for this host")
        if ref.get("phase") not in ("observing",):
            raise ValueError("wait until the power command has been sent")
        restore_held(item, uncordon=False)
        OPS._write(items)
    return {"ok": True, "detail": "Started again: " + (", ".join(ref["restored"]["started"]) or "nothing")}


def _power_in_background(operation_id, power_plan, force, resumed=False):
    def run():
        try:
            run_power_job(operation_id, power_plan, force, resumed)
        except (PowerNotSent, OPS.Superseded):
            pass  # recorded on the job, or carried on by another replica
        except Exception as error:  # never leave the job looking busy
            try:
                OPS.record_phase(operation_id, "failed", 10, f"Host power job stopped: {error}"[:400])
            except Exception:
                pass
        finally:
            with _POWER_RESUME_LOCK:
                _POWER_RESUMING.discard(operation_id)
    threading.Thread(target=run, name=f"node-power-{power_plan['node']}", daemon=True).start()


def send_reviewed_power(power_plan, force=False, background=False):
    """Cordon, drain and send a reviewed reboot or shutdown, as a job - what
    Host actions does once its review is accepted, and what an OS update of
    every host does for each host that needs a restart. background returns
    the job at once and leaves the work, and its outcome, to the job."""
    node, action = power_plan["node"], power_plan["action"]
    planned_outage = bool(power_plan.get("planned_outage")) and not force
    operation = OPS.start(
        "node-power", f"{action} {node}", {"kind": "Node", "name": node},
        "/nodes?node=" + urllib.parse.quote(node),
        {"node": node, "node_uid": power_plan["node_uid"], "action": action, "boot_id": power_plan["boot_id"],
         "choices": power_plan.get("choices") or {},
         # A host cordoned before the review stays so after it; one this job
         # cordoned is allowed scheduling again when it is back.
         "cordoned_before": bool(power_plan.get("cordoned")),
         "volumes": [v["name"] for v in power_plan["volumes"]],
         "planned_outage": planned_outage, "forced": bool(force),
         # What a resumed job needs: the reviewed plan, and which replica runs it.
         "plan": power_plan, "worker": POD_NAME,
         "phase": "reviewed", "phase_at": time.time(), "started_epoch": time.time()},
        "Planned whole-cluster outage; sending without cordon or drain" if planned_outage else
        "Forced by an admin; sending without cordon or drain" if force else "Host impact reviewed; preparing cordon and drain")
    if not background:
        result = run_power_job(operation["id"], power_plan, force)
        result["operation"] = operation
        return result
    with _POWER_RESUME_LOCK:
        _POWER_RESUMING.add(operation["id"])
    _power_in_background(operation["id"], power_plan, force)
    return {"operation": operation, "steps": [], "background": True}


def power_worker_gone(pod):
    """Whether the replica a power job named has gone - evicted by the drain."""
    try:
        found = kget(f"/api/v1/namespaces/{SELF.NS}/pods/{urllib.parse.quote(pod, safe='')}")
    except urllib.error.HTTPError as error:
        return error.code == 404
    except Exception:
        return False  # unknown is not gone
    if (found.get("metadata") or {}).get("deletionTimestamp") or \
            (found.get("status") or {}).get("phase") in ("Succeeded", "Failed"):
        return True
    # A host shut down or lost leaves its pods listed for minutes; one whose
    # node is not Ready is not running anything.
    node = (found.get("spec") or {}).get("nodeName")
    if not node:
        return False
    try:
        conditions = (kget(f"/api/v1/nodes/{urllib.parse.quote(node, safe='')}").get("status") or {}).get("conditions") or []
    except Exception:
        return False
    return not any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions)


def resume_power_job(item):
    """Carry on a host power job whose replica was evicted before it sent the
    command. Only the leader does, once; called from the job's resolver, so it
    writes nothing here - the thread claims the job, then cordons and drains
    again (both safe to repeat), rechecks and sends."""
    ref = item.get("ref") or {}
    if not LEADER.is_leader() or not isinstance(ref.get("plan"), dict):
        return False
    with _POWER_RESUME_LOCK:
        if item["id"] in _POWER_RESUMING:
            return True
        _POWER_RESUMING.add(item["id"])

    def claim_then_run():
        try:
            OPS.record_phase(item["id"], ref.get("phase", "draining"), item.get("progress", 10),
                             "Homestead moved while preparing this host; carrying on from another replica", worker=POD_NAME)
        except Exception:
            with _POWER_RESUME_LOCK:
                _POWER_RESUMING.discard(item["id"])
            return
        _power_in_background(item["id"], ref["plan"], bool(ref.get("forced")), resumed=True)
    threading.Thread(target=claim_then_run, name=f"node-power-resume-{ref.get('node', '')}", daemon=True).start()
    return True


SYSTEM_HOST_PATHS = ("/var/lib/kubelet", "/run", "/var/run", "/dev", "/sys", "/proc", "/lib/modules",
                     "/etc/localtime", "/var/log", "/var/lib/rancher", "/etc/rancher")


SYSTEM_POD_NAMESPACES = ("kube-system", "longhorn-system", "kubevirt", "cdi", "cattle-system", "system-upgrade",
                         "cattle-fleet-system", "harvester-system")


def _system_host_path(row):
    """A host path that is the host's own plumbing - kubelet's plugin and pod
    directories, sockets, devices, logs - not data a pod keeps there. Nor
    is anything Kubernetes or Longhorn itself mounts: an instance manager
    mounts / and /var/lib/longhorn, and the volumes it serves are judged by
    their copies, not by its mounts."""
    if not str(row.get("kind", "")).startswith("host-local path"):
        return False
    if str(row.get("pod") or "").split("/", 1)[0] in SYSTEM_POD_NAMESPACES:
        return True
    path = "/" + str(row.get("source") or "").strip("/")
    return any(path == p or path.startswith(p + "/") for p in SYSTEM_HOST_PATHS)


def rollout_reboot(node, allow_single_copy=False):
    """A restart for an OS update: the same review Host actions shows, with
    nobody to accept its warnings - so what a person would have to accept
    stops it, except a volume's only copy when the settings accept that."""
    power_plan = POWER.plan(node, "reboot")
    if power_plan.get("planned_outage"):
        raise ValueError("A single-host cluster outage needs a manual review and acknowledgement in Host actions")
    if not power_plan["ready"]:
        raise ValueError("; ".join(power_plan["blockers"]))
    if power_plan["stranded"]:
        raise ValueError("some workloads have no other host to run on")
    hold = power_plan.get("hold") or []
    if any(i["kind"] == "VirtualMachine" for i in hold):
        raise ValueError("Running VMs are on this host; migrate or stop them and review again")
    waits = [f"{i['ns']}/{i['name']}" for i in hold if i["default"] != "move"]
    if waits:
        raise ValueError("These would have to stop and wait for the host: " + ", ".join(waits[:6]))
    power_plan["choices"] = HOLD.choose(hold, None)
    if power_plan["requires_data_ack"] and not allow_single_copy:
        # What a person would have to accept, said as it is. A pod's emptyDir
        # is scratch space every drain deletes - on a k3s host metrics-server
        # and Traefik have one - and is no reason to leave a host unrestarted.
        reasons = []
        single = [v["claim"] for v in power_plan.get("volumes") or [] if v.get("risk") in ("unavailable", "single-copy")]
        if single:
            reasons.append("a volume has its only healthy copy on this host: " + ", ".join(single[:4]))
        # Nor are the host's own sockets and devices data: Longhorn's CSI
        # attacher mounts /var/lib/kubelet/plugins/driver.longhorn.io.
        kept = [f"{row['pod']} ({row['source']})" for row in (power_plan.get("maintenance") or {}).get("local_storage") or []
                if not str(row.get("kind", "")).startswith("emptyDir") and not _system_host_path(row)]
        if kept:
            reasons.append("pods keep data on this host itself: " + ", ".join(kept[:4]))
        if power_plan.get("storage_unknown"):
            reasons.append("Longhorn's volumes could not be read")
        if reasons:
            raise ValueError("; ".join(reasons) + " (the settings do not accept that)")
    try:
        return send_reviewed_power(power_plan)["operation"]["id"]
    except PowerNotSent as e:
        raise ValueError(str(e)) from e


def rollout_power_job(node, since):
    """A restart of this host started since the rollout began and not failed:
    the one a drained leader left running."""
    # The stored records: the public list leaves each job's ref out.
    for item in OPS._read():
        ref = item.get("ref") or {}
        if (item.get("kind") == "node-power" and ref.get("node") == node and ref.get("action") == "reboot"
                and float(ref.get("started_epoch") or 0) >= float(since or 0)
                and item.get("status") not in ("failed", "cancelled")):
            return item["id"]
    return ""


def _plan(request):
    q = request.query
    return power_plan_with_job(POWER.plan((q.get("node") or [""])[0], (q.get("action") or [""])[0],
                                          force=(q.get("force") or [""])[0] == "1"))


def _send_power(request):
    """Host actions' send: refused (409) whenever the review it carries no
    longer holds, so the browser shows the plan again."""
    b = request.body
    if b.get("confirm") != b.get("node"):
        raise ValueError("confirmation must repeat the node name")
    force = b.get("force") is True
    running = active_power_job(b.get("node"))
    if running:
        return ROUTER.Reply(409, {"error": "A power job for this host is still running; follow it instead of sending another",
                                  "operation": running})
    power_plan = POWER.plan(b["node"], b["action"], force=force)
    if not power_plan["ready"]:
        return ROUTER.Reply(409, {"error": "; ".join(power_plan["blockers"]), "plan": power_plan})
    if b.get("review_token") != power_plan["review_token"]:
        return ROUTER.Reply(409, {"error": "host impact changed; review the plan again", "plan": power_plan})
    if power_plan.get("planned_outage") and b.get("allow_cluster_outage") is not True:
        return ROUTER.Reply(409, {"error": "acknowledge the whole-cluster outage before host power control",
                                  "plan": power_plan})
    power_plan["choices"] = HOLD.choose(power_plan.get("hold") or [], b.get("choices") or {})
    stranded = {(w["ns"], w["name"]) for w in power_plan["stranded"]}
    if any((i["ns"], i["name"]) in stranded and power_plan["choices"].get(i["id"]) == "move"
           for i in power_plan.get("hold") or []) and not b.get("allow_stranded"):
        return ROUTER.Reply(409, {"error": "some workloads have no eligible failover host", "plan": power_plan})
    if power_plan["requires_data_ack"] and not b.get("allow_data_risk"):
        return ROUTER.Reply(409, {"error": "acknowledge the volume risk before host power control", "plan": power_plan})
    # Cordon and drain can take many minutes: the request returns the job at
    # once and the browser follows its phases.
    return ROUTER.Reply(202, send_reviewed_power(power_plan, force, background=True))


ROUTES = {
    ("GET", "/api/node/power/plan"): ("viewer", _plan),
    ("POST", "/api/node/power"): ("admin", _send_power),
    ("POST", "/api/node/power/release"): ("operator", lambda request: release_held_power(str(request.body.get("id") or ""))),
}
