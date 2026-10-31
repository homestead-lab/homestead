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
recovery module. Find its Pod
in the move's namespace by label `homestead.io/self-data-handoff=<operation>`;
select the independent `homestead-handoff-*` coordinator, not the copy Pod.
These commands require a coordinator running a release with this module.
While a move is held, its status page (and the Jobs entry) shows the exact
preview command for that coordinator, with the namespace and Pod filled in.
The page itself stays read-only; there is no recovery button.

```sh
kubectl -n <namespace> exec <coordinator-pod> -- \
  python3 /srv/homestead_self_data_recovery.py --action resume
```

The preview writes nothing. It verifies the exact control record, Deployment,
PVC/PV bindings, coordinator, node UIDs and boot IDs, fresh leases, known journal
outcomes and writer/mount inventory. It reports the action, phase, volume names
and fingerprint. Review the result, then confirm that exact fingerprint:

```sh
kubectl -n <namespace> exec <coordinator-pod> -- \
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
  `resume` retries the stop with every check it had.
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

Only the hosts the move uses are checked for reboots and stale heartbeats: the
copy host, the nodes of the coordinator, of Homestead's own pods and of any pod
mounting either claim, and the nodes either volume is pinned to. A reboot of
another host does not hold the move. A pod that has left the API was removed
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
