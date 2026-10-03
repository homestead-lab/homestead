# Homestead

Homestead brings hyperconvergence home: apps, virtual machines, storage,
networking and cluster administration in one friendly interface.
**[k3s](Installing-on-k3s) is preferred for home self-hosting and is the most
widely tested platform with Homestead.** One command turns a spare Linux machine
into a cluster with Homestead on it. RKE2 and Harvester are supported alternatives;
you can also install Homestead on an existing Kubernetes cluster.

[![Homestead dashboard](https://github.com/homestead-lab/homestead/releases/latest/download/homestead-dashboard.jpg)](https://homestead-lab.github.io/homestead/)

**[Try the live demo](https://homestead-lab.github.io/homestead/)**: the real interface on made-up data, in your
browser, with nothing to install. Every page works, and nothing you do there
is saved.

*Every picture in this wiki comes from the latest release, taken from
Homestead's demo data, so they stay current on their own.*

## Start here

**Starting from nothing?** One line on a Linux machine asks what to build and
does it:

```bash
curl -sfL https://raw.githubusercontent.com/homestead-lab/homestead/main/scripts/install.sh | sudo sh
```

Choose **k3s** in the installer for a new home setup, or follow the guide for
your chosen platform:

| You want | Guide |
|---|---|
| **Recommended for home self-hosting:** a light cluster on old PCs, mini PCs, VMs or supported ARM64 boards | [Installing on k3s](Installing-on-k3s) |
| Rancher's RKE2 distribution on your own Linux machines | [Installing on RKE2](Installing-on-RKE2) |
| An integrated HCI operating system with VMs and storage on dedicated hardware | [Installing on Harvester](Installing-on-Harvester) |
| Homestead on a cluster you already run (RKE2, kubeadm, a Headlamp user's cluster) | [Installing on an existing cluster](Installing-on-an-existing-cluster) |

**Two machines?** Prefer [two linked single-node clusters](Linked-clusters#two-machines-at-home),
or add a [small third voting k3s server](Installing-on-k3s#using-a-small-third-server).
Two etcd servers cannot tolerate losing either vote. Linked clusters provide
shared management, not automatic failover.

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
- [Longhorn V2 support and considerations](Storage#longhorn-v2-support-and-considerations) - hardware, ARM, resource costs and migration
- [Data protection](Data-protection) - snapshots, backups, backup storage, restores
- [Network shares](Network-shares) - Samba shares from any volume
- [Networking](Networking) - VIPs, which node answers for each address, services, IP address management
- [Virtual machines](Virtual-machines) - create, edit, console, move
- [GPU passthrough](GPU-passthrough) - host preparation, vBIOS capture, VM setup and physical displays
- [Cluster shutdown](Cluster-shutdown) - confirmed shutdown of every host, live progress, and recovery
- [Linked clusters](Linked-clusters) - several clusters from one sign-in, switched or shown together
- [Moving between clusters](Moving-between-clusters) - bring workloads from one cluster to another
- [Helm and Resources](Helm-and-resources) - charts, and every Kubernetes object
- [API](API) - scoped, expiring API keys for Home Assistant, scripts and AI agents
- [Settings](Settings) - cluster add-ons, hardware, users, MQTT, Homestead updates and health
- [Troubleshooting](Troubleshooting) - the problems people actually hit

The [reference](https://github.com/homestead-lab/homestead/blob/main/docs/reference.md) covers every feature in depth; this wiki is the guided
tour.
