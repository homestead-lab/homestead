# Storage

**Volumes** lists every volume in the cluster - Kubernetes calls them
persistent volume claims - with its size, how full it is, who uses it, and
what Longhorn thinks of it.

![Volumes](https://github.com/wjcloudy/homestead/releases/latest/download/homestead-volumes.jpg)

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

If the PVC is already gone, **Delete** inspects the exact retained Longhorn
volume instead of failing on the missing claim. It checks PV ownership,
workload references, CSI attachments and Longhorn attachment tickets. Only
detached, unclaimed backing data can be removed, with a fresh identity check
and exact-name confirmation. A Released PV is handed to CSI's normal Delete
reclaim policy; finalizers are never forced. External backups are kept.

## Storage classes

A storage class is the recipe for new volumes: how many copies, which engine,
whether VM disks on it can live-migrate. The **Storage classes** card creates
them and picks the default. Kubernetes cannot edit a class once made, so change
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

![Disks](https://github.com/wjcloudy/homestead/releases/latest/download/homestead-disks.jpg)

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

![Settings - Cluster](https://github.com/wjcloudy/homestead/releases/latest/download/homestead-settings-cluster.jpg)

To make room: add a disk, delete old copies and volumes you no longer need, or
move volumes with too many copies onto a class with fewer.

## Changing a volume's storage class

Kubernetes cannot change a volume's class, so **Change storage class** makes a
copy on the new class and swaps it in under the original's name - every
container, VM, share and backup job that uses it by name carries on unchanged.

![Change storage class](https://github.com/wjcloudy/homestead/releases/latest/download/homestead-storage-class-change.jpg)

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

![Storage class change in progress](https://github.com/wjcloudy/homestead/releases/latest/download/homestead-storage-class-progress.jpg)

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
