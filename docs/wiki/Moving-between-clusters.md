# Moving between clusters

A container or VM can move from one cluster to another when both run Homestead -
from an old cluster to a new one, say. The **destination** does the work: it
reads the workload's definition from the other Homestead, and its volumes from
the Longhorn backups the other cluster writes. The source never holds anything
of the destination's.

Both need Longhorn. Below, **source** is the cluster the workload is on now, and
**destination** the one it is moving to.

## 1. Update both

Both clusters should run a recent Homestead. The destination's cluster card
says whether the two releases can move workloads between them, and which side
to update if not.

## 2. Link the two

[Link the clusters](Linked-clusters): **Import → ＋ Homestead cluster** takes
the other Homestead's address and an admin account there, used once. Linked
clusters are sources for each other with nothing more to add - in both
directions.

Clusters added before linking existed, with an account and password kept in a
Secret, still work, and are listed on Import beside the linked ones.

## 3. Give the source backup storage

A move copies volumes through backups, so the source must be able to write
them somewhere the destination can read. The cluster's card lists what is
still needed, with a button beside each:

- **The source has a backup target already** (a NAS, S3) - the destination
  uses the same one. If the two clusters use different targets, the review
  says so and the destination is pointed at the source's for the move.
- **It has none** - **Set it up** runs an S3 server (RustFS) on the source, on a
  Longhorn volume, makes the bucket and points the source's Longhorn at it.

That S3 server needs a **LAN address on the source**: the source cluster's load
balancer announces it there, and the destination connects to it. The addresses
offered are the source's own [VIPs](Networking#your-vips), then free addresses
in its Harvester IP pools. After it is set, Homestead checks from the
destination that the address answers.

The in-cluster store shares the fate of the cluster it is on: it is for moving
workloads, not your only backup.

## 4. Browse and move

From the workload's side: **Move to cluster** in its `…` menu picks the
destination and opens it at the review below. From the destination's side:

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

## Afterwards

The original stays on the source, **stopped**, until you remove it there - so
it can be started again at any point. Finished moves can be dismissed from the
destination's list without touching the source's copy.
