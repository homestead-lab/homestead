# Homelab platform comparison: scope and sources

The [README comparison](../README.md#how-homestead-compares) focuses on tasks
people running a home server or several homelab machines commonly need. Sources
were checked on **3 October 2026**. Recheck them before reusing the table in
directory submissions; product capabilities and terminology change.

## How to read the table

The table compares the named products and explicitly identified integrations.
It is not a count of checkmarks, a performance test, or a claim that Homestead
replaces every function of the other products. The symbols mean: ✅ a built-in or guided workflow; ⚪ support through
add-ons, guest services or custom configuration; ❌ no supported workflow in
the reviewed product. A cross does not mean arbitrary software could never be
installed on the underlying machine. Absence of a built-in workflow is an assessment of the documented
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

## Detailed coverage

Homestead brings the everyday experience of a home server to a cluster:
apps, volumes, shares and VMs, with compute, storage and networking managed
together. It is currently **beta**. This comparison describes the documented
management experience, including the integrations named in each cell. The
README summarises this evidence with icons. A tick describes an integrated or
guided capability, not an independently measured ease-of-use score. The app-store
row distinguishes self-hosted app catalogues from OS and VM image catalogues;
Proxmox, Harvester, Talos and MicroCloud can use additional application tools.
The distributed-storage row means live storage shared and replicated across hosts,
not Unraid local parity or ZFS send/receive backups.

| Homelab need | **Homestead** | [Unraid](#unraid) | [Proxmox VE](#proxmox-ve) | [CasaOS](#casaos) | [Harvester](#harvester) | [Talos Linux](#talos-linux) | [MicroCloud](#microcloud) |
|---|---|---|---|---|---|---|---|
| Main focus | **Simple hyperconvergence for home** | NAS, apps and VMs | Virtualization and clusters | Personal cloud and apps | Hyperconverged infrastructure | Kubernetes operating system | Private cloud / HCI |
| Everyday management | **Web UI for apps and infrastructure** | Web UI | Web UI | Web UI | Web UI; Rancher integration | API / CLI; optional Omni UI | UI / CLI |
| App discovery | **Community Applications catalogue** | Community Applications plugin | OS / OCI templates | App Store | VM images; apps via guest clusters / Rancher | Kubernetes / Helm ecosystem | OS image catalogue |
| Containers | **Kubernetes apps** | Docker | LXC; OCI images¹ | Docker | Kubernetes via Rancher / guests² | Kubernetes | LXD system containers; Docker in guests |
| Virtual machines | **KubeVirt integration** | KVM | KVM | Separate tooling | KubeVirt | Add KubeVirt | LXD / QEMU |
| Multi-node management | **Clusters and linked clusters** | Individual servers | Cluster management | Individual servers | Cluster management | Kubernetes clusters | LXD clusters |
| Storage across nodes | **Longhorn replicas** | Local array / pools; ZFS replication | Ceph; ZFS replication | Host storage | Longhorn replicas | Add storage such as Longhorn | MicroCeph |
| Workload failover³ | **Kubernetes + configured storage** | External solution | HA manager + suitable storage | External solution | VM HA + Longhorn | Kubernetes + added storage | LXD healing + shared storage |
| File sharing | **Managed SMB / NFS shares** | Managed SMB / NFS shares | Configure in a guest / service | SMB sharing | Configure in a guest / service | Deploy a sharing service | Configure in a guest / service |
| Snapshots and backups | **Volume / VM controls and schedules** | ZFS snapshots; backup apps / plugins | Guest backups; storage snapshots | Backup apps / external tools | VM snapshots and backups | Add workload / storage backup tools | LXD snapshots and exports |
| Installation and updates | **Guided cluster setup; app, platform and host controls** | USB install; WebGUI updates | ISO install; web / package tools | Linux installer; CasaOS UI updates | ISO install; cluster upgrade workflow | Declarative install; API-driven upgrades | Interactive setup; snap update workflow |

¹ Proxmox's OCI support is distinct from running the Docker engine or a Compose
stack. ² Harvester's bare-metal container support depends on the Harvester and
Rancher versions and feature configuration. ³ Failover in every clustered
option depends on quorum, healthy storage, spare capacity and networking;
replication is not a backup. Local disk redundancy is different from keeping
workloads available after a host fails.

“Add”, “guest” and “external” indicate an additional component or workflow,
not an inability to achieve the feature. Homestead's integrations also need
their supporting components; on Harvester, host upgrades follow Harvester's
own process. This is a documentation comparison, not a usability benchmark.

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

### Hardware, phones and home automation

[Hardware features](wiki/Dashboard-and-nodes.md#hardware) let an app request an
iGPU, Coral TPU or USB radio by a name you give it. Homestead discovers matching
devices and keeps the container on an eligible host. This includes USB device
access for Zigbee2MQTT, rather than just mounting a USB disk's files.
[VM passthrough](wiki/Virtual-machines.md#pci-and-usb-passthrough) provides host
inspection, IOMMU preparation on supported k3s/RKE2 hosts, device assignment and
a VM device picker. Firmware, drivers and IOMMU grouping still matter. A VM with
a host device cannot live-migrate; recovery needs another eligible device and
host, as well as suitable storage. Replication cannot move a physical USB stick.

[IPAM](wiki/Networking.md#ip-addresses) combines a LAN address inventory, subnet
scanning, DHCP-range conflict checks and live cluster addresses, with optional
read-only UniFi import. VIP allocation is a related, separate workflow. This
does not replace the router's DHCP server.

[Phone installation and notifications](wiki/Settings.md#notifications) use an
HTTPS PWA with browser push, including while the app is closed. iOS requires
Home Screen installation. [MQTT](wiki/Settings.md#mqtt-and-home-assistant)
publishes node and cluster statistics with Home Assistant discovery; the
[API](wiki/API.md#home-assistant) also supports scoped control integrations.

### Choose the foundation; start small and grow

Homestead supports [k3s](wiki/Installing-on-k3s.md),
[RKE2](wiki/Installing-on-RKE2.md) and [Harvester](wiki/Installing-on-Harvester.md).
**k3s is preferred for home self-hosting and is the most widely tested platform
with Homestead.** RKE2 and Harvester are supported alternatives.
This is a choice at deployment, not a promise of an in-place conversion between
distributions. The k3s starting point is one machine with 2 cores and 4 GB RAM;
VMs and replicated storage require more capacity. Hosts can be added and clusters
linked as the lab grows. No maximum node count, "huge cluster" benchmark or
enterprise-scale validation has been established for Homestead itself.

The installer accepts x86-64 and ARM64, and the release workflow builds both
images. Application images, VM guests, storage engines and device drivers must
support the chosen architecture. ARM support does not promise feature parity
on every board or mixed-architecture support in every underlying distribution.

### What differs from Proxmox

Homestead's focus is the combined home-server workflow: discover an app, give it
storage, a reachable address and any required hardware, then manage its updates,
backups and alerts alongside the cluster. Longhorn supplies per-volume replicas;
the file-share UI manages SMB/NFS services; MQTT discovery connects infrastructure
health to Home Assistant. The same interface works over the three supported
foundations and as an installable phone app.

Proxmox already covers many infrastructure capabilities well: KVM, LXC, Ceph,
HA, passthrough, backups, SDN/IPAM, notifications and mobile management. It also
has cluster device mappings. The comparison therefore does not treat hardware
access, distributed storage, ARM support or room to grow as exclusive to Homestead.
An app catalogue and managed file-share services are the clearer workflow
differences. Choosing between Longhorn/Kubernetes and Ceph/KVM/LXC is also an
architectural choice, not evidence of better performance or reliability.

## Scope of the additional comparison rows

- **Mobile app / PWA** counts a documented installable management app. Homestead
  supplies its PWA; Proxmox supplies an Android app; Unraid has separate community
  clients. A desktop web UI alone does not earn this tick. Crosses mean no such
  workflow was documented in the reviewed product, not that its web UI cannot
  be opened on a phone.
- **Alerts** counts event delivery, not just viewing logs. Homestead uses browser
  push; Unraid and Proxmox have configurable notification destinations; Harvester
  has a guided monitoring/Alertmanager add-on. Others require an external monitor
  and notification service. Guided product-managed add-ons count as ticks;
  assembling independent components counts as a grey circle.
- **Container USB** includes Docker, Kubernetes, LXC and LXD device access; these
  are different container models. Proxmox has an LXC Device Passthrough editor;
  LXD has USB device configuration. CasaOS supports Compose device mappings;
  Harvester and Talos need guest/container configuration and suitable host access.
- **IPAM** compares address-management coverage, not identical scope: Homestead
  includes LAN inventory and VIP allocation; Proxmox has SDN guest IPAM; Harvester
  has load-balancer IP pools; LXD has managed network address allocation. Docker
  network allocation alone is not a LAN inventory; Unraid/CasaOS need additional
  tooling for that. Talos needs additional network/address-management components.
- **Home Assistant / MQTT monitoring** means monitoring the platform from home
  automation, not merely hosting Home Assistant. Homestead supplies MQTT discovery.
  Proxmox has a documented Home Assistant integration, so receives a tick too.
  Other grey cells describe custom integration through APIs, exporters or scripts;
  they do not claim a ready-made MQTT discovery integration. Home Assistant's
  [REST integration](https://www.home-assistant.io/integrations/rest/) is one
  building block; authentication and entity mapping remain configuration work.
- **Growth and ARM64** do not imply equal minimum hardware or maximum scale.
  Harvester's single-node and architecture restrictions apply; MicroCloud documents
  a 50-member limit and distinguishes test from production requirements. Current
  Proxmox requirements list Armv9-A or newer; this is not Raspberry Pi support.

Backups and snapshots retain their existing row: they protect workload data with
the storage/backend qualifications in the detailed table. A replica is not a
backup, and a storage snapshot is not automatically application-consistent.

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

Additional sources: [PCI/GPU/USB VM setup](https://docs.unraid.net/unraid-os/using-unraid-to/create-virtual-machines/vm-setup/),
[notifications](https://docs.unraid.net/unraid-os/getting-started/set-up-unraid/customize-unraid-settings/),
and [community Unraid PWA](https://github.com/laurensguijt/Unraid-PWA).

## Proxmox VE

Proxmox provides integrated web management, KVM, LXC, clustering, HA, Ceph and
backup workflows. Current documentation also describes creating containers
from OCI images. That deserves explicit credit; it is not equivalent to a
Docker daemon or automatic Compose-stack compatibility. File-server workloads
can be hosted in guests; the table distinguishes them from a NAS share wizard.
The reviewed OCI documentation labels that support a technology preview, as
does the SDN documentation for IPAM/DHCP. A tick denotes coverage, not maturity.

Sources: [official features](https://proxmox.com/en/products/proxmox-virtual-environment/features)
and [container documentation, including OCI images](https://github.com/proxmox/pve-docs/blob/master/pct.adoc).

Additional sources: [PCI/USB passthrough and cluster device mappings](https://github.com/proxmox/pve-docs/blob/master/qm.adoc),
[LXC device editor](https://github.com/proxmox/pve-manager/blob/master/www/manager6/lxc/Resources.js),
[SDN/IPAM](https://github.com/proxmox/pve-docs/blob/master/pvesdn.adoc),
[notification destinations](https://github.com/proxmox/pve-docs/blob/master/notifications.adoc),
[current hardware requirements](https://www.proxmox.com/en/products/proxmox-virtual-environment/requirements),
and [Home Assistant integration](https://www.home-assistant.io/integrations/proxmoxve/).
The features page above documents official Android and mobile web access.

## CasaOS

CasaOS concentrates on an approachable personal cloud, Docker applications,
an app store and drive/file management. It offers UI updates and SMB sharing.
The reviewed core scope does not provide a VM/HA cluster manager; those cells
therefore identify separate tooling rather than asserting that a CasaOS host
cannot run it. This column is CasaOS, not the separate ZimaOS product.

Sources: [official repository and feature list](https://github.com/IceWhaleTech/CasaOS)
and [sharing implementation](https://github.com/IceWhaleTech/CasaOS/blob/main/service/shares.go).
The repository lists ARM64 support. [Compose device mappings](https://github.com/IceWhaleTech/CasaOS-AppManagement/blob/main/service/compose_app.go)
support custom hardware access; the [Jellyfin template](https://github.com/IceWhaleTech/CasaOS-AppStore/blob/main/Apps/Jellyfin/docker-compose.yml)
already includes GPU device paths.

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

Additional sources: [PCI/USB device add-on](https://docs.harvesterhci.io/v1.8/advanced/addons/pcidevices/),
[ARM64 and cluster requirements](https://docs.harvesterhci.io/v1.8/install/requirements/),
[IP pools](https://docs.harvesterhci.io/v1.4/networking/ippool/),
and [monitoring and Alertmanager](https://docs.harvesterhci.io/v1.8/monitoring/harvester-monitoring/).

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
The [Talos build configuration](https://github.com/siderolabs/talos/blob/main/.kres.yaml)
includes AMD64 and ARM64. Kubernetes device access, alert delivery and home
automation require extra components and configuration, rather than a built-in
home-server workflow.

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

Additional sources: [cluster size and hardware requirements](https://canonical.com/microcloud/docs/default/reference/requirements/),
[MicroCloud platform overview](https://canonical.com/microcloud),
[LXD USB](https://canonical.com/lxd/docs/default/reference/devices_usb/),
[PCI](https://canonical.com/lxd/docs/default/reference/devices_pci/),
[GPU](https://canonical.com/lxd/docs/default/reference/devices_gpu/),
[managed network addresses](https://canonical.com/lxd/docs/default/reference/network_bridge/),
and [events for external integrations](https://canonical.com/lxd/docs/default/events/).
