# Installing on Harvester

[Harvester](https://harvesterhci.io) is an operating system for a small
cluster of servers: it installs from a USB stick, and gives you Kubernetes,
Longhorn storage and KubeVirt virtual machines already wired together.
Homestead supports it as an integrated HCI alternative.

**For home self-hosting, [k3s](Installing-on-k3s) is preferred and is the most
widely tested platform with Homestead.** Choose Harvester when you want its
integrated operating system or already run it; k3s also supports VMs and
replicated storage with KubeVirt and Longhorn.

This guide goes from bare machines to Homestead running. Allow an hour for the
first host.

## 1. What you need

| | Minimum | Better |
|---|---|---|
| Hosts | 1 | 3 - Harvester keeps running, and your volumes keep a copy, when one fails |
| CPU | 8 cores, x86-64 with virtualisation (VT-x / AMD-V) on | 16 cores |
| Memory | 32 GB | 64 GB - Harvester itself uses around 10 GB per host |
| Install disk | 250 GB SSD | 500 GB NVMe; spinning disks are too slow for Longhorn |
| Network | 1 Gb/s | 10 Gb/s between hosts, since storage copies travel over it |

Check [Harvester's own requirements](https://docs.harvesterhci.io/latest/install/requirements)
for the release you install; they change a little between releases.

Also have ready, on your home network:

- **An address for each host** - static, or a DHCP reservation on your router.
- **One address for the cluster** (Harvester calls it the *VIP*). Harvester's
  dashboard answers on it, whichever host is up.
- **A few addresses for Homestead and your apps**, outside your router's DHCP
  range. Homestead needs one; each app you publish on its own address takes
  another. Keeping, say, `.240`-`.250` free for this is plenty.

## 2. Install the first host

1. Download the ISO from
   [Harvester's releases](https://github.com/harvester/harvester/releases) -
   the newest release not marked *pre-release*.
2. Write it to a USB stick. On Windows use [Rufus](https://rufus.ie) and choose
   **DD image mode** when asked; elsewhere,
   [balenaEtcher](https://etcher.balena.io). A server with a BMC (iDRAC, iLO,
   IPMI) can mount the ISO as virtual media instead.
3. Boot the machine from it and choose **Create a new Harvester cluster**.
4. Work through the screens:
   - **Installation disk** - the SSD Harvester lives on. Its free space also
     holds volumes unless you give Longhorn other disks later.
   - **Hostname** - short and lower-case, e.g. `harvester-node1`.
   - **Management network** - the network card on your LAN, a **static**
     address for this host, your gateway and DNS server.
   - **VIP** - the cluster address from step 1, static.
   - **Cluster token** - any long secret. Write it down: every other host
     needs it to join.
   - **Password** - for the `rancher` user, used for SSH to this host.
   - **NTP servers** - keep the defaults unless your network blocks them.
5. Confirm, and wait. The machine reboots, then shows a console screen that
   turns to **Ready** after 10-20 minutes.
6. Open `https://<VIP>` in a browser, accept the self-signed certificate, and
   set the dashboard's admin password.

## 3. Add more hosts (optional, recommended)

Boot each further machine from the same USB stick and choose **Join an existing
Harvester cluster**, giving the VIP and the cluster token. The first three hosts
all become management hosts, so the cluster survives any one of them failing.

Once Homestead is running, **Cluster → Add a host** walks through this for the
exact release your cluster runs - the ISO link, where to find the token, and
the next free hostname - and shows the host arriving.

## 4. Get a command line

You need `kubectl` once, to install Homestead. Two ways:

- **On a host.** `ssh rancher@<host-address>`, then `sudo -i`. `kubectl` is
  already set up for root on every Harvester host.
- **On your own computer.** In Harvester's dashboard, **Support → Download
  KubeConfig**, then point `kubectl` at it: `export KUBECONFIG=~/Downloads/local.yaml`
  (PowerShell: `$env:KUBECONFIG = "$HOME\Downloads\local.yaml"`). Use this way
  for Helm.

Check it works:

```bash
kubectl get nodes
kubectl get storageclass
```

The storage class list shows `harvester-longhorn (default)`; Homestead's data
goes there.

## 5. Install Homestead

The easiest way: on a management host (step 4, first way), as root, run

```bash
curl -sfL https://raw.githubusercontent.com/homestead-lab/homestead/main/scripts/install.sh | sudo sh
```

It sees that this is Harvester, checks the host reaches the cluster and
GitHub's registry, and asks two things: Homestead's address (one from your free
range) and the storage class for its data (`harvester-longhorn` is the usual
one). The installation summary shows the settings and the Homestead version -
the newest release, or one chosen from the list - then it installs, waits for
Homestead to start, and prints its address.

By hand instead: pick an address from your free range - `192.0.2.242` below; use your own.
From a host (step 4, first way), one line fetches the manifest, gives it your
address and Harvester's storage class, and applies it:

```bash
curl -sfL https://raw.githubusercontent.com/homestead-lab/homestead/main/deploy/deploy.yaml | sed -e 's/192\.0\.2\.242/192.0.2.242/g' -e 's/longhorn-r2/harvester-longhorn/g' -e 's/accessModes: \[ReadWriteMany\]/accessModes: [ReadWriteOnce]/' | kubectl apply -f -
```

Change the second `192.0.2.242` to your address. Or, with Helm on your own
computer:

```bash
helm install homestead oci://ghcr.io/homestead-lab/charts/homestead -n homestead --create-namespace --set service.loadBalancerIP=192.0.2.242
```

Either way, watch it start (the manifest installs into `lab`, the Helm chart
into `homestead`):

```bash
kubectl -n lab rollout status deployment/homestead --timeout=5m
```

Then open `http://192.0.2.242:8088` and create the first administrator.
[Installing Homestead](Installing-Homestead) explains every choice here and
what to do next.

> **Why ReadWriteOnce?** Harvester's default class is *migratable*, made so VM
> disks can move between hosts. A shared (ReadWriteMany) volume on it can still
> only be mounted by one host. That is fine for one copy of Homestead; to run
> two or three copies later, **Settings → Homestead → Copies** moves Homestead's
> data to a shareable class for you.

## 6. After installing

- **Keep addresses for apps.** **Networking → Workload VIPs → ＋ Add VIP**,
  and add the free range from step 1. Harvester has nothing that hands out
  addresses to apps on its own; Homestead gives them out from this list.
  See [Networking](Networking).
- **Give Longhorn your other disks.** Each node's card on **Nodes** lists disks
  nothing uses yet, with **Add to Longhorn**. See [Storage](Storage).
- **Check drive health.** Homestead installs the node probe automatically with
  either the Helm chart or manifest. See
  [Dashboard and nodes](Dashboard-and-nodes).
- **Plan backups.** [Data protection](Data-protection) sets up snapshots and
  backups in one go.

> Harvester runs kube-vip for load-balanced addresses. Do not add MetalLB to a
> Harvester cluster - two load balancers answering for the same addresses fight.
