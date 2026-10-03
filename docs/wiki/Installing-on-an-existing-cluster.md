# Installing on an existing cluster

**Building a new home setup? [k3s](Installing-on-k3s) is preferred for home
self-hosting and is the most widely tested platform with Homestead.** This
guide is for installing on a cluster you already have.

Already run RKE2, k3s, kubeadm, Talos or a managed Kubernetes, and look after
it with Headlamp, Lens or kubectl? Homestead installs beside whatever is there
and changes nothing until you ask it to.

On a **k3s or RKE2 server**, the one-line installer does it for you - it
finds the cluster, adds Longhorn if it is missing, and installs Homestead:

```bash
curl -sfL https://raw.githubusercontent.com/homestead-lab/homestead/main/scripts/install.sh | sudo sh
```

For anything else - or to choose every value yourself - use Helm or the
manifest below. Headlamp can stay: Homestead's
**Resources** page covers the same ground (every kind, YAML, events, logs), and
the rest of Homestead adds the homelab layer on top.

## What Homestead uses, and what happens without it

Homestead looks at what the cluster has and each page works with that:

| The cluster has | Without it |
|---|---|
| **A storage class** - [Longhorn](https://longhorn.io) best | Required, for Homestead's own data and your volumes. Without Longhorn, the volume pages still work but copies, snapshots, backups and moves do not; Homestead offers to install Longhorn from its **Helm** page |
| **A LoadBalancer** - MetalLB, kube-vip, k3s's ServiceLB, Cilium | Homestead can still run as a `NodePort` or be reached by port-forward, and publishes apps the same way |
| **KubeVirt**, plus **CDI** for disk images | No VM pages |
| **A Helm controller** - k3s and RKE2 have one built in | The Helm page still lists every release, but cannot install charts |
| **Harvester** | Its extras (images, IP pools, host joining) are simply not shown |

Kubernetes 1.25 or newer, x86-64 or ARM64.

## Install with Helm

Pick an address your load balancer can give out (`192.0.2.242` below):

```bash
helm install homestead oci://ghcr.io/homestead-lab/charts/homestead -n homestead --create-namespace --set service.loadBalancerIP=192.0.2.242 --set service.kubeVip=false
```

`service.kubeVip=false` is for anything but Harvester and kube-vip: it stops the
chart asking for the address in kube-vip's annotation.

Useful values (`helm show values oci://ghcr.io/homestead-lab/charts/homestead` lists
them all):

| Value | Default | Meaning |
|---|---|---|
| `service.type` | `LoadBalancer` | `NodePort` or `ClusterIP` for a cluster without a load balancer |
| `service.loadBalancerIP` | empty | Homestead's address; empty lets the load balancer choose |
| `persistence.storageClass` | the default class | where Homestead's own 2 GiB of data lives |
| `storageClass` | the default class | the class new volumes start on in Deploy and Import |
| `workloadNamespace.name` | `lab` | where your apps, shares and the node probe go |
| `nodeprobe.enabled` | `true` | temperatures, host devices and drive health from every node |

Without a load balancer, reach it through kubectl until you set one up:

```bash
kubectl -n homestead port-forward service/homestead 8088:8088
```

then open `http://localhost:8088`.

## Install with the plain manifest

The manifest installs the same things into the `lab` namespace. Download it,
then change three things:

```bash
curl -sfLO https://raw.githubusercontent.com/homestead-lab/homestead/main/deploy/deploy.yaml
```

1. `storageClassName: longhorn-r2` and the `STORAGE_CLASS` value - to a class
   `kubectl get storageclass` lists. Keep `ReadWriteMany` for a shareable
   Longhorn class; use `ReadWriteOnce` for anything else.
2. `LB_IP` and every `192.0.2.242` - to Homestead's address.
3. The `kube-vip.io/loadbalancerIPs` annotation - delete it unless kube-vip is
   your load balancer.

```bash
kubectl apply -f deploy.yaml
kubectl -n lab rollout status deployment/homestead --timeout=5m
```

## What Homestead is allowed to do

Homestead runs as its own ServiceAccount with a ClusterRole that can read every
kind and change most of them - it deploys apps, makes volumes, edits objects
from the Resources page, and keeps its own role up to date as releases need
more. That is close to cluster-admin, and is said plainly in
[`deploy/rbac.yaml`](https://github.com/homestead-lab/homestead/blob/main/deploy/rbac.yaml).
Container consoles are limited to the namespace your apps go in. If that is more than
you want in a shared cluster, Homestead is not the right fit there.

## Next

[Installing Homestead](Installing-Homestead) covers first sign-in, users,
updates and the node probe.
