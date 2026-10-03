# GitHub About and topics

Status: proposed metadata only; repository settings have not been changed. Current metadata was read from the [repository API](https://api.github.com/repos/wjcloudy/homestead) on 3 October 2026.

The current About description names Harvester, Rancher, Longhorn, Fleet, KubeVirt and Kubernetes. The proposal below leads with the user-facing use case. The current homepage field is empty; the demo is already linked in the README.

## Proposed About description

```text
Hyperconvergence for home, made simple. Apps, VMs, replicated storage and networking, with guided Kubernetes setup and updates. Source-available; beta.
```

## Proposed website

```text
https://wjcloudy.github.io/homestead/
```

This is an interactive demo with sample data. Keep the repository and wiki links prominent for installation and documentation.

## Proposed complete topic set

This proposed set covers the infrastructure, homelab and hyperconvergence use cases. Review legacy integration topics against the current release when applying it.

```text
backup
container-management
fleet
harvester
high-availability
homelab
cluster-management
homeserver
hyperconverged-infrastructure
k3s
kubernetes
kubevirt
longhorn
rancher
rke2
self-hosted
storage
unraid
virtualization
```

Use `hyperconverged-infrastructure` and `cluster-management` to reflect the whole product, alongside the component topics. Avoid adding a broad `docker` topic solely for search traffic: Homestead's operational deployment requires Kubernetes.
