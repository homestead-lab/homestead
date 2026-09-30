# Homestead

Homestead is a homelab control panel for Kubernetes: containers, virtual
machines, storage, network shares, backups and addresses in one place, in
words that make sense if you came from Unraid or Docker. It runs on
**[k3s](https://k3s.io)** - one command turns a spare Linux machine into a
cluster with Homestead on it - on [Harvester](https://harvesterhci.io), where
it grew up, and on RKE2 or any cluster you look after with Headlamp today.

[![Homestead dashboard](https://github.com/wjcloudy/homestead/releases/latest/download/homestead-dashboard.jpg)](https://wjcloudy.github.io/homestead/)

**[Try the live demo](https://wjcloudy.github.io/homestead/)**: the real interface on made-up data, in your
browser, with nothing to install. Every page works, and nothing you do there
is saved.

*Every picture in this wiki comes from the latest release, taken from
Homestead's demo data, so they stay current on their own.*

## Start here

**Starting from nothing?** One line on a Linux machine asks what to build and
does it:

```bash
curl -sfL https://raw.githubusercontent.com/wjcloudy/homestead/main/scripts/install.sh | sudo sh
```

Or pick the cluster you want, and follow its guide through to Homestead
running:

| You want | Guide |
|---|---|
| VMs and containers on dedicated hardware, all managed for you | [Installing on Harvester](Installing-on-Harvester) |
| A light cluster on ordinary Linux machines - old PCs, mini PCs, VMs, 64-bit ARM boards | [Installing on k3s](Installing-on-k3s) |
| Full upstream Kubernetes on your own machines - hardened, as Harvester runs it, closer to what work runs | [Installing on RKE2](Installing-on-RKE2) |
| Homestead on a cluster you already run (RKE2, kubeadm, a Headlamp user's cluster) | [Installing on an existing cluster](Installing-on-an-existing-cluster) |

**On k3s, everything works**: containers, the App Store, Compose, networking,
IP addresses, Helm, volumes and data protection (the k3s script installs
Longhorn), and virtual machines once KubeVirt is added. Only following a
Harvester upgrade is Harvester's alone.

Then [Installing Homestead](Installing-Homestead) covers the choices every
install shares - Helm or plain manifests, the address, storage, the node
probe - and your first sign-in.

## Using Homestead

- [Dashboard and nodes](Dashboard-and-nodes) - health, every disk, hardware, drive health
- [Containers](Containers) - deploy, edit, groups, updates, privileges, placement
- [App Store](App-Store) - Unraid's Community Applications, deployed properly
- [Importing](Importing) - containers and VMs from an Unraid server, a Docker Compose file, or a VM disk image
- [Storage](Storage) - volumes, Longhorn allocation, adding disks, changing a storage class
- [Data protection](Data-protection) - snapshots, backups, backup storage, restores
- [Network shares](Network-shares) - Samba shares from any volume
- [Networking](Networking) - VIPs, which node answers for each address, services, IP address management
- [Virtual machines](Virtual-machines) - create, edit, console, move
- [Linked clusters](Linked-clusters) - several clusters from one sign-in, switched or shown together
- [Moving between clusters](Moving-between-clusters) - bring workloads from one cluster to another
- [Helm and Resources](Helm-and-resources) - charts, and every Kubernetes object
- [Settings](Settings) - cluster add-ons, hardware, users, MQTT, Homestead updates and health
- [Troubleshooting](Troubleshooting) - the problems people actually hit

The [reference](https://github.com/wjcloudy/homestead/blob/main/docs/reference.md) covers every feature in depth; this wiki is the guided
tour.
