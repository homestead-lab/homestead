# Installing on k3s

[k3s](https://k3s.io) is Kubernetes in one small program. It runs on almost any
Linux machine - an old desktop, a mini PC, a VM - and needs far less than
Harvester: 2 cores and 4 GB of memory is enough to start. It is the DIY route:
the kind of cluster people build and then look after with Headlamp.

One script turns a bare machine into a k3s cluster with
[Longhorn](https://longhorn.io) storage and Homestead running on it.

## What works on k3s

Everything Homestead does, with nothing Harvester-specific needed:

- **Containers, App Store, Docker Compose, Portal, Resources** - as anywhere.
- **Volumes, disks, data protection, network shares** - on Longhorn, which the
  script installs. With `--no-longhorn` apps still get volumes from k3s's
  `local-path`, but the Volumes and Data protection pages need Longhorn.
- **Addresses** - k3s's built-in load balancer (ServiceLB) puts every app on the
  machines' own addresses, so nothing else is needed; MetalLB, if you want an
  address per app, is [below](#4-addresses-for-apps).
- **Helm** - charts install through the Helm controller k3s already runs.
- **Adding and removing machines** - **Cluster → Add a host** gives this
  cluster's join lines; removing one gives k3s's uninstall steps.
- **Virtual machines** - with [KubeVirt](https://kubevirt.io) and CDI: add
  `--kubevirt` to the script, or install them later from **Settings → Cluster →
  Add-ons** (see [Virtual machines](Virtual-machines#vms-on-k3s-or-rke2)). The
  machines need hardware virtualisation for VMs to run at full speed.
- **Longhorn later** - started with `--no-longhorn`? **Settings → Cluster →
  Add-ons** installs it once the machines have open-iscsi.

## 1. What you need

- **One or more Linux machines** with a 64-bit OS (Ubuntu Server 24.04 LTS,
  Debian 12, Rocky/Alma 9 or openSUSE Leap are all fine), `curl`, and root
  access. x86-64 or 64-bit ARM.
- **A fixed address for each** - static, or a DHCP reservation on your router.
- **Disk for your data.** Longhorn stores volumes under
  `/var/lib/longhorn` on each machine's system disk to begin with; give it
  bigger disks later from Homestead.
- **Virtual machines are optional.** k3s runs containers. To run VMs too, the
  machines need hardware virtualisation, and KubeVirt added afterwards - see
  [Virtual machines](Virtual-machines).

Open these between the machines if a firewall runs on them: TCP 6443 (the
Kubernetes API), UDP 8472 (the pod network), TCP 10250 (kubelet), and TCP
2379-2380 between servers (etcd). Longhorn also talks between machines on
TCP 9500-9504.

## 2. The first machine

On the first machine:

```bash
curl -sfL https://raw.githubusercontent.com/wjcloudy/homestead/main/scripts/bootstrap-k3s.sh | sudo sh -s - server
```

It takes 5-10 minutes, and:

1. installs what Longhorn needs on the host (`open-iscsi` and an NFS client);
2. installs k3s with an embedded etcd, so more servers can join later;
3. asks k3s to install Longhorn (one copy of each volume, while there is one
   machine) and Homestead's own manifest;
4. waits for Homestead and prints its address - `http://<this machine>:8088`.

Open that address and create the first administrator.

Options go after `server`:

| Option | Does |
|---|---|
| `--no-longhorn` | uses k3s's built-in local-path storage instead: simpler, no copies, and each volume stays on the machine it was made on |
| `--kubevirt` | also installs KubeVirt and CDI, so Homestead can run virtual machines - emulated, and slow, if the machine has no hardware virtualisation (`/dev/kvm`) |
| `--k3s-version v1.33.4+k3s1` | pins k3s instead of its stable channel |
| `--homestead-version 2.8.118` | pins Homestead instead of the newest release |

The script is safe to run again: each step finds what the last run left.

## 3. More machines

Each further machine joins with the first machine's address and its token. The
token is on the first machine:

```bash
sudo cat /var/lib/rancher/k3s/server/node-token
```

As a **worker** (runs apps, not the control plane):

```bash
curl -sfL https://raw.githubusercontent.com/wjcloudy/homestead/main/scripts/bootstrap-k3s.sh | sudo sh -s - agent https://192.0.2.10:6443 <token>
```

As another **server** (control plane and etcd as well):

```bash
curl -sfL https://raw.githubusercontent.com/wjcloudy/homestead/main/scripts/bootstrap-k3s.sh | sudo sh -s - join https://192.0.2.10:6443 <token>
```

Use `192.0.2.10` as the first machine's address, and your token. **Cluster →
Add a host** in Homestead shows these lines already filled in.

**How many servers?** etcd needs more than half of its servers up. One server
is fine for a homelab; three survive one failing; two are worse than one,
since losing either stops the cluster.

The script sets Longhorn up with one copy of each volume, which is all one
machine can hold. Once there are three machines, make new volumes keep three:
**Volumes → Storage classes** creates a class with three copies and makes it
the default. **Volumes** shows each volume's copies - `×1` in orange is a
volume a single failed disk would lose - and
[Changing a volume's storage class](Storage#changing-a-volumes-storage-class)
moves an existing one onto the new class.

### More disks

Longhorn starts on each machine's system disk. To give it another drive,
**Nodes → Disks → Add to Longhorn** shows the commands to run on that machine
first. Mount it the way they do - with `nofail` in `/etc/fstab` - or a machine
whose drive dies stops at an emergency shell when it next starts, instead of
starting without it. See [Storage](Storage#booting-with-a-dead-or-missing-drive).

## 4. Addresses for apps

k3s's built-in load balancer, ServiceLB, publishes a LoadBalancer service on
**every machine's own address**. So Homestead answers on
`http://<any machine>:8088`, and each app is reached the same way on its own
port. Two apps cannot both take port 80. The script does not install MetalLB,
and nothing in Homestead needs it.

When you want an address per app, [MetalLB](https://metallb.io) replaces
ServiceLB: install k3s with `disable: [servicelb]` in
`/etc/rancher/k3s/config.yaml`, then install MetalLB (from **Helm** in
Homestead, or MetalLB's own instructions) and give it an address pool.
Homestead then asks MetalLB for addresses, and the **Specific VIP** picker offers
the addresses you keep under **Networking → Your VIPs**. This is easiest to
decide before you have many apps.

## 5. Without the script

The script only runs k3s's own installer and drops two files into
`/var/lib/rancher/k3s/server/manifests`, which k3s applies itself. To do it by
hand, install k3s your way, install Longhorn (or use `local-path`), and follow
[Installing on an existing cluster](Installing-on-an-existing-cluster).

## Next

[Installing Homestead](Installing-Homestead) covers what the install made,
first sign-in, updates and the node probe.

## If something is stuck

- `sudo k3s kubectl get pods -A` - is anything not Running?
- `sudo journalctl -u k3s -e` - k3s's own log.
- Longhorn pods crash-looping usually means `open-iscsi` is missing or
  `iscsid` is not running: `sudo systemctl enable --now iscsid`.
- More in [Troubleshooting](Troubleshooting).
