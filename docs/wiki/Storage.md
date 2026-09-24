# Storage

**Volumes** lists every volume in the cluster - Kubernetes calls them
persistent volume claims - with its size, how full it is, who uses it, and
what Longhorn thinks of it.

![Volumes](https://github.com/wjcloudy/homestead/releases/latest/download/homestead-volumes.png)

## Reading the table

| Badge | Means |
|---|---|
| **RWO** / **RWX** | one host can mount it (ReadWriteOnce), or many at once (ReadWriteMany, served by Longhorn over NFS) |
| **×3**, **×2** | how many copies Longhorn keeps, each on a different host. **×1** is orange: one failed disk loses that volume |
| **V1** / **V2** | Longhorn's data engine - V1 is the standard one; V2 (SPDK) is faster, with more to set up |
| healthy / degraded / faulted | degraded usually means a copy is being rebuilt, and the volume still works meanwhile; the row says why |

A volume opens to its usage over time, snapshots and backups, and what mounts
it. From the row:

- **Grow** - volumes can grow, never shrink. Most apps see the new size
  without a restart.
- **Files** - browse and edit the files on it, in the same editor VS Code uses.
  A helper pod mounts it for up to 30 minutes; a ReadWriteOnce volume in use
  must be stopped first. Saves keep the old file as `<name>.homestead-bak`.
- **Ownership** - hand the files to the user an app runs as (read from its
  PUID/PGID), for data imported as root by an older release.
- **Snapshots** - **Take snapshot now** and **Back up now**, and the volume's
  list of both - see [Data protection](Data-protection).
- **Change storage class** - below.
- **Delete** - refused while anything uses it. Then you choose: keep the data
  (the volume is released but its disk kept) or delete it for good, after
  typing its name.

## Storage classes

A storage class is the recipe for new volumes: how many copies, which engine,
whether VM disks on it can live-migrate. The **Storage classes** card creates
them and picks the default. Kubernetes cannot edit a class once made, so change
means create a new one.

One trap worth knowing: a **migratable** class (Harvester's default,
`harvester-longhorn`) makes VM disks that can move between hosts, and a shared
(RWX) volume on it can still only be mounted by one host. Homestead refuses RWX
on such a class and offers ones that work.

## Disks

Each node's card lists every disk on the host: the system disk, the disks
Longhorn uses, and any nothing uses yet. **Disks** (on Volumes, on a node, or in
Settings → Cluster) opens them all.

![Disks](https://github.com/wjcloudy/homestead/releases/latest/download/homestead-disks.png)

- **Add to Longhorn** (Harvester) - Harvester formats the disk (wiping it first
  if you say so) and gives it to Longhorn, as its own UI does.
- On k3s and other clusters, mount the disk on the host (an `/etc/fstab` line)
  and give Longhorn the folder - or the raw device, for the V2 engine.
- A Longhorn disk can stop taking new copies, have them moved elsewhere
  (**Move replicas off**), and be taken away once empty (**Remove from
  Longhorn**). Its files stay on the disk.

## Longhorn allocation

Longhorn books a copy's full size on a disk when it places it, however little
the volume holds. A disk takes no new copy once those bookings reach its size ×
the **over-provisioning** percentage, or once too little of it is actually
free. After that, new volumes come up a copy short, rebuilds wait and growing a
volume is refused - nothing already placed moves.

**Volumes** shows each node's allocation against that limit, and the largest
new volume that still fits with one, two or three copies. The dashboard names a
node past 80%, and a notification goes out.

**Settings → Cluster** sets over-provisioning and the minimum free space, with a
preview of each node's new limit, and turns the V2 engine on or off.

![Settings - Cluster](https://github.com/wjcloudy/homestead/releases/latest/download/homestead-settings-cluster.png)

To make room: add a disk, delete old copies and volumes you no longer need, or
move volumes with too many copies onto a class with fewer.

## Changing a volume's storage class

Kubernetes cannot change a volume's class, so **Change storage class** makes a
copy on the new class and swaps it in under the original's name - every
container, VM, share and backup job that uses it by name carries on unchanged.

![Change storage class](https://github.com/wjcloudy/homestead/releases/latest/download/homestead-storage-class-change.png)

The review lists everything that uses the volume and what happens to each, the
room the copy needs on each node (both copies exist until you remove the old
one), how much data moves and how long things are stopped. Then, as a job:

1. everything using the volume stops, and how each was running is noted;
2. a volume the same size is made on the new class;
3. the data is copied and checked - files with rsync (owners, permissions,
   ACLs, links kept) then compared by checksum; a VM disk block by block, then
   compared byte for byte;
4. the original is released and the copy takes its name;
5. everything starts again as it was.

![Storage class change in progress](https://github.com/wjcloudy/homestead/releases/latest/download/homestead-storage-class-progress.png)

If anything fails before the swap, everything is put back on the untouched
original. After it, the original stays as an **old copy** on Volumes until you
remove it - your way back if something is wrong.

## Without Longhorn

On a cluster whose storage is k3s's local-path or another provisioner, volumes
still work - create, grow, browse, delete - but copies, snapshots, backups,
allocation and moves need Longhorn. **Helm** can install Longhorn.
