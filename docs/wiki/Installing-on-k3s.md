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
- **Addresses** - k3s's built-in load balancer (ServiceLB) publishes apps on the
  machines' own addresses. kube-vip, for a virtual IP per app, and Multus, for a
  VM's or container's own LAN address, are installed by Homestead after it
  starts, and macvtap - a VM's LAN address through the machine's NIC - once
  KubeVirt is there - see [below](#4-addresses-for-apps).
- **Helm** - charts install through the Helm controller k3s already runs.
- **Adding and removing machines** - **Cluster → Add a host** gives this
  cluster's join lines; removing one gives k3s's uninstall steps.
- **Virtual machines** - with [KubeVirt](https://kubevirt.io) and CDI: add
  `--kubevirt` to the script, or install them later from **Settings → Hardware and storage →
  Add-ons** (see [Virtual machines](Virtual-machines#vms-on-k3s-or-rke2)). The
  machines need hardware virtualisation for VMs to run at full speed.
- **Longhorn later** - started with `--no-longhorn`? **Settings → Hardware and storage →
  Add-ons** installs it once the machines have open-iscsi.

## 1. What you need

- **One or more Linux machines** with a 64-bit OS (Ubuntu Server 24.04 LTS,
  Debian 12, Rocky/Alma 9 or openSUSE Leap are all fine), `curl`, and root
  access. x86-64 or 64-bit ARM.
- **A fixed address for each** - static, or a DHCP reservation on your router.
- **Disk for your data.** Longhorn stores volumes under
  `/var/lib/longhorn` on each machine's system disk to begin with; give it
  more from Homestead later. Plan the disks before installing the OS - see
  [Disks](#disks).
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
curl -sfL https://raw.githubusercontent.com/wjcloudy/homestead/main/scripts/install.sh | sudo sh
```

![The installer's menu](https://github.com/wjcloudy/homestead/releases/latest/download/homestead-tui-menu.png)

Select **Install Homestead**, then **Create a new cluster**, then **k3s**
(the other choice, RKE2, has [its own guide](Installing-on-RKE2)). The installer
checks the machine first (memory, disk, the internet, ports, the hostname, the
clock, the firewall, `/dev/kvm`, an address from DHCP) and stops on anything
that would make the install fail, saying what to put right.

![The checks](https://github.com/wjcloudy/homestead/releases/latest/download/homestead-tui-checks.png)

It asks which address the other machines reach this one on, when it has more
than one, and whether to install Longhorn, the node probe and KubeVirt. Where
the system is on LVM with room, it asks how much of the free space Longhorn
gets as a volume of its own (see [Room for the system](Storage#room-for-the-system)).
It also asks for **an address for Homestead and apps**: an unused LAN address
outside your router's DHCP range. Once kube-vip is running, Homestead reserves
it, makes it the apps' default, and puts itself, its backup storage and shares
on it. Homestead is then at `http://<VIP>:8088` as well as on each node's
address. Leave it empty to keep the nodes' own addresses and add a VIP later
(see [Networking](Networking#homestead-itself-on-a-vip)). `--vip 192.0.2.200`
does the same for `bootstrap-k3s.sh`.
The installation summary then shows the settings and the version of each component:

![The installation summary](https://github.com/wjcloudy/homestead/releases/latest/download/homestead-tui-ready.png)

Each defaults to its current recommended release - k3s's stable channel,
Longhorn's and CDI's newest release, KubeVirt's stable release, Homestead's
newest. To install another version, select the component: the list comes
live from k3s's release channels and from GitHub, and **Enter a version
manually** takes any other.

![Choosing the k3s version](https://github.com/wjcloudy/homestead/releases/latest/download/homestead-tui-versions.png)

Select **Install**, and it takes 5-10 minutes, with a progress bar.

It then:

1. installs what Longhorn needs on the host (`open-iscsi` and an NFS client),
   keeps `multipathd` off the devices Longhorn makes (below), caps the systemd
   journal at 1 GB, and - where the system is on LVM with room - mounts a
   logical volume of Longhorn's own at `/var/lib/longhorn`;
2. installs k3s with an embedded etcd, so more servers can join later;
3. asks k3s to install Longhorn (one copy of each volume, while there is one
   machine) and Homestead's own manifest;
4. asks Homestead to install kube-vip, Multus and the node probe once it is up;
5. waits for Homestead, takes the first etcd snapshot - k3s takes one only every
   12 hours, and until then there is nothing to restore the cluster from - and
   prints its address, `http://<this machine>:8088`.

Open that address and create the first administrator.

The machine's own screen (not SSH) now shows a live status console in place of
the login prompt - press Enter for the usual login. See
[Local host console](Installing-Homestead#local-host-console).

![The host console](https://github.com/wjcloudy/homestead/releases/latest/download/homestead-tui-host-status.png)

The installer runs [`bootstrap-k3s.sh`](https://github.com/wjcloudy/homestead/blob/main/scripts/bootstrap-k3s.sh)
to do this. You can run it yourself instead, with no questions:

```bash
curl -sfL https://raw.githubusercontent.com/wjcloudy/homestead/main/scripts/bootstrap-k3s.sh | sudo sh -s - server
```

Options go after `server`:

| Option | Does |
|---|---|
| `--no-longhorn` | uses k3s's built-in local-path storage instead: simpler, no copies, and each volume stays on the machine it was made on |
| `--kubevirt` | also installs KubeVirt and CDI, so Homestead can run virtual machines - emulated, and slow, if the machine has no hardware virtualisation (`/dev/kvm`) |
| `--k3s-version v1.33.4+k3s1` | pins k3s instead of its stable channel |
| `--longhorn-version v1.9.1` | pins Longhorn instead of its newest release |
| `--kubevirt-version v1.6.0`, `--cdi-version v1.62.0` | pin KubeVirt and CDI instead of their current releases |
| `--homestead-version 2.8.118` | pins Homestead instead of the newest release |
| `--node-ip 192.0.2.10` | the address k3s registers this machine by, when it has more than one |
| `--longhorn-volume 200` | the size in GB of Longhorn's own LVM volume at `/var/lib/longhorn`; `auto` (the default) takes the volume group's free space less a tenth kept for the system, `none` keeps Longhorn on the root filesystem. Works for `agent` and `join` too |
| `--no-node-probe` | leaves out the node probe (temperatures, drive health, each host's network interfaces); add it later under **Settings → Hardware and storage → Add-ons** |

The script is safe to run again: each step finds what the last run left.

## 3. More machines

On each further machine, run the same line:

```bash
curl -sfL https://raw.githubusercontent.com/wjcloudy/homestead/main/scripts/install.sh | sudo sh
```

Select **Install Homestead**, then **Join an existing cluster as a worker
node** (runs apps) or **as a server node** (control plane and etcd as well).
It asks for the first machine's address and the cluster's token, checks it
can reach the cluster before changing anything, and joins. Install the same
k3s version as the existing servers: the summary uses the cluster's version
where the cluster reports it, and otherwise lets you select it.

Or, with no questions, each joins with the first machine's address and its
token. The token is on the first machine:

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
Add a host** in Homestead shows these lines already filled in. Give it a server
machine's **own address**, not the VIP for Homestead and apps: that VIP carries
apps, not the cluster, so the machine joins through a server's address on
6443 (k3s) or 9345 (RKE2).

**How many servers?** etcd needs more than half of its servers up. One server
is fine for a homelab; three survive one failing; two are worse than one,
since losing either stops the cluster.

The script sets Longhorn up with one copy of each volume, which is all one
machine can hold. As machines join, Homestead raises the default for new
volumes with them, up to three - while it is still the installer's one copy, so
a number you chose is kept. Existing volumes keep their count: **Settings →
Hardware and storage → Storage classes** makes a class with three copies the default. **Volumes** shows each volume's copies - `×1` in orange is a
volume a single failed disk would lose - and
[Changing a volume's storage class](Storage#changing-a-volumes-storage-class)
moves an existing one onto the new class.

### A machine that joins later

The node probe, Longhorn, Multus, macvtap and KubeVirt run as DaemonSets and
reach a new machine by themselves. What was set up when the cluster was one
machine does not, so the leader brings each new one into line within ten
minutes of it being Ready:

- **multipathd.** Ubuntu Server runs it, and it claims every plain `/dev/sd*`
  disk - Longhorn's volumes among them, whose mounts then fail as "already
  mounted or mount point busy". As Longhorn asks, `/etc/multipath.conf` gets
  `devnode "^sd[a-z0-9]+"` in its blacklist (a copy of the file is kept), unless
  the machine boots from a multipath device. `iscsid`, installed but stopped, is
  started.
- **kube-vip's interface.** Installed on one machine, kube-vip was told that
  machine's interface. Once the node probes report machines whose interfaces are
  named differently, the name is taken out and kube-vip finds each machine's own.
- **Longhorn's copies**, as above.

### Host limits

A busy Kubernetes node runs out of inotify instances - each file watcher is
one, a Linux host allows each user 128 by default, and on a node nearly
everything runs as root - and whatever starts next cannot watch files. The
script raises them to 8192 instances and 524288 watches in
`/etc/sysctl.d/90-homestead.conf`, and Homestead does the same, once, on
every k3s or RKE2 node that joined before (never lowering a higher value).

### Disks

Longhorn starts on each machine's system disk, in `/var/lib/longhorn`, and
keeps 30% of that filesystem free for the system. Plan the rest before
installing the OS:

| The machine has | At OS install | Then in Homestead |
|---|---|---|
| A second drive | Install on the first; leave the second blank | **Nodes → Disks → Add to Longhorn** on it: formatted (ext4 or XFS) and mounted safely, or kept as it is when it already holds Longhorn data |
| One drive | Choose LVM (Ubuntu Server's default) and give the root volume 64-128 GB; leave the rest of the volume group unallocated | Nothing: the installer mounts a volume of Longhorn's own at `/var/lib/longhorn`. Keep some of the group free for V2 and add it later with **Use free space** |
| One drive, plain partitions | Leave space unpartitioned after the last partition | **Use free space** makes a partition there, live, for V1 or V2 |
| One drive, one root partition filling it | Nothing more to do | Longhorn stays in `/var/lib/longhorn` on the root filesystem, kept from filling it (see [Room for the system](Storage#room-for-the-system)) |

- **Why a filesystem of its own.** Longhorn filling a separate volume cannot
  fill the system's filesystem. **Use free space** keeps a tenth of the
  volume group (at least 10 GB) unallocated, so the root volume can still grow
  (`lvextend -r`).
- **What Homestead will not do.** It never shrinks or moves a partition or
  filesystem of a running system; it only adds a partition in space nothing
  uses.
- **Copies on one drive are not redundancy.** A copy on the system's folder and
  one on `/mnt/longhorn-os` share a drive. For two copies on one machine, add a
  second drive and choose **Copies go on: Different disks** for the class - see
  [Storage](Storage#storage-classes).
- **V2 (SPDK)** needs a whole drive of its own, given to it raw.
- **Safe mounts.** Homestead mounts a drive with `nofail` in `/etc/fstab`, and
  locks the empty folder, so a machine whose drive dies still starts and nothing
  lands on the system disk in its place. See
  [Storage](Storage#booting-with-a-dead-or-missing-drive).

## 4. Addresses for apps

k3s's built-in load balancer, ServiceLB, publishes a LoadBalancer service on
**every machine's own address**. So Homestead answers on
`http://<any machine>:8088`, and each app is reached the same way on its own
port. Two apps cannot both take port 80. The script does not install MetalLB,
and nothing in Homestead needs it.

For an address per app, Homestead installs **kube-vip**, as Harvester uses,
and **Multus**, which [LAN networks](Networking#lan-networks) need - and, once
KubeVirt is installed, **macvtap**, which puts VMs on the LAN through the
machines' own NIC without changing their network (or move a machine's NIC
into a [host bridge](Networking#a-host-bridge)). The script
requests both in Homestead's manifest (a `homestead-install` ConfigMap) and
Homestead installs them through the Helm controller once it starts, at the
chart versions it has tested; progress is shown in its job tray. Leave either
out with `--no-kube-vip` or `--no-multus`, or pin a chart with
`--kube-vip-version 0.11.1` / `--multus-version v4.3.102` (in the guided
installer: `HS_KUBEVIP=no`, `HS_MULTUS=no`, `HS_KUBEVIP_VERSION`,
`HS_MULTUS_VERSION`). Homestead records what it installed, so a component you
remove later is not reinstalled.

An installation from before 2.8.221 shows **Required components not
installed** under **Settings → Hardware and storage → Add-ons** and on **Networking**, with
**Install components**. Both are upgraded under **System → Cluster → Platform
versions**, one minor chart version at a time, keeping the values Homestead
set.

kube-vip runs beside ServiceLB rather than replacing it: it takes only the
Services given a VIP, and everything else - Homestead and Traefik included -
stays on the machines' own addresses. Then:

1. Add the addresses apps may have under **Networking → Your VIPs**, outside
   your router's DHCP range.
2. When deploying, editing, importing or exposing an app, choose **Every
   node's own address**, **New automatic VIP** (the next free one from your
   list) or **Specific VIP**.

kube-vip announces each VIP with ARP from one machine, on the interface the
machines' default route uses; if another machine takes over, the address
follows. To move an existing app to a VIP, remove its Service under
**Networking** and **Expose workload** again with a VIP.

## 5. Without the script

The script only runs k3s's own installer and drops two files into
`/var/lib/rancher/k3s/server/manifests`, which k3s applies itself. To do it by
hand, install k3s your way, install Longhorn (or use `local-path`), and follow
[Installing on an existing cluster](Installing-on-an-existing-cluster).

## Next

[Installing Homestead](Installing-Homestead) covers what the install made,
first sign-in, updates and the node probe.

## If something is stuck

Run the node doctor on the machine: the same line, then **Check node
health**.

```bash
curl -sfL https://raw.githubusercontent.com/wjcloudy/homestead/main/scripts/install.sh | sudo sh
```

It checks the host and the cluster, lists what it found worst first, and
offers a fix for each - see [Troubleshooting](Troubleshooting#node-doctor). By
hand:

- `sudo k3s kubectl get pods -A` - is anything not Running?
- `sudo journalctl -u k3s -e` - k3s's own log.
- Longhorn pods crash-looping usually means `open-iscsi` is missing or
  `iscsid` is not running: `sudo systemctl enable --now iscsid`.
- More in [Troubleshooting](Troubleshooting).

### Multus installation and upgrade diagnostics

Homestead installs both the Multus agents and the separate `rke2-multus-crd` chart,
with version-pinned releases and k3s-specific CNI paths. Add-on readiness requires
the network-attachment API **and** the current DaemonSet available on its scheduled
nodes. A completed Helm job alone is not enough.

For older Homestead installs, **Settings → Hardware and storage → Add-ons → Repair configuration**
corrects the known missing `multusAutoconfigDir` and installs a missing CRD dependency.
It only handles Homestead's recognised configuration, not arbitrary customised CNIs.
The diagnostic command collects both Helm logs and Multus agent errors.

If Homestead's own upgrade waits on `data-permissions`, inspect its init-container
error. `lookup ghcr.io` means host DNS, not a missing app configuration. Check
`resolvectl status`, `resolvectl query ghcr.io`, the host route and outbound HTTPS.
Fix the host's persistent DNS/network configuration before retrying; do not remove
the old serving pod or its volume just to clear an image-pull error.

## If the installer stops before showing a menu

The last message can say `Installing whiptail` even when that package has
finished installing. On Ubuntu 26.04 with sudo-rs, `curl | sudo sh` can leave
terminal-size and menu subprocesses stopped before the menu accepts input. The installer now
asks you to press Enter in the main shell first, so sudo can hand over the
terminal before a menu subprocess uses it. Reports and unattended installs
skip this prompt. Terminal-size queries are read-only, with a 24-row fallback.

For an older installer, download it first so sudo starts with terminal input:

```bash
curl -fL https://raw.githubusercontent.com/wjcloudy/homestead/main/scripts/install.sh -o /tmp/homestead-install.sh
sudo sh /tmp/homestead-install.sh
```

Add `--text` to the saved-script command to use plain prompts.

From another SSH session, inspect the process tree and the package log:

```bash
ps -eo pid,ppid,tty,stat,etime,args --forest | grep -E 'stty|whiptail|apt-get|dpkg|sudo|sh -s'
sudo tail -n 40 /var/log/apt/term.log
```

A `T` in the process state means stopped, rather than downloading packages.
If the log shows `Setting up whiptail` followed by `Log ended`, the package
installation completed. If a package manager is still running, check its
output and locks before attempting another installation; do not delete lock
files. The optional menu-package attempt now shows its output, is limited to
120 seconds plus a 10-second termination grace period, and falls back to text
prompts on failure. If interrupted during package configuration, inspect the
package-manager state (`sudo dpkg --audit` on Debian/Ubuntu) before retrying.

To trace startup without changing the cluster, save the script and run it
with `--dry-run` (exit at the first menu):

```bash
curl -fL https://raw.githubusercontent.com/wjcloudy/homestead/main/scripts/install.sh -o /tmp/homestead-install.sh
sudo sh -x /tmp/homestead-install.sh --dry-run
```

## Cluster details in setup

On an existing node, setup's main menu shows the host IP, Kubernetes node
versions, cluster members and internal IPs, the configured API endpoint, service
VIPs, and the Longhorn manager and Homestead deployment readiness. Select
**Cluster details** for the full lists. Members and VIPs wrap in the main menu;
when they exceed the terminal height, **More member/VIP addresses** cycles
through the remaining lines while component status and actions stay visible.
The summary refreshes whenever you return to the main menu.

Assigned LoadBalancer addresses are distinguished from requested addresses that
are still pending. The API endpoint can be a local address such as
`127.0.0.1:6443`; it is not necessarily a control-plane VIP. Component versions
come from workload image tags; a digest-pinned image may not expose a version
tag. Ready/desired replicas describe the component, not overall volume health.
Use **Check node health** for the broader checks.

Discovery is read-only, with short timeouts. Unreachable or unauthorized queries
show **unavailable**, while a successful lookup with no component shows
**not installed**. Workers without administrator credentials direct you to run
setup on a server node.
