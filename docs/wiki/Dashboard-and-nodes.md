# Dashboard and nodes

## Dashboard

The Dashboard is the cluster at a glance: CPU, memory, network and disk for the
whole cluster, what is unhealthy right now, and the containers and VMs using the
most.

![Dashboard](https://github.com/wjcloudy/homestead/releases/latest/download/homestead-dashboard.png)

- **Health** changes only on a real change - a node going not-Ready, a volume
  degrading, a workload failing to start - and a problem shows once it has
  lasted a minute, so a rollout or restart does not flash red.
- **Over time** covers ninety days: cluster CPU and memory (average and peak),
  network, pods, and how much of the time each node was Ready. Homestead
  records it every five minutes whether or not a browser is open.
- A node whose Longhorn disks are nearly fully allocated is named here - see
  [Storage](Storage#longhorn-allocation).

Warning levels (temperatures, disk errors, CPU and memory) are set in
**Settings → Health**.

## Nodes

**Nodes** has a card per host: its role, CPU, memory, pods, temperature and
every disk on it - the system disk, the disks Longhorn stores data on (with
how full each is), and any disk nothing uses yet.

![Nodes](https://github.com/wjcloudy/homestead/releases/latest/download/homestead-nodes.png)

Clicking a node opens it: its hardware, the pods and VMs on it, per-disk read
and write speed, and each drive's health.

![Node detail](https://github.com/wjcloudy/homestead/releases/latest/download/homestead-node-detail.png)

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
- reboot or shut it down, if `ENABLE_NODE_POWER` is set on Homestead.

## The node probe

Kubernetes knows nothing of temperatures, USB devices, which physical disk is
which, or drive health. The node probe - a small DaemonSet, one pod per host -
reads them. The Helm chart installs it; otherwise **Settings → About → Install
node probe**, or **Install node probe** on a node with no temperature.

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
reported**, judged from its SMART counters against **Settings → Health →
Drive health policy** - not from the drive's own PASSED/FAILED flag, which
says PASSED until failure is close. Where a drive reports how much life it
has left (NVMe endurance, an SSD's life-left attribute) it is shown as a
percentage. USB bridges and virtual disks that hide SMART are shown as
unsupported, not failed.

## Hardware

**Settings → Hardware** names devices on your hosts - a Coral TPU, a Zigbee
stick, an Intel iGPU - as hardware features containers can ask for. Hosts are
checked every 30 seconds, so a device plugged in later is found without a
restart; **Rescan hosts** checks at once. A container given a feature is kept
on a host that has it. See [Containers](Containers#hardware).

## Cluster

**System → Cluster** is the platform's health rather than your apps': versions,
control plane and etcd (and how many servers can fail before it stops), node
pressure, core services, and platform warnings from the last day.

![Cluster](https://github.com/wjcloudy/homestead/releases/latest/download/homestead-cluster.png)

- **Add a host** walks through adding a machine, for the cluster you have:
  Harvester's installer screens, or k3s/RKE2's join command, and shows the new
  host arriving.
- **Remove from cluster** takes out a host that is gone - including one that
  died for good - after checking that etcd keeps quorum and that no volume
  loses its last copy.
- On Harvester, the newest Harvester releases are listed, and a running
  upgrade is followed stage by stage. Upgrades themselves are started from
  Harvester's own dashboard.
