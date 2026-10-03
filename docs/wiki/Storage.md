# Storage

**Volumes** lists every volume in the cluster - Kubernetes calls them
persistent volume claims - with its size, how full it is, who uses it, and
what Longhorn thinks of it.

![Volumes](https://github.com/homestead-lab/homestead/releases/latest/download/homestead-volumes.jpg)

## Reading the table

The **Usage** column separates three different measurements (GiB = 1,024³ bytes):

- **Files** and its bar: fresh filesystem usage from kubelet, against the
  filesystem's capacity. Formatting can make that capacity slightly smaller
  than the provisioned device. Missing or stale readings say **Filesystem usage
  unavailable**, not zero. Detached volumes, raw block devices and some shared
  mounts do not report this measurement; shared observations are never summed.
- **Provisioned**: the logical size requested for the volume.
- **Longhorn footprint**: allocated blocks including snapshots and untrimmed
  blocks, not a sum across replicas. It can legitimately exceed the provisioned
  size, for example after an expansion snapshot. It is not used for the files bar.

Review snapshots and recovery needs before choosing cleanup; Homestead does not
automatically delete recovery points to bring the footprint below the volume size.
See Longhorn's [space consumption guide](https://longhorn.io/kb/space-consumption-guideline/).

| Badge | Means |
|---|---|
| **RWO** / **RWX** | one host can mount it (ReadWriteOnce), or many at once (ReadWriteMany, served by Longhorn over NFS) |
| **×3**, **×2** | how many copies Longhorn keeps. **×1** is orange: one failed disk loses that volume; so is a count with a copy it cannot place |
| **on k3s: nvme0n1 + OS disk** | where each copy is: its host and disk - the device a disk Homestead set up is named after, **OS disk** for the system drive (its default folder, or its free space). A copy that is not running is orange. Hover for the whole story |
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
  without a restart. The claim must be bound and its StorageClass must allow
  expansion. Edit checks these before enabling the size field. Replica count
  can still be changed when expansion is unavailable.
  If an older Homestead release removed a restored volume's class, an admin
  can use **Repair resize support** in Edit. Homestead checks the bound
  Longhorn volumes and recreates their class before you resize. It does not
  change the volume size or copy its data. Restore cleanup keeps classes
  while any claim or retained backing volume references them. VM disk and
  share edits use the same expansion checks; a disk with an expansion
  pending cannot be reduced to its old reported capacity.
- **Files** - browse and edit the files on it, in the same editor VS Code uses.
  A helper pod mounts it for up to 30 minutes; a ReadWriteOnce volume in use
  must be stopped first. Closing the browser with its button, X, Escape, or
  navigation requests removal of the helper. Closing the tab also sends a
  cleanup request when the browser permits it. Abandoned helpers stop after
  30 minutes; Homestead removes expired pod objects when the cluster API is
  available. Saves keep the old file as `<name>.homestead-bak`.
- **Ownership** - hand the files to the user an app runs as (read from its
  PUID/PGID), for data imported as root by an older release.
- **Snapshots** - **Take snapshot now** and **Back up now**, and the volume's
  list of both - see [Data protection](Data-protection).
- **Change storage class** - below.
- **Delete** - refused while anything uses it. Then you choose: keep the data
  (the volume is released but its disk kept) or delete it for good, after
  typing its name.

If the PVC is already gone, **Delete** inspects the exact retained Longhorn
volume instead of failing on the missing claim. It checks PV ownership,
workload references, CSI attachments and Longhorn attachment tickets. Only
detached, unclaimed backing data can be removed, with a fresh identity check
and exact-name confirmation. A Released PV is handed to CSI's normal Delete
reclaim policy; finalizers are never forced. External backups are kept.

## Upgrading Longhorn V2

Open **Settings → Updates → Platform versions** (also on **Cluster**) and choose
Longhorn's **Upgrade** or **V2 upgrade status**. The upgrade dialog checks hosts,
Kubernetes versions, volume health and actual RW replica placement before starting.

**Live** upgrades keep V2 volumes attached. Longhorn 1.13 requires Kubernetes 1.34+
and a source installation of Longhorn 1.12.2 or later, including its V2 instance
managers. Each affected volume needs healthy replicas on at least two eligible
hosts, with running V2 instance managers and schedulable block disks. Single-host,
ublk and sharded configurations use **Offline**: stop workloads, detach V2 volumes
and wait for their replicas to stop. Homestead does not stop workloads for you.

Back up the volumes and leave space for replica rebuilding. After the manager
rollout completes, Homestead enables Longhorn's V2 rolling controller for a live
upgrade and follows each host through engine relocation, instance-manager update,
engine restoration and volume recovery. Success requires ready V2 pods on the new
image and healthy volumes. V1 volumes retain Longhorn's existing engine-upgrade policy.

**V2 upgrade status** offers pause/resume and the host timeout (normally 60 minutes).
Pausing allows the current host to finish; it does not roll back versions. Enabling
live upgrades also applies to later manager upgrades. The timeout does not apply
while waiting for healthy volumes. Volume expansion and VM live migration using
V2 disks are blocked until every host upgrade finishes, including paused or failed upgrades. Resolve failed replica
recovery or full disks before resuming. Homestead leaves resetting an exhausted
retry controller to Longhorn's manual recovery procedure. Harvester-managed
Longhorn is upgraded through Harvester.

See [Longhorn's V2 upgrade guide](https://longhorn.io/docs/1.13.0/deploy/upgrade/v2-instance-upgrade/).

## Storage classes

A storage class is the recipe for new volumes: how many copies, which engine,
whether VM disks on it can live-migrate. The **Storage classes** card, under
**Settings → Hardware and storage** beside Longhorn's own settings (and from
**Volumes ⋯ → Storage classes**), creates them and picks the default. Kubernetes cannot edit a class once made, so change
means create a new one.

**Copies go on** decides where a class's copies may be. **Different hosts**
(Longhorn's default) puts each on a host of its own, so a host or a disk can
fail; with fewer hosts than copies, the rest are never placed and the volume
runs a copy short. **Different disks** lets copies share a host but never a
disk (Longhorn's `replicaSoftAntiAffinity` on, `replicaDiskSoftAntiAffinity`
off): on a one-host cluster with two drives, a failed drive is survived, a
failed host is not. It is offered first on a one-host cluster, and the
dialog counts the hosts or disks that can hold the copies as you choose.

New classes keep a volume's data when its claim is deleted (**Retain**): the
volume stays on **Volumes** marked **no claim**, to reuse or delete there.
Choose **Delete** for a class whose data is disposable.

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
Settings → Hardware and storage) opens them all.

![Disks](https://github.com/homestead-lab/homestead/releases/latest/download/homestead-disks.jpg)

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

### Prepare an existing V1 disk for V2

After setting up V2 on the host, open its **Storage** section. On a healthy
V1 disk, choose **… > Prepare for V2**. An administrator completes two
separate reviews:

1. **Evacuate V1.** Homestead checks volume health, full replica sizes, free
   space, tags and placement rules. Confirm disabling new replicas and moving
   existing ones off. Longhorn chooses their destinations. The filesystem
   remains mounted; this approval does not allow an erase.
2. **Review erase.** Continue only after Longhorn has removed the old replicas
   and their replacements are healthy and writable. Check the host and
   device, type its exact name, and confirm erasing it. Homestead removes the
   V1 entry, unmounts the filesystem normally, removes its fstab entry and
   clears its signatures. It registers the device using its stable hardware
   ID and waits for Longhorn to report a ready, schedulable V2 block disk.

Other **V1** disks need room for these replicas. V2 space cannot receive them.
With three replicas across three hosts, clearing a host's only V1 disk
requires a spare V1 disk on that host or another eligible V1 host. The review
checks capacity; it does not reserve it while evacuation runs.

This guided preparation supports dedicated whole disks with ext4 or XFS.
System disks, shared filesystems, partitions and LVM require separate disk
planning. Harvester manages its own disks. Resolve backing-image copies
before starting, and attach affected volumes so their writable replicas can
be checked.

Open **V2 preparation** on the disk or its saved task in **Jobs** to return.
You can stop evacuation before approving the erase; replacement replicas
remain and the old disk stays disabled for new replicas. A preparation
helper continues through a Homestead restart. Successful helpers are removed;
their output stays in the task log. A failed or missing helper is never
automatically recreated. Keep the task and inspect its log, the device and
the host receipt in `/var/lib/homestead/disk-v2` before recovering partial
changes; unresolved tasks cannot be dismissed.

Preparing a physical disk does not convert its volumes. Create a V2 storage
class, then use **Change storage class** on each volume you want to move.
That copies the volume and stops its workloads for the copy.

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
  when it never appears. **Add to Longhorn** mounts a drive the safe way, as
  below: `nofail` so the host starts without it, and the empty folder locked
  (`chattr +i`) so nothing is written onto the system disk in its place -
  Longhorn marks the disk failed instead.

### Adding a disk on k3s or RKE2

**Add to Longhorn** on an unused disk sets it up from Homestead, as Harvester
does on Harvester. It first looks at the disk on its host, changing nothing:
its size and stable name, its partitions and filesystem, whether it is the
system disk or mounted, and - mounting it read-only for a moment, without
replaying a journal - whether it already holds Longhorn's data. Then it offers
only what is safe for what it found:

- **Format it** (a blank disk) - ext4 or XFS, typed to confirm;
- **Keep its Longhorn data** (a disk from before, V1) - mounted as it is, with
  no format. Replicas that belonged to another cluster show in Longhorn as
  orphaned data; volumes come back from their backups;
- **Erase it and format** (a disk holding anything else) - typed to confirm;
- the **V2 engine** is given the raw device by its `/dev/disk/by-id` name.

A disk something else holds open is found first, rather than failing in
`wipefs` as "Device or resource busy". `multipathd`, which Ubuntu Server runs,
claims plain SATA and SAS disks as maps of its own: setting such a disk up
releases the map and adds that disk's WWID to the blacklist in
`/etc/multipath.conf` (a copy is kept), so `multipathd` leaves that one disk
alone. A disk in a RAID array or an encrypted volume is refused, naming what
holds it.

A disk from an earlier install, such as an old Ubuntu drive with its
`ubuntu-vg`, which the host may have switched on at boot, offers **Wipe and
prepare it**. You confirm by typing the device path. Homestead switches the
old volume groups off and removes them, wipes the LVM labels, filesystem
signatures and partition table, has the host re-read the disk, and looks at it
again. It formats the disk only once it's blank. The wipe is refused, with
each reason listed, when:

- anything on the disk is mounted or used as swap
- one of its volumes is open
- its volume group is the one the running system's root is on, matched by
  UUID because the old drive and the system can both call theirs `ubuntu-vg`
- the volume group also spans another disk, which wiping this one would break

The system disk, and a disk that is mounted, are refused. A V1 disk is mounted
at `/mnt/<device>`: the folder is locked while empty, fstab names the
filesystem by UUID with `nofail` (a copy of fstab is kept first as
`/etc/fstab.homestead-backup`), and the mount is checked to be that disk before
Longhorn is told. A failed, empty Longhorn entry for the same folder - left by
adding the folder before the disk was mounted - is cleared first. The work is
done by a short-lived privileged helper on that host, admins only.

**Use free space** on the system disk gives Longhorn space nothing else
uses, from one of two places:

- **The system's LVM volume group**, when it has room (Ubuntu Server gives its
  root volume 100 GB and leaves the rest free). For V1, a logical volume
  `<group>/longhorn`, formatted ext4 and mounted at `/mnt/longhorn-os` the same
  safe way; for V2, `<group>/longhorn-v2`, given to the V2 engine raw. A tenth
  of the group (at least 10 GB) stays unallocated for the system to grow into.
- **Unallocated space on a GPT disk** - past its last partition, where an
  installer was told to leave some. A new partition is made there while the
  system runs: the partition table is saved to `/var/lib/homestead` first, the
  space is checked free again right before the write, the table gets one more
  entry without the disk being re-read, and only the new partition is shown to
  the kernel - nothing mounted is touched. For V1 it is formatted ext4 and
  mounted at `/mnt/<disk>-longhorn`; for V2 it is given raw by its PARTUUID.
  Typing the disk's name confirms it.

What Homestead never does is shrink or move a partition or filesystem of a
running system: ext4 cannot shrink while mounted and XFS cannot shrink at all.
Making room that way needs a rescue boot. MBR disks are not partitioned either
(four entries, often one of them extended).

On the system's drive a copy there shares the drive with the system's own
Longhorn folder, so pair it with another drive or host for redundancy.

### Room for the system

Longhorn's first disk, `/var/lib/longhorn`, is a folder on the system's root
filesystem unless it is a volume of its own. Longhorn keeps 30% of that
filesystem out of its sums, but that only decides where *new* copies go:
copies already there, and their snapshots, keep growing. Three things keep the
system's space the system's:

- **A volume of its own.** Where the system is on LVM with room, the installer
  mounts a logical volume at `/var/lib/longhorn` before Longhorn starts - on
  every machine it sets up, joining ones too - so Longhorn can never fill the
  root filesystem. It asks how big; space left out stays free for the system or
  for V2.
- **A capped journal.** The systemd journal is capped at 1 GB on every host
  (journald's own default is up to 4 GB), unless a cap is set already.
- **A floor.** When a root filesystem holding a Longhorn disk falls under 15%
  free (never under 10 GB), Homestead stops Longhorn placing new copies there,
  and says so as an alert; above 20% (15 GB) it may again. Copies already there
  stay: **Move replicas off** moves them, or give Longhorn a volume of its own.

### Disk tags

New disks are tagged by what they are: **ssd** (SSD or NVMe) or **hdd**. The
disk the system runs from is tagged **os** only - so a class choosing `ssd`
does not place replicas on the system disk. Disks already in Longhorn with no
tags get the same once; a tag you change or remove afterwards is yours and is
not put back.

## Longhorn allocation

Longhorn books a copy's full size on a disk when it places it, however little
the volume holds. A disk takes no new copy once those bookings reach its size ×
the **over-provisioning** percentage, or once too little of it is actually
free. After that, new volumes come up a copy short, rebuilds wait and growing a
volume is refused - nothing already placed moves.

**Volumes** shows each node's allocation against that limit, and an
**empty-volume allocation limit** for one, two or three copies. This is a logical
upper bound, not a guarantee that an existing volume's replica can rebuild there.
The separate **physical rebuild budget** is free space above Longhorn's
minimum-free-space reserve on the best eligible disk, not the sum of spare space
across disks. A rebuild's existing block/snapshot footprint must fit below that
budget as well as meeting the logical allocation, tags and placement rules.
Over-provisioning increases allocation headroom, not physical free space.
The dashboard names a node past 80%, and a notification goes out.

**Settings → Hardware and storage** sets over-provisioning and the minimum free space, with a
preview of each node's new limit, and turns the V2 engine on or off.

## Longhorn V2 support and considerations

Homestead supports Longhorn's **V2 data engine (SPDK)** alongside V1, including
host preparation, V2 storage classes, block-disk setup and upgrade status.
**V1 is the simpler starting point for a small home lab.** Consider V2 when your
storage hosts have dedicated disks and CPU/memory headroom, and the workload
benefits from it. V2 targets lower latency and higher throughput; it is not a
promise of lower CPU use or better performance on every machine.

### Version and hardware support

Homestead's setup accepts Longhorn 1.8 or newer, but that is an integration
minimum, not a statement that every release has the same V2 maturity. Upstream
marks V2 generally available from **Longhorn 1.12.0**. Use the requirements and
release notes for the version actually installed, including Harvester's bundled
version when applicable.

For **Longhorn 1.13.0**, the upstream installation guide specifies:

| Item | Plan for |
|---|---|
| Kubernetes / kernel | Kubernetes 1.34+; Linux kernel 6.7+ for the documented NVMe/TCP path |
| CPU architecture | AMD64 with SSE4.2, or ARM64; compatible device drivers are also required |
| Memory | Normally 2 GiB of 2 MiB hugepages per V2 node, in addition to OS and workload memory |
| CPU budget | Dedicated capacity for each V2 instance manager; polling mode can keep a core busy even at low application load |
| Kernel modules | `vfio_pci`, `uio_pci_generic`, `nvme_tcp` |
| Storage | Dedicated raw block devices; local NVMe is recommended for performance |

See [installation requirements](https://longhorn.io/docs/1.13.0/deploy/install/)
and [resource planning](https://longhorn.io/docs/1.13.0/best-practices/).
These are version-specific requirements, not an instruction to upgrade every
cluster to 1.13.0. Homestead reads the installed memory settings, including
supported alternatives to hugepages; changing that setting does not remove
the engine's actual memory needs.

### Power use, ARM and small quorum nodes

Polling is the default. Longhorn 1.13's full interrupt mode can reduce idle CPU
use, with a latency trade-off under load; changing it requires V2 volumes to be
detached. Check measured power draw and application latency before choosing it
for an always-on home server.

ARM64 support has a specific 1.13 caveat: upstream reports possible stuck I/O
with NVMe-driver node disks and two or more SPDK CPU cores, and recommends
AIO-backed disks as a workaround. UBLK remains experimental, with a kernel 6.17
panic warning. See the [version's important notes](https://longhorn.io/docs/1.13.0/important-notes/)
before choosing a driver or frontend. A Pi suitable for etcd is not automatically
a suitable V2 storage host.

Longhorn supports [selective V2 activation](https://longhorn.io/docs/1.13.0/advanced-resources/v2-data-engine/selective-v2-data-engine-activation/):
the Kubernetes node label `node.longhorn.io/disable-v2-data-engine: "true"`
excludes that node from V2. Workloads using V2 volumes must also stay on
V2-enabled nodes. This does not by itself exclude ordinary apps or V1 replicas.

**Current Homestead limitation:** the guided V2 setup checks every registered
Longhorn node and does not exempt nodes with that exclusion label. A small
quorum-only node can therefore block guided enablement. Selective activation
needs upstream Longhorn configuration and verification; do not reserve large
amounts of memory on a small node just to clear the wizard. Keep V1 if you need
the simpler guided route. See [the small third-server design](Installing-on-k3s#using-a-small-third-server).

### Disks, migration and recovery

V1 and V2 can coexist, but enabling V2 does not convert existing volumes or
filesystem disks. Homestead's guided V2 disk preparation uses dedicated whole
disks. Check IOMMU grouping for the SPDK NVMe driver; a device that cannot be
isolated may need the upstream AIO path instead. Never hand over an OS disk or
a device still carrying wanted data.

Back up first, create a V2 class, and try a disposable workload. Check attachment,
snapshots, backup/restore, expansion and any VM migration you need against the
installed release. Then use [Change storage class](#changing-a-volumes-storage-class)
for the reviewed copy-and-swap workflow, with downtime and room for both copies.
[Prepare an existing V1 disk for V2](#prepare-an-existing-v1-disk-for-v2) first
evacuates its replicas, then separately reviews erasing it; it is not an in-place
volume conversion.

Control-plane quorum, storage replica count and V2 upgrade eligibility are
separate. A third server with no storage does not add a third data copy or make
a single storage host eligible for live V2 upgrades. Keep independent backups
and follow [the live/offline upgrade rules](#upgrading-longhorn-v2).

### What the V2 engine needs

Open **Settings > Hardware and storage > Longhorn > Set up Longhorn V2**.
The dialog checks the installed Longhorn requirement and each host before
allowing enablement. Administrators can also open it from **What each host needs**.

1. **Prepare hosts.** Review and confirm one host at a time. On Linux k3s/rke2,
   a saved Kubernetes Job installs `nvme-cli` if needed, loads and persists
   `vfio_pci`, `uio_pci_generic` and `nvme_tcp`, and reserves 2 MiB hugepages.
   The usual requirement is 2048 MiB per host; newer Longhorn memory settings
   and the ordinary-memory option are respected. Existing larger reservations
   and other pods' hugepage requests are preserved. Package repositories must
   be reachable; unsupported operating systems require manual preparation.
2. **Verify capacity.** Live status and **Task log** show progress. Configuration
   saved is not the same as Kubernetes seeing the new capacity. If it remains
   zero, **Review reboot** opens the existing drain, quorum and storage review.
   Reboot one host at a time. A single-node reboot interrupts Homestead and
   its workloads; reopen setup when it returns. After reboot, verify the host
   and allow scheduling from its node page when appropriate.
3. **Enable V2.** Confirm its ongoing CPU and memory costs after all host checks
   pass. The dialog follows observed V2 instance-manager readiness. Longhorn's
   admission checks still apply if the cluster changes during the request.
4. **Choose disks and a storage class.** Add suitable block disks through the
   existing disk review, then create a V2 storage class. Preparing hosts does
   not format disks or change existing volumes' data engines.

Host tasks continue when the dialog closes or Homestead restarts. Failed tasks
retain their logs and partial configuration; review before retrying. Cancelling
tracking cannot undo installed packages or reserved memory, so these tasks have
no cancel action. Preparation jobs and their logs expire after seven days;
recheck setup if preparation is older than this.

On Harvester, the enable review uses Harvester's own setting. Harvester owns
host preparation and required restarts; Homestead observes its progress rather
than running generic host jobs. V2 needs Longhorn 1.8 or newer. It cannot be
disabled while V2 volumes or block disks remain.

## Changing a volume's storage class

Kubernetes cannot change a volume's class, so **Change storage class** makes a
copy on the new class and swaps it in under the original's name - every
container, VM, share and backup job that uses it by name carries on unchanged.

![Change storage class](https://github.com/homestead-lab/homestead/releases/latest/download/homestead-storage-class-change.jpg)

The review lists affected workloads, the new volume's size, replica allocation
and estimated destination room. Both copies use space until you remove the old
one. Unknown capacity is labelled unknown, not free space. Approve the downtime
and warnings once, then choose **Move volume**.

The initial confirmation expires after ten minutes and is bound to your account,
the PVC/PV identities, destination class and affected workload versions. A changed
selection or resource needs a new review. Incomplete/paginated inventory and API
errors block the review; absent optional VM/Longhorn APIs are accepted only when
API discovery confirms they are not installed. Opening this review reads job
history without advancing jobs. A queued move rechecks its initial inventory
before stopping workloads, and duplicate queued moves of the same volume are blocked.

Keep backups: preflight is not an atomic storage/scheduler reservation. After a
lost Start response, check **Recent jobs**; the dialog does not repeat the request,
and its approval cannot be consumed twice. The durable workflow for new moves:

1. everything using the volume stops, and how each was running is noted;
2. a volume the same size is made on the new class;
3. the data is copied and checked - files with rsync (owners, permissions,
   ACLs, links kept) then compared by checksum; a VM disk block by block, then
   compared byte for byte;
4. the original is released and the copy takes its name;
5. everything starts again as it was.

![Storage class change in progress](https://github.com/homestead-lab/homestead/releases/latest/download/homestead-storage-class-progress.jpg)

New moves record each request's intent before sending it and verify its resource
identity afterwards. A lost or unverified reply is not retried or adopted by name.
Copy completion requires the completed copy Job and checksum evidence from its
own pod; unavailable log progress is shown as unavailable, not zero percent.
Cutover reserves the new PV for the original claim name, preserves the original
PV with Retain, and waits for the replacement claim's exact binding. Fresh joint
placement checks run before cutover and restart. Warning-free checks continue;
capacity warnings require review, and hard blockers cannot be overridden.

After success, the original remains an **old copy** until explicitly removed.
During a move or recovery hold, Homestead protects the claims, backing volumes,
copy-verification Job and snapshot operations against conflicting changes.
Unresolved storage jobs survive history cleanup. Live host-loss, CSI-controller
and shared-filesystem failure rehearsals remain necessary before claiming HA.

### When a storage move needs review

Use **Recent jobs → Review storage move**. Pause stops further Homestead steps,
but an already accepted copy Job can keep running. Continue rechecks identities,
capacity and current placement; it does not undo data or replay an uncertain write.
If the outcome is unknown, the screen remains inspection-only:

1. Keep both copies, the copy Job, and Homestead's persistent job history. Do not
   clear finalizers, force-detach storage, edit the journal, or delete resources
   merely to make a request retry.
2. Inspect the retained-resource details, exact UIDs, Kubernetes events and API
   audit records. Check the actual PVC/PV binding and which copy workloads use;
   names, Running status, and a disconnected node alone are not sufficient proof.
3. Before manual recovery, establish that no old workload, copy helper or pending
   API request can still write. An unreachable writer requires actual fencing by
   the cluster/storage operator. If that cannot be established, leave the hold in
   place and seek storage-administrator help rather than guessing which copy wins.
4. Recover or restore verified data under a separately reviewed procedure. There
   is currently no automatic acknowledgement or adoption of unknown API outcomes;
   the retained job cannot safely be dismissed just because resources look correct.

Homestead must have its data PVC mounted at its configured data directory. Every
running process with writable access to that claim must report the supported
storage protocol from the same Homestead version. Registration happens on each
replica, normally within 20 seconds of startup; old-container records cannot
authorize replacements. Finish the rollout before starting a move. Homestead's
own update, restart and replica changes are interlocked while a move is unfinished
or needs recovery. Jobs from older releases keep their original engine and recovery
behavior. Externally forced downgrades, manual cluster changes, and external
controllers are outside these internal guards; do not force an upgrade to bypass
a recovery hold without an administrator-approved recovery plan.

## Without Longhorn

On a cluster whose storage is k3s's local-path or another provisioner, volumes
still work - create, grow, browse, delete - but copies, snapshots, backups,
allocation and moves need Longhorn. **Helm** can install Longhorn.
