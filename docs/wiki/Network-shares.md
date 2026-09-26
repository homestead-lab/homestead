# Network shares

**Shares** serves volumes to Windows, macOS and Linux over SMB (Samba), the way
an Unraid share does - `\\192.0.2.245\media`.

![Shares](https://github.com/wjcloudy/homestead/releases/latest/download/homestead-shares.png)

## The first share

The first share installs Samba. It asks which address Samba answers on - one of
[your VIPs](Networking#your-vips), or the next free one - because a share needs
an address of its own on port 445. Samba is put in place before anything else,
so a share that could not be served leaves nothing behind.

The SMB server appears as `homestead-smb` in Containers, in the **Homestead**
group. Its share mappings, mounts and image are managed from **Network Shares**,
not by editing the container. Enable, stop or remove the SMB server under
**Settings → Cluster → Add-ons**. Stopping or removing it keeps every share
definition, password, PVC and file; only the server Deployment and Service are
removed. Older `samba` workloads
are migrated to `homestead-smb`; the existing SMB address is retained when the
service can be recreated.

## Making a share

A share either:

- **creates its own volume** - a size, a class, copies; or
- **publishes a volume that exists** - including one an app is using - optionally
  just one folder inside it. That is how you reach an app's appdata from your
  desktop without copying it.

Then choose **guest** (anyone on the LAN) or **private** (a username and
password), and **read-only** or **read/write**.

Samba keeps one password per user, so a second private share for the same user
reuses its password when the field is left blank, and a new password changes it
for every share that user has - the editor says so first.

## Connecting

- **Windows**: `\\<address>\<share>` in File Explorer, or **Map network drive**.
- **macOS**: Finder → Go → Connect to Server → `smb://<address>/<share>`.
- **Linux**: `smb://<address>/<share>` in the file manager, or mount with
  `mount -t cifs`.

## Changes and safety

Growing a share happens in place. Access changes restart Samba, followed in the
job tray. One Samba pod serves every share, so a change is tested first: if the
share's volume cannot be mounted, the old shares are put back and the change is
refused with the reason, rather than taking every share down.

Removing a share keeps its volume and data. Share settings are in the
`homestead-shares` ConfigMap; passwords in the `homestead-share-credentials`
Secret, and never sent back to the browser.

The Network Shares page shows the server's own address and whether its live
mappings match the saved share list. If a Kubernetes update is rejected, the
saved settings are restored so a failed share cannot reappear on the next edit.
The **Repair mapping** button reapplies the saved shares, and Homestead also
checks for drift in the background.

## Optional NFSv4 server

NFS is a **separate container** (`homestead-nfs`), not a service inside Samba.
It is off until you choose an export on a share and enable **NFSv4 network
shares** under **Settings → Cluster → Add-ons**. Each export requires a Bound
ReadWriteMany (RWX) claim and an explicit IPv4 client or CIDR; an unrestricted
export is refused. Exports default to read-only and use root squashing. Enable
write access per share only when needed. Clients mount `<VIP>:/<share>` over
NFSv4/TCP port 2049.

The NFS image needs the host's `nfs` and `nfsd` kernel support and `SYS_ADMIN`
inside its container. A dedicated VIP is required so the export sees the real
client address for its allowlist; k3s ServiceLB alone is not suitable, but
kube-vip or MetalLB can provide one. Longhorn RWX volumes already use NFS, so
serving them again is an NFS re-export and may add overhead. Check the server's
pod logs and mount a noncritical test share before relying on it for workloads.

Stopping or removing `homestead-nfs` deletes neither the SMB server nor share
definitions, credentials, claims or files. Removing a share itself first drops
its NFS export; the volume still remains. Homestead owns the NFS workload's
image and mounts, so change them from Network Shares and Add-ons rather than
through the generic container editor.
