# Homelab platform comparison: scope and sources

The [README comparison](../README.md#how-homestead-compares) focuses on tasks
people running a home server or several homelab machines commonly need. Sources
were checked on **3 October 2026**. Recheck them before reusing the table in
directory submissions; product capabilities and terminology change.

## How to read the table

The table compares the named products and explicitly identified integrations.
It is not a count of checkmarks, a performance test, or a claim that Homestead
replaces every function of the other products. “External solution” and “add”
describe work outside the reviewed built-in workflow; they do not mean a feature
is impossible. Absence of a built-in workflow is an assessment of the documented
product scope, not proof that no community project exists.

System containers, application containers and VMs are different. A VM image
catalogue is useful, but is not the same experience as selecting a packaged
self-hosted application. A local parity array or mirrored pool protects against
some disk failures; clustered storage and workload failover address different
failure modes. Every HA configuration needs enough healthy nodes, storage and
capacity. Snapshots and replicas do not replace independent backups.

Simplicity is Homestead's design aim, supported by its guided installer, app
catalogue and shared management interface. We have not measured setup time or
user success against the other products. Homestead is beta; the table does not
claim equal maturity, support coverage, security assurance or hardware support.

## Homestead

The compared baseline is 2.8.300 plus the documentation/licensing changes in this
branch. [Features and installation](../README.md#features),
[cluster setup](wiki/Installing-on-k3s.md), [updates](wiki/Settings.md),
[network shares](wiki/Network-shares.md), [storage](wiki/Storage.md), and
[VM management](wiki/Virtual-machines.md) support the Homestead column.

The App Store consumes Community Applications templates and deploys workloads
on Kubernetes; it is not the Docker engine. Longhorn, KubeVirt and networking
components supply the underlying capabilities. Some are optional or already
provided by an existing cluster. Homestead can run on Harvester, so these two
can complement each other. Talos is shown as an alternative infrastructure
approach, not as a claim that Homestead's host-management functions support it.

## Unraid

Unraid combines NAS storage, Docker and KVM VMs with web management. The table
credits its Community Applications plugin, file sharing and ZFS capabilities.
Its documented local storage and server-management workflows are not presented
as cross-host automatic workload failover.

Sources: [Community Applications](https://docs.unraid.net/unraid-os/manual/applications/),
[VM overview](https://docs.unraid.net/unraid-os/using-unraid-to/create-virtual-machines/overview-and-system-prep/),
[shares](https://docs.unraid.net/unraid-os/using-unraid-to/manage-storage/shares/),
[ZFS storage](https://docs.unraid.net/unraid-os/advanced-configurations/optimize-storage/zfs-storage/),
and [updates](https://docs.unraid.net/unraid-os/updating-unraid/).

## Proxmox VE

Proxmox provides integrated web management, KVM, LXC, clustering, HA, Ceph and
backup workflows. Current documentation also describes creating containers
from OCI images. That deserves explicit credit; it is not equivalent to a
Docker daemon or automatic Compose-stack compatibility. File-server workloads
can be hosted in guests; the table distinguishes them from a NAS share wizard.

Sources: [official features](https://proxmox.com/en/products/proxmox-virtual-environment/features)
and [container documentation, including OCI images](https://github.com/proxmox/pve-docs/blob/master/pct.adoc).

## CasaOS

CasaOS concentrates on an approachable personal cloud, Docker applications,
an app store and drive/file management. It offers UI updates and SMB sharing.
The reviewed core scope does not provide a VM/HA cluster manager; those cells
therefore identify separate tooling rather than asserting that a CasaOS host
cannot run it. This column is CasaOS, not the separate ZimaOS product.

Sources: [official repository and feature list](https://github.com/IceWhaleTech/CasaOS)
and [sharing implementation](https://github.com/IceWhaleTech/CasaOS/blob/main/service/shares.go).

## Harvester

Harvester is an HCI system using Kubernetes, KubeVirt and Longhorn, with VM
management, replicated storage, backups and cluster upgrades. Container
workloads can run in guest Kubernetes clusters or through its Rancher
integration. The v1.9 integration guide marks bare-metal workload support as
experimental; newer development documentation describes different support
conditions. Check the exact deployed versions instead of treating all Harvester
installs as identical.

Sources: [product overview](https://harvesterhci.io/),
[v1.9 Rancher integration](https://docs.harvesterhci.io/v1.9/rancher/rancher-integration/),
[VM backup and snapshots](https://docs.harvesterhci.io/v1.7/vm/backup-restore),
and [upgrade workflow](https://docs.harvesterhci.io/v1.7/upgrade/index/).

## Talos Linux

Talos is an immutable, API-managed Kubernetes operating system. It provides a
different foundation from a complete home-server UI. Omni is a separate
management option and is identified as such. Kubernetes storage, virtualization,
sharing and application catalogues can be added; the table does not falsely
credit those add-ons as part of a default Talos installation. This comparison
is Talos Linux, not the separately named Talos Hypervisor product.

Sources: [Talos Linux overview](https://www.siderolabs.com/talos-linux),
[Longhorn integration](https://docs.siderolabs.com/kubernetes-guides/csi/longhorn),
and [KubeVirt installation guide](https://www.talos.dev/v1.9/advanced/install-kubevirt/).

## MicroCloud

MicroCloud is also hyperconverged infrastructure: LXD supplies VMs and system
containers, MicroCeph supplies distributed storage, and MicroOVN supplies
networking. It has a graphical management option as well as a CLI. Docker apps
can run inside guests; LXD system containers are not the same packaging model
as Docker application containers. Cluster healing and shared storage require
appropriate configuration.

Sources: [MicroCloud architecture](https://documentation.ubuntu.com/microcloud/en/latest/microcloud/explanation/microcloud/),
[management and update guides](https://canonical.com/microcloud/docs/default/how-to/),
[container types](https://ubuntu.com/server/docs/explanation/virtualisation/container-tools-in-the-ubuntu-space/),
[LXD backup documentation](https://canonical.com/lxd/docs/default/backup/), and
[cluster healing requirements](https://canonical.com/lxd/docs/latest/howto/cluster_manage/).
