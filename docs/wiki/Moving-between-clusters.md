# Moving between clusters

A container or VM can move from one cluster to another when both run Homestead -
from an old cluster to a new one, say. The **destination** does the work: it
reads the workload's definition from the other Homestead, and its volumes from
the Longhorn backups the other cluster writes. The source never holds anything
of the destination's.

Both need Longhorn. Below, **source** is the cluster the workload is on now, and
**destination** the one it is moving to.

## 1. Update both

Both clusters should run a recent Homestead. On the destination, **Settings →
Linked clusters → Moving workloads** has a card for each cluster, saying whether
the two releases can move workloads between them, and which side to update if
not.

## 2. Link the two

[Link the clusters](Linked-clusters): **Settings → Linked clusters → Link a
cluster** takes the other Homestead's address and an admin account there, used
once. Linked
clusters are sources for each other with nothing more to add - in both
directions.

Clusters added before linking existed, with an account and password kept in a
Secret, still work; **Settings → Linked clusters** offers to link each and
delete its password.

## 3. Give the source backup storage

A move copies volumes through backups, so the source must be able to write
them somewhere the destination can read. The source's card under **Moving
workloads** lists what is still needed, with a button beside each:

- **The source has a backup target already** (a NAS, S3) - the destination
  uses the same one. If the two clusters use different targets, the review
  says so and the destination is pointed at the source's for the move.
- **It has none** - **Set it up** runs an S3 server (RustFS) on the source, on a
  Longhorn volume, makes the bucket and points the source's Longhorn at it. The
  **Migration** button beside the source under Linked clusters does the same.
- **Migration from it is off** - the source's store is stopped; its
  **Migration** button enables it again.

That S3 server needs an address on your LAN that the destination can reach. By
default it shares the source's **shared address** - the one its apps share -
and answers on port 9000 there, so no address of its own is needed. On k3s it
answers on the nodes' own addresses instead. If port 9000 is already taken
there, the setup says which Service has it and names a free port; choose that
one in **Port** (the port above it is the store's console). You can also give it an address of
its own - one of the source's [VIPs](Networking#add-default-and-choose-workload-vips) or a free address in
its IP pools - to keep its traffic apart. After it is set, Homestead checks from
the destination that the address answers.

The in-cluster store shares the fate of the cluster it is on: it is for moving
workloads, not your only backup.

## 4. Browse and move

From the workload's side: **Move to cluster** in its `…` menu picks the
destination and opens it at the review below. From the destination's side,
under **Settings → Linked clusters → Moving workloads**:

**Browse workloads** lists what the source runs, with the reason anything
cannot move - a ConfigMap, Secret or host folder Homestead did not make and so
cannot rebuild. A passed-through device such as `/dev/dri` travels, with a note
that the destination needs a host that has it.

**Move to this cluster** reviews the namespace, address and storage on the
destination, and lists every blocker and warning from both sides before
anything stops. Then, in the job tray:

1. the workload stops on the source;
2. its volumes are backed up;
3. they are restored on the destination;
4. the workload is created and started there.

A move survives either Homestead restarting, and has no time limit that would
abandon a large volume. A failed step can be retried once its cause is fixed.

A move that fails says why - the reason Kubernetes gave, or that this cluster
cannot reach the source's backup storage. **Retry** carries on from the step
that failed. **Cancel**, offered while nothing has stopped on the source yet,
drops the move and anything it set up here; once the source has stopped,
the same button is **Put back**, and starts it again there.

### Moving a volume on its own

Below the workloads, **Volumes** lists the source's Longhorn volumes with what
uses each one. **Move to this cluster** backs the volume up to the shared
backup storage and restores it here, under the same name, on the storage class
you choose. There is nothing to create or start afterwards.

A volume moves only while nothing uses it. One mounted by a running app or VM
is listed as **cannot move**, naming that app or VM. Stop it, or move the app
or VM instead, which brings its volumes along. While the move runs, the volume
is held on the source: an app that starts using it stops the move rather than
copying it half-written.

The original stays on the source until you remove it there (**Remove from
&lt;cluster&gt;** in the move's row). An app on the source that still names the
volume is left as it is.

## Afterwards

The original stays on the source, **stopped**, until you remove it there - so
it can be started again at any point. Finished moves can be dismissed from the
destination's list without touching the source's copy.
