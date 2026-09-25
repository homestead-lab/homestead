# Homestead

Homestead is a homelab control panel for Kubernetes: containers, virtual
machines, storage, network shares, backups and addresses in one place, in
words that make sense if you came from Unraid or Docker. It runs on
**[k3s](https://k3s.io)** - one command turns a spare Linux machine into a
cluster with Homestead on it - on [Harvester](https://harvesterhci.io), where
it grew up, and on RKE2 or any cluster you look after with Headlamp today.

![Homestead dashboard](https://github.com/wjcloudy/homestead/releases/latest/download/homestead-dashboard.png)

*Every picture in this wiki comes from the latest release, taken from
Homestead's demo data, so they stay current on their own.*

## Start here

**Starting from nothing?** Pick the cluster you want, and follow it through
to Homestead running:

| You want | Guide |
|---|---|
| VMs and containers on dedicated hardware, all managed for you | [Installing on Harvester](Installing-on-Harvester) |
| A light cluster on ordinary Linux machines - old PCs, mini PCs, VMs, 64-bit ARM boards | [Installing on k3s](Installing-on-k3s) |
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
- [Importing](Importing) - from an Unraid server, a Docker Compose file, or a VM disk image
- [Storage](Storage) - volumes, Longhorn allocation, adding disks, changing a storage class
- [Data protection](Data-protection) - snapshots, backups, backup storage, restores
- [Network shares](Network-shares) - Samba shares from any volume
- [Networking](Networking) - your VIPs, services, IP address management
- [Virtual machines](Virtual-machines) - create, edit, console, move
- [Moving between clusters](Moving-between-clusters) - bring workloads from one cluster to another
- [Helm and Resources](Helm-and-resources) - charts, and every Kubernetes object
- [Settings](Settings) - cluster, hardware, users, MQTT, redundancy, Homestead's own health
- [Troubleshooting](Troubleshooting) - the problems people actually hit

The [README](https://github.com/wjcloudy/homestead#readme) has the full
reference for every feature; this wiki is the guided tour.
