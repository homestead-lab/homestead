"""Explicit operator recovery, reachable through Kubernetes exec, not status HTTP.

Preview is read-only. Confirmation repeats live fencing and capacity checks and
CAS-writes one recovery receipt. It never rewrites unknown journal outcomes,
accepts replacement UIDs, removes a local marker or deletes a data volume.
"""
import argparse
import copy
import json
import os
import time

import homestead_self_data_coordinator as C
from homestead_storage_journal import Held, digest, identity, shape


def review(anchor, read, logs, action, *, clock=time.time):
    """Every held phase has one reviewed way out (stuck-jobs C1, C2):

    - prepare, nothing sent: Homestead was never stopped. return-original
      keeps it as it is, with no write; resume retries the stop's checks.
    - prepare after the stop, quiesce, copy, verify, switch before its
      request: resume, or return-original (restart on the original volume).
    - after the switch request: only resume, forward to the new volume;
      returning to the original volume could lose writes made on the new one.
    """
    state = anchor.state
    writes = state["journal"]["ref"]["storage_writes"]
    if (action not in ("resume", "return-original") or state.get("runtime", {}).get("state") != "held"
            or state["phase"] not in ("prepare", "quiesce", "copy", "verify", "switch", "start")
            or any(e["state"] != "accepted" for e in writes)):
        raise Held("Recovery needs a held move with known request outcomes")
    cutover = any(e["step"] in ("switch", "start") for e in writes)
    untouched = state["phase"] == "prepare" and not writes
    if cutover and action != "resume":
        raise Held("Cutover has been requested; Homestead can only resume onto its new volume, never return to the original")
    if action == "resume" and (state.get("copy_receipt", {}).get("state") == "uncertain"
                               or state.get("recovery", {}).get("action") == "return-original"):
        raise Held("An uncertain copy or original-volume recovery cannot resume the move")
    started = clock()
    candidate = copy.copy(anchor)
    candidate.state = copy.deepcopy(state)
    candidate.state["runtime"]["state"] = "running"
    observations = {}
    def current(path):
        obj = read(path)
        if isinstance(obj.get("items"), list):
            rows = obj["items"]
        else:
            rows = [obj]
        # Lease freshness is verified separately; controller status/version churn
        # must not invalidate every operator confirmation between heartbeats.
        if "/leases/" not in path and not path.startswith("/apis/metrics.k8s.io/"):
            observations[path] = sorted([(r["metadata"]["uid"], shape(r)) for r in rows])
        return obj
    def forbidden(*_): raise Held("Recovery preview cannot write cluster resources")
    engine = C.Coordinator(candidate, current, forbidden, logs, None,
                           worker_uid=state["plan"]["worker"]["uid"], clock=clock)
    dep = engine._environment()
    volumes = [v for v in dep["spec"]["template"]["spec"].get("volumes", []) if v["name"] == state["plan"]["data_volume"]]
    selected = state["destination"] if cutover else state["source"]["name"]
    if len(volumes) != 1 or volumes[0].get("persistentVolumeClaim", {}).get("claimName") != selected:
        raise Held("The Deployment no longer selects the reviewed " + ("new" if cutover else "original") + " volume")
    known = engine._entry("copy-job")
    restarting = engine._entry("recover-start")
    released = engine._entry("release-copy") or engine._entry("recover-release-copy")
    if cutover:
        # The coordinator rechecks admission before its start request; once
        # that is sent, a pod outside Homestead on either volume still holds.
        if engine._entry("start"): engine._ready(dep)
        elif not engine._quiet(dep): raise Held("Wait for normal Homestead pod removal before reviewing recovery")
    elif untouched:
        if dep["spec"].get("replicas") != state["replicas"]:
            raise Held("Homestead's replica count changed after review; inspect it before recovery")
    elif restarting:
        if action != "return-original": raise Held("Original-volume restart is already committed")
        engine._ready(dep, original=True)  # also detects unexpected data consumers
    elif not engine._quiet(dep, helper_uid=known["after"]["uid"] if known else None):
        raise Held("Wait for normal Homestead pod removal before reviewing recovery")
    if cutover:
        if state.get("copy_receipt", {}).get("state") != "verified":
            raise Held("Resuming onto the new volume needs a verified copy receipt")
        if not released or engine.writer.observe(released) is not None:
            raise Held("The verified copy Job has no completed release; inspect it before recovery")
    elif known and not released:
        job = engine.writer.observe(known)
        if action == "resume":
            receipt = engine._copy_receipt(job, [p for p in engine._pods() if C.owner(p, "Job", known["after"]["uid"])])
            if state["phase"] != "copy" and not receipt:
                raise Held("The retained copy has no completion evidence")
    elif released:
        if engine.writer.observe(released) is not None or not restarting and not engine._quiet(dep):
            raise Held("Wait for the copy Job and its mounts to be removed")
    result = {"action": action, "operation": state["operation"], "anchor": identity(anchor.obj),
              "phase": state["phase"], "source": state["source"]["name"], "destination": state["destination"],
              "retention_policy": "keep_both_volumes"}
    if action == "return-original" and not restarting and not untouched and "admission" in state["plan"]:
        from homestead_self_data_admission import review as capacity
        proposed = copy.deepcopy(dep); proposed["spec"]["replicas"] = state["replicas"]
        from homestead_self_data_fence import pin_app_image
        pin_app_image(proposed, state["plan"]["copy_image"])
        report = capacity(current, anchor.namespace, "restart", proposed, state["plan"]["nodes"],
                          state["plan"]["admission"]["threshold"], clock=clock)
        result["restart"], result["warnings"] = report["receipt"], report["warnings"]
    if not 0 <= clock() - started <= 30:
        raise Held("Recovery inspection took too long; obtain a fresh review")
    result["fingerprint"] = digest({"review": result, "observations": observations})
    return result


def confirm(anchor, read, logs, action, fingerprint, *, clock=time.time):
    result = review(anchor, read, logs, action, clock=clock)
    if result["fingerprint"] != fingerprint:
        raise Held("Recovery facts changed; review the current proposal before confirming")
    anchor.recover(result, max(int(clock()), anchor.state["runtime"]["checked_at"]))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--action", choices=("resume", "return-original"), required=True)
    parser.add_argument("--confirm", help="Fingerprint from the immediately preceding preview")
    args = parser.parse_args(argv)
    try:
        import homestead_self_data_anchor as A
        import homestead_self_data_kube as K
        import homestead_self_data_launch as L
        config, scope = L.parse_configuration(os.environ.get(L.CONFIG_ENV))
        client = K.Client(scope)
        anchor = A.Anchor(client.read, client.send, scope.namespace, scope.deployment).load(
            operation=scope.operation, uid=config["anchor_uid"])
        result = (confirm(anchor, client.read, client.logs, args.action, args.confirm) if args.confirm else
                  review(anchor, client.read, client.logs, args.action))
        print(json.dumps({**result, "accepted": bool(args.confirm)}, indent=2))
        return 0
    except Held as error:
        print(str(error)); return 1
    except Exception:
        print("Recovery could not be verified; inspect the retained record. No request was retried.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
