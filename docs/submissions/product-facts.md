# Product facts and reusable copy

Verified against the local [README](../../README.md), [project licence](../../LICENSE), [k3s installation guide](../wiki/Installing-on-k3s.md), [deployment manifest](../../deploy/deploy.yaml) and [release workflow](../../.github/workflows/release.yml), 3 October 2026.

## Canonical details

| Field | Value |
|---|---|
| Name | Homestead |
| Maintainer handle | wjcloudy |
| Repository / project homepage | https://github.com/wjcloudy/homestead |
| Interactive demo | https://wjcloudy.github.io/homestead/ |
| Documentation | https://github.com/wjcloudy/homestead/wiki |
| Installation | https://github.com/wjcloudy/homestead#quick-install |
| Releases | https://github.com/wjcloudy/homestead/releases |
| License / price | Apache 2.0 + Commons Clause 1.0 / free to self-host |
| Maturity | Beta, as stated by the README |
| Implementation | Python backend; JavaScript browser interface |
| Runtime | Kubernetes on Linux; k3s, RKE2, Harvester or an existing Kubernetes cluster |
| Recommended platform | k3s: preferred for home self-hosting and the most widely tested platform with Homestead; RKE2 and Harvester are supported alternatives |
| Installation routes | Guided Linux installer, Helm chart or Kubernetes manifest |
| Image architectures | Release workflow builds linux/amd64 and linux/arm64; component and hardware support still varies |
| Storage / VMs | Longhorn enables volume and data-protection features; KubeVirt enables VMs |
| Networking | kube-vip provides virtual IP support; Multus provides additional network attachment support |
| Hardware access | Guided GPU/PCI and USB passthrough for VMs; named hardware features and eligible-host placement for containers, including USB radios |
| IPAM | LAN subnet/address inventory, scanning, live cluster addresses and VIP allocation; optional read-only UniFi import |
| Mobile and alerts | Installable HTTPS PWA on supported iOS/Android browsers, with push notifications |
| Home automation | MQTT cluster/node statistics with Home Assistant discovery; scoped API access for monitoring and control |
| Growth | Start with one host and add hosts or link clusters; no verified maximum scale or large-cluster benchmark |
| Demo behaviour | Interactive interface with sample data; no live cluster and no saved changes |

The [GitHub repository API](https://api.github.com/repos/wjcloudy/homestead) reported creation on **19 September 2026**. The earliest release retained in the current API listing was v2.8.294, published **2 October 2026**. Neither observation proves the original first public release date. Do not calculate a definitive Awesome-list eligibility date from a commit timestamp or repository creation date; establish the first public release separately.

## Reusable copy

### Positioning

Lead with **hyperconvergence for home, made simple**. Use **hyperconverged homelab platform** as the product category, and explain it in everyday language: bring compute, storage and networking together across ordinary machines, with one friendly interface for apps, VMs and the infrastructure underneath. Guided installation, familiar app and volume workflows, and integrated update controls support the simplicity message. Include cluster setup and ongoing administration in the supporting copy. Use **dashboard** for the overview screen.

[Harvester describes itself](https://harvesterhci.io/) as a hyperconverged infrastructure (HCI) solution for bare metal servers. Homestead brings that combined compute, storage and networking model to the homelab, using Kubernetes, KubeVirt, Longhorn and networking components. Its guided installer sets up the supporting stack on Linux; it can also manage existing clusters, including Harvester. The platform description refers to that integrated experience, with the underlying components supplying virtualization, storage and networking.

Lifecycle evidence: [guided cluster creation and joining](../../README.md#quick-install), [node and platform management](../../README.md#features), and [platform, host and Homestead updates](../wiki/Settings.md). Upgrade support varies by platform; Harvester's host OS is managed through Harvester upgrades.

### Tagline

```text
Hyperconvergence for home, made simple.
```

### Compact description — target: at most 160 characters

```text
Hyperconverged homelab platform with a simple web interface for apps, VMs, storage and networking, plus guided cluster setup and upgrades.
```

### List description — target: fewer than 250 characters

```text
Hyperconverged homelab platform combining compute, storage and networking, with guided Kubernetes installation, cluster upgrades, apps, VMs and backups.
```

### Short paragraph — target: 50–80 words

```text
Homestead brings hyperconvergence home: apps, VMs, replicated storage and networking in one friendly interface. Start with k3s, the preferred and most widely tested platform for home self-hosting, or use RKE2 or Harvester. Guided installation, hardware assignment, snapshots, backups and upgrades simplify everyday management. An installable phone app provides push notifications, while MQTT discovery connects cluster health to Home Assistant. Homestead is source-available and currently beta.
```

## Claims to keep precise

- Describe simplicity through specific workflows: guided installation, app deployment, volume management and integrated updates. The design aims to make hyperconvergence approachable; this is not a measured setup-time or usability claim. Host, disk and network preparation still depends on the environment.

- Kubernetes is required for real cluster management. A published container image and Compose import do not mean Homestead is a standalone Docker Compose deployment or a Docker-host manager.
- Describe resilience as configurable: it depends on control-plane quorum, healthy replicas, workload placement, spare capacity and network configuration. The k3s guide explains that three servers tolerate one failing and that a two-server control plane does not. Avoid blanket zero-downtime or zero-data-loss promises.
- The k3s guide gives 2 CPU cores and 4 GB RAM as a starting point. This is not a validated minimum for every VM, storage or high-availability configuration.
- The dashboard container's manifest requests 50m CPU and 96Mi RAM and sets a 256Mi memory limit. These are container settings, not whole-cluster hardware requirements. Its data claim requests 2Gi mounted at `/data`; the Service exposes TCP 8088 to container port 8080.
- Mention iOS/Android access as a browser/PWA interface over HTTPS, not native App Store applications.
- Describe ARM64 support as conditional on the underlying distribution, images and hardware; do not promise every feature on every ARM board. Use "start small and grow" rather than an untested large-scale or maximum-node claim.
- Passthrough devices remain physical and host-bound. Hardware-aware placement needs an eligible host; VM passthrough prevents live migration. Replicated storage does not remove these restrictions.
- Do not describe Homestead as a hypervisor, NAS operating system, Kubernetes distribution, or full replacement for every feature of Unraid, Proxmox or Portainer.
- Avoid unverified adoption numbers, security certifications, telemetry claims, performance benchmarks and production-readiness claims.

Feature evidence: [README feature table](../../README.md#features), [platform matrix](../../README.md#where-it-runs), [installation prerequisites and quorum](../wiki/Installing-on-k3s.md), [deployment configuration](../../deploy/deploy.yaml), [security reference](../security.md).
