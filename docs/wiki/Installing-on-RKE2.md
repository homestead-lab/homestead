# Installing on RKE2 (full Kubernetes)

[RKE2](https://docs.rke2.io) is Rancher's Kubernetes distribution: upstream
Kubernetes with etcd, containerd and the Canal network, hardened to the CIS
benchmark out of the box. It is what Harvester is built on, and the closest a
homelab gets to the Kubernetes that businesses run. It asks more of the
machines than [k3s](Installing-on-k3s) does.

The same one-line installer builds either one. It installs RKE2, with
[Longhorn](https://longhorn.io) storage and Homestead running on it.

## k3s or RKE2?

| | k3s | RKE2 |
|---|---|---|
| **What it is** | Kubernetes in one small program | Upstream Kubernetes, hardened (CIS), as Harvester runs it |
| **Memory for a server** | 2 GB to start, 4 GB comfortable | 4 GB at least, 8 GB comfortable |
| **Suits** | old PCs, mini PCs, ARM boards, small VMs | machines with room to spare; learning Kubernetes as work runs it |
| **Storage** | Longhorn, or k3s's own local-path | Longhorn (RKE2 has no storage of its own) |
| **Addresses for apps** | ServiceLB: every node's own address | the same - the installer turns RKE2's ServiceLB on |
| **In Homestead** | everything | everything |

Homestead does not mind which: every page works the same on both. If you are
unsure, k3s is lighter; RKE2 is the choice when you want full Kubernetes.

## What works on RKE2

Everything Homestead does, with nothing Harvester-specific needed:

- **Containers, App Store, Docker Compose, Portal, Resources** - as anywhere.
- **Volumes, disks, data protection, network shares** - on Longhorn, which the
  installer always adds on RKE2.
- **Addresses** - RKE2's ServiceLB, which the installer turns on
  (`enable-servicelb`), puts every app on the machines' own addresses, so
  Homestead answers on `http://<any machine>:8088`. For an address per app,
  add kube-vip - see [Addresses for apps](#4-addresses-for-apps).
- **Helm** - charts install through the Helm controller RKE2 already runs.
- **Adding and removing machines** - **Cluster → Add a host** gives RKE2's
  join lines; removing one gives RKE2's uninstall steps.
- **Virtual machines** - with [KubeVirt](https://kubevirt.io) and CDI: say yes
  when the installer asks, or add them later from **Settings → Cluster →
  Add-ons** (see [Virtual machines](Virtual-machines#vms-on-k3s-or-rke2)).

## 1. What you need

- **One or more Linux machines** with a 64-bit OS (Ubuntu Server 24.04 LTS,
  Debian 12, Rocky/Alma 9, openSUSE Leap or SLES), `curl`, and root access.
  x86-64 or 64-bit ARM.
- **4 GB of memory and 2 cores** for each server at least - 8 GB is
  comfortable with Longhorn and a few apps. Workers can be smaller.
- **A fixed address for each** - static, or a DHCP reservation on your router.
- **Disk for your data.** Longhorn stores volumes under `/var/lib/longhorn` on
  each machine's system disk to begin with; give it bigger disks later from
  Homestead.

Open these between the machines if a firewall runs on them:

| Port | For |
|---|---|
| TCP 6443 | the Kubernetes API |
| TCP 9345 | RKE2's supervisor - machines join the cluster through it |
| TCP 10250 | kubelet |
| UDP 8472 | the pod network (Canal's VXLAN) |
| TCP 2379-2380 | etcd, between servers |
| TCP 9500-9504 | Longhorn, between machines |
| TCP 8088 | Homestead, from your browser |

**NetworkManager.** On a machine where NetworkManager runs (Rocky, Alma, SLES,
Ubuntu Desktop), tell it to leave Canal's interfaces alone before installing,
or pods lose their network:

```bash
printf '[keyfile]\nunmanaged-devices=interface-name:cali*;interface-name:flannel*\n' | sudo tee /etc/NetworkManager/conf.d/rke2-canal.conf
```

```bash
sudo systemctl reload NetworkManager
```

## 2. The first machine

On the first machine:

```bash
curl -sfL https://raw.githubusercontent.com/wjcloudy/homestead/main/scripts/install.sh | sudo sh
```

![The installer's menu](https://github.com/wjcloudy/homestead/releases/latest/download/homestead-tui-menu.png)

Select **Install Homestead**, then **Create a new cluster**, then **RKE2**:

![Which Kubernetes](https://github.com/wjcloudy/homestead/releases/latest/download/homestead-tui-kubernetes.png)

The installer checks the machine first - memory, disk, the internet (it
reaches `get.rke2.io` and `ghcr.io`), ports 6443, 9345 and 10250, the
hostname, the clock, the firewall, `/dev/kvm`, an address from DHCP - and
stops on anything that would make the install fail, saying what to put right.

![The checks](https://github.com/wjcloudy/homestead/releases/latest/download/homestead-tui-checks.png)

It asks which address the other machines reach this one on, when it has more
than one, and whether to install KubeVirt. The installation summary then
shows the settings and the version of each component - RKE2 from its stable
channel, Longhorn, KubeVirt, CDI and Homestead from their current releases.
Select a component to install another version: the list comes live from
RKE2's release channels and from GitHub.

![The installation summary](https://github.com/wjcloudy/homestead/releases/latest/download/homestead-tui-ready.png)

Select **Install**. The first installation takes 10-15 minutes while RKE2
downloads its images, with a progress bar. It:

1. installs what Longhorn needs on the host (`open-iscsi` and an NFS client);
2. writes `/etc/rancher/rke2/config.yaml` (this machine's address, and
   `enable-servicelb: true`), installs RKE2 and starts `rke2-server`;
3. asks RKE2 to install Longhorn (one copy of each volume, while there is one
   machine) and Homestead's own manifest, from RKE2's manifests folder;
4. waits for Homestead and prints its address - `http://<this machine>:8088`.

Open that address and create the first administrator.

The installer runs [`bootstrap-k3s.sh`](https://github.com/wjcloudy/homestead/blob/main/scripts/bootstrap-k3s.sh)
with `--rke2` to do this. You can run it yourself instead, with no questions:

```bash
curl -sfL https://raw.githubusercontent.com/wjcloudy/homestead/main/scripts/bootstrap-k3s.sh | sudo sh -s - server --rke2
```

Options go after `server`:

| Option | Does |
|---|---|
| `--rke2` | RKE2 instead of k3s |
| `--kubevirt` | also installs KubeVirt and CDI, so Homestead can run virtual machines - emulated, and slow, if the machine has no hardware virtualisation (`/dev/kvm`) |
| `--rke2-version v1.33.4+rke2r1` | pins RKE2 instead of its stable channel |
| `--longhorn-version v1.9.1` | pins Longhorn instead of its newest release |
| `--kubevirt-version v1.6.0`, `--cdi-version v1.62.0` | pin KubeVirt and CDI instead of their current releases |
| `--homestead-version 2.8.118` | pins Homestead instead of the newest release |
| `--node-ip 192.0.2.10` | the address RKE2 registers this machine by, when it has more than one |

Unattended, the installer takes its answers ahead:

```bash
curl -sfL https://raw.githubusercontent.com/wjcloudy/homestead/main/scripts/install.sh | sudo HS_ROLE=new HS_DIST=rke2 HS_NODE_IP=192.0.2.10 HS_YES=1 sh
```

## 3. More machines

On each further machine, run the same line:

```bash
curl -sfL https://raw.githubusercontent.com/wjcloudy/homestead/main/scripts/install.sh | sudo sh
```

Select **Install Homestead**, then **Join an existing cluster as a worker
node** (runs apps) or **as a server node** (control plane and etcd as well),
and give the first machine's address. Install the same RKE2 version as the
existing servers; select it on the summary if the cluster does not report it. The installer sees that the cluster runs RKE2 - its
supervisor answers on port 9345 - and asks for the cluster's token, which is
on the first machine:

```bash
sudo cat /var/lib/rancher/rke2/server/node-token
```

Or, with no questions, as a **worker**:

```bash
curl -sfL https://raw.githubusercontent.com/wjcloudy/homestead/main/scripts/bootstrap-k3s.sh | sudo sh -s - agent https://192.0.2.10:9345 <token> --rke2
```

As another **server**:

```bash
curl -sfL https://raw.githubusercontent.com/wjcloudy/homestead/main/scripts/bootstrap-k3s.sh | sudo sh -s - join https://192.0.2.10:9345 <token> --rke2
```

Use `192.0.2.10` as the first machine's address, and your token. **Cluster →
Add a host** in Homestead shows the join lines for this cluster too.

**How many servers?** etcd needs more than half of its servers up. One server
is fine for a homelab; three survive one failing; two are worse than one,
since losing either stops the cluster.

Longhorn starts with one copy of each volume. Once there are three machines,
**Volumes → Storage classes** creates a class with three copies and makes it
the default; see [Storage](Storage).

## 4. Addresses for apps

With ServiceLB on, RKE2 publishes each LoadBalancer service on **every
machine's own address**, as k3s does: Homestead on `:8088`, and each app on
its own port. For an address per app, Homestead installs **kube-vip** and
**Multus** after it starts, as on [k3s](Installing-on-k3s#4-addresses-for-apps);
an older installation offers them from **Settings →
Cluster → Add-ons**, then list the addresses apps may have under
**Networking → Your VIPs** - the same as on [k3s](Installing-on-k3s#4-addresses-for-apps).

## 5. kubectl on RKE2

RKE2 keeps its own kubectl and credentials:

```bash
sudo /var/lib/rancher/rke2/bin/kubectl --kubeconfig /etc/rancher/rke2/rke2.yaml get nodes
```

To make plain `kubectl` work in root's shell, add to `/root/.bashrc`:

```bash
export PATH=$PATH:/var/lib/rancher/rke2/bin KUBECONFIG=/etc/rancher/rke2/rke2.yaml
```

## RKE2 already running?

On an RKE2 server, the same one-line installer finds the cluster and offers
**Install Homestead on this cluster**: Longhorn if it is missing, then Homestead.
For other Kubernetes - kubeadm, Talos, a managed cluster - see
[Installing on an existing cluster](Installing-on-an-existing-cluster).

## Next

[Installing Homestead](Installing-Homestead) covers what the install made,
first sign-in, updates and the node probe.

## If something is stuck

Run the node doctor on the machine: the same line, then **Check node
health** - see [Troubleshooting](Troubleshooting#node-doctor). By
hand:

- `sudo journalctl -u rke2-server -f` (or `rke2-agent`) - RKE2's own log. The
  first start takes several minutes while it fetches its images.
- `sudo /var/lib/rancher/rke2/bin/kubectl --kubeconfig /etc/rancher/rke2/rke2.yaml get pods -A`
  - is anything not Running?
- A joining machine that never appears: port 9345 closed between the
  machines, or the wrong token.
- Longhorn pods crash-looping usually means `open-iscsi` is missing or
  `iscsid` is not running: `sudo systemctl enable --now iscsid`.
- More in [Troubleshooting](Troubleshooting).
