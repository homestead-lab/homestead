# GPU passthrough

Give a physical GPU to a virtual machine for a desktop, gaming, transcoding or
compute. The VM uses its own GPU driver, and can drive a monitor connected to
the card. This guide covers whole-device PCI passthrough on k3s, RKE2 and
Harvester, including capturing and attaching a GPU's vBIOS.

For Jellyfin or another **application container**, use
[container hardware access](Containers#hardware) instead. Giving a GPU to a VM
removes it from the host driver, so host applications and containers cannot
use that same GPU at the same time. Sharing a GPU through vGPU or SR-IOV is a
different, hardware-specific setup.

## Before you start

- Sign in as a Homestead **administrator** for host preparation and ROM capture.
- Have KubeVirt running: Harvester includes it; on k3s/RKE2 see
  [installing VM support](Virtual-machines#vms-on-k3s-or-rke2).
- Enable CPU virtualization and IOMMU in the machine's firmware. IOMMU may be
  called **Intel VT-d**, **AMD-Vi** or **IOMMU**; enabling VT-x/SVM alone is not
  the same setting.
- Keep remote access to Homestead and the host working. Passing through the
  host's **boot display** GPU can make its local screen go dark. A second GPU
  or integrated graphics for the host can make this easier.
- Stop workloads using the GPU. Plan any host restart around the other workloads
  on that machine, and back up an existing VM before changing its boot firmware.

A passed-through GPU belongs to one running VM at a time. The VM needs a host
with an available matching device and cannot live-migrate with it attached.
Replicated storage alone does not make that GPU available on another node.

## 1. Prepare the host

1. Open **Nodes → select a host → Hardware → Devices for VMs**.
2. Choose **Look at its devices**, or **Refresh devices** if already inspected.
   Locate the GPU by model, PCI address and vendor/device IDs.
3. Check **IOMMU**. On k3s/RKE2, **Switch IOMMU on** prepares the host's kernel
   command line; restart the host through Host actions, then inspect it again.
   Firmware settings must also be enabled. If Homestead reports that the host
   has no supported GRUB configuration, follow the host OS's bootloader procedure
   instead; the change must be active after reboot before proceeding.
4. Review the GPU's **IOMMU group** before choosing **Give to VMs**. On k3s/RKE2,
   Homestead hands the group's non-bridge devices to `vfio-pci`, including the
   GPU's audio function, and persists the handoff across boots. A group containing
   host networking or storage in use is refused. Use a different PCI slot or
   hardware layout if the group contains devices the host must keep.
5. Refresh and confirm the GPU is offered to VMs. On Harvester, enable its
   **pcidevices-controller** add-on first; Homestead uses Harvester's device
   claims rather than the k3s/RKE2 driver-handoff mechanism.

An IOMMU group is the hardware's isolation boundary. Inspecting it matters:
functions in one group may need to be released together even if the VM only
needs the GPU. See [Linux VFIO](https://docs.kernel.org/driver-api/vfio.html)
and [Harvester PCI devices](https://docs.harvesterhci.io/v1.8/advanced/addons/pcidevices/).

## 2. Decide whether you need a vBIOS file

The **vBIOS** is the GPU's firmware image, also called its option ROM. It helps
initialize the card before the guest's GPU driver takes over. Some cards work
with their default ROM and need no uploaded file. Others, including some cards
used as the host's boot display, need an explicit copy supplied to the VM.

**Try the default ROM first unless your card is known to need a file.** Capturing
a copy for later is also useful. A ROM does not replace IOMMU setup, the guest
driver or a working GPU reset mechanism.

Capturing a ROM reads the card's firmware into a file. Attaching that file makes
it available to the VM. **Neither operation flashes the physical GPU.**

### Capture the ROM in Homestead

1. Keep the GPU given to VMs. Its host display driver must no longer be active.
2. Stop any VM using the card or another device in its IOMMU group, and wait for
   the VM instance to release it. Keep those VMs stopped during capture.
3. On the host's **Hardware → Devices for VMs** page, choose **Capture vBIOS**
   beside the GPU.
4. Save the downloaded `.rom` file. Keep a copy labelled with the card model,
   host and PCI address so it is not confused with another GPU's firmware.

Homestead reads the card's sysfs ROM and checks the PCI image headers, image
lengths, and vendor/device IDs. It refuses capture if it cannot establish that
the device is unused. It does not detach an active host driver or reset the card.
For a runtime-suspended VFIO GPU, it temporarily wakes the device and restores
the previous power policy afterwards. The ROM read switch is disabled again
when the helper finishes, including after failure.

**Capture only downloads the file. It does not attach it to a VM.**

### If capture fails

Read the error first: an active host driver or VM must be dealt with before
retrying. Refresh the host inventory and check the whole IOMMU group. Use a
current Homestead release; older versions could fail to read an idle,
runtime-suspended card.

Some cards do not expose a readable ROM through sysfs. In that case, obtain a
dump from the **same physical card**, for example with GPU-Z on a Windows system
where the card is accessible, then upload that file in the VM editor. A matching
marketing name alone does not establish that another card's ROM is suitable.

Homestead accepts a ROM up to **640 KiB**, starting with the PCI ROM signature
`55 AA`. Some dump tools prepend a header. Keep the original file and only
remove a header when the dump format is understood; do not blindly truncate a
ROM to satisfy the size limit. Uploaded files receive size/signature checks,
not the capture path's full hardware-match checks. You must choose the right
image. Physical UEFI boot output needs a UEFI-capable ROM when supplying one.

## 3. Attach the GPU and optional ROM to the VM

1. Stop the target VM for this initial setup. Open **VM → Edit → Passthrough**,
   or **New VM → Passthrough**.
2. Choose **Add device** and select the GPU resource. The picker shows inspected
   models, PCI IDs, hosts, addresses and existing users. A resource can represent
   matching cards on more than one host; the displayed address is not an
   exclusive reservation. Pin the VM to the intended host if the setup depends
   on a particular card or connected monitor.
3. Add the GPU's audio function separately if the guest needs HDMI/DisplayPort
   audio. Handing the group to VFIO does not automatically attach every function
   to the VM.
4. If needed, select the downloaded **ROM file beside the GPU**. Otherwise keep
   its default ROM. Review and save the configuration.

Homestead stores uploaded ROMs in the VM's managed `<vm>-vbios` ConfigMap and
configures a KubeVirt hook sidecar to supply them to QEMU. Several ROMs must fit
together within the ConfigMap's **1 MiB** limit. Unrelated VM edits retain them;
**clear** removes a device's uploaded ROM. A missing saved ROM must be restored
or explicitly cleared before the affected edit can proceed.

## 4. Choose where the display appears

For GPU compute or transcoding while retaining Homestead's VNC access, leave
**Primary boot output** set to **Web console (virtual display)**. The guest can
use the attached GPU once its driver is installed.

For a monitor connected directly to the GPU:

1. Open **Hardware → Devices → Primary boot output** and choose
   **Passed-through GPU (physical monitor)**.
2. Keep the serial console enabled as a possible recovery path. The guest OS
   must also be configured to send output to its serial port for this to help.
3. Review and save. This choice disables virtual VGA and selects **UEFI**.
   A guest installed for legacy BIOS may need bootloader conversion or repair
   before it can boot this way. Secure Boot starts off when switching from BIOS.
4. Connect the monitor to the passed-through card and choose the correct input.
   Start the VM and install the GPU manufacturer's driver inside the guest.

With physical GPU output selected, an unavailable **Screen** (VNC) is expected.
Use the physical monitor, a configured serial console, or the guest's existing
SSH/remote-desktop connection. With several GPUs, guest firmware chooses which
one to initialize; this setting does not select a specific connector.

## 5. Check the result

Confirm that the guest sees the expected GPU and its driver loads successfully.
Test the intended workload: physical display output, a transcode, or a compute
job. Then shut down and start the VM again to check that the card can be reused.
Some GPUs have reset limitations that appear only after their first use; a ROM
file does not guarantee that repeated starts will work.

| Symptom | Check |
|---|---|
| GPU missing from the picker | Refresh host devices; check IOMMU, VFIO handoff or Harvester claims, and KubeVirt availability. |
| Device is already in use | Stop the named holder and wait for its instance to release the device. Stopped configurations do not reserve GPUs. |
| VM starts but monitor stays blank | Check cable/input, selected host and GPU, physical boot output, UEFI compatibility, optional ROM and guest driver. |
| VNC disappears after selecting GPU output | Expected: virtual VGA is disabled. Use physical/serial/remote access, or restore web-console output. |
| Display works until the OS starts | Check the guest GPU driver and guest display settings. |
| First start works, later starts fail | Check device reset errors in host/launcher logs. A host reboot may recover it; plan around other workloads first. |
| ROM is rejected | Check the original dump's format, size and card identity; see the capture section above. |

More diagnostics: [GPU troubleshooting](Troubleshooting#gpu-passthrough).

## Undo the setup or move to another host

Stop the VM. If removing its last GPU, select **Web console (virtual display)**
or serial output in the same edit. Remove the passthrough device and review the
change; removing the device also removes its managed ROM. Clearing only the ROM
does not restore the virtual display. Switching back to the web console leaves
UEFI enabled, so it does not undo an earlier firmware change.

When no VM needs the device, use **Give back** on its host to return it to the
host driver. If a boot display does not return immediately, a planned host
restart may be needed. Container hardware users will need the host driver again.

For another host or cluster, prepare the destination hardware first and review
the [transfer's device and ROM mapping](Virtual-machines#hardware-during-a-cluster-transfer).
A different GPU may need a different ROM; do not automatically reuse the old
card's dump. Ordinary GPU passthrough does not gain live migration from this
transfer workflow.

Related reference: [PCI and USB passthrough](Virtual-machines#pci-and-usb-passthrough)
and [KubeVirt host-device assignment](https://kubevirt.io/user-guide/compute/host-devices/).
