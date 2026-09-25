# Virtual machines

**VMs** runs virtual machines with [KubeVirt](https://kubevirt.io) - built into
Harvester, and something you can add to k3s or RKE2. The page appears when the
cluster has KubeVirt.

![VMs](https://github.com/wjcloudy/homestead/releases/latest/download/homestead-vms.png)

## Each VM

A VM shows what it is really doing - Running, Stopped, Starting, Paused, or an
error with its reason - and offers what fits: **Start**; **Shut down**,
**Restart**, **Pause** or **Console** for a running one; **Force off** for one
that will not shut down. **Shut down** asks the guest to power off, as its own
power button would; **Force off** cuts the power at once, like pulling the
plug, so unsaved work in it is lost. It opens to its disks, network cards and addresses, the guest OS
(when the guest agent runs), and recent events.

A VM whose disk is still downloading shows how far it has got; one whose
download has not started after three minutes says why.

## Console

**Console** has two views:

- **Screen** - the VM's display (VNC) in the page, scaled to fit, with
  Ctrl+Alt+Del and full screen. **Paste** (or Ctrl+Shift+V on the screen)
  types the clipboard into the VM key by key - a VM's display has no clipboard
  of its own - with Enter for each new line. Over HTTPS it types straight
  away; on a plain `http://` address the browser does not let the page read
  the clipboard, so a box opens to paste into first, with **Press Enter
  after** for a command. Plain Ctrl+V and Ctrl+C still go to the VM. When the
  VM shares what it copies (a guest with a clipboard agent), **Copy from VM**
  (Ctrl+Shift+C) puts it on yours.
- **Serial** - the first serial port, as a terminal, for VMs that boot without
  a display (most cloud images). Its output is plain text: select it and press
  Ctrl+C, or **Copy** (Ctrl+Shift+C) for the selection or everything.
  **Paste** (Ctrl+Shift+V) sends the clipboard to the port as typed, a line at
  a time. Ctrl+C with nothing selected interrupts, as in a terminal.

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

No VM network yet? **Networking → ＋ VM network** makes one on Harvester:
untagged, on the same LAN as the hosts, or on a VLAN your switch carries to
them. Any form that needs one offers to make it, and comes back once it is
made. It is the same network Harvester's dashboard makes under *Networks → VM
Networks*, so it shows there too.

## The list

Each VM shows its address in full - with a button to copy it, how many more
it has, and the network it is on - then its size and host on one line. A
running VM also shows what it is using: CPU against its cores, memory against
what it was given, and how fast it is reading from and writing to its disks.
CPU and memory are its launcher pod's, from the metrics API; disk traffic is
KubeVirt's own count, read from virt-handler on each node every half minute.
What cannot be measured shows a dash that says why. The switch at the top
shows the same as rows, one VM a line.

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

**Log** on the job shows each step it has taken, and each node's console as it
installs - cloud-init, then k3s and what comes with it. The nodes ask KubeVirt
to keep their console output, which it does from 1.1 even where Harvester
turns that off for other VMs; with an older KubeVirt, the log says so and
where to look instead.

**Cancel** on the job, while the cluster is still coming up, deletes its VMs
with their disks and frees their addresses in IP addresses, after the cluster's
name is typed. Only VMs carrying the cluster's label are deleted. A build that
fails part-way, one VM not made, removes the ones it had made already. A build
that failed later - the cluster never came up - keeps its VMs so you can read
their logs; **Clean up** on the failed job removes them, their disks and their
addresses, and the job stays in the list as failed.

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
