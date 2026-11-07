# Installing on an existing cluster

**Building a new home setup? [k3s](Installing-on-k3s) is preferred for home
self-hosting and is the most widely tested platform with Homestead.** This
guide is for installing on a cluster you already have.

Already run RKE2, k3s, kubeadm, Talos or a managed Kubernetes, and look after
it with Headlamp, Lens or kubectl? Homestead installs beside whatever is there.
It changes your apps only when you ask; what it makes for itself, and when, is
on [What Homestead changes](What-Homestead-changes).

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

1. `storageClassName: longhorn` and the `STORAGE_CLASS` value - Longhorn makes
   `longhorn` itself, so with Longhorn there is nothing to change. Without it,
   use a class `kubectl get storageclass` lists (k3s's `local-path`, say). If
   the data claim stays Pending, `kubectl describe pvc -n lab` shows why.
   Keep `ReadWriteMany` for a shareable Longhorn class; use `ReadWriteOnce`
   for anything else.
2. `LB_IP` and every `192.0.2.242` - to Homestead's address.
3. The `kube-vip.io/loadbalancerIPs` annotation - delete it unless kube-vip is
   your load balancer.

Homestead installs the node probe soon after it starts: a privileged
DaemonSet on every node, for temperatures, drive health and each node's
network interfaces. To leave it out, add
`HOMESTEAD_NODEPROBE_AUTO_INSTALL` with the value `"false"` to the `env` of
the `homestead` container before applying, and keep it there when you apply
the manifest again; add the probe later under **Settings > Cluster > Add-ons**.

```bash
kubectl apply -f deploy.yaml
kubectl -n lab rollout status deployment/homestead --timeout=5m
```

Longhorn's `longhorn` class asks for three copies. On a cluster with one host
Homestead lowers its own data volume to the one copy that host can keep, once
Longhorn says another cannot be placed, so it reads healthy; no other volume
or Longhorn setting is changed. Once a second host can hold a copy,
`longhorn-r2` ("Longhorn, two copies") is the class to move Homestead's data
onto: **Settings > About > Redundancy** then offers **Keep two copies**,
which makes it and opens the move.

Installed earlier from a manifest that said `longhorn-r2`? A claim's class
cannot change. Check the claim first:

```bash
kubectl -n lab get pvc homestead-data
```

Only if its STATUS is `Pending` does it hold nothing: then
`kubectl -n lab delete pvc homestead-data` and apply again (or create
`longhorn-r2` first). **Never delete a `Bound` claim: that can lose
Homestead's data.** A Bound claim keeps its class and keeps working, and
in-app updates change only the image; to apply the manifest again over it,
set `storageClassName` back to the class that command shows.

## What Homestead is allowed to do

Homestead runs as its own ServiceAccount with a ClusterRole that can read every
kind and change most of them - it deploys apps, makes volumes, edits objects
from the Resources page, and keeps its own role up to date as releases need
more. That is close to cluster-admin, and is said plainly in
[`deploy/rbac.yaml`](https://github.com/homestead-lab/homestead/blob/main/deploy/rbac.yaml).
Container consoles are limited to the namespace your apps go in. If that is more than
you want in a shared cluster, Homestead is not the right fit there.
[What Homestead changes](What-Homestead-changes) lists what it actually makes
and changes, and when.

Some kinds a feature depends on are also named in a rule of their own, so the
role says what each is for. Image clean-up, for one, has read-only `get` and
`list` on KubeVirt's `virtualmachinepools` (`pool.kubevirt.io`) and
`virtualmachineinstancereplicasets` (`kubevirt.io`): a VM pool or replica set
scaled to zero still starts from its template's container disks, and only these
lists name them. The same read-only access covers Harvester's
`virtualmachinetemplateversions` (`harvesterhci.io`), KubeVirt's
`virtualmachinesnapshotcontents` (`snapshot.kubevirt.io`) and CDI's
`dataimportcrons` (`cdi.kubevirt.io`): a VM template or snapshot boots from the
container disks it names, and a CDI import that pulls through the node
(`pullMethod: node`) needs its image again. Without that read, Homestead keeps every image and says why on
the Image cache page; it never cleans up an image it cannot account for. The
Volumes page, for another, has read-only `get` and `list` on
`replicationcontrollers`, so a volume that only a ReplicationController made on
its own uses is not shown as orphaned. Until that permission is applied
(Homestead brings its own permissions up to date, or apply `deploy/rbac.yaml`),
a volume nothing else names shows as unknown, never as orphaned, and installing
an app over a leftover volume of the same name is refused, because Homestead
cannot check what uses it.

## Next

[Installing Homestead](Installing-Homestead) covers first sign-in, users,
updates and the node probe.
