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

When nothing is using a volume right now, **Attached to** says why - which is
what tells you whether its data is wanted:

| Shows | Means |
|---|---|
| **nextcloud · stopped** | a container, VM or job is set up to use it and is stopped; its data waits for the next start |
| **orphaned** | nothing refers to it at all - no container, VM or job. If its data is not wanted, it can be deleted |
| **no claim** | its claim is gone and the volume was kept: an old copy from a storage class change, or a claim deleted with its data retained |

**Show N unused** at the top lists just the orphaned and unclaimed ones - the
place to look when freeing space.

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

### Classes for some disks - SSDs, say

Tag the disks first (below), then choose **Only on disks tagged** - `ssd`, say -
when making a class, and Longhorn puts that class's replicas only on disks with
every tag chosen. **Only on nodes tagged** narrows it to nodes with a tag too.
The form says which nodes can hold its replicas as you choose, and warns when
they are fewer than its replicas (volumes would run a copy short) or none (they
would not start). The class table shows each class's tags.

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
- **Add tags** on a Longhorn disk labels it - `ssd`, `nvme`, `hdd`, anything -
  for storage classes to choose by, and **Node tags** does the same for a
  whole node. On Harvester, a disk Harvester added keeps its tags on its block
  device, as Harvester's dashboard does, because Harvester writes the Longhorn
  disk from it and would undo tags set on Longhorn alone.

## When a drive fails

A volume keeps running on its other copies when a drive dies, and Longhorn
starts rebuilding the lost copies on other nodes after about ten minutes -
**Volumes** shows each rebuild's progress. What it cannot do alone is let go
of the dead disk: it keeps the disk, and the failed copies it held, until told
otherwise. A volume that already has a copy on every other node then has
nowhere to rebuild until that node has a working disk again.

The failed disk shows on its node, and in **Disks**, with what happened in
words - *Harvester no longer finds this drive*, or *nothing is mounted at
/mnt/disk2* - and an alert goes out. **Replace failed disk** reviews every
volume that had a copy on it:

| Outcome | Means |
|---|---|
| **rebuilds elsewhere** | another node has room and no copy yet: it rebuilds there now |
| **waits for the new disk** | every other node already has a copy: it rebuilds on this node once the new drive is added |
| **only copy** | a single-copy volume that lived on this disk |

Then, as a job you can follow and carry on if interrupted: new copies stop
going to the disk, its failed copies are let go of (only where a healthy copy
exists elsewhere), the disk is taken out of Longhorn - on Harvester, released
the way Harvester's own UI does it - and Harvester's record of the dead drive
is cleared. Add the new drive with **Add to Longhorn** and the waiting copies
rebuild onto it.

**Only copies are never given up unless you say so.** A drive that is only
unplugged, or not mounted, comes back with its data - reconnect it instead.
If it is truly dead, restore those volumes from a backup (Data protection), or
tick *Give it up* and type the disk's name.

### Booting with a dead or missing drive

- **Harvester** mounts the drives it manages itself, so a host starts without
  one; the disk shows as failed, as above.
- **k3s and other Linux** mount Longhorn's drives from `/etc/fstab`. A plain
  line there makes the host wait for the drive and stop at an emergency shell
  when it never appears. **Add to Longhorn** off Harvester gives the commands
  to mount a drive safely: `nofail` so the host starts without it, and the
  empty folder locked (`chattr +i`) so nothing is written onto the system disk
  in its place - Longhorn marks the disk failed instead.

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

### What the V2 engine needs

**What each host needs** (beside the V2 switch, or **details** on the storage
classes card) is a checklist. Each line is ticked, crossed or marked unknown,
and hovering its **?** shows how to do it on Harvester, k3s or RKE2:

- **The cluster:** Longhorn 1.8 or newer, and the V2 engine switched on.
- **Each host:**
  - a CPU with SSE4.2 (any x86 from about 2008, or arm64);
  - the kernel modules `vfio_pci`, `uio_pci_generic` and `nvme_tcp`;
  - 2 GiB of hugepages;
  - a whole empty disk given to Longhorn as a V2 (block) disk.
- **Yours to check:** `nvme-cli` on each host, which Homestead cannot see.
- **Worth knowing:** V2 keeps one CPU core busy on every node that runs it.

On Harvester, switching V2 on reserves the hugepages and loads the modules
itself; on k3s and RKE2 those are two commands on each host, given in the
tooltips. A V2 volume schedules only on hosts where everything is ticked.

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
