# Troubleshooting

Start with **Settings → Homestead**: it shows which background parts of Homestead are
working and the last error of any that are not. The job tray (bottom right)
keeps every job's steps and errors across restarts.

## Node doctor

On any node of a k3s, RKE2, Harvester or plain Kubernetes cluster:

```bash
curl -sfL https://raw.githubusercontent.com/wjcloudy/homestead/main/scripts/install.sh | sudo sh
```

Select **Check node health**. It looks at:

- **The host:** the Kubernetes service, disk and inodes, memory, the clock,
  `iscsid` and multipath (Longhorn needs one, and is broken by the other),
  certificate expiry, containerd.
- **The cluster,** where kubectl reaches it (a server): the API, etcd, this
  node (Ready, cordoned?), the other nodes, pods failing or stuck terminating,
  failed pods left behind, Longhorn volumes, CoreDNS, Homestead, and how old
  the newest etcd snapshot is.

![The node doctor](https://github.com/wjcloudy/homestead/releases/latest/download/homestead-tui-doctor.png)

The results are listed by severity: `[FAIL]`, `[WARN]`, then `[ OK ]`.
Selecting one shows what is wrong and why it matters, and offers its fix,
asking first.

![A finding and its fix](https://github.com/wjcloudy/homestead/releases/latest/download/homestead-tui-doctor-fix.png)

The fixes restart a stopped service (which also renews
certificates due within 90 days), turn on time sync or `iscsid`, uncordon the
node, delete failed pods, force-delete pods stuck on a node that is gone,
restart CoreDNS or Homestead, blacklist Longhorn's devices from multipath, or
clean up a full disk. **Apply all safe fixes** applies the safe ones
together.

The main menu also has **Clean up disk space** (unused images, the journal to
200 MB, failed pods, etcd snapshots beyond the ten most recent), **Take an etcd
snapshot**, and on a k3s server **Restore from an etcd snapshot**. That
stops k3s, resets the cluster to the snapshot you choose - after you type
RESTORE - and starts it again. Everything since that moment is lost, but
volumes' data is not in the snapshot; Longhorn keeps that.

`--report` prints the results and exits 0 (healthy), 1 (warnings) or 2
(failures), for cron. `--fix-safe` applies the safe fixes with no
questions. Every fix is written to `/var/log/homestead-doctor.log`.

## Homestead

**The page says "Waiting for the cluster".** Homestead cannot read its user
store from Kubernetes - usually the API server is restarting (an upgrade, a host
rebooting). It retries by itself; the page comes back when the API does.

**Through a Cloudflare tunnel, Homestead shows Cloudflare's "502 Bad gateway ·
Host Error" while other apps work.** The browser is set to show another
linked cluster (the switch in the top bar), and that cluster isn't answering
this one. Cloudflare swaps Homestead's own "is not answering · Back to this
cluster" page for its error page. Open `/api/fleet/home` on the same address
to come back to this cluster. Since 2.8.253, a cluster choice this Homestead
doesn't know, such as one left over from a Homestead that used to answer at
the address, is forgotten by itself. The "not answering" page is served as a
503, which Cloudflare shows as it is.

**An update is stuck with "invalid controller count".** Longhorn refuses a
second pod mounting Homestead's data volume on a migratable class while the old
pod still has it. Scale to zero, let the volume detach, then back to one:

```bash
kubectl -n lab scale deployment/homestead --replicas=0
kubectl -n lab scale deployment/homestead --replicas=1
```

Current releases replace Homestead with the Recreate strategy, which avoids
this.

**Settings says Homestead cannot update its role.** Grant it once, as shown
there:

```bash
kubectl apply -f https://raw.githubusercontent.com/wjcloudy/homestead/main/deploy/rbac.yaml
```

## Containers

**A VPN container logs `RTNETLINK answers: Operation not permitted`.** It needs
the tunnel device: **Edit → Privileges → VPN tunnel**. See
[Containers](Containers#privileges).

**A container is stuck in ContainerCreating.** Its row says why. The usual
causes:
- a ReadWriteOnce volume already attached on another host - stop the other user,
  or make the volume shareable;
- an RWX volume on a migratable class - see [Storage](Storage#storage-classes);
- a hardware feature no host has.

**A container stays Pending, "unscheduled".** Its card says why the scheduler
cannot place it: not enough free memory or CPU on any node, pinned to a host
that cannot take it, or a port taken. On k3s, an app on the **host network**
with a LoadBalancer Service of its own blocks itself: ServiceLB holds the
Service's ports on every node, the same ports the app needs there. It needs no
Service - it answers on the node itself - so remove the Service under
**Networking**. Moving such an app to k3s leaves the Service behind.

**A container runs but its update check failed.** The registry could not be
reached or needs credentials; that container shows the error, and the others
still show their updates.

## Storage

**New volumes come up degraded, or growing one is refused.** Longhorn has run out
of allocation on a node - see [Longhorn allocation](Storage#longhorn-allocation).
Add a disk, remove old copies, or raise over-provisioning in **Settings →
Cluster**.

**A volume is degraded for hours.** Its row says why - usually a copy that
cannot be placed because too few hosts have room.

**A drive died.** Volumes keep running on their other copies. The node's
disk shows as failed with the reason; **Replace failed disk** lets go of it so
volumes can rebuild, then add the new drive. See
[Storage](Storage#when-a-drive-fails).

**A k3s machine stops at an emergency shell after a drive died.** Its
`/etc/fstab` line for the drive lacks `nofail`. At the emergency prompt, run
`nano /etc/fstab`, add `nofail` to that line's options (after `defaults,`),
save, and `reboot`. The machine starts without the drive, and Homestead shows
the disk as failed.

## Networking

**"No free address"** when exposing an app or making a share. Nothing is
handing out addresses: add some under **Networking → Workload VIPs**. See
[Networking](Networking#add-default-and-choose-workload-vips).

**New hosts cannot join, though the dashboard works.** Something else is on the
cluster's own address - the VIP hosts join through on port 9345. Homestead
names any app sitting there on **Networking** and in **Settings → Homestead →
Addresses**; give each an address of its own (**Edit → Network**). If
Homestead's own shared address (`LB_IP` on its Deployment) is the cluster's
address, change it to a free one. Homestead no longer offers the cluster's
address, or one another program owns, anywhere it asks for one.

**An app's address does not answer.** **Networking → Nodes & addresses**
states each address's condition:

- **not reachable** - a node answers for it (ping works) but its Service does
  not carry it, so the ports are refused. Homestead records the address for
  kube-vip within 30 seconds and says so above the cards; if it persists, an
  alert is raised.
- **no node answers** - its apps are running but the load balancer is not
  announcing it: check that kube-vip is running and that the Service has its
  load balancer class.
- **nothing running** - no pod behind it is ready, so no node announces it.
- **port taken** - ServiceLB could not publish the Service on the nodes'
  addresses because another Service holds the port.

On Harvester, also check the address is not given out by your router's DHCP.

**kube-vip or Multus is missing on k3s or RKE2.** **Settings → Hardware and storage →
Add-ons** and **Networking** show **Required components not installed** with
**Install components**. A new installation gets both after Homestead starts;
the job tray shows the progress.

## VMs

**On k3s, a VM's disk is refused: "cannot get access mode from StorageProfile
local-path".** CDI knows nothing of k3s's `local-path` class. From 2.8.154
Homestead spells out the access mode for such a class; delete the VM with its
disk and create it again.

**A VM waits at "Provisioning" or "ImportScheduled".** The VM's card says why
after three minutes - most often the image URL is wrong or unreachable from the
cluster, or its volume cannot be scheduled. **Edit → Disks** can give a failed
disk a new source.

**A VM from a downloaded image stays Unschedulable: "pod has unbound
immediate PersistentVolumeClaims".** Releases before 2.8.132 guessed the name
of the storage class Harvester makes for a downloaded image, and newer
Harvester names it differently. Delete the VM with its disk and create it
again: the image is already downloaded, so it is quick.

### GPU passthrough

**The VM runs, but the physical monitor stays blank.** Work through these
checks in order:

1. Connect the monitor to the GPU assigned to the VM, select the correct input,
   and check the cable. A motherboard video port belongs to different hardware.
2. On the GPU's host, **Hardware → Devices for VMs → Refresh devices** should
   show IOMMU on and the card handed to VMs. Confirm the VM's **Passthrough**
   tab contains that GPU resource and that it started on a host offering it.
3. In **VM → Edit → Hardware → Devices**, choose **Passed-through GPU (physical
   monitor)** as **Primary boot output**. This selects UEFI and disables virtual
   VGA. Save and restart the VM; adding the GPU alone does not choose its boot
   display. A BIOS-installed guest may need a UEFI bootloader repair.
4. If the card needs an explicit ROM, capture it on the host and select the
   downloaded file beside the GPU in **Edit → Passthrough**. Use a ROM from the
   actual card that supports UEFI, then restart the VM. Output that disappears
   when the guest OS takes over also needs the guest's GPU driver checked.

With physical GPU output selected, a blank or unavailable **Screen** (VNC)
console is expected. Use **Serial** if the guest is configured for it, or an
existing guest SSH/remote-desktop connection. Changing back to **Web console
(virtual display)** restores the virtual display at the next start; UEFI stays
enabled. With several GPUs, Homestead does not select a specific connector.

**"GPU ROM reading failed" or the ROM cannot be read.** Stop VMs using the card's
IOMMU group and give the card to VMs before capture. Homestead refuses to detach
an active host display driver during capture. Current releases wake an idle,
runtime-suspended VFIO GPU for the read and restore its power policy afterwards.
Older releases could fail on these cards even when the ROM existed in sysfs.
Update Homestead and try again. If the card still exposes no readable ROM,
upload a dump from that card instead; see
[ROM requirements](Virtual-machines#a-gpus-rom-vbios).

**Only PCI IDs appear in the VM device picker.** Inspect or refresh the host
under **Hardware → Devices for VMs**, then reopen VM Edit. Homestead retains
the inspected model names; the PCI resource ID still identifies what the VM
requests. See [passthrough setup](Virtual-machines#pci-and-usb-passthrough).

**"Device ... is in use by VM ...".** Multiple stopped configurations may use
the same device, but starting requires a free physical allocation. Stop the
named holder and wait for its instance to release the device, or choose another
available resource/host. A stable running VM can keep its own GPU and USB devices
when editing. If an older release reports that VM as its own conflict, update
Homestead and reopen the edit. A **stable, verified resident launcher** error
can still occur during startup, shutdown or migration: wait, refresh, and
review again. See [device sharing rules](Virtual-machines#several-vms-configured-for-the-same-device).

## Moves between clusters

**The backup storage card says "no LAN address".** The S3 server on the source
needs an address on your LAN - one of the source cluster's VIPs. Pick one on the
card, and Homestead checks that the destination can reach it.

**A VM move fails with "no default storageClass found for backingImage".**
Harvester sets up a restored image from a storage class, and releases before
2.8.122 left it to use the cluster's default - which this cluster did not
have. Update Homestead here and **Retry**; it now names a Longhorn class
itself.

**A move failed at "backup".** Update the source cluster's Homestead, then
**Retry** - a failed step picks up where it stopped.

## Asking for help

[Open an issue](https://github.com/wjcloudy/homestead/issues) with the
Homestead version (Settings → Homestead), the cluster type, and the job's steps from
the job tray.
