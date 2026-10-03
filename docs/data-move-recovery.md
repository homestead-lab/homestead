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

An administrator with Kubernetes `pods/exec` access can review a **held move
before cutover** using the coordinator's bundled recovery module. Find its Pod
in the move's namespace by label `homestead.io/self-data-handoff=<operation>`;
select the independent `homestead-handoff-*` coordinator, not the copy Pod.
These commands require a coordinator running a release with this module.

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

Recovery refuses replaced resources, structural drift, stale/rebooted hosts,
unexpected consumers, unknown API write outcomes and any requested cutover.
After cutover, inspect both retained volumes and fence writers before planning
a separate recovery; this command deliberately cannot switch back. There is
no automatic timeout rollback, since that could start two competing writers.

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
