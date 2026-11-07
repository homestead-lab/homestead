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

import homestead_self_data_anchor as A
import homestead_self_data_coordinator as C
from homestead_storage_journal import Held, digest, identity, shape


def _still_there(engine):
    names = ", ".join(engine.waited_for[:3])
    return "Wait for normal Homestead pod removal before reviewing recovery" + (f" (still there: {names})" if names else "")


def review(anchor, read, logs, action, *, clock=time.time):
    """Every held phase has one reviewed way out (stuck-jobs C1, C2):

    - prepare, nothing sent: Homestead was never stopped. return-original
      keeps it as it is, with no write; resume retries the stop's checks.
    - prepare after the stop, quiesce, copy, verify, switch, and after the
      switch request up to the start request: resume, or return-original (restart
      on the original volume). After the switch Homestead has stayed at 0 copies,
      so nothing was written to the new volume, and one write goes back.
    - after the start request: only resume, forward to the new volume;
      returning to the original volume could lose writes made on the new one.
    """
    state = anchor.state
    writes = state["journal"]["ref"]["storage_writes"]
    if (action not in ("resume", "return-original") or state.get("runtime", {}).get("state") != "held"
            or state["phase"] not in ("prepare", "quiesce", "copy", "verify", "switch", "start")
            or any(e["state"] != "accepted" for e in writes)):
        raise Held("Recovery needs a held move with known request outcomes")
    switched = any(e["step"] == "switch" for e in writes)
    start_requested = any(e["step"] == "start" for e in writes)
    cutover = action == "resume" and (switched or start_requested)
    untouched = state["phase"] == "prepare" and not writes
    if start_requested and action != "resume":
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
    engine.going_back = action == "return-original"
    if action == "resume" and A.stop_check_expired(state, clock()):
        raise Held("The copy check expired before Homestead was stopped, so resuming can't get past it: going back is the only "
                   "way. It changes nothing, as Homestead was not stopped. Preview --action return-original, then start a new move")
    target = A.worker_target(state)
    # Forward from the switch on, nothing more is placed against the review, so resuming
    # ignores host changes; _hosts below still measures the new volume's host and every pod in use.
    if action == "resume" and not cutover and target and target["kind"] == "Job":
        changes = engine.cluster_changes()
        if changes:
            raise Held("Resuming needs the cluster as it was reviewed, and " + ", ".join(changes) + ". "
                       "Preview --action return-original, which does not need it unless the original volume or Homestead's pods are on a missing host")
    try:
        dep = engine._environment()
    except C.HostRemoved as gone:
        if action == "resume" and not start_requested:
            raise Held(f"{gone}. Or preview --action return-original, which needs it only if the original volume is on it") from None
        raise
    except C.HostDown as down:
        names = ", ".join(down.hosts)
        if action == "return-original":
            raise Held(f"Going back needs {names}, which is down: the original volume or one of Homestead's pods is on it. "
                       "Bring it back, then preview again. Nothing was changed") from None
        raise Held(f"Resuming needs {names}, which is down. Bring it back, then preview again. Nothing was changed"
                   + (". If it was removed, wait for Homestead's pod on it to leave Kubernetes" if down.removed else "." if start_requested else ". Or preview --action return-original, which needs it only if the original volume is on it")) from None
    volumes = [v for v in dep["spec"]["template"]["spec"].get("volumes", []) if v["name"] == state["plan"]["data_volume"]]
    restarting = engine._entry("recover-start")
    on_new = switched and not restarting
    selected = state["destination"] if on_new else state["source"]["name"]
    if len(volumes) != 1 or volumes[0].get("persistentVolumeClaim", {}).get("claimName") != selected:
        raise Held("The Deployment no longer selects the reviewed " + ("new" if on_new else "original") + " volume")
    known = engine._entry("copy-job")
    released = engine._entry("release-copy") or engine._entry("recover-release-copy")
    if cutover:
        # The coordinator rechecks admission before its start request; once
        # that is sent, a pod outside Homestead on either volume still holds.
        if engine._entry("start"): engine._ready(dep)
        elif not engine._quiet(dep): raise Held(_still_there(engine))
    elif untouched:
        if dep["spec"].get("replicas") != state["replicas"]:
            raise Held("Homestead's replica count changed after review; inspect it before recovery")
    elif restarting:
        if action != "return-original": raise Held("Original-volume restart is already committed")
        engine._ready(dep, original=True)  # also detects unexpected data consumers
    elif not engine._quiet(dep, helper_uid=known["after"]["uid"] if known else None):
        raise Held(_still_there(engine))
    if cutover:
        if state.get("copy_receipt", {}).get("state") != "verified":
            raise Held("Resuming onto the new volume needs a verified copy receipt")
        if not released or engine.writer.observe(released) is not None:
            raise Held("The verified copy Job has no completed release; inspect it before recovery")
    elif switched and not released:
        raise Held("The verified copy Job has no completed release; inspect it before recovery")
    elif known and not released:
        job = engine.writer.observe(known)
        if action == "resume":
            receipt = engine._copy_receipt(job, [p for p in engine._pods() if C.owner(p, "Job", known["after"]["uid"])])
            if state["phase"] != "copy" and not receipt:
                raise Held("The retained copy has no completion evidence")
    elif released:
        names = [p["metadata"]["name"] for p in engine._pods() if known and C.owner(p, "Job", known["after"]["uid"])]
        if engine.writer.observe(released) is not None or names or not restarting and not engine._quiet(dep):
            raise Held("Wait for the copy Job and its mounts to be removed" + (f" (still there: {', '.join(names[:3])})" if names else ""))
    result = {"action": action, "operation": state["operation"], "anchor": identity(anchor.obj),
              "phase": state["phase"], "source": state["source"]["name"], "destination": state["destination"],
              "retention_policy": "keep_both_volumes"}
    if action == "return-original" and not restarting and not untouched and "admission" in state["plan"]:
        from homestead_self_data_admission import review as capacity
        report = capacity(current, anchor.namespace, "restart", C.return_proposal(dep, state, state["plan"]), state["plan"]["nodes"],
                          state["plan"]["admission"]["threshold"], clock=clock, boots=False, members=False)
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
