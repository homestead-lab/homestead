# Dashboard and nodes

## Dashboard

The Dashboard is the cluster at a glance: CPU, memory, network and disk for the
whole cluster, what is unhealthy right now, and the containers and VMs using the
most.

![Dashboard](https://github.com/wjcloudy/homestead/releases/latest/download/homestead-dashboard.jpg)

- **Health** changes only on a real change - a node going not-Ready, a volume
  degrading, a workload failing to start - and a problem shows once it has
  lasted a minute, so a rollout or restart does not flash red.
- **Over time** covers ninety days: cluster CPU and memory (average and peak),
  network, pods, and how much of the time each node was Ready. Homestead
  records it every five minutes whether or not a browser is open.
- A node whose Longhorn disks are nearly fully allocated is named here - see
  [Storage](Storage#longhorn-allocation).

Warning levels (temperatures, disk errors, CPU and memory) are set in
**Settings → Monitoring**.

## Nodes

**Nodes** has a card per host: its role, CPU, memory, pods, temperature and
every disk on it - the system disk, the disks Longhorn stores data on (with
how full each is), and any disk nothing uses yet. **Addresses** lists the
node's own IP and each VIP it currently answers for; those VIPs move to
another node if it goes down. [Networking](Networking#nodes--addresses) shows
the same addresses with the ports and apps on each.

![Nodes](https://github.com/wjcloudy/homestead/releases/latest/download/homestead-nodes.jpg)

Clicking a node opens it: its hardware, the pods and VMs on it, per-disk read
and write speed, and each drive's health.

Each disk on the card goes by its name, or by its device name if it has none.
Its tags say what it is: **system**, **Longhorn**, both, or what else uses it.

- A Longhorn folder on the system drive, such as Longhorn's default
  `/var/lib/longhorn`, is counted on that drive's line. That includes a system
  installed on LVM, as Ubuntu Server does, where it used to show as a separate
  "Longhorn" disk.
- Hovering the name shows the device, the model and the Longhorn folders on it.
- To name a drive, open the node and choose **Name** (or **Rename**) beside it
  in the drive list. The name is kept by the drive's serial number, so it
  follows the drive to another port. Clearing it goes back to the device name.

### Host OS

On k3s and RKE2 the hosts are ordinary Linux machines, so Homestead looks after
their OS as Harvester does its own. The leader reads each host every six
hours, through the same short-lived helper disk set-up uses, and the node's
**Host OS** card shows:

- the distribution, kernel, and how full the root filesystem is;
- updates waiting, and which are security fixes, from the package lists the
  host keeps (Ubuntu refreshes them daily; **Check now** refreshes them first);
- whether it needs a restart to finish an update, and any failed services;
- whether its clock is synchronised.

**Install updates** runs the host's own package manager (apt, dnf or zypper)
detached on the host, followed in the job tray; workloads keep running. A kernel
update then asks for a restart, which **Host actions** does with its drain
review. Security updates, a restart needed, failed services and a root
filesystem over 90% are also [notifications](Settings#notifications), and the node's card shows a
**host OS** chip.

#### Every host at once

**Nodes → OS updates**, also available through **Settings → Updates → Host updates → Manage host updates**, updates every host, one at a time - now, or in a weekly
window (days and an hour, in your browser's time zone). Each Ready host in
turn, the one Homestead's leader runs on last, refreshes its package lists and
installs what is waiting. When an update needs a restart, the host gets the
same review as **Host actions**; if nothing stops it, it is cordoned, drained
through disruption budgets, restarted, and uncordoned once it is Ready and its
Longhorn volumes are healthy. Only then is the next host touched.

- A restart the review refuses - running VMs, the cluster's only etcd member,
  or a volume whose only healthy copy is on that host (unless the settings
  accept that) - is skipped: the host keeps its updates and is listed as
  needing a restart, and the run goes on.
- A failed install stops the run before the next host.
- **Stop after this host** finishes the host in hand first.
- The run is kept on disk, so it carries on if Homestead itself is moved off a
  host it drains; a restart cut short that way is tried once more.
- **Restarts: Never** installs updates without restarting anything.

**Ubuntu's automatic updates.** Each host's Host OS card says whether
`unattended-upgrades` is on, and whether it restarts the host by itself
(`Automatic-Reboot`) - which it does without a drain, and which is an alert.
Homestead holds it off on every host while an update of every host runs, with
`/etc/apt/apt.conf.d/99-homestead-hold`, and removes that file after. Choose
**Who installs updates: Homestead** to keep it off for good, and let the
weekly window install updates instead.

The node's **Disks** card draws each disk's partition table to scale - each
partition's size, filesystem and mount, what sits on it (LVM, RAID) and space
left unallocated.

![Node detail](https://github.com/wjcloudy/homestead/releases/latest/download/homestead-node-detail.jpg)

### Uptime

Each node card says how long the host has been up since it last booted, and
how much of the last 30 days it was Ready. A node's page has more:

- the share of the last **24 hours, 7, 30 and 90 days** it was up;
- a strip of the last 90 days, one bar a day: green is fully up, amber lost
  a little, red lost more than 1%;
- each **outage** (when it went down and for how long) and each **reboot**.

Homestead checks every node every five minutes and keeps those checks for two
days, then keeps hourly summaries for 90 days. So an outage in the last two
days is timed to five minutes; an older one is shown as "about" a length.
Reboots are noticed when a node's boot ID changes, so a quick reboot counts
even if the node was never seen as down. The host's own uptime comes from the
[node probe](#the-node-probe); without the probe, the card shows how long the
node has been Ready.

From a node you can:

- **Cordon** it (no new pods) and **drain** it (move what runs there elsewhere),
  before maintenance;
- open its **Disks** and give a new disk to Longhorn - see [Storage](Storage#disks);
- run a **SMART self-test** on a drive (short or extended), followed in the job
  tray;
- reboot or shut it down, with an administrator's reviewed confirmation.

### Terminal

**Terminal** on a node's page opens a root shell on that host, as SSH would
give it: its own files, tools and login shell. It needs the **admin** role.
Full-screen tools work (`top`, `vi`, `less`), and copy and paste work as in any
terminal. Homestead starts a small helper pod on the node (busybox, with the
host's process and network namespaces) and enters the host from it; it
serves every open session on that node and is deleted when the last one
closes, or after eight hours at most. Each session's start and end are written
to the console audit log with the node's name; what is typed is not recorded.
A node that is not Ready cannot run the helper, so it has no terminal.

### Reviewed host maintenance

Host power control is enabled by default from v2.8.167. Set
`ENABLE_NODE_POWER=false` on Homestead to disable it; an existing explicit
opt-out is preserved on image updates. Enabling the feature does not reboot
anything or bypass its admin-only access, confirmations or safety blockers.

Reboot and shutdown first show quorum, affected workloads, VMs, Longhorn copies,
disruption budgets and local/external storage used by pods being drained.
Unmanaged pods, unavailable inventories and blocking or stale disruption budgets
stop the action. The eviction API is authoritative; a positive budget is not a
promise that every eviction will succeed. Homestead never force-deletes pods to
bypass it. The same helper policy applies to host reboot/shutdown and cluster
shutdown. Verified image-pull progress watchers and image-cache scans are evicted
without a replacement controller: only their reports are lost. Their read-only
runtime mounts do not count as application data. They are not started on cordoned
hosts. Progress checks also clean up abandoned pull watchers; after a Homestead
restart, the idle timer starts on the first check instead of resetting forever.

Active host commands, source probes, image removal, imports/copies, ownership
changes, volume moves, host preparation and earlier power/shutdown helpers block
with guidance to finish or recover their operation first, including helpers with
Job controllers. Close host shells, file browsers and snapshot browsers before
maintenance: they can have commands, file changes or clone mounts in use. Other
unmanaged pods still block. See Kubernetes' [disruption-budget reference](https://kubernetes.io/docs/reference/kubernetes-api/policy/pod-disruption-budget-v1/).

Drain deletes `emptyDir` data. Host-local PVCs and paths do not follow a pod to
another host. External NFS/CSI availability is not proven by the replica check.
These risks require explicit acknowledgement. Longhorn surviving copies are
counted on distinct Ready hosts, not duplicate replicas on one host. Remaining
host capacity, static pods/DaemonSets and all external dependencies are not fully
simulated; this is not a complete failover guarantee.

The job is recorded **before cordon**, with phases for cordoning, draining,
post-drain verification, helper submission and observation. System workload pods
are drained too; static pods and DaemonSets remain. Pod UID preconditions prevent
evicting a replacement under a reused name. Before power, Homestead rechecks
quorum, VMs, replicas and remaining pods. A changed risk or incomplete drain
leaves the host cordoned, with no power command sent.

**Single-host clusters.** Normal **Reboot** and **Shut down** use a planned
whole-cluster outage. Type the host name and acknowledge that all applications,
storage and Homestead will go offline. To start a shut-down host again you need
console or physical access. Homestead leaves scheduling unchanged and asks the
host's systemd for power control without cordoning or evicting pods. Running VMs,
active data operations and incomplete inventory still block the request. A sole
etcd member with additional worker nodes still needs quorum protection. The full
node inventory and reviewed impacts are checked again before power is sent;
unattended OS updates cannot approve this outage.

**Override.** Other stops can be overridden by an administrator: quorum on a
multi-host cluster, running VMs, disruption budgets and inventories that cannot
be read. Tick **Override** in the power review for a forced review that lists
what the override means. A forced reboot or shutdown sends no cordon or drain:
the host's own systemd stops everything in order, as its power button would,
and pods start again when it is back, with nothing left cordoned. It is still
refused when power control is off, the host is not Ready, an earlier helper is
active, or the host's identity or boot ID changed since the review.

Interrupted pre-power work times out without automatically resuming. The helper
identity is saved before submission; an uncertain submission is observed, never
automatically resent. An existing active helper blocks another request. A new
boot ID verifies a reboot even if polling missed NotReady. A reboot that stays
down times out after ten minutes; volume recovery has a separate thirty-minute
wait. NotReady alone cannot prove physical shutdown. No automatic uncordon is
performed after a multi-host drain: inspect the node, workloads and storage before
using **Uncordon**. A planned single-host outage leaves scheduling unchanged.

If this host runs Homestead itself, draining can interrupt the request. Inspect
the persisted job after Homestead returns before retrying. Disposable-host
rehearsal remains necessary before relying on unattended maintenance.

The updated ClusterRole includes read-only `policy/poddisruptionbudgets` access.
Homestead normally reconciles this automatically on startup. If **Settings →
About this installation → Permissions** reports that it cannot update its role,
apply the release's `deploy/rbac.yaml` or perform a Helm upgrade. Power review
blocks safely until the permission is available; see [updating](Installing-Homestead#updating-homestead).

## The node probe

Kubernetes knows nothing of temperatures, USB devices, which physical disk is
which, or drive health. The node probe - a small DaemonSet, one pod per host -
reads them. Homestead installs it automatically, including on existing clusters,
unless installation was explicitly declined. **Settings → Homestead** reports
its health. Administrators can retry from **Settings → Hardware and storage → Add-ons**.

It has two parts:

- **Telemetry**: read-only, non-root, no capabilities. Temperatures, host
  devices, disks and mounts, per-disk throughput.
- **SMART**: a separate container that runs `smartctl`, which needs the raw
  drives and so runs privileged. It accepts only requests signed by Homestead.
  Delete that container from the DaemonSet if you do not want drive health;
  everything else keeps working.

Homestead keeps the probe's scripts up to date itself when it updates.

### Drive health

Each drive is **healthy**, **needs attention**, **critical** or **not
reported**, judged from its SMART counters against **Settings → Monitoring →
Drive health policy** - not from the drive's own PASSED/FAILED flag, which
says PASSED until failure is close. Where a drive reports how much life it
has left (NVMe endurance, an SSD's life-left attribute) it is shown as a
percentage. USB bridges and virtual disks that hide SMART are shown as
unsupported, not failed.

## Hardware

**Settings → Hardware and storage** names devices on your hosts - a Coral TPU, a Zigbee
stick, an Intel iGPU - as hardware features containers can ask for. Hosts are
checked every 30 seconds, so a device plugged in later is found without a
restart; **Rescan hosts** checks at once. A container given a feature is kept
on a host that has it. See [Containers](Containers#hardware).

## Cluster

**System → Cluster** is the platform's health rather than your apps': versions,
control plane and etcd (and how many servers can fail before it stops), node
pressure, core services, and platform warnings from the last day.

![Cluster](https://github.com/wjcloudy/homestead/releases/latest/download/homestead-cluster.jpg)

- **Add a host** walks through adding a machine, for the cluster you have:
  Harvester's installer screens, or k3s/RKE2's join command, and shows the new
  host arriving.
- **Remove a host** (beside **Add a host**) takes out a host that has failed
  or is being retired - on Harvester, k3s, RKE2 or plain Kubernetes. It lists
  the hosts, failed ones first, and checks before anything changes: etcd
  quorum, volumes whose only copy it held (named by claim and the apps using
  them), volumes kept on the host itself (k3s's `local-path`), and apps pinned
  to it. Anything lost must be ticked as accepted. **It is gone for good**
  also force-stops what it held, lets pinned apps run elsewhere, and makes
  its local volumes again empty, so their apps can start on another host.
  It also deletes the k3s/RKE2 node password, so a rebuilt host of the same
  name can join. Afterwards it lists anything still left over, each with a
  button. On plain Kubernetes, remove its etcd member yourself with
  `etcdctl member remove`, as the check says.
- On Harvester, the newest Harvester releases are listed. A version Harvester
  offers can be upgraded to from here (type the version to confirm), as its
  own dashboard's Upgrade button does. Either way, the upgrade is followed
  stage by stage and node by node.

### Platform versions

**Platform versions**, on the Cluster page and under **Settings > Updates**,
lists what runs under your apps: the cluster itself
(k3s or RKE2), Longhorn, KubeVirt, CDI, and the network components kube-vip
and Multus. For each it shows the version
running and the newest release, with a link to the release notes. Homestead
checks for releases twice a day; **Check** asks now.

Where Homestead can upgrade something, it offers the next version. Each
project supports going **one minor version at a time**: first the newest
patch of the minor you are on, then the newest of the next minor. A further
step is offered once that one is done.

- **k3s and RKE2** are upgraded by Rancher's system-upgrade-controller.
  Homestead installs it the first time. The servers are cordoned and upgraded
  one at a time, then the agents. The job tray shows how many nodes are done.
  Cancelling stops any further node being upgraded.
- **Longhorn, KubeVirt and CDI** that Homestead installed (Settings > Hardware and storage >
  Add-ons) move on through their HelmChart. Installed another way, they are
  shown with their notes: upgrade them the way they were installed.
- **kube-vip and Multus** that Homestead installed are listed by chart
  version, with the version the chart runs (kube-vip chart 0.11.1 is kube-vip
  v1.2.3), and upgraded one minor chart version at a time. The chart's values -
  kube-vip's settings, Multus's CNI paths - are kept.
- **On Harvester**, Longhorn, KubeVirt, kube-vip and Multus come with Harvester
  and are upgraded with it, so they are shown but not upgraded apart from it.

### Devices for VMs

For a complete walkthrough from host preparation to the guest's first boot,
see [GPU passthrough](GPU-passthrough).

Under a host's **Hardware → Devices for VMs**, **Look at its devices** reads
PCI and USB hardware once. The last complete inspection is retained when you
leave the page or reload Homestead; its timestamp is shown. **Refresh devices**
reads the host again. This display is a snapshot, and PCI handoffs still check
the host before changing its drivers.

PCI devices are listed together under their **IOMMU group**, including GPU
audio functions and PCI bridges. Each group explains how many devices move
together. On k3s/RKE2, the other functions move to vfio-pci with the selected
device; PCI bridges stay with the host. A group containing the host's network
or a disk in use cannot be handed over. On Harvester, its controller manages
the individual device claims.

The host's boot GPU is marked **boot display**. Giving it to VMs removes the
host's local screen output. Hardware preparation and vBIOS capture require an
administrator; VM attachment is configured separately in the VM's
**Passthrough** tab.

For a GPU, **Capture vBIOS** downloads a ROM after checking that its host driver
is detached and no active VM uses its IOMMU group. An idle VFIO card is woken
temporarily if needed, then its original power policy is restored. The capture
does not upload the ROM into a VM: choose the downloaded file beside the GPU
in **VM → Edit → Passthrough**. See
[GPU ROMs](Virtual-machines#a-gpus-rom-vbios) and
[first boot on a physical GPU](Virtual-machines#first-boot-on-a-physical-gpu).
