# Recovering a held Homestead data move

Both PVCs and PVs are retained. A hold is durable: restarting the coordinator or
waiting for a storage warning to disappear does not authorize another write.
The maintenance browser's status token remains read-only.

Longhorn's `longhorn.io/volume-scheduling-error` PV annotation is diagnostic and
may change during attachment or replica rebuild. It is excluded from storage
shape comparison. All other annotations, labels, ownership, the full storage
specification, resource UIDs and claim bindings remain checked. A structural
hold names the resource and distinguishes a changed identity, deletion and
specification/metadata drift. Ignoring a diagnostic does not repair degraded
Longhorn replicas or bypass scheduling and capacity checks.

An administrator with Kubernetes `pods/exec` access can review a **held move**,
in any phase from preparation to restart, using the coordinator's bundled
recovery module. Find its Job
in the move's namespace by label `homestead.io/self-data-handoff=<operation>`;
select the independent `homestead-handoff-*` coordinator, not the copy Job.
kubectl picks the Job's running Pod, whichever try that is.
These commands require a coordinator running a release with this module.
While a move is held, its status page (and the Jobs entry) shows the exact
preview command for that coordinator, with the namespace and Job filled in.
The page itself stays read-only; there is no recovery button.

```sh
kubectl -n <namespace> exec job/<coordinator> -- \
  python3 /srv/homestead_self_data_recovery.py --action resume
```

The preview writes nothing. It verifies the exact control record, Deployment,
PVC/PV bindings, coordinator, node UIDs and boot IDs, fresh leases, known journal
outcomes and writer/mount inventory. It reports the action, phase, volume names
and fingerprint. Review the result, then confirm that exact fingerprint:

```sh
kubectl -n <namespace> exec job/<coordinator> -- \
  python3 /srv/homestead_self_data_recovery.py --action resume --confirm <fingerprint>
```

Confirmation repeats the checks and conditionally updates the existing control
record. A changed proposal or concurrent update refuses recovery. The running
coordinator consumes the durable recovery receipt and continues from known
receipts, including an already completed copy. It does not repeat unknown
requests or reset copy evidence.

To abandon a move while the Deployment **still selects the original claim**,
preview and confirm `--action return-original` instead. This obtains a separate
capacity review for the original-volume restart. The coordinator deletes only
its UID-bound copy Job using normal foreground deletion, waits for its Pods and
both mounts to disappear, and restores the reviewed replica count and pinned
application image on the original claim. Startup stays read-only until every
replica is verified ready. A durable original-volume completion receipt then
allows normal startup and retires only the exact temporary control helpers.
The Jobs view records the move as cancelled; neither data volume is deleted.

What each held phase allows:

- **Held before Homestead was stopped** (phase prepare, no request sent; for
  example the copy preflight expired): `return-original` keeps Homestead as
  it is. Nothing is written to the Deployment; the coordinator checks it is
  still the reviewed one on the original claim with its replica count, then
  records the return, startup writes resume and cleanup removes the helpers.
  It does not need a host that is down. `resume` works only within about a minute: the copy
  check has a one-minute life and can't be done again from here, so after that its preview
  refuses and going back is the only way. Then start a new move. The hold offers going back first.
- **Held after the stop, before the switch request**: `resume`, or
  `return-original` as above.
- **Held after the switch request** (phase switch or start): `resume` only,
  forward to the new volume. `return-original` stays refused: the new pods
  may already have written to the new volume, so going back could lose data
  or start two writers.

Resuming forward is not a rollback, so it keeps the rule this recovery was
built on (#165): nothing automatic may start two competing writers. Its
preview requires a verified copy receipt, a completed release of the copy
Job, the Deployment selecting the new claim, and, before the start request,
zero replicas with no Homestead or other pod on either claim; after it, no
pod outside Homestead's own new replicas on either claim. The coordinator
then repeats its admission check before it sends the start request, as it
would have without the hold.

A restart that is still not ready after 15 minutes says so on the status
page. It does not hold, since a hold would keep the new pods unready.

## A pod that never stops

The waits for Homestead's pods to stop and for the copy pod to release both
volumes last at most 10 minutes. Then the move holds, and the hold names what
it waited for. The status page says "Homestead's pods did not stop within 10
minutes: ...". A pod stuck Terminating usually means a volume that will not
unmount. A Failed Homestead pod left from before the move looks the same: no
controller removes it.

Nothing here force-deletes a pod, and recovery cannot start a writer with an
old mount still in place, so the preview says "still there: <pod>" and writes
nothing until the pod is gone. Fix the volume, or if you know its host is down
and the pod cannot be writing, remove the pod yourself. Then preview either
`resume` or `return-original` as above. A reviewed recovery starts a new 10
minutes. The hold stays until someone reviews it, even if the pod goes by itself.

A slow copy never holds, however long it takes.

## If the coordinator is lost

The coordinator is a Job with `backoffLimit: 3`. If its Pod crashes, runs out of
memory, is evicted or deleted, or is lost with its host, Kubernetes starts another
on any reviewed host, preferring the one you chose. Homestead stays as it was
meanwhile, and the status page comes back with the new Pod.

- **How long:** a crash or a deletion takes from a few seconds to a few minutes:
  the Job controller backs off, 10 s then 20 s then 40 s, and a Pod being deleted
  has up to a minute to stop. A host going down takes about 7 minutes: Kubernetes
  notices after about 40 s, waits 5 minutes before evicting, then the new Pod waits
  for the old one's grace to end.
- **`backoffLimit` is a count for the whole move**, not for a run of failures: four
  tries in all, and a drain or a delete uses one up just as a crash does.
- The new Pod reads only until the previous one has gone, finished, or passed
  the end of its deletion grace. It then takes over with a conditional write to
  the control record, so the old Pod can write nothing more if it was only cut off.
- It continues from the record, which holds the Job's identity, the coordinator's
  configuration and every receipt. Nothing already recorded is done again: a
  running copy is followed, a finished one is verified, and after the switch
  the move only goes forward. A hold stays held, and a wait keeps its deadline.

A new Pod that hasn't taken over never holds, because nothing it held could be written or
cleared. If it is refused, the page says "This coordinator has not taken over yet and is retrying",
with the reason. Most often that clears by itself; if it lasts, the reason is what to fix,
such as the helper's permissions. Recovery can't see this: it acts only on the move's record.

A move started by an earlier release keeps its bare coordinator Pod, on its pinned
image, with the old behaviour. Use `kubectl exec <coordinator-pod>` for it.

### A replacement that never starts

Nothing bounds this and nothing says so: a Job does not time out a Pod that cannot
start. It happens when no reviewed host has room, or the image cannot be pulled
(a registry that is down, or one that needs `imagePullSecrets`: the coordinator
has none, and a host that does not already have the image must pull it). The
page does not answer and the tries are not used up. Look at the Job's Pods:

```sh
kubectl -n <namespace> get pods -l homestead.io/self-data-handoff=<operation>
kubectl -n <namespace> describe pod <pod>
```

Make room or fix the pull; the Pod then starts and the move carries on.

### After the fourth try

The Job has failed and nothing looks after the move. What you see depends on when:
before Homestead was stopped it still answers, read-only, and its Jobs page says
the worker has not reported. After that, Homestead's address does not answer. No
alert fires. Read why the tries ended with `kubectl logs` on the Job's Pods, which
stay until the Job is deleted, and fix that first.

A failed Job cannot be restarted, and a new Job is refused because the record pins
this one. **Do not delete the Job, delete the marker file or scale Homestead up.**
Start one more try by hand: a Pod made from the Job's own template and owned by it,
which the record accepts as this Job's. Save this as `new-try.py`:

```python
# new-try.py: reads the failed Job as JSON, prints one more Pod for it.
import json
import sys

job = json.load(sys.stdin)
if not any(c.get("type") == "Failed" and c.get("status") == "True" for c in job.get("status", {}).get("conditions", [])):
    sys.exit("This Job has not failed: it starts its own next try")
meta, template = job["metadata"], job["spec"]["template"]
json.dump({"apiVersion": "v1", "kind": "Pod", "metadata": {
    "generateName": meta["name"] + "-by-hand-", "namespace": meta["namespace"],
    "labels": template["metadata"]["labels"],
    "ownerReferences": [{"apiVersion": "batch/v1", "kind": "Job", "name": meta["name"], "uid": meta["uid"],
                         "controller": True, "blockOwnerDeletion": False}]},
    "spec": template["spec"]}, sys.stdout)
```

```sh
kubectl -n <namespace> get job <coordinator> -o json | python3 new-try.py | kubectl create -f -
```

It takes over as any try does, and the status page comes back. If it dies, nothing
replaces it: run the same command again. Cleanup removes it with the Job.

### A request with no recorded outcome

If a Pod died between saying it would send a request and recording the answer, the
hold names the step, and nothing sends it again. Recovery refuses until the outcome
is known, because guessing is how a request gets sent twice. Only a person can look:

```sh
kubectl -n <namespace> get configmap <deployment>-data-handoff -o jsonpath='{.data.state\.json}' \
  | python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin)["journal"]["ref"]["storage_writes"][-1], indent=2))'
```

Read `step`, then read the object it names and decide with this table. The script
below checks your answer against it and refuses a mismatch, because a wrong answer here
can't be undone.

| `step` | It was **applied** if | It was **not sent** if |
|---|---|---|
| `stop` | the Deployment's `spec.replicas` is 0 | it still has its replica count |
| `copy-job` | the copy Job exists | no such Job |
| `release-copy`, `recover-release-copy` | the copy Job is gone or has a `deletionTimestamp` | it is there with none |
| `switch` | the Deployment's data volume names the new claim | it names the original |
| `start` | the claim is the new one and `spec.replicas` is back to its count | `spec.replicas` is 0 |
| `recover-start` | the claim is the original and `spec.replicas` is back to its count | `spec.replicas` is 0 |

If you cannot tell, stop and keep everything as it is. Then record what you found.
The move must be held and no other Pod of the Job may still be running, and the
script refuses otherwise: a request still on its way would make your answer wrong. A
Pod stuck Terminating counts: see "A Pod on a dead host" below.
This needs a running coordinator: if there is none, start a try as above first.
Save this as `settle.py`:

```python
# settle.py: writes what you found about the last request into the move's record.
import json
import os
import sys
import urllib.error

sys.path.insert(0, "/srv")
import homestead_self_data_kube as K
import homestead_self_data_launch as L
from homestead_storage_journal import identity, shape

outcome = sys.argv[1]
if outcome not in ("applied", "not-sent"):
    sys.exit("Say applied or not-sent")
_, scope = L.parse_configuration(os.environ[L.CONFIG_ENV])
client = K.Client(scope)


def optional(path):
    try:
        return client.read(path)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


record = client.read(scope.anchor)
state = json.loads(record["data"]["state.json"])
if state.get("runtime", {}).get("state") != "held":
    sys.exit("The move is not held, so a request may still be on its way. Nothing was changed")
entries = state["journal"]["ref"]["storage_writes"]
if not entries or entries[-1]["state"] == "accepted":
    sys.exit("Nothing to settle: the last request has a recorded outcome")
others = [p["metadata"]["name"] for p in client.read(scope.core + "/pods")["items"]
          if any(o.get("uid") == os.environ[L.JOB_UID_ENV] for o in p["metadata"].get("ownerReferences", []))
          and p["metadata"]["uid"] != os.environ[L.UID_ENV] and p.get("status", {}).get("phase") not in ("Succeeded", "Failed")]
if others:
    sys.exit("Another Pod of this Job has not finished: " + ", ".join(others) + ". Nothing was changed")

entry, count = entries[-1], state["replicas"]
step, found = entry["step"], optional(entry["target"]["path"])
if step in ("stop", "switch", "start", "recover-start"):
    replicas = found["spec"].get("replicas")
    claim = next(v for v in found["spec"]["template"]["spec"]["volumes"] if v["name"] == state["plan"]["data_volume"])["persistentVolumeClaim"]["claimName"]
    applied = {"stop": replicas == 0, "switch": claim == state["destination"],
               "start": claim == state["destination"] and replicas == count,
               "recover-start": claim == state["source"]["name"] and replicas == count}[step]
    not_sent = {"stop": replicas == count, "switch": claim == state["source"]["name"],
                "start": replicas == 0, "recover-start": replicas == 0}[step]
elif step == "copy-job":
    applied, not_sent = found is not None, found is None
elif step in ("release-copy", "recover-release-copy"):
    applied = found is None or bool(found["metadata"].get("deletionTimestamp"))
    not_sent = not applied
else:
    sys.exit("This script does not know the step " + step + ". Nothing was changed")
if not (applied if outcome == "applied" else not_sent):
    says = "applied" if applied else "not-sent" if not_sent else "neither"
    sys.exit("The cluster does not say that for " + step + ": it says " + says + ". Nothing was changed")

if outcome == "not-sent":
    entries.pop()
else:
    entry["state"] = "accepted"
    if entry["method"] != "DELETE":
        entry.update(after=identity(found), shape=shape(found))
record["data"]["state.json"] = json.dumps(state, sort_keys=True, separators=(",", ":"))
try:
    client.send("PUT", scope.anchor, record)
except urllib.error.HTTPError as error:
    if error.code == 409:
        sys.exit("The record changed while this ran, and nothing was written. Check again, then run it again")
    raise
print("Recorded. Now preview recovery.")
```

```sh
kubectl -n <namespace> exec -i job/<coordinator> -- python3 - applied < settle.py   # or not-sent
```

Then preview and confirm `--action resume` or `--action return-original` as above.

### A Pod on a dead host

A plain `kubectl delete pod` never finishes while the host is down, because its kubelet has to
confirm it. Force it only with proof the Pod's process is gone: the host is powered off or
its machine is known dead (look at it, or its console), or its boot id has changed. A host that
is only cut off from the network may still be running the Pod, and a forced delete then
leaves a process nothing can see, which is how two writers happen.

```sh
kubectl -n <namespace> delete pod <pod> --force --grace-period=0
```

## Going back to an earlier release

After **any** finished move, the volume keeps a receipt (`.self-data-completed-v1.json`).
A release from before the coordinator became a Job cannot verify a receipt made by a
later one, so it stays read-only on that volume, whichever volume that is. Going
forward again fixes it. To go back, first wait for the temporary helpers to be gone
(`kubectl -n <namespace> get configmap <deployment>-data-handoff` says NotFound), then
remove the receipt with the current release still running:

```sh
kubectl -n <namespace> exec deploy/<deployment> -- rm /data/.self-data-completed-v1.json
```

With no marker, no receipt and no control record, a release starts normally.

## Which hosts are checked

Only the hosts the move uses are checked for reboots and stale heartbeats: the
nodes of Homestead's own pods and of any pod mounting either claim, which includes
the copy pod while it exists, and the nodes either volume is pinned to. A reboot of
another host does not hold the move, and neither does one of the coordinator's
host: it mounts nothing, and its next try carries on. A reboot of the copy host
before the copy or after its pod has gone holds nothing either, and nor does one
before Homestead's new pods start on it.

**A volume pinned to a host** (local-path keeps both volumes on one host's disk) needs
that host up with a fresh heartbeat; its reboot alone holds nothing, at any phase. While
it is down the status page names it, the move sends nothing and it carries on by itself
when the host is back. No pod listed in the API can be writing unnoticed, since those
are measured, and the copy flushes to disk before its receipt. A pin that names no
reviewed host, a zone for example, still measures every host's boot id. The same goes
for the host of one of Homestead's own pods before any stop request: those pods are
fenced, and deleting one would only start another on the same host.

A host with a pod listed on it that stops answering is different: that pod may still
be writing, so the move holds at once.

### A host that never comes back

Before any stop request, the wait lasts only as long as the copy check, about a minute: after
that this move can never stop Homestead, so it holds at once and the way out is going back,
which writes nothing and does not need the host. Homestead keeps running as it was.

After the stop request and before the switch, the move waits 30 minutes for it, saying so
on the page, then holds. The 10-minute deadlines don't run during that wait.

- **The original volume is not on that host:** preview and confirm
  `--action return-original`. It does not need the host, and Homestead restarts on
  the original volume. The new volume stays as it is; delete it yourself when you
  no longer want it.
- **The original volume is on that host too:** recovery refuses, and nothing here can
  help, because that disk holds the only copy Homestead has. Bring the host or its disk back, or
  restore from a backup. Do not point the Deployment at another volume by hand.

After the switch, the move waits with no limit and the page says so, because a hold would
only keep the new pods unready. Recovery can't go back from here. If the new volume's host
will never return, the verified copy on it is gone too, and what that costs depends on when:

- **After the switch, before the start request:** replicas are still 0, so **nothing
  has been written to the new volume, and the original volume is complete.** Nothing is
  lost. Recovery still can't go back in this window, so it is an administrator-reviewed
  recovery outside this tool.
- **After the start request:** the new pods may have written to the new volume. The
  original holds the data as it was when Homestead stopped, and whatever was written after that
  is not on it. Going back to it is also an administrator-reviewed recovery outside this tool.

### Hosts removed or added

A Node deleted, a host replaced under the same name, or a new one joining holds the move at
its next capacity check before the switch, because the cluster is no longer the one reviewed.

- **Before the switch, resuming refuses**, and says what changed. Going back is the way on,
  or put a host back if that is possible; a removed host's identity can't come back.
- **Going back ignores the change**, unless the original volume or one of Homestead's pods is on
  the missing host; then it refuses and names it. Remove a pod left on a host that is gone, as
  "A Pod on a dead host" says. Homestead may start on a host that has joined, both when a
  return restarts it and if a pod is already there before any stop request. That is safe:
  nothing about that host matters to its original volume.
- **After the switch there is no going back, so resuming and the start request ignore
  the change** and the move finishes, unless what it needs is on a missing host:
  - **Homestead's new pod listed on a deleted Node** counts as waiting, with no limit, until it
    leaves Kubernetes. Pod GC removes it, then its ReplicaSet starts another. Nothing needs doing.
  - **The new volume's own host removed** holds, and recovery refuses and says so. The data
    is on that host. This is an administrator-reviewed recovery outside this tool.


**A host just back** has no live memory figure for a minute or two. If that is the only
thing new at a capacity check, the move waits up to 5 minutes for it, saying so on the
page. After that it holds: "new warning: live memory usage is unavailable". Nothing was
sent. Wait for the figure to return, then preview and confirm `--action resume`. Any
other capacity change holds at once, and so does a missing figure on a host that did not reboot.

**A reboot during the copy, or with one of Homestead's old pods still listed on that
host, still needs hand work:** the move holds and recovery refuses while that pod is
listed. The changed boot id is itself the proof that the pod's process is dead, so
removing it is safe. Compare the node's boot id with the record's:

```sh
kubectl get node <host> -o jsonpath='{.status.nodeInfo.bootID}'
kubectl -n <namespace> get configmap <deployment>-data-handoff -o jsonpath='{.data.state\.json}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["plan"]["nodes"])'
kubectl -n <namespace> delete pod <the pod the hold is about>
```

Check the Pod is not somewhere else first, and only delete it if it is on the host
whose boot id changed. Then preview recovery. See "A Pod on a dead host" below if the
delete never finishes. After a copy pod dies, its Job
has failed and a copy is never repeated, so `resume` refuses: use
`return-original`, and move again afterwards. After an old Homestead pod, both are
offered.

A pod that has left the API was removed
normally (nothing force-deletes one), so its writer had stopped. If a volume's
placement names no reviewed host, every reviewed host is checked.

Recovery refuses replaced resources, structural drift, stale/rebooted hosts,
unexpected consumers and unknown API write outcomes. This command
deliberately cannot switch back after cutover. There is no automatic timeout
rollback, since that could start two competing writers.

A Kubernetes API that does not answer (a timeout, a refused connection, a
429 or 5xx) does not hold the move by itself: the coordinator retries from
its durable record for up to 5 minutes, then holds and says so. A write
whose outcome is unknown still holds at once.

For an older coordinator without this module, retain the complete anchor and
copy evidence and perform an administrator-reviewed recovery. Do not deploy a
new image into a pinned move, clear its hold, delete its marker, or scale the
application up by itself. Those edits bypass the move's evidence and startup
fence. The incident recovery must establish the same exact volume binding,
known write outcomes and normal mount release before restoring the source.

## Clearing finished moves from Jobs

After a move succeeds or recovery has verified Homestead on its original volume,
**Dismiss** and **Clear finished** hide its entry from Jobs. Both data volumes,
the recovery audit, dispatch identities and saved log remain retained; clearing
does not delete cluster resources. Jobs that still need recovery cannot be cleared.
