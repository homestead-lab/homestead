# What Homestead changes

Homestead is a control plane: most of what it does is change the cluster
when you ask it to. Some things it does on its own, and it writes notes onto
the apps it touches and changes a few settings on the hosts themselves. This
page lists the kinds of Kubernetes object Homestead creates, changes or
deletes, and when.

Most of what Homestead makes for itself is named `homestead-*`. Much of it
also carries the `homestead.io/managed=true` label, **but apps you deployed
through Homestead carry it too**, so the label alone does not tell Homestead's
own objects from your apps. The same goes for
`app.kubernetes.io/managed-by=homestead`, which is on apps' Secrets and on
restored volumes. Never delete by any Homestead label.

**How complete this is.** A test in the repository
(`tests/test_what_homestead_changes.py`) fails when the install manifests or
Homestead's code build an object of a kind this page does not name. Kinds the
test cannot see (ones Homestead only changes or deletes, and ones its code
holds in a variable) are listed in the test by hand, and must be named here
too. What you hand Homestead yourself is not covered: the **Resources** page
can create or change any kind you give it, and a chart from **Helm** (or an
add-on such as KubeVirt, CDI or macvtap, installed from its upstream release)
makes whatever that chart or release contains. The situations below are
written by hand from the code.

## What the install creates

The Helm chart and the plain manifest make the same objects
([Installing Homestead](Installing-Homestead#what-gets-installed)):

| Kind | Name | What it is |
|---|---|---|
| `Namespace` | `lab` | the namespace your apps go in, and Homestead's own with the plain manifest. With Helm, Homestead's own namespace comes from `helm install --create-namespace` |
| `ServiceAccount` | `homestead` | who Homestead is to the cluster |
| `ClusterRole`, `ClusterRoleBinding` | `homestead` | what Homestead may do: read every kind and change most of them ([why](Installing-on-an-existing-cluster#what-homestead-is-allowed-to-do)) |
| `Role`, `RoleBinding` | `homestead-console` | container consoles, only in the apps' namespace |
| `Role`, `RoleBinding` | `homestead-setup` | lets Homestead make, read and delete its one-time setup code, only in its own namespace. Read and delete are for that one Secret; create is for any Secret there, as Kubernetes cannot limit create to one name |
| `Secret` | `homestead-setup` | the one-time setup code for the first administrator, only when you chose it ahead (the chart's `setupCode`, the installer's `HS_SETUP_CODE`); otherwise Homestead makes it when it starts (below) |
| `PersistentVolumeClaim` | `homestead-data` | settings, job history, stats and the audit log |
| `Deployment`, `Service` | `homestead` | Homestead itself and its address |
| `DaemonSet`, `ConfigMap` | `homestead-nodeprobe` and its scripts | the node probe: one privileged pod per host, for temperatures, devices and drive health. The Helm chart installs it (`nodeprobe.enabled`, on by default); with the plain manifest Homestead installs it itself once it starts (below) |

The install script for k3s and RKE2 writes its manifests to
`/var/lib/homestead/install` and applies them once: a `HelmChart` for
Longhorn, KubeVirt's and CDI's operators with their `KubeVirt` and `CDI`
resources when you chose VMs, Homestead's manifest, and a `ConfigMap`,
`homestead-install`, in `lab`, saying which components Homestead should add
once it starts. On the host itself it installs open-iscsi and an NFS client
for Longhorn, raises the inotify limits (`/etc/sysctl.d/90-homestead.conf`),
caps the systemd journal and keeps multipathd off Longhorn's disks. Where the
system disk has free space in LVM, it can give Longhorn a volume of its own,
mounted at `/mnt/longhorn-os` through `/etc/fstab` for new clusters, while
runtime files remain on the OS at `/var/lib/longhorn`. Joining hosts match
the existing cluster's path, and reruns preserve existing chart settings
and data. The one-line
`install.sh` can also install the host console.

## What Homestead does on its own

Nobody clicks anything for these. Most run on whichever copy of Homestead
leads (it holds a `Lease`, `homestead-leader`, renewed all the time).
"Always" means there is no switch.

**Every time Homestead starts**

| What | Kinds | Turned off by |
|---|---|---|
| Brings its own role up to this release's rules | `ClusterRole` `homestead` | always; skipped when Homestead may not change its own role |
| Renames old `harvui.io/*` labels and annotations to `homestead.io/*`, once | nodes, services, claims, ConfigMaps, Secrets, Deployments, StatefulSets, DaemonSets, VMs | runs once only |
| Brings the node probe's scripts up to this release, and the VM placement checks if you switched them on | `ConfigMap`, `DaemonSet` | removing the probe |
| Fits its own Deployment's rollout strategy, without a restart | `Deployment` `homestead` | always |
| Makes the one-time setup code until the first account exists, and deletes it once one does (and again at each start, should an upgrade put it back) | `Secret` `homestead-setup` | always |
| Removes join tokens and network-boot pods left by 2.8.68-2.8.71 | `Secret`, `Pod` | always |

**Once, soon after it starts**

- **The node probe**, on every cluster (Harvester and existing clusters too),
  unless the installer was told not to (`node-probe: no` in
  `homestead-install`) or Homestead runs with
  `HOMESTEAD_NODEPROBE_AUTO_INSTALL=false`; and again after a reinstall that
  keeps its data.
- **Outage webhooks saved before Allow a LAN address existed** that resolve
  to a LAN or cluster address get it ticked (`lan` in `homestead.io/outage-actions`, on the
  `Deployment` or `VirtualMachine`), so they keep working. An address a
  webhook never reaches stays refused. This happens once per cluster: what
  was done is kept in the `ConfigMap` `homestead-outage-carry`, in
  Homestead's namespace next to `homestead-settings`. A webhook it could not carry within a day is a warning in the bell.
- **What `homestead-install` asked for:** the VIP, and kube-vip and Multus as
  `HelmChart`s. macvtap comes with Multus, once KubeVirt is there.

Each is done once. One you remove later is not put back.

**In the background, while it runs**

| What | Kinds | Turned on or off by |
|---|---|---|
| Keeps Homestead ranked at least as high as any app, every minute: moves Homestead to the lowest class that preempts and is at or above the highest app's, creating `homestead-critical` only when there is none, or back to the default when no app ranks above it. Each move restarts Homestead once and is marked with `homestead.io/priority-protect` | `PriorityClass`, `Deployment` `homestead` | always |
| Trims every attached Longhorn volume once a week (Saturday 04:00 UTC unless moved) as a Homestead job, and removes the Longhorn `RecurringJob` `homestead-weekly-trim` earlier releases made, once, if it is unchanged ([details](Data-protection#weekly-trim-by-default)) | Longhorn `Volume` (trim action); Longhorn `RecurringJob` `homestead-weekly-trim` | the weekly trim switch and **Change time** under **Data protection › Housekeeping** |
| Lets Homestead's own pods reach Longhorn's API (`longhorn-manager`, TCP 9500), only while another network policy fences the manager in (Longhorn 1.13's own, by default, or one you made). Those policies own it, so it goes when they do; Homestead removes it too | `NetworkPolicy` `homestead-backend-access` in `longhorn-system` | always; without it, rolling back and **Trim now** cannot reach Longhorn |
| More copies of each volume as storage hosts are added (up to 3), while the value is still the installer's or Homestead's own | Longhorn `HelmChart` values and `Setting`, Homestead's own Longhorn `Volume` | change the value yourself; k3s and RKE2 only |
| On a cluster of one host, Homestead's own data volume asks for the one copy that host can keep, once Longhorn says another cannot be placed and none is being rebuilt; no other volume, class or setting | Homestead's own Longhorn `Volume` | always, with Longhorn on exactly one host; not on Harvester |
| Rebuilds missing copies of detached volumes: Homestead attaches a detached volume that is missing a copy (with no frontend, so nothing can use it) until the copy is rebuilt, for up to 6 hours, and releases it as soon as an app asks for the volume. It turns Longhorn's own `offline-replica-rebuilding` off, once, because Longhorn's rebuild keeps the volume until it finishes even while an app is waiting for it | Longhorn `Setting` (marked `homestead.io/offline-rebuilding-own`); Longhorn `VolumeAttachment` ticket `homestead-rebuild` | **Rebuild copies of detached volumes** under **Settings → Hardware and storage** (on by default) |
| Recovery after a host failure, once, on clusters still at Longhorn's defaults: apps with storage may move off a failed host (`node-down-pod-deletion-policy` set to delete both kinds of pod); shared folders restart on another host within about a minute (`rwx-volume-fast-failover` on); and Longhorn waits an hour, not 10 minutes, for a failed host before copying its data again (`replica-replenishment-wait-interval`). Each setting is marked so a later choice is kept; Harvester manages its own | Longhorn `Setting` | **Settings → Hardware and storage** |
| Stops Longhorn using a host's root disk when it has less than 15 % or 10 GB free, and allows it again above 20 % or 15 GB, only for disks it turned off itself ([details](Storage#room-for-the-system)) | Longhorn `Node` | always |
| Tags a new Longhorn disk the first time it sees it | Longhorn `Node` | always |
| Marks a new host "storage pending" (a preference, `PreferNoSchedule`) until Longhorn runs on it, so apps go elsewhere first | `Node` taint `homestead.io/storage-pending` | always, where Longhorn's CSI driver is installed |
| Hot spare hosts kept out of normal use | `Node` taint `homestead.io/standby` | **Settings → How hosts share the work** |
| Hardware labels on hosts (GPUs, devices, an NFS-ready mark) | `Node` labels and an annotation | always |
| Keeps one default storage class (clears the default flag on extra ones), and puts back a restore class volumes still use | `StorageClass` | always |
| Keeps kube-vip's settings in step with the hosts (its interface, leader election, network capabilities), and removes a Homestead-made LoadBalancer `Service` whose app has been gone over 10 minutes | `HelmChart` `kube-vip`, `Service` | always; k3s and RKE2 only |
| Puts back an app address kube-vip lost, and restarts kube-vip (deletes its pods) when it stops electing a leader | `Service` status, kube-vip `Pod` | always, where kube-vip is the load balancer |
| Host changes, once per host, through a short helper pod: keeps multipathd off Longhorn's disks (`/etc/multipath.conf`), starts `iscsid`, caps the systemd journal at 1 GB, raises inotify limits, and puts a `.skip` beside manifests older installers left in k3s's or RKE2's auto-deploy folder so they are not applied again at boot | helper `Pod`, `Lease`; files on the host | always; k3s and RKE2 only |
| Host facts (OS, updates) read every 6 hours through a helper pod | `Pod` | always; k3s and RKE2 only |
| Host console on each host: installs, updates or removes a console service on the host | helper `Pod`, `Lease`; files on the host | **Settings → Cluster → Add-ons → Host console**; k3s and RKE2 only |
| Image scans: a short pod per host whose scan is old; finished scans and pre-pulls cleared away | `Pod`, `DaemonSet` | always |
| Unused images removed from hosts | helper `Pod` | image **Automatic clean-up** (off by default) |
| Automatic image updates: a snapshot of each volume, then the new image (keeping the two newest snapshots) | Longhorn `Snapshot`, `Deployment` | per app, **Updates** set to automatic, and the update window |
| Update checks about twice a day record the image digests an app ran | `Deployment` annotation | always |
| Power schedules stop and start apps and VMs; when several start in the same minute, the apps others start after go first | `Deployment` (replicas), `VirtualMachine` | an app's or VM's own schedule |
| Outage actions restart an app or VM that stays down | `Deployment`, `VirtualMachine` | an app's or VM's outage actions |
| A rebalance moves apps back onto a host that returned; under Spread by capacity, apps Flux applies by the restart mark `homestead.io/restartedAt` alone, with a host preference only Balance hosts left taken off in the same write | `Deployment` | **Settings → When a host comes back** |
| OS updates on a schedule: cordon, drain, reboot, apps held and given back. Taking over updates also turns Ubuntu's own automatic updates off on each host (`/etc/apt/apt.conf.d/99-homestead-hold`) | helper `Pod`s, `Node`, `Eviction`, `Deployment`, `StatefulSet`, `VirtualMachine` | **Nodes → OS updates** (off by default); k3s and RKE2 only |
| Restore tests: restore an app's backup into an isolated copy, check it, remove it, and record the result on the app (`homestead.io/restore-tested`) | `PersistentVolumeClaim`, `Deployment`, `NetworkPolicy` | **Test each app every 30 days** (off by default) |
| A restore into an app that does not come up on the restored data (not ready in time, stops being ready, or the uptime check does not answer) is swapped back on its own: the app is stopped, pointed at the volume it left, and started again. The restored volume is kept as its previous volume ([details](Data-protection#previous-volumes)) | `Deployment`, `PersistentVolumeClaim` | always, once someone has started the restore |
| A previous volume (the one a restore into an app moved the app off) is deleted, data included, 7 days after it was replaced, with an alert (Warnings) a day ahead. Checked every hour; never while anything mounts it, a job holds it, or its volume is set to **Retain** | `PersistentVolumeClaim`, and the volume behind it | **Data protection › Previous volumes › Keep for** (1 to 90 days; **until deleted**, 0, deletes nothing), or **Keep** on one volume |
| ISO volumes no VM has used for 7 days are removed; ISOs on stopped VMs are made writable again | `PersistentVolumeClaim`, `Job`, `VirtualMachine` | ISO keep days (0 keeps them) |
| Harvester images set to "auto" are downloaded and removed | `VirtualMachineImage` | per image |
| The SMB and NFS share servers kept in step with the saved shares | `Deployment` in the apps' namespace | only when a share server is installed |
| UniFi addresses synced into IP records | `ConfigMap`, `Secret` | only while UniFi is connected |
| Expired helpers for browsing files and snapshots removed | `Pod`, `VolumeSnapshotContent` | always |

Two things outside the cluster happen on their own too: linked Homesteads are
told when this one's address changes, and, with MQTT power control on, Home
Assistant can start, stop and restart the apps and VMs an admin picked.

## What Homestead writes onto your apps

Homestead keeps notes on the apps themselves, as annotations. Every key it
writes starts with `homestead.io/`. Among them:

| Annotation | On | When |
|---|---|---|
| `homestead.io/changedBy` | `Deployment` | each write Homestead makes to an app, automatic ones too: who (a person, a job or a schedule) and when. Every write carries it when it can, scaling and JSON patches included. It is left off, and the write goes as it came, before setup (no signing key yet) and on a JSON patch that sets the app's metadata wholesale, that comes while the app or the key cannot be read, or whose app has no annotations and no test of the `resourceVersion` just read. A cluster shutdown's own helpers (running Homestead as one copy, and the automatic recovery that gives its copies back) write without it too; recovering from the dialog carries it |
| `homestead.io/changedBy` | Longhorn `Setting` `freeze-filesystem-for-snapshot`, Longhorn `Volume` | an admin changes the filesystem freeze for snapshots on **Data protection**: who and when, as on apps, for the value written. A mark sent with any other write is dropped, and one left behind by a later change made elsewhere no longer checks out. **Data protection** shows who made the setting what it is |
| `homestead.io/update-action`, `update-previous`, `update-sources`, `update-rollout-at`, `updates-managed` | `Deployment` | an update or a rollback, automatic ones too |
| `homestead.io/update-mode` | `Deployment` | you set the app's updates to automatic |
| `homestead.io/ran-digests` | `Deployment` | update checks, about twice a day |
| `homestead.io/schedule`, `schedule-copies` | `Deployment`, `VirtualMachine` | you give an app a power schedule; a schedule stops it |
| `homestead.io/outage-actions` | `Deployment`, `VirtualMachine` | you set what happens when it stays down; once after an upgrade, a webhook saved before Allow a LAN address existed is given it when its address is on the LAN. A webhook address with anything after its host is kept in a `Secret` (below): the annotation holds only its scheme, host and port, and the `Secret`'s name. One still on the annotation, saved before, is moved there by the leader after its next monitoring round |
| `homestead.io/restartedAt`, `editedAt`, `movedAt` | the pod template | you restart, edit or move the app, or an outage action restarts it |
| `homestead.io/uptime` | `Deployment`, `VirtualMachine` | you turn monitoring on |
| `homestead.io/icon`, `icon-source` | `Deployment`, `VirtualMachine` | an app's or VM's icon |
| `homestead.io/group`, `placement`, `lan`, `firewall`, `primary-port`, `vip-mode`, `hardware`, `autostart-replicas`, `helm-previous` | `Deployment`, `VirtualMachine` | you set the app's group, placement, LAN address, firewall, ports, address, devices, autostart or Helm values |
| `homestead.io/tier`, `tier-class` | `Deployment` | an app follows its group's priority tier |
| `homestead.io/held-for-power`, `held-as` | `Deployment`, `StatefulSet`, `VirtualMachine`, `CronJob` | a host goes down for a reboot or shutdown, or a volume is rolled back to a snapshot; removed when the app is given back, or when the rollback ends with the app left stopped. While one is there, a rollback that would stop that app is refused; one left after its job ended can be removed by hand |
| `homestead.io/restore-tested` | `Deployment` | a restore test of the app finishes |
| `homestead.io/renamed-from`, `renamed-at` | `Deployment` | you rename an app |
| `homestead.io/balance-preference` | the `Deployment` (restarts nothing) | Balance hosts leaves a host preference on an app it moves; a person's Move or Edit of the host takes it off |
| `homestead.io/rebalancing` | Longhorn `Volume` | a volume's copies move to balance hosts |
| `homestead.io/one-host-copies` | Longhorn `Volume` | on a one-host cluster Homestead lowers its own data volume to one copy, or an admin lowers app volumes with **Match copies to hosts** in Cluster Health; it holds the count asked for before, and is removed when **Put copies back** restores it |
| `homestead.io/iso-unused-since` | ISO `PersistentVolumeClaim` | an ISO volume no VM uses |
| `homestead.io/restored-into` | `PersistentVolumeClaim` | a restore into an app makes the claim the backup goes into: it names the app (`namespace/app`) |
| `homestead.io/previous-volume` (a label), `previous-of`, `replaced-by`, `replaced-at`, `replaced-job`, `replaced-uid`, `replaced-reason`, `reminded-at`, `keep-previous` | `PersistentVolumeClaim` | a restore into an app, or a swap back, moves the app off a claim: it is marked as the app's previous volume (which app, what replaced it, when, and why); `reminded-at` when the day's notice is given; `keep-previous` when someone chooses **Keep**. They only mirror Homestead's own record for people and `kubectl`: a mark alone never makes a claim expire |
| `homestead.io/starts-after` | `Deployment` | you set **Starts after** on an app. It comes with the start gate: an init container, `homestead-start-gate`, first in the pod template, which only opens TCP connections to the apps it waits for. Saving it, changing it or clearing it restarts the app once. Never written on an app Flux or Argo CD applies |

Apps deployed through Homestead also carry the `homestead.io/managed=true`
label. Hosts get the taints and labels in the table above.

## What Homestead makes when you ask

Each of these happens only after someone signed in (or an API key, or an AI
agent with the right scope) asks for it, and most go through a review first.
A job someone starts (copying or renaming an app, an import, a host network
change) carries on in the background without a page open: the copy of
Homestead that leads moves every job on, in one loop, and a new leader picks
them up where the last one left off.

| Kind | When |
|---|---|
| `Deployment`, `Service` | deploying an app, an App Store app, a Docker Compose import or a container import; a VIP or port for an app; the object store for backups; a restore test's isolated copy of an app; the helper that brings the cluster back after a cluster shutdown |
| `PersistentVolumeClaim` | new volumes and VM disks, imports, restores from a backup, moving a volume to another storage class, ISO volumes for VMs, moves between clusters, a claim made again empty when its host is removed for good |
| `Pod` | short helpers: file browsing on a volume, a node shell, a host's script (OS updates, disk setup), asking a node's container runtime about images, import probes, the last step of a reboot or shutdown, copies while moving Homestead's own data |
| `Job`, `CronJob` | copies during imports and storage changes, preparing a disk for Longhorn V2, ISO copies, schedules you save on **Schedules**, running a Longhorn job now |
| `DaemonSet` | pre-pulling an image onto every host (`homestead-pull-*`, removed once done, or when its pull keeps failing), including the start gate's image once, when the first app gets **Starts after**; the VM placement checks when you switch them on |
| `Secret` | registry and source passwords, a VM's cloud-init, an app's secret-looking environment values (kept out of its Deployment), an app's or VM's outage webhook address (`homestead-outage-app-<app>`, `homestead-outage-vm-<vm>`, in its namespace, deleted with it, or by the leader an hour after it was deleted outside Homestead), backup target keys, linked-cluster and move credentials, MQTT and UniFi settings, user accounts (`homestead-auth`) |
| `ConfigMap` | Homestead's settings and records: settings, portal links, IP records, hardware mappings, group tiers, linked clusters (`homestead-fleet`), the outage webhook carry-over (`homestead-outage-carry`), the VM store (`homestead-vm-store`, in `lab`), a VM's GPU ROMs, a configuration restore |
| `Namespace` | **Settings → Namespaces**, and a move from another cluster into a namespace that is not there yet |
| `StorageClass` | a storage class you create, the class for ISO volumes, the object store's one-copy class, a restore class Homestead repairs, a snapshot's temporary read-only clone |
| `VolumeSnapshot`, `VolumeSnapshotContent` | importing a Longhorn backup as a snapshot, browsing the files of a snapshot |
| `RecurringJob`, `Snapshot`, `Backup`, `BackupTarget` | Longhorn data protection: schedules and groups, a snapshot or backup now, the backup target, a move between clusters |
| `BackingImage`, `BackupBackingImage` | moving a Harvester VM image between clusters |
| `VirtualMachine`, `DataVolume`, `VirtualMachineImage`, `UploadTokenRequest` | creating, importing and editing VMs, an Unraid VM brought across, a Harvester image from a URL, a k3s cluster made of VMs |
| `VirtualMachineInstanceMigration` | moving a running VM to another host, by hand or while a host goes down |
| `PCIDeviceClaim`, `USBDeviceClaim` | handing a host's PCI or USB device to VMs on Harvester |
| `NetworkAttachmentDefinition` | a VM network, or a container's own LAN address |
| `NetworkPolicy` | a workload firewall you save; the fence around a restore test |
| `ClusterNetwork`, `VlanConfig` | Harvester uplinks |
| `HelmChart` | installing a chart from **Helm**; Longhorn, KubeVirt, CDI, kube-vip, Multus or macvtap from Homestead; the system upgrade controller; CSI snapshot support (`homestead-snapshot-controller`) the first time a backup is imported as a snapshot |
| `KubeVirt`, `CDI` | installing or upgrading KubeVirt and CDI |
| `Plan`, `Upgrade` | a k3s or RKE2 upgrade (system upgrade controller Plans), a Harvester upgrade |
| `PriorityClass` | a priority tier an app or group is given, when the cluster has no class for it yet |
| `Eviction` | draining a host before a reboot, a shutdown or a cluster shutdown |
| `Lease` | a change on a host held while it runs, so two cannot overlap |
| `ServiceAccount`, `Role`, `RoleBinding`, `ClusterRole`, `ClusterRoleBinding` | the short-lived, narrowly scoped helper that moves Homestead's own data volume, removed when it finishes |

### Changed or deleted, not created

Homestead also changes and deletes existing objects when you ask:

- an app's `Deployment` or `StatefulSet` when you edit, scale, restart,
  update, stop or move it;
- an app's own `Secret` (`<app>-env`) when you save it: environment values
  that look secret move there - by name, a password or token in the value, or
  a value it already held under a variable you renamed;
- a `Node` when you cordon, drain, label or remove a host, and the Cluster
  API `Machine` behind it, where there is one;
- a `PersistentVolume`'s reclaim policy and claim while a volume moves to
  another class, and a stuck storage `VolumeAttachment`'s finalizers when a
  dead host is removed;
- Longhorn's own objects: a `Volume` (its copies, or whether its filesystem is
  frozen for snapshots), a `Replica`, a Longhorn `Node`'s disks,
  a `Setting` (offline rebuilding on **Volumes**, the
  filesystem freeze for snapshots on **Data protection**);
- any object you edit or delete on the **Resources** page.

And it deletes what you delete.

## What removing Homestead leaves behind

**Before you uninstall,** make sure no OS update, host reboot, cluster shutdown
or other Homestead job (a copy, an import, a restore, a rollback) is running,
and that no app is stopped by a power schedule. An app
Homestead stopped stays stopped once Homestead is gone. Its
`homestead.io/held-as`, `schedule-copies` or `autostart-replicas` annotation
says how many copies it had, so you can scale it back up yourself.

**`install.sh --uninstall`**, run on a server node, removes Homestead and the
leftovers listed below as safe. It lists every object by exact name and asks
first: type `yes`, or nothing happens. `--dry-run` prints the list and stops.
It goes through `helm uninstall` if Homestead was installed with Helm. It stops
Homestead first and waits for its pods to go, so nothing puts anything back; if
stopping it fails, it stops there. Run again, it finds nothing left and says so.

It warns, and goes on if you say so, about what is stopped: apps with a mark
saying Homestead stopped them, suspended CronJobs among them, and anything with
`homestead.io/held-for-power`, which it names as "a job holds this" whatever
state it is in.

It refuses, removing nothing, when it cannot tell what is Homestead's:

- **Homestead running in more than one namespace.** They share a `ClusterRole`,
  so even naming one with `HS_NAMESPACE` would break the other.
- **`HS_NAMESPACE` naming a namespace** while Homestead, or its data volume, is
  in another.
- **Neither a Homestead `Deployment` nor a `homestead-data` claim**, when objects
  with these names are there. A cluster with none of them simply has nothing to
  remove.
- **A `Deployment` named `homestead` that is not Homestead's** (no `app=homestead`
  label), when there is anything else to remove, marks on the hosts included. It
  may be what mounts a `homestead-data` claim, so that claim proves nothing either.

Nothing overrides these: remove any leftovers by hand, with the steps below, and
skip anything named `homestead` that is not Homestead's.

It looks in Homestead's namespace, in your apps' namespace and in `lab`. Once the
`Deployment` has gone, it can no longer read where your apps' namespace is, and
says so: set `HS_APPS_NAMESPACE` if yours is elsewhere.

- **By default (`--keep-data`)** the data volume, the accounts and Homestead's
  records stay, so a reinstall picks them up. **That reinstall puts the node
  probe back**, unless you removed it: that is recorded with the data and stays.
  Removing it on the page is recorded at once; deleting it with kubectl is
  recorded when Homestead next restarts. To keep it away for certain, reinstall
  with `HS_NODEPROBE=no` on k3s or RKE2, which sets `node-probe: no` in
  `homestead-install`; installing again keeps that choice, unless the run says
  `HS_NODEPROBE=yes`. With the plain manifest
  or on Harvester, set `HOMESTEAD_NODEPROBE_AUTO_INSTALL=false`. Homestead on a
  release that doesn't record its install yet leaves the probe away after a
  reinstall: install it from **Settings › Cluster › Add-ons**. A Helm reinstall brings the probe back
  in any case, unless `nodeprobe.enabled` is false, though Homestead's record may
  still say removed.
- **`--everything`** removes them too, for good, once you type `everything`.
  It deletes Homestead's own namespace only when nothing else is left in it:
  with the plain manifest that is `lab`, which holds your apps, so it stays and
  the script says what is in it. It never deletes `default`, `kube-*` or
  `longhorn-system`.
- **Data on another claim.** After Homestead's data has moved, or with the chart's
  `existingClaim`, the script reads the claim from the `Deployment`, names it as
  staying on every run, and does not delete it, nor any claim named
  `homestead-data`. A later run cannot recognise this, as the `Deployment` that said
  so has gone: it may offer an old `homestead-data` claim as Homestead's data volume,
  listed and asked about as usual, or refuse if there is none. To remove the accounts
  and records with no such surprise, choose `--everything` on the first run.
- **A failed `--everything`** keeps `homestead-data`: it goes last, and only when
  everything else went, because a later run needs it to recognise the install.
- **If Flux, Argo CD or a k3s `HelmChart` manages Homestead,** stop that first: it
  would put Homestead back after the script has checked it is gone.
- **It leaves the `homestead.io/*` annotations on your apps.** They are harmless,
  and they are the only record of how many copies a stopped app had.
- It never touches anything under "do not delete" below. A `Service` that selects
  something else stays, and so does a `ClusterRoleBinding` (with its `ClusterRole`)
  for any subject but `ServiceAccount` `homestead` in Homestead's namespace.
- **The ticket `homestead-revert` stays while Homestead's `Deployment` was there
  when the run began**, as a rollback may be running. Once Homestead has gone,
  wait at least 5 minutes after any rollback, then run it again to take the ticket.
  The app on that volume may be stopped, and nothing else says so: a rollback
  leaves no annotation.
- `/etc/apt/apt.conf.d/99-homestead-hold` goes on the host you run it on, and
  only if Homestead wrote it. The script says to remove it on each other host.

`helm uninstall` alone removes Homestead's Deployment, Service, permissions and
the node probe the chart installed. It keeps your apps, the `lab` namespace,
Homestead's data volume and the `homestead-auth` Secret
([Removing Homestead](Installing-Homestead#removing-homestead)). By hand, a
plain manifest install is removed object by object, and a node probe Homestead
installed itself stays until you delete it.

> **Do not delete by any Homestead label** (`homestead.io/managed=true`,
> another `homestead.io/*` label, or `app.kubernetes.io/managed-by=homestead`).
> Apps you deployed through Homestead carry them, and so do their Services,
> their Secrets and restored volumes. Only the objects named under "Safe to
> delete" below can go.

Much of what Homestead made after the install is still working for your apps
and data, and stays. Below, `lab` means your apps' namespace: `lab` unless you
changed `workloadNamespace` in the chart.

### Safe to delete once Homestead is gone

Only Homestead uses these. `install.sh --uninstall` removes them, and
`tests/test_what_homestead_changes.py` fails if its list and this one part ways.

| Object | Where | Why it is safe |
|---|---|---|
| `Deployment`, `Service`, `ServiceAccount` `homestead`; `ClusterRole`, `ClusterRoleBinding` `homestead` | Homestead's namespace (`lab` with the plain manifest); the cluster | Homestead itself and what it may do. These go first; `helm uninstall` takes them with a chart install |
| The node probe: `DaemonSet` and `ConfigMap` `homestead-nodeprobe`, `Secret`s `homestead-smart-key` and `homestead-allocation-auth` | `lab` | the probe only reports to Homestead, and only the probe uses the two Secrets. Delete all four together |
| Longhorn `RecurringJob` `homestead-weekly-trim` | `longhorn-system` | only there from an earlier release, when you changed it (Homestead removes an unchanged one). Nothing refers to it; keep it if you want the weekly trim without Homestead |
| `NetworkPolicy` `homestead-backend-access` | `longhorn-system` | it only lets Homestead's pods reach Longhorn's API; Longhorn's own policies are separate and stay |
| The `homestead.io/standby` taint on hot spare hosts | the hosts | with Homestead gone nothing moves work onto a spare, so the taint only keeps the host empty |
| The Longhorn attachment tickets `homestead-rebuild` and `homestead-revert` | `longhorn-system` | Homestead holds a volume attached with these while it rebuilds copies or reverts a volume, and lets go itself. One left behind keeps the volume attached where it is, so its app cannot start on another host. Remove it (below) |
| `/etc/apt/apt.conf.d/99-homestead-hold` on each host | the host | only there if Homestead managed OS updates. Removing it gives Ubuntu its own automatic updates back, with the host's own settings: if those reboot automatically, hosts reboot without being drained first |
| `Role` and `RoleBinding` `homestead-console` | `lab` | they give only Homestead's own account the container consoles |
| `ConfigMap` `homestead-shell-sessions` | `lab` | which node shells are open, for Homestead alone. It holds nothing a reinstall needs |
| `Secret`, `Role` and `RoleBinding` `homestead-setup` | Homestead's namespace (`lab` with the plain manifest) | the one-time setup code and the permission to use it; only Homestead reads them. The Secret is only still there if no account was ever made |

#### Safe, but kept unless you ask (`--everything`)

Nothing but Homestead reads these: no manifest, chart, script or other part of
the install names them, so nothing breaks without them. They stay by default
because a reinstall picks them up, and because some cannot be made again.

| Object | Where | What goes with it |
|---|---|---|
| `PersistentVolumeClaim` `homestead-data` | Homestead's namespace | settings, job history, stats and the audit log. Depending on the volume's reclaim policy, the data is gone for good |
| `Secret` `homestead-auth` | `lab` | Homestead's user accounts. Keep it if you may reinstall and want the same accounts |
| `ConfigMap` `homestead-install` | `lab` | the installer's request to Homestead |
| `ConfigMap`s `homestead-settings`, `homestead-outage-carry`, `homestead-portal`, `homestead-ipam`, `homestead-group-tiers`, `homestead-mqtt`, `homestead-hardware`, `homestead-sources`, `homestead-vm-store`, `homestead-vips`, `homestead-fleet`, `homestead-fleet-replay`, `homestead-clusters`, `homestead-iso-library` | `lab` | Homestead's records: settings, portal links, IP records, group tiers, hardware mappings, import sources, the VM store's list of kept images, address records, linked clusters, clusters known for moves, the ISO library's list. Your apps and VMs keep running without them |
| `Secret`s `homestead-unifi`, `homestead-mqtt`, `homestead-fleet-key` | `lab` | the UniFi and MQTT passwords, and the key linked clusters know this one by |
| `Lease` `homestead-leader` | Homestead's namespace | which copy of Homestead led; nothing else reads it |
| Homestead's own address, `Service` `homestead-vip`, if you gave it one | Homestead's namespace | nothing once Homestead is gone, but it keeps the address taken while it is there. It goes only if it selects Homestead |

By hand, without the script. Skip anything named `homestead` that is not Homestead's:

```bash
# Homestead's own namespace: lab with the plain manifest, the release's with Helm.
# Homestead itself first; with Helm, helm uninstall takes these
kubectl -n lab delete deployment/homestead service/homestead serviceaccount/homestead
kubectl delete clusterrolebinding/homestead clusterrole/homestead
kubectl -n lab delete secret/homestead-setup role/homestead-setup rolebinding/homestead-setup
kubectl -n lab delete daemonset/homestead-nodeprobe configmap/homestead-nodeprobe \
  secret/homestead-smart-key secret/homestead-allocation-auth configmap/homestead-shell-sessions \
  role/homestead-console rolebinding/homestead-console
kubectl -n longhorn-system delete recurringjobs.longhorn.io/homestead-weekly-trim \
  networkpolicies.networking.k8s.io/homestead-backend-access
kubectl taint nodes --all homestead.io/standby-
```

And only if you want the accounts, records and data gone too, for good:

```bash
kubectl -n lab delete secret/homestead-auth configmap/homestead-install \
  configmap/homestead-settings configmap/homestead-outage-carry configmap/homestead-portal \
  configmap/homestead-ipam configmap/homestead-group-tiers configmap/homestead-mqtt \
  configmap/homestead-hardware configmap/homestead-sources configmap/homestead-vm-store \
  configmap/homestead-vips configmap/homestead-fleet configmap/homestead-fleet-replay \
  configmap/homestead-clusters configmap/homestead-iso-library \
  secret/homestead-unifi secret/homestead-mqtt secret/homestead-fleet-key
kubectl -n lab delete service/homestead-vip lease/homestead-leader
kubectl -n lab delete persistentvolumeclaim/homestead-data
```

Some of these may not exist on your cluster. kubectl then says so, and
deletes the rest. To find a leftover attachment ticket, list the volumes
holding one; for each volume the first command prints, remove the ticket by
its name:

```bash
kubectl -n longhorn-system get volumeattachments.longhorn.io \
  -o custom-columns=VOLUME:.metadata.name,TICKETS:.spec.attachmentTickets | grep -E 'homestead-(rebuild|revert)'
kubectl -n longhorn-system patch volumeattachments.longhorn.io <volume> --type=merge \
  -p '{"spec":{"attachmentTickets":{"homestead-rebuild":null,"homestead-revert":null}}}'
```

### Still used by your apps or data: do not delete

Deleting a `HelmChart` uninstalls that chart, and everything it runs goes
with it. Anything else named `homestead-*` that is not in the safe list above
stays too.

| Object | Where | What breaks without it |
|---|---|---|
| `HelmChart` `longhorn` | `kube-system` | Longhorn, and with it every volume |
| `HelmChart` `kube-vip` | `kube-system` | the addresses (VIPs) of your apps. With Homestead gone, kube-vip's interface no longer follows new hosts, so a host with a differently named network card may not announce VIPs |
| `HelmChart` `multus` and `homestead-macvtap` | `kube-system` | the LAN addresses of VMs and of containers with their own address |
| `HelmChart` `homestead-kubevirt` and `homestead-cdi`, the `KubeVirt` and `CDI` resources | `kube-system`, `kubevirt`, the cluster | your VMs |
| `HelmChart` `homestead-snapshot-controller` | `kube-system` | volume snapshots and restores from CSI snapshots |
| `HelmChart` `homestead-system-upgrade` and its `Plan`s | `kube-system`, `system-upgrade` | k3s and RKE2 upgrades in progress |
| `Secret`s `homestead-backup-target` and `homestead-backup-credentials` | `longhorn-system` | Longhorn's backup target still reads its keys from them: backups stop |
| The object store: `Deployment`, `Service` and claim `homestead-objectstore`, `Secret` `homestead-objectstore-keys` | `lab` | the backups kept in it |
| The share servers and their records: `homestead-smb`, `homestead-nfs`, `homestead-shares`, `homestead-share-credentials` | `lab` | your network shares |
| `StorageClass`es Homestead made (`homestead-single-copy`, the ISO class `homestead-isos`, restore classes) | the cluster | volumes made from them |
| `PriorityClass`es `homestead-critical`, `homestead-normal`, `homestead-low`, or `homestead-<tier>-<value>` | the cluster | Kubernetes refuses a new pod that names a class that is gone, so an app, VM or Helm release still naming one cannot restart, update or move. Delete one only once nothing names it (below) |
| What Homestead made for an app: its VIP `Service`, firewall `NetworkPolicy`s, `NetworkAttachmentDefinition`s, its environment `Secret`, its outage webhook `Secret` (`homestead-outage-*`) | the app's namespace | the app; without the webhook's `Secret`, its outage webhook |
| `ConfigMap` `homestead-data-handoff` | Homestead's namespace | the journal of a move of Homestead's own data: where it got to, kept off both volumes. A move part-way through has no other record of which volume holds the data |
| `Secret` `homestead-cluster-shutdown` | Homestead's namespace | the journal of a cluster shutdown: what to bring back, and how. The helpers that recover the cluster belong to it, and go with it |
| `Secret`s `homestead-move-credentials-<move>` | `longhorn-system` | a Longhorn backup target set up for a move between clusters may still read its keys from one. Each name is made for its move, so the script could not name it anyway |
| `Lease`s `homestead-host-change-<host>` | `lab` | nothing: each only marks a change running on a host. Each name holds its host, so the script leaves them, and they keep `lab` from being deleted |
| The `homestead.io/*` annotations on apps, and the other taints and labels on hosts | apps, hosts | nothing: they are harmless. The annotations are the only record of how many copies a stopped app had |
| Settings Homestead changed: Longhorn's copies count and offline rebuilding, kube-vip's values, Longhorn disks the root-disk guard turned off, the default flag it took off extra storage classes | Longhorn, kube-vip, storage classes | they are your cluster's settings now; change them only on purpose |
| On each host: the journal cap (`/etc/systemd/journald.conf.d/90-homestead.conf`), the inotify limits (`/etc/sysctl.d/90-homestead.conf`), the multipath setting | the host | Longhorn needs multipathd kept off its disks, or volumes fail to mount; apps may need the inotify limits |
| On each host: Longhorn's own volume (`/mnt/longhorn-os` on new installs, `/var/lib/longhorn` on older ones, label `hs-longhorn`) and its `/etc/fstab` line, and the fstab lines of disks Homestead set up for Longhorn | the host | Longhorn's data: without the mount, Longhorn starts on an empty folder |
| On each host: `/etc/modules-load.d/99-homestead-longhorn-v2.conf` and `/etc/sysctl.d/99-homestead-longhorn-v2.conf` | the host | Longhorn V2 volumes after the next reboot |
| On each host: the cloud-init files `/etc/cloud/cloud.cfg.d/99-homestead-bridge.cfg` and `99-homestead-bond.cfg`, and the netplan configuration Homestead wrote for a bridge or bond | the host | the host's network: without them cloud-init writes its own netplan again at boot, and the host or its VMs can lose the LAN |
| On each host: `/usr/local/sbin/homestead-vfio`, `homestead-vfio.service`, `/etc/homestead/vfio.list`, and the IOMMU options in `/etc/default/grub` | the host | PCI and GPU passthrough to VMs after the next boot |
| On each host: the `.skip` files in k3s's or RKE2's auto-deploy folder | the host | without them k3s or RKE2 applies the old installer manifests again at boot, taking Homestead, Longhorn and KubeVirt back to the versions first installed |
| On each host: `/var/lib/homestead/partitions-*.sfdisk` | the host | nothing runs on them, but each is the only copy of a disk's partition table from before Homestead changed it |
| On each host: the host console, `/var/lib/homestead/install`, `/var/log/homestead-os-upgrade.log`, `/var/lib/homestead/os-upgrade.status` | the host | nothing known; harmless to keep. To remove the host console, run `install.sh --no-console` on the host |

**Never restore a `*.homestead-backup` file** (`/etc/fstab`, `/etc/default/grub`,
`/etc/multipath.conf`). Each is the host as it was before Homestead set it up:
putting one back drops Longhorn's mount, passthrough or the multipath setting.
Deleting the backup itself is harmless.

To check whether anything still names one of Homestead's priority classes:

```bash
kubectl get deployments,statefulsets,daemonsets -A \
  -o custom-columns=NAMESPACE:.metadata.namespace,NAME:.metadata.name,CLASS:.spec.template.spec.priorityClassName | grep homestead-
kubectl get virtualmachines -A \
  -o custom-columns=NAMESPACE:.metadata.namespace,NAME:.metadata.name,CLASS:.spec.template.spec.priorityClassName | grep homestead-
```

An app Flux or Argo CD applies, or a Helm release, may name the class in Git
or in its values; check those too.

What you made through Homestead (apps, volumes, VMs and the rest) is yours,
and stays.
