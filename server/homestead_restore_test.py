"""Restore tests: does an app's backup actually bring it back?

A backup that has never been restored is a hope. A restore test restores the
newest completed Longhorn backup of each of an app's volumes into new volumes,
starts a copy of the app on them, and checks that the copy becomes ready and
answers on its ports. Then everything it made is removed, whatever happened,
and the result is recorded on the app (homestead.io/restore-tested).

The copy must not touch anything real:

* it runs beside the app, in its namespace, so the app's ConfigMaps and
  Secrets resolve - but under labels of its own, so no Service sends it
  traffic, with no host network, LAN attachment, host ports or passthrough
  devices, and without the app's service account token;
* a NetworkPolicy lets in only Homestead's check and lets out only DNS;
* every volume that is not one of the restored ones - an NFS share, a host
  path, a claim with no backup - is replaced by an empty one, so the copy
  cannot write to live data.

Restore tests run one at a time, outside the maintenance window, only when
Longhorn has room for the restored volumes. Monthly, for every app with
backups, when switched on (Data Protection); Test now runs one at any time.

The test is a job. Its steps run in the job's resolver under the operations
lock every replica shares, each checkpointed before it writes, so a restart
carries it on rather than repeating it.
"""
import copy
import json
import time
import urllib.error

KIND = "restore-test"
EVERY = 30 * 86400
READY_WAIT = 600            # the copy has this long to become ready
ANSWER_WAIT = 180           # then this long to answer on a port
RESULT = "restore-tested"   # annotation suffix on the app
LABEL = "restore-test"      # label suffix on everything a test makes
KEEP_VOLUMES = ("configMap", "secret", "downwardAPI", "projected", "emptyDir", "serviceAccountToken")

# Bound by server.py.
kget = ksend = None
names = None
backups = lambda volume: []                 # Longhorn backups of a volume, newest first
restore = lambda cfg: {}                    # homestead_longhorn.restore_backup
restore_status = lambda item: ("running", 0, "")   # homestead_operations' volume-restore resolver
csi_cleanup = lambda: []
capacity = lambda: {}
ask = lambda target: {"ok": False}          # homestead_uptime.ask_ports
self_namespace = "lab"


def bind(_kget, _ksend, _names, _backups, _restore, _restore_status, _csi_cleanup, _capacity, _ask, _self_namespace):
    global kget, ksend, names, backups, restore, restore_status, csi_cleanup, capacity, ask, self_namespace
    kget, ksend, names, backups, restore, restore_status = _kget, _ksend, _names, _backups, _restore, _restore_status
    csi_cleanup, capacity, ask, self_namespace = _csi_cleanup, _capacity, _ask, _self_namespace


def _get(path):
    try:
        return kget(path)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


def _send(method, path, body=None):
    """A write that is done if Kubernetes says it already was."""
    try:
        return ksend(method, path, body) if body is not None else ksend(method, path)
    except urllib.error.HTTPError as error:
        if (method == "POST" and error.code == 409) or (method == "DELETE" and error.code == 404):
            return None
        raise


def last_result(annotations):
    try:
        return json.loads((annotations or {}).get(names.key(RESULT)) or "null")
    except ValueError:
        return None


def plan(ns, dep):
    """What a test of this app restores: [{claim, volume, backup, ...}], and what it cannot."""
    claims = sorted({(v.get("persistentVolumeClaim") or {}).get("claimName")
                     for v in dep["spec"]["template"]["spec"].get("volumes") or []} - {None, ""})
    rows, without = [], []
    for claim in claims:
        pvc = _get(f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{claim}") or {}
        pv_name = (pvc.get("spec") or {}).get("volumeName")
        pv = _get(f"/api/v1/persistentvolumes/{pv_name}") if pv_name else None
        csi = ((pv or {}).get("spec") or {}).get("csi") or {}
        if csi.get("driver") != "driver.longhorn.io":
            without.append({"claim": claim, "why": "not a Longhorn volume"})
            continue
        found = [b for b in backups(csi.get("volumeHandle") or pv_name) if b.get("restorable")]
        if not found:
            without.append({"claim": claim, "why": "no completed backup"})
            continue
        newest = found[0]
        rows.append({"claim": claim, "volume": csi.get("volumeHandle") or pv_name, "backup": newest["name"],
                     "created": newest.get("created", ""), "size_gb": newest.get("volume_size_gb", 1),
                     "storage_class": (pvc.get("spec") or {}).get("storageClassName") or ""})
    return rows, without


def room_for(rows, cap, classes):
    """Whether Longhorn has room for every restored volume at its class's copies."""
    for row in rows:
        copies = str((classes.get(row["storage_class"]) or {}).get("numberOfReplicas") or "3")
        if float((cap.get("largest") or {}).get(copies, 0) or 0) < float(row["size_gb"]) * 1.2:
            return False
    total = sum(float(r["size_gb"]) for r in rows)
    return float((cap.get("largest") or {}).get("1", 0) or 0) >= total * 1.2


def due(workload, now):
    last = workload.get("restore_tested") or {}
    return not last or now - float(last.get("at") or 0) >= EVERY


def pick(workloads, operations, enabled, window_open, now, classes):
    """The next app to test, with its plan, or None: one at a time, never in
    the maintenance window, and only with room for its restored volumes."""
    if not enabled or window_open:
        return None
    if any(o.get("kind") == KIND and o.get("status") in ("queued", "running") for o in operations or []):
        return None
    cap = None
    for w in sorted(workloads or [], key=lambda w: float((w.get("restore_tested") or {}).get("at") or 0)):
        if not w.get("claims") or w.get("platform") or w.get("self") or w.get("homestead") \
                or w.get("managed_smb") or w.get("managed_nfs") or w.get("site") or not due(w, now):
            continue
        dep = _get(f"/apis/apps/v1/namespaces/{w['ns']}/deployments/{w['name']}")
        if not dep:
            continue
        rows, _ = plan(w["ns"], dep)
        if not rows:
            continue
        cap = cap if cap is not None else capacity()
        if room_for(rows, cap, classes):
            return w
    return None


def _copy_name(item):
    return f"restore-test-{item['id'][:10]}"


def _restored_name(item, index):
    return f"restore-test-{item['id'][:10]}-{index}"


def copy_of(dep, item, rows):
    """The app's Deployment as an isolated copy on the restored volumes."""
    test = item["id"]
    spec = copy.deepcopy(dep["spec"]["template"]["spec"])
    restored = {r["claim"]: r["restored"] for r in rows}
    volumes = []
    for v in spec.get("volumes") or []:
        claim = (v.get("persistentVolumeClaim") or {}).get("claimName")
        if claim in restored:
            volumes.append({"name": v["name"], "persistentVolumeClaim": {"claimName": restored[claim]}})
        elif v.get("projected"):
            # Without any service-account token it projects: the copy gets none.
            sources = [s for s in v["projected"].get("sources") or [] if "serviceAccountToken" not in s]
            volumes.append({**v, "projected": {**v["projected"], "sources": sources}} if sources
                           else {"name": v["name"], "emptyDir": {}})
        elif any(k in v for k in KEEP_VOLUMES):
            volumes.append(v)
        else:                                       # a share, a host path, a claim with no backup
            volumes.append({"name": v["name"], "emptyDir": {}})
    spec["volumes"] = volumes
    for key in ("hostNetwork", "hostPID", "hostIPC", "nodeName", "affinity", "nodeSelector", "topologySpreadConstraints"):
        spec.pop(key, None)
    if spec.get("dnsPolicy") == "ClusterFirstWithHostNet":
        spec["dnsPolicy"] = "ClusterFirst"
    spec["automountServiceAccountToken"] = False
    spec["restartPolicy"] = "Always"
    for container in (spec.get("containers") or []) + (spec.get("initContainers") or []):
        for port in container.get("ports") or []:
            port.pop("hostPort", None)
            port.pop("hostIP", None)
        resources = container.get("resources") or {}
        for part in ("requests", "limits"):       # passthrough devices stay with the real app
            if part in resources:
                resources[part] = {k: v for k, v in resources[part].items() if k in ("cpu", "memory", "ephemeral-storage")}
    label = {names.key(LABEL): test}
    return {"apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": _copy_name(item), "namespace": dep["metadata"]["namespace"],
                         "labels": {**label, names.key("managed"): "true"},
                         "annotations": {names.key("restore-test-of"): dep["metadata"]["name"]}},
            "spec": {"replicas": 1, "strategy": {"type": "Recreate"}, "selector": {"matchLabels": label},
                     "template": {"metadata": {"labels": label}, "spec": spec}}}


def fence(item, ns):
    """In: only Homestead's check. Out: only DNS."""
    label = {names.key(LABEL): item["id"]}
    return {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
            "metadata": {"name": _copy_name(item), "namespace": ns, "labels": {**label, names.key("managed"): "true"}},
            "spec": {"podSelector": {"matchLabels": label}, "policyTypes": ["Ingress", "Egress"],
                     "ingress": [{"from": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": self_namespace() if callable(self_namespace) else self_namespace}},
                                            "podSelector": {"matchLabels": {"app": "homestead"}}}]}],
                     "egress": [{"ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}]}]}}


def _ports(dep):
    return sorted({int(p["containerPort"]) for c in dep["spec"]["template"]["spec"].get("containers") or []
                   for p in c.get("ports") or [] if p.get("containerPort") and (p.get("protocol") or "TCP") == "TCP"})


def privileged(dep):
    """Whether the app runs a privileged container - so its copy does too."""
    spec = dep["spec"]["template"]["spec"]
    return any((c.get("securityContext") or {}).get("privileged")
               for c in (spec.get("containers") or []) + (spec.get("initContainers") or []))


def _pod(ns, item):
    selector = f"{names.key(LABEL)}%3D{item['id']}"
    pods = (_get(f"/api/v1/namespaces/{ns}/pods?labelSelector={selector}") or {}).get("items") or []
    return pods[0] if pods else None


def _waiting(pod):
    for status in ((pod or {}).get("status") or {}).get("containerStatuses") or []:
        waiting = (status.get("state") or {}).get("waiting") or {}
        if waiting.get("reason") in ("CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "CreateContainerConfigError",
                                     "CreateContainerError"):
            return f"{status.get('name')}: {waiting['reason']}" + (f" - {waiting.get('message')[:140]}" if waiting.get("message") else "")
    for condition in ((pod or {}).get("status") or {}).get("conditions") or []:
        if condition.get("type") == "PodScheduled" and condition.get("status") == "False":
            return condition.get("message") or "it could not be scheduled"
    return ""


def resolve(item, checkpoint, now=None):
    """Move one restore test on a step: (status, progress, message)."""
    now = now or time.time()
    ref = item["ref"]
    ns, name = ref["namespace"], ref["name"]
    phase = ref.get("phase", "plan")

    def to(next_phase, **changes):
        ref.update(changes, phase=next_phase, phase_at=now)
        checkpoint(item)

    if phase == "plan":
        dep = _get(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")
        if not dep:
            return "failed", 0, f"{name} no longer exists"
        rows, without = plan(ns, dep)
        if not rows:
            return "succeeded", 100, f"Nothing to test: {name} has no Longhorn volume with a completed backup"
        for i, row in enumerate(rows):
            row["restored"] = _restored_name(item, i)
        size = sum(r["size_gb"] for r in rows)
        to("restoring", rows=rows, without=without, deadline=now + 1800 + 120 * size, started=now)
        phase = "restoring"
    if phase == "restoring":
        for row in ref["rows"]:
            if row.get("requested"):
                continue
            row["requested"] = True
            checkpoint(item)
            try:
                made = restore({"backup": row["backup"], "namespace": ns, "name": row["restored"],
                                "storage_class": row["storage_class"] or None,
                                "annotations": {names.key(LABEL): item["id"]}}) or {}
                if made.get("created") is False:
                    return _finish(item, to, False, f"the backup of {row['claim']} could not be restored: {made.get('message') or 'restores are not supported here'}")
            except ValueError as error:
                if "already exists" not in str(error):
                    return _finish(item, to, False, f"the backup of {row['claim']} could not be restored: {str(error)[:160]}")
        states = [restore_status({"ref": {"namespace": ns, "name": r["restored"]}}) for r in ref["rows"]]
        failed = next(((r, s) for r, s in zip(ref["rows"], states) if s[0] == "failed"), None)
        if failed:
            return _finish(item, to, False, f"the backup of {failed[0]['claim']} did not restore: {failed[1][2]}")
        if all(s[0] == "succeeded" for s in states):
            try:
                csi_cleanup()
            except Exception:
                pass
            to("starting", restored_at=now)
            return "running", 60, "Backups restored; starting a copy of the app on them"
        if now > ref["deadline"]:
            return _finish(item, to, False, "the backups did not finish restoring in time")
        progress = min(s[1] for s in states) if states else 0
        return "running", 5 + int(progress * 0.5), "Restoring " + "; ".join(f"{r['claim']}: {s[2]}" for r, s in zip(ref["rows"], states))
    if phase == "starting":
        dep = _get(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")
        if not dep:
            return _finish(item, to, False, f"{name} was removed during the test")
        to("checking", copy_at=now, ports=_ports(dep), privileged=privileged(dep))
        _send("POST", f"/apis/networking.k8s.io/v1/namespaces/{ns}/networkpolicies", fence(item, ns))
        _send("POST", f"/apis/apps/v1/namespaces/{ns}/deployments", copy_of(dep, item, ref["rows"]))
        return "running", 70, "Copy started, cut off from the network; waiting for it to be ready"
    if phase == "checking":
        if not _get(f"/apis/apps/v1/namespaces/{ns}/deployments/{_copy_name(item)}"):
            # Saved as checking, then stopped before the copy was made: make it now.
            dep = _get(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")
            if not dep:
                return _finish(item, to, False, f"{name} was removed during the test")
            _send("POST", f"/apis/networking.k8s.io/v1/namespaces/{ns}/networkpolicies", fence(item, ns))
            _send("POST", f"/apis/apps/v1/namespaces/{ns}/deployments", copy_of(dep, item, ref["rows"]))
        pod = _pod(ns, item)
        ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in ((pod or {}).get("status") or {}).get("conditions") or [])
        if not ready:
            why = _waiting(pod)
            if now - ref["copy_at"] > READY_WAIT or (why and "Back" in why and now - ref["copy_at"] > 180):
                return _finish(item, to, False, f"the copy on the restored data did not become ready{f' ({why})' if why else ''}")
            return "running", 80, "Waiting for the copy to be ready" + (f": {why}" if why else "")
        if not ref.get("ready_at"):
            to("checking", ready_at=now)
        ip = (pod.get("status") or {}).get("podIP", "")
        if not ref.get("ports"):
            return _finish(item, to, True, "the copy on the restored data became ready (it publishes no port to ask)")
        result = ask({"kind": "http", "host": ip, "port": ref["ports"][0], "scheme": "http", "path": "/", "strict": False,
                      "asks": [{"host": ip, "port": p, "scheme": "https" if p in (443, 8443, 9443) else "http"} for p in ref["ports"][:4]]})
        if result.get("ok"):
            return _finish(item, to, True, f"the copy on the restored data became ready and answered on port {result.get('port')}")
        if now - ref["ready_at"] > ANSWER_WAIT:
            return _finish(item, to, False, f"the copy became ready but did not answer on {', '.join(str(p) for p in ref['ports'][:4])}"
                                           f" ({result.get('error') or 'no answer'})")
        return "running", 90, "Ready; waiting for it to answer"
    if phase == "cleanup":
        return _finish(item, to, ref.get("ok"), ref.get("why", ""))
    return "failed", item.get("progress", 0), f"Unknown step {phase}"


def _finish(item, to, ok, why):
    """Remove everything the test made, then record the result on the app."""
    ref = item["ref"]
    ns, name, now = ref["namespace"], ref["name"], time.time()
    if ref.get("phase") != "cleanup":
        to("cleanup", ok=ok, why=why)
    left = []
    for path in (f"/apis/apps/v1/namespaces/{ns}/deployments/{_copy_name(item)}",
                 f"/apis/networking.k8s.io/v1/namespaces/{ns}/networkpolicies/{_copy_name(item)}",
                 *[f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{r['restored']}" for r in ref.get("rows") or []]):
        try:
            _send("DELETE", path)
        except Exception as error:
            left.append(f"{path.rsplit('/', 1)[-1]} ({str(error)[:60]})")
    took = int(now - ref.get("started", now))
    said = ", ".join(f"{r['claim']} from {r['backup']}" for r in ref.get("rows") or [])
    skipped = ref.get("without") or []
    message = (("Restore test passed: " if ok else "Restore test failed: ") + why
               + (f". Restored {said}." if said else ".")
               + (" Not tested: " + "; ".join(f"{w['claim']} ({w['why']})" for w in skipped) + "." if skipped else "")
               + (" The copy ran privileged, as the app does, so it could reach the host's devices; it had no network"
                  " and none of the app's shares or host folders." if ref.get("privileged") else "")
               + (" Could not remove " + ", ".join(left) + "; remove them by hand." if left else ""))
    try:
        ksend("PATCH", f"/apis/apps/v1/namespaces/{ns}/deployments/{name}",
              {"metadata": {"annotations": {names.key(RESULT): json.dumps(
                  {"at": round(now), "ok": bool(ok), "message": message[:400], "seconds": took}, separators=(",", ":"))}}},
              ctype="application/merge-patch+json")
    except Exception:
        pass                    # the job keeps the result even if the app is gone
    return ("succeeded" if ok and not left else "failed"), 100, message
