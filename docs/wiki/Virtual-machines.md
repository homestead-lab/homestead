# Virtual machines

**VMs** runs virtual machines with [KubeVirt](https://kubevirt.io) - built into
Harvester, and something you can add to k3s or RKE2. The page appears when the
cluster has KubeVirt.

It has two tabs: **Machines**, the VMs here, and **Import**, which brings VMs
across [from an Unraid server](Importing#vms-from-unraid), settings and disks,
and [disk images](Importing#a-vm-disk-image) from a web address.

![VMs](https://github.com/wjcloudy/homestead/releases/latest/download/homestead-vms.jpg)

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
  Ctrl+Alt+Del and full screen. On a phone or tablet, **Keyboard** opens the
  device's on-screen keyboard and types into the VM; it stays open while you tap
  the screen to click. **Paste** (or Ctrl+Shift+V on the screen)
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

![New VM](https://github.com/wjcloudy/homestead/releases/latest/download/homestead-vm-new.jpg)

**＋ New VM** asks for a name, CPU cores, memory, a disk size, a root password
and a boot disk:

- **Boot disk** - an image from the [image store](#image-store), a Harvester
  image, an image downloaded from a URL, a disk you
  [imported](Importing#a-vm-disk-image), or blank, to install from an ISO added
  later as a CD-ROM.
- **Root password** - set through cloud-init, so a cloud image has a login
  from its first boot. Optional for an imported disk that already has one.
- **Storage class** - on Harvester, a disk from an image lives on that image's
  own class, and every disk can live-migrate between hosts. On other clusters
  the disk goes on the class you pick; on local-path the VM stays on the host
  its disk is on.

**Guest type** sets the firmware, TPM, clock and devices as Proxmox's OS type
does, and the **network card**: VirtIO for Linux, an Intel e1000e for Windows.
Windows has no VirtIO network driver of its own, so a Windows guest on VirtIO
comes up with no network until the guest tools from `virtio-win.iso` are
installed; Windows drives an e1000e out of the box (Proxmox gives its Windows
types an Intel e1000 for the same reason). For a Windows VM made before this,
**Edit → Network** changes its card to e1000e, or install the guest tools from
its drivers CD and keep VirtIO.

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

No VM network yet? **Networking → ＋ LAN network** makes one. On Harvester:
untagged, on the same LAN as the hosts, or on a VLAN your switch carries to
them - the same network Harvester's dashboard makes under *Networks → VM
Networks*, so it shows there too. On k3s or RKE2: on a host bridge, or on the
hosts' NIC **For: Virtual machines**, through macvtap - see
[LAN networks](Networking#lan-networks) and
[A host bridge](Networking#a-host-bridge). Any form that needs one offers to
make it, and comes back once it is made.

## Image store

**Image store** on the VMs page lists cloud images from their publishers, for
your nodes' architecture (amd64 or arm64), grouped by distribution. Each
variant shows its download size, and a tooltip says what it is for:

| Distribution | Variants |
|---|---|
| Ubuntu 24.04 and 22.04 LTS | **Server**, and **Minimal**: smaller, fewer packages, no manuals |
| Debian 13 and 12 | **Cloud**: a kernel slimmed for VMs; **Generic**: every driver, for passed-through hardware |
| Fedora Cloud | the newest release |
| Rocky Linux 10 and 9 | **Base**, and **LVM**: the disk under LVM |
| AlmaLinux 10 and 9, CentOS Stream 10 and 9 | the standard cloud image |
| openSUSE Leap 16.0, 15.6 and Tumbleweed | minimal cloud VM |
| Arch Linux | cloud image |
| Alpine Linux | cloud-init image: tiny, musl and OpenRC |

Each comes from the publisher's own "latest" address, has cloud-init, and
grows to fill its disk. The table shows which user to sign in as (`ubuntu`,
`debian`, `alpine`...), with the password set on **New VM**. **Filter** narrows
the list, for example to "minimal". **New VM** on a row opens the form with
that image chosen.

**Your images on this cluster** lists the VM images already here that did not
come from the catalogue: uploads, other downloads, anything made in
Harvester's dashboard. Each has **New VM**. An image downloaded from a
catalogue address, even outside the store, shows on that catalogue row as
**here**.

On Harvester, **Keep** makes an image a local Harvester image. It uses one
already downloaded from the same address, or downloads it. Every VM then
copies the local image instead of downloading it again. A kept image with
**current** ticked checks for a newer build twice a day, by asking the
publisher (nothing is downloaded until there is one). A new build downloads
beside the old; once it is ready, new VMs use it, and older builds that no
disk was made from are deleted. **Check for newer builds** asks now.

On k3s and RKE2, CDI fills each VM's disk straight from the publisher, so a
new VM always starts from the newest build and there is nothing to keep.

Your own disk image (qcow2, vmdk, raw, vdi, vhd or vhdx) is
[imported](Importing#a-vm-disk-image) from a web address.

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

**Include KubeVirt** installs KubeVirt and CDI on the new cluster too, so its
Homestead can run VMs of its own. The nodes then get this host's CPU as it
is, so VMs inside run with hardware virtualisation where this host allows
nested virtualisation; where it does not, they are emulated. Give the nodes
more memory for it.

**Log** on the job shows each step it has taken, and each node's console as it
installs - cloud-init, then k3s and what comes with it. The nodes ask KubeVirt
to keep their console output, which it does from 1.1 even where Harvester
turns that off for other VMs; with an older KubeVirt, the log says so and
where to look instead.

Interrupted or failed creation keeps partial VMs, disks and Secrets. Open the
job's **Inspect outcome** action to compare recorded creation receipts with the
current resources. An administrator can resolve tracking after inspecting the
resources and typing the batch name. This does not delete resources, resend a
request, cancel an upstream operation or prove that the guest cluster is ready.
Remove unwanted resources deliberately after checking their ownership and data.

## Placement checks before starting

Create, start, restart, resume and start-capable edits show a capacity review.
High or unknown RAM usage can be acknowledged. Missing hardware, storage conflicts,
changed resource identities and insufficient scheduler capacity cannot be bypassed.
The server rechecks before writing; the review is not a scheduler reservation.

**Launch host** identifies a required host, a preferred host, or the eligible
hosts Kubernetes can choose from at launch. A preference allows another eligible
host. Resuming a paused VM keeps its current host; restarting can reschedule it.

For NUMA VMs, enable **Settings → Hardware and storage → Add-ons → Node probe → VM placement
checks**. The short dialog explains host access and the monitoring restart; socket
settings are under **Advanced**. It uses an additional 32–96 MiB per probe node.
**Check hosts** explains missing or unsupported evidence. Workloads are not
restarted, and disabling checks does not delete their volumes.

Locality checks need Static CPU and memory managers. Multi-NUMA hosts also need
pod-scoped `single-numa-node` topology policy. Homestead verifies current policy,
exclusive CPU allocations and local memory/hugepages together; it does not configure
kubelet policy for you. Dynamic claims and unverified device-local NUMA placement
remain unsupported and blocked. Kubernetes still makes the final placement.

Power requests and saves are journalled in Recent jobs. If a response is lost,
inspect the outcome before trying again; Homestead does not automatically replay
an uncertain mutation. During an HA upgrade, finish upgrading all Homestead replicas
before making VM changes. Test shared-storage locking and guest-agent compatibility
on disposable resources before relying on them for production recovery.

## Edit

![Edit VM](https://github.com/wjcloudy/homestead/releases/latest/download/homestead-vm-edit.jpg)

**Edit** covers what a VM is made of:

- **General** - cores, memory, run strategy, description, a host to keep it on;
- **Hardware** - the virtual machine's hardware, in folded sections that each
  show what they are set to (see [Hardware](#hardware));
- **Disks** - boot order, bus, growing a disk, detaching one (its volume is
  kept), adding a disk or CD-ROM - from the [ISO library](#iso-library), a
  Harvester image or a URL - and a new source for a disk that failed to
  download;
- **Network** - model, network, MAC, adding and removing cards;
- **Cloud-init** - user and network data.

Changes apply at the next boot, or at once with **Restart now so the changes
take effect**. **Edit
YAML** opens the VM in the Resources editor for anything else.

## Hardware

The **Hardware** tab keeps each group folded, with its settings on one line:

- **Processor** - sockets, cores per socket and threads; the CPU model
  (host-model, host-passthrough, or a named model every node offers, which
  keeps the VM movable across mixed hardware); dedicated CPUs, pinned one to
  one as Proxmox's affinity does; and an isolated emulator thread.
- **Firmware and security** - BIOS or UEFI, Secure Boot, kept EFI variables,
  a virtual TPM 2.0 (on, or with its state kept), and the machine type.
- **Guest tuning** - Hyper-V enlightenments for Windows, hiding KVM from GPU
  drivers that refuse to run in a VM, and the clock's time zone (UTC, or local
  time for Windows).
- **Devices** - the display the web console shows, the serial console, a
  tablet pointer, a random-number device, the memory balloon, a sound card.
- **Memory and placement** - hugepages, and what happens when its node is
  drained: live-migrate, try to, or stop.

**Guest type** applies a preset, as Proxmox's OS type does: **Windows 11 /
Server 2022+** (UEFI with Secure Boot, a kept TPM, Hyper-V enlightenments, a
tablet pointer, local time), **Windows 10 / Server 2019**, **Linux server**,
**Linux desktop**, **Linux cloud image (BIOS)** and **Headless appliance**. A
preset only fills in the fields; nothing changes until the edit is reviewed
and saved, and only the fields that changed are written. The same presets are
the **Guest type** of a new VM.

What a setting needs from the cluster is said as it is chosen: dedicated CPUs
need the kubelet's static CPU manager policy, hugepages must be reserved on
the node, host-passthrough limits live migration to identical CPUs, and kept
EFI or TPM state needs KubeVirt's VMPersistentState feature before KubeVirt 1.5.
Settings KubeVirt would refuse - Secure Boot on BIOS, say - are refused before
anything is saved.

## PCI and USB passthrough

A VM can have a host's own PCI device - a GPU, a NIC, an HBA - or a USB device.
Two steps: the host hands the device over, then the VM asks for it.

**On the host** - **Nodes → select a host → Hardware → Devices for VMs → Look at its devices**:

- **IOMMU** has to be on. On k3s and RKE2, **Switch IOMMU on** adds
  `intel_iommu=on iommu=pt` (`iommu=pt` on AMD, whose IOMMU is on by default)
  to the kernel command line - a copy of `/etc/default/grub` is kept - and it
  is on from the host's next restart, from Host actions. The firmware needs
  VT-d or AMD-Vi enabled too.
- **Give to VMs** hands a PCI device to `vfio-pci` at once and at every boot,
  with every device in its IOMMU group (a GPU's audio function, say; PCI
  bridges stay), and lists it with KubeVirt as `homestead.io/pci-<vendor>-<device>`.
  A device carrying the host's network, or with a disk the host has mounted, is
  refused. **Give back** returns it to its own driver.
- **Offer to VMs** on a USB device lists it with KubeVirt by vendor and product
  (`homestead.io/usb-<vendor>-<product>`), on any host that has one; nothing on
  the host changes.
- On **Harvester**, the same buttons make and remove Harvester's own
  PCIDeviceClaims and USBDeviceClaims, as its Devices page does. Its
  pcidevices-controller add-on must be enabled.

**On the VM** - **New VM → Passthrough** or **Edit → Passthrough**: **Add device** from what the hosts offer,
each shown with the hosts that have it. A VM with a host device runs only on
such a host and cannot live-migrate; the change applies at its next start.
The edit form also lets you select a different resource for an existing device.
Host preparation opens in a separate tab so the VM configuration stays in place.

### A GPU's ROM (vBIOS)

Some GPUs need their ROM given to the VM - a card the host booted from, which
hides its ROM afterwards, or one that needs a patched ROM. On the device's row,
choose the ROM file when adding the device or editing it: up to 640 KiB, starting with the PCI ROM signature `55 AA`
(a dump from GPU-Z or `nvflash` may carry a header to trim first). KubeVirt has
no field for this, so Homestead uses its hook sidecar: a small script in a
ConfigMap of the VM's (`<vm>-vbios`), with the ROM inside, runs as KubeVirt
defines the VM, writes the ROM where the VM's QEMU can read it, and names it as
that device's ROM - changing nothing if anything goes wrong. It switches on
KubeVirt's `Sidecar` feature. **clear** removes it; removing the device removes
its ROM.

Clearing a ROM removes its contents and the VM's hook reference; the empty
managed ConfigMap is retained for inspection and reuse. Several ROM files
must fit together within the ConfigMap's 1 MiB limit. If a saved ROM is missing,
replace or clear it explicitly in the edit form; unrelated edits keep it intact.

### Hardware during a cluster transfer

**Copy to cluster** and **Move to cluster** show a passthrough mapping for each
source device. Choose a resource offered by the destination's hosts, or
**Leave out**. Selected devices must be available on a common host. Prepare
IOMMU and devices on that host before starting the transfer.

For each retained device, keep the source vBIOS, use the destination device's
default ROM, or upload a replacement. A different GPU may require a different
ROM. Homestead copies managed ROM data and rebuilds its hook ConfigMap on the
destination. It removes the source hostname selector; other placement rules
remain. Custom hook sidecars require separate dependency setup and are refused
by this transfer flow. The source's device settings are unchanged.

The **Passthrough** picker shows inspected device models, vendor/product IDs,
hosts, PCI addresses and IOMMU groups. On k3s/RKE2, devices offered before
Homestead retained inventories may initially show only their IDs: inspect or
refresh that host under **Hardware → Devices for VMs** once to populate their
names. Harvester device names come directly from its inventory. Names and
addresses describe the hardware; the selection still requests a KubeVirt
resource, which can represent matching devices on several hosts. Current host
availability comes from the cluster, independently of the retained names.

## ISO library

**ISO library** (on the VMs page) lists the `.iso` files in folders you pick
on your [Network shares](Network-shares) - copy installers there from your PC
over SMB. **＋ Folder** chooses a share, then a folder on it; nothing else is
set up. A CD-ROM cannot read one file on a shared volume, so each ISO is made
ready once: **Make ready** copies it into a volume of its own, on a storage
class every node can mount where there is one, so one copy serves every VM on
any node. Its progress shows in the library; a file replaced by a new one of
another size is copied again under a new name.

On Longhorn the copies go on a class Homestead makes for them,
`homestead-isos`: **one replica**, since the original stays on the share, and
no data locality. VMs on any node still read the one copy - it is served over
the network, so nothing is tied to the node holding the replica. If that node
is down, a VM with the ISO still in a drive waits until it is back, or until
the ISO is ejected or made ready again; a VM whose install is done and ISO
ejected is not affected.

Copies are kept only while wanted: one no VM has in a drive is removed after
the days set at the foot of the library (7 by default, **0** keeps them), and
the library says how long each has left. It comes back with **Make ready**.

A ready ISO is offered as a CD-ROM's contents in **Edit → Disks → ＋ CD-ROM**,
attached read-only, and **Detach** ejects it. A new VM can **Install from an
ISO**: it gets a blank disk and the ISO in a CD-ROM drive that boots first,
with no root password - the installer asks for its own. An ISO's volume a VM
still has in a drive cannot be deleted; deleting one keeps the file on the
share.

## Delete

**Delete** asks for the VM's name and whether to take its disks too. Disks you
keep are released from the VM first, so they are not deleted with it. A VM
deleted while its image is still downloading stops the download.

## VMs on k3s or RKE2

VMs need [KubeVirt](https://kubevirt.io), and
[CDI](https://github.com/kubevirt/containerized-data-importer) to fill their
disks from images. Three ways to get both:

- **A new k3s cluster:** add `--kubevirt` to the k3s script's `server` line -
  see [Installing on k3s](Installing-on-k3s).
- **A cluster already running:** **Settings → Hardware and storage → Add-ons → Install
  KubeVirt**, or **Install KubeVirt** on the VMs page. Homestead installs the
  newest KubeVirt and CDI releases through the Helm controller k3s and RKE2
  run, and the VMs page appears once they are up.
- **By hand**, following [KubeVirt's](https://kubevirt.io/user-guide/cluster_admin/installation/)
  and CDI's instructions.

Hosts need hardware virtualisation (`ls /dev/kvm` shows it) for VMs to run at
full speed. The node probe reports it on each node, and Add-ons says which
have it. With none, KubeVirt is set to emulate: VMs work, many times slower.
Without CDI, VMs start from a blank disk and downloading images asks for CDI
first.
