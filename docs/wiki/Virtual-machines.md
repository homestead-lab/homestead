# Virtual machines

**VMs** runs virtual machines with [KubeVirt](https://kubevirt.io) - built into
Harvester, and something you can add to k3s or RKE2. The page appears when the
cluster has KubeVirt.

![VMs](https://github.com/wjcloudy/homestead/releases/latest/download/homestead-vms.png)

## Each VM

A VM shows what it is really doing - Running, Stopped, Starting, Paused, or an
error with its reason - and offers what fits: **Start**; **Stop**, **Restart**,
**Pause** or **Console** for a running one; **Force stop** for one that will
not shut down. It opens to its disks, network cards and addresses, the guest OS
(when the guest agent runs), and recent events.

A VM whose disk is still downloading shows how far it has got; one whose
download has not started after three minutes says why.

## Console

**Console** has two views:

- **Screen** - the VM's display (VNC) in the page, scaled to fit, with
  Ctrl+Alt+Del, full screen, and **Type text** to type a password or a long
  command for you, since a VM has no shared clipboard.
- **Serial** - the first serial port, as a terminal, for VMs that boot without
  a display (most cloud images).

## New VM

![New VM](https://github.com/wjcloudy/homestead/releases/latest/download/homestead-vm-new.png)

**＋ New VM** asks for a name, CPU cores, memory, a disk size, a root password
and a boot disk:

- **Boot disk** - a Harvester image, an image downloaded from a URL (an Ubuntu
  or Debian cloud image, say), a disk you [imported](Importing#a-vm-disk), or
  blank, to install from an ISO added later as a CD-ROM.
- **Root password** - set through cloud-init, so a cloud image has a login
  from its first boot. Optional for an imported disk that already has one.
- **Storage class** - on Harvester, a disk from an image lives on that image's
  own class, and every disk can live-migrate between hosts. On other clusters
  the disk goes on the class you pick; on local-path the VM stays on the host
  its disk is on.

The VM starts on the pod network; **Edit → Network** puts it on a bridged
network (Harvester's VM networks, or any Multus network), where it gets an
address from your router. **Edit → Cloud-init** takes SSH keys, users and
packages.

### An address of its own

On the **pod network** a VM is reached through a Service, like a container.
On a **VM network bridged to the LAN** it is a machine there like any other:
choose **Address → One of its own** and pick one of the free addresses
[IP addresses](Networking#ip-addresses) knows of in that subnet - outside the
DHCP range and the VIP pools. It is written into cloud-init's network config
(matched to the VM's MAC, so it lands on the right interface whatever the
guest calls it), checked against everything already on the network, and
recorded in IP addresses under the VM's name. Cloud-init - the password
included - is kept in a Secret, not in the VM's own definition.

On Harvester, a VM network is made in its dashboard: **Networks → VM
Networks → Create**, an *Untagged Network* on the `mgmt` cluster network (or
an *L2 VLAN Network* with your VLAN's ID).

## A k3s cluster of VMs

**＋ k3s cluster** makes a small k3s cluster from VMs here - for trying k3s,
an app, or Homestead itself on a cluster of its own. Choose how many servers
(one, or three to survive one failing) and workers, their size, the image
(Ubuntu 24.04 by default), the VM network and one address each. **Review**
says what goes where and names anything already at an address; **Create
cluster** makes the VMs with a join token made for them, and the job tray
follows the cluster coming up - VMs running, k3s answering, then its own
Homestead at `http://<first address>:8088`. It can run k3s with Longhorn and
Homestead (what a new install gets), k3s and Homestead on local-path storage,
or k3s alone. The nodes' login is `ubuntu` with the password you chose.

## Edit

![Edit VM](https://github.com/wjcloudy/homestead/releases/latest/download/homestead-vm-edit.png)

**Edit** covers what a VM is made of:

- **General** - cores, memory, run strategy, description, a host to keep it on;
- **Disks** - boot order, bus, growing a disk, detaching one (its volume is
  kept), adding a disk or CD-ROM, and a new source for a disk that failed to
  download;
- **Network** - model, network, MAC, adding and removing cards;
- **Cloud-init** - user and network data.

Changes apply at the next boot, or at once with **Restart now so the changes
take effect**. **Edit
YAML** opens the VM in the Resources editor for anything else.

## Delete

**Delete** asks for the VM's name and whether to take its disks too. Disks you
keep are released from the VM first, so they are not deleted with it. A VM
deleted while its image is still downloading stops the download.

## VMs on k3s or RKE2

Install [KubeVirt](https://kubevirt.io/user-guide/cluster_admin/installation/)
and, for disk images, [CDI](https://github.com/kubevirt/containerized-data-importer),
following their instructions; hosts need hardware virtualisation (`ls
/dev/kvm` shows it). Reload Homestead and the VMs page appears. Without CDI,
VMs start from a blank disk and downloading images asks for CDI first.
