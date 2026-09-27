<p align="center">
  <img src="web/assets/homestead-lockup.svg" width="340" alt="Homestead">
</p>

<h3 align="center">A fully redundant homelab - compute, storage and network - in one friendly dashboard.</h3>

<p align="center">
  Run your apps and virtual machines across ordinary machines, and keep them running when one fails.<br>
  Built on Kubernetes, without having to become a Kubernetes expert.
</p>

<p align="center">
  <a href="https://github.com/wjcloudy/homestead/releases/latest"><img src="https://img.shields.io/github/v/release/wjcloudy/homestead?label=release&color=f59e0b" alt="Latest release"></a>
  <a href="https://github.com/wjcloudy/homestead/actions/workflows/ci.yml"><img src="https://github.com/wjcloudy/homestead/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://github.com/wjcloudy/homestead/pkgs/container/homestead"><img src="https://img.shields.io/badge/ghcr.io-homestead-2453ff?logo=docker&logoColor=white" alt="Container image"></a>
  <img src="https://img.shields.io/badge/status-alpha-f59e0b" alt="Status: alpha">
  <img src="https://img.shields.io/badge/license-MIT-30ba78" alt="MIT licence">
</p>

<p align="center">
  <a href="#quick-install"><b>Quick install</b></a> &nbsp;·&nbsp;
  <a href="#features"><b>Features</b></a> &nbsp;·&nbsp;
  <a href="https://github.com/wjcloudy/homestead/wiki"><b>Wiki</b></a> &nbsp;·&nbsp;
  <a href="docs/reference.md"><b>Reference</b></a>
</p>

<p align="center">
  <img src="https://github.com/wjcloudy/homestead/releases/latest/download/homestead-hero.png" alt="Homestead on a phone: containers, the dashboard and virtual IPs">
</p>

## Why Homestead

A homelab on a single machine is one failed disk, power supply or update away
from going dark. Homestead spreads your homelab across two or more ordinary
machines - mini PCs, old desktops, virtual machines - so that every layer has
a spare:

<table>
<tr>
<td width="33%" valign="top">

**Compute**

Containers and VMs restart on a healthy node when one fails. Placement rules
decide where each app may run, and VMs live-migrate between hosts for
maintenance.

</td>
<td width="33%" valign="top">

**Storage**

Every volume is replicated across nodes by Longhorn, so a dead disk or host
loses nothing. Snapshots, scheduled backups to S3 or NFS, and restores in a
few clicks.

</td>
<td width="33%" valign="top">

**Network**

Each app can have its own virtual IP, announced by kube-vip, that stays
reachable when a node goes down. IP address management keeps track of every
address on your LAN.

</td>
</tr>
</table>

**It feels like a NAS, not a cluster.** Homestead speaks in apps, volumes,
shares and VMs - the words you know from Unraid or Docker - and looks after
the Kubernetes underneath. When you want the raw objects, every resource is a
click away as YAML. It runs on [k3s](https://k3s.io), RKE2,
[Harvester HCI](https://harvesterhci.io) or any Kubernetes you already run.

## Quick install

On a Linux machine - a bare one, a k3s or RKE2 server, or a Harvester node:

```bash
curl -sfL https://raw.githubusercontent.com/wjcloudy/homestead/main/scripts/install.sh | sudo sh
```

- **New cluster or join one.** The installer checks the machine, then creates
  a **k3s** or **RKE2** cluster - or joins this machine to an existing one -
  with Longhorn, optionally KubeVirt, and Homestead. The installation summary
  lets you choose the version of each component.
- **More machines.** Run the same line on each further machine to join it.
- **Node doctor.** On any node, the same line checks the node and the cluster
  and offers a fix for each problem it finds.

Then open `http://<your machine>:8088` and create the administrator account.
Already running a cluster? Install with
[Helm or the manifest](docs/reference.md#install-with-helm). The wiki walks
through [k3s](https://github.com/wjcloudy/homestead/wiki/Installing-on-k3s),
[RKE2](https://github.com/wjcloudy/homestead/wiki/Installing-on-RKE2) and
[Harvester](https://github.com/wjcloudy/homestead/wiki/Installing-on-Harvester)
step by step.

## Features

<table>
<tr>
<td width="50%" valign="top">

**App Store** - Unraid's Community Applications catalogue, deployed properly:
volumes, ports, hardware and updates handled for you.

**Containers** - deploy, edit, logs and console; groups, autostart and
placement; update checks with a monitored rollout and one-click rollback.

**Virtual machines** - KubeVirt VMs from a store of cloud images or your own
disks, with a console, power actions and live migration.

**Data protection** - snapshots, recurring backups, restores, and moving
containers and VMs between clusters.

**Networking** - a virtual IP per app, collision-free port exposure, and IP
address management with scanning and UniFi sync.

**Imports** - from an Unraid server, a Docker Compose file, or a VM disk image
(qcow2, VMDK).

</td>
<td width="50%" valign="top">

**Nodes and hardware** - node health, SMART drive health, disks, iGPU, Coral
and USB passthrough, and a terminal on every node.

**Cluster** - control-plane and etcd health, adding and removing hosts, and
platform upgrades on Harvester.

**Dashboard and alerts** - 90 days of history, push notifications, and MQTT
with Home Assistant discovery.

**Network shares and Portal** - Samba shares from any volume, and a tile for
every web interface on your network.

**Helm and Resources** - every Helm release and every Kubernetes object, with
its YAML and events.

**Safe by design** - viewer, operator and admin roles, guarded deletion, and
every job cancellable, with a rollback.

</td>
</tr>
</table>

## On your phone

Homestead installs as an app on iOS and Android (over HTTPS), with push
notifications for outages, degraded storage, failed jobs and image updates.

<p align="center">
  <img src="https://github.com/wjcloudy/homestead/releases/latest/download/homestead-mobile.png" alt="Homestead on a phone: dashboard, containers, VIPs, replicated volumes, and snapshots and backups">
</p>

## On the desktop

<p align="center">
  <img src="https://github.com/wjcloudy/homestead/releases/latest/download/homestead-dashboard.png" alt="The Homestead dashboard">
</p>

<table>
<tr>
<td width="50%"><img src="https://github.com/wjcloudy/homestead/releases/latest/download/homestead-containers.png" alt="Containers"><p align="center"><b>Containers</b></p></td>
<td width="50%"><img src="https://github.com/wjcloudy/homestead/releases/latest/download/homestead-architecture.png" alt="Architecture: VIP to workload to volume to replica"><p align="center"><b>Architecture</b></p></td>
</tr>
<tr>
<td width="50%"><img src="https://github.com/wjcloudy/homestead/releases/latest/download/homestead-volumes.png" alt="Replicated volumes"><p align="center"><b>Volumes</b></p></td>
<td width="50%"><img src="https://github.com/wjcloudy/homestead/releases/latest/download/homestead-vms.png" alt="Virtual machines"><p align="center"><b>Virtual machines</b></p></td>
</tr>
</table>

Every screenshot is taken from Homestead's demo data by each release, so they
always show the current version.

## Where it runs

Homestead checks what the cluster has, and each page works with that.

| | k3s | Harvester | RKE2 or other Kubernetes |
|---|---|---|---|
| **Install** | [one line](docs/reference.md#one-line-install-and-node-doctor) from bare Linux | [one line](docs/reference.md#one-line-install-and-node-doctor) on a node, or Helm or the manifest | RKE2: [one line](docs/reference.md#one-line-install-and-node-doctor) from bare Linux, or onto an RKE2 server; any other: [Helm or the manifest](docs/reference.md#installing-with-the-manifest) |
| **Containers, App Store, Compose, Portal, Networking, IP addresses, Resources, dashboard** | yes | yes | yes |
| **Volumes, data protection, disks** | yes, with Longhorn - the installer adds it, or Settings → Cluster → Add-ons | yes, Longhorn is built in | yes, with Longhorn - Settings → Cluster → Add-ons installs it on RKE2 |
| **Virtual machines** | yes, with [KubeVirt](https://kubevirt.io) - the installer adds it, or Add-ons | built in | yes, with KubeVirt - Add-ons installs it on RKE2 |
| **Addresses for apps** | the nodes' own addresses (k3s's ServiceLB), or a VIP per app with kube-vip | a VIP per app (kube-vip) | RKE2 from the installer: the nodes' own addresses (its ServiceLB), or a VIP per app with kube-vip; others: MetalLB or kube-vip |
| **Helm charts** | yes (k3s's Helm controller) | yes (RKE2's Helm controller) | yes on RKE2; listing only without a Helm controller |
| **Adding a host** | the join command for a worker or a server | a guide to Harvester's installer | RKE2's join commands |
| **Platform upgrades** | - | followed on the Cluster page | - |

## Documentation

- **[Wiki](https://github.com/wjcloudy/homestead/wiki)** - the guided tour:
  building a cluster, installing Homestead, then each page in turn.
- **[Reference](docs/reference.md)** - every feature in depth, security notes,
  and how releases are made.
- **[Troubleshooting](https://github.com/wjcloudy/homestead/wiki/Troubleshooting)** -
  the problems people actually hit, and the node doctor.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for how the project is put together,
running it locally against demo data, the tests, and how releases are made.

## Licence

Homestead is released under the [MIT License](LICENSE). The Monaco editor it
bundles, the images it starts and the catalogue it reads are listed with their
own terms in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md), along with the
trademarks it mentions.

Unraid® is a registered trademark of Lime Technology, Inc. Homestead is not
affiliated with, endorsed, or sponsored by Lime Technology, Inc.
