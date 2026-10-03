# Network shares

Removing a share keeps its PVC and data. Homestead rebuilds SMB's volume and
mount lists from the saved share inventory, and the removal job waits for old
SMB pods to release their mounts before reporting completion. If another share
uses the same PVC (including a different subfolder), the PVC stays mounted and
the result names those remaining shares. Other workloads can also keep a volume
attached. Retrying removal repairs a stale mapping even when the saved share
was already removed; it does not delete the underlying volume.

**Shares** serves volumes to Windows, macOS and Linux over SMB (Samba), the way
an Unraid share does - `\\192.0.2.245\media`.

![Shares](https://github.com/wjcloudy/homestead/releases/latest/download/homestead-shares.jpg)

## The first share

The first share installs Samba. It asks which address Samba answers on - one of
[your VIPs](Networking#add-default-and-choose-workload-vips), or the next free one - because a share needs
an address of its own on port 445. Samba is put in place before anything else,
so a share that could not be served leaves nothing behind.

The SMB server appears as `homestead-smb` in Containers, in the **Homestead**
group. Its share mappings, mounts and image are managed from **Network Shares**,
not by editing the container. Enable, stop or remove the SMB server under
**Settings → Hardware and storage → Add-ons**. Stopping or removing it keeps every share
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

Samba keeps one password per user. Select an existing SMB user in **New share**
to reuse it, or add a new user. Use **SMB users** beside **New share** to change
a password for all of that user's private shares.

## Shared file access

Homestead keeps Samba's existing filesystem identity, UID `100`, GID `101`,
by default so an upgrade preserves access to existing files. This identity is
separate from SMB login accounts. Set `samba.uid` and `samba.gid` in Helm values,
or `SAMBA_UID` and `SAMBA_GID` on Homestead, to match your applications.
Reconciliation maintains these IDs on the SMB container. Applications can keep
different UIDs: they need the shared GID (as their primary or effective
supplementary group), `UMASK=002`, and group-write access to existing files.
An image that resets supplementary groups at startup may require setting PGID.

SMB prepares only writable share roots with the shared group and group-write,
traverse and setgid permissions. Setgid makes new files inherit the directory's
group. SMB-created files allow group write, and new directories retain setgid.
Read-only roots and all existing descendants are left untouched. Samba no
longer runs its recursive `-p` ownership/permission rewrite on every startup.

Existing files may need a one-time, scoped group/permission migration; changing
the shared GID does not migrate descendants. Check every application using the
volume first. Keep private `/config` ownership with its application UID.
For a setup using `99:100`, choose `samba.uid=99` and `samba.gid=100`
explicitly and review existing file access before changing groups.
Storage must allow required root-directory group/mode updates; root-squashed
exports need preparation on the storage server. Already-prepared roots need
no metadata writes. Apps that explicitly create restrictive
permissions need their own configuration even with `UMASK=002`.

This follows the [Servarr shared-group guidance](https://wiki.servarr.com/docker-guide).

## Connecting

- **Windows**: `\\<address>\<share>` in File Explorer, or **Map network drive**.
- **macOS**: Finder → Go → Connect to Server → `smb://<address>/<share>`.
- **Linux**: `smb://<address>/<share>` in the file manager, or mount with
  `mount -t cifs`.

## Folder names and capitalization

Use one spelling for each folder shared with Windows, including in container
mount paths and application settings. Linux can store `Movie-Library` and
`movie-library` as separate directories, while Windows SMB access can resolve
both names to the same folder. Explorer may show both names with identical
contents even though Linux lists different files in each.

If this happens, compare both exact paths through Homestead's volume file
browser or Linux. Check which path each application uses, then give one folder
a distinct name and update any references to it. Renaming on Linux avoids
Windows resolving the change against the wrong folder. Keep both sets of files
until their contents and application paths have been checked.

SMB name handling does not prevent containers or host processes writing
directly to Linux storage from creating case-only duplicates. Homestead does
not currently scan for these collisions or reject them in application folder
paths. Manually editing the managed Samba container is not a durable prevention
measure; its configuration is rebuilt from the saved share settings.

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

### Partial SMB service after a storage or node failure

One SMB pod normally mounts every configured share. If a host holding the only
usable replica goes offline, that mount can prevent *all* shares from starting.
The leader's reconciliation loop now checks actual Longhorn data replicas and
node readiness, not just the desired replica count. After an unavailable volume
is observed consistently for at least one minute, its mounts **and SMB exports**
are temporarily omitted. The other shares can then start on a surviving host.

Network Shares lists each offline share and its reason; Add-ons shows partial
service rather than healthy full service. Definitions, credentials, PVCs, files,
the Service and VIP are kept. No empty directory or replacement volume is served
in place of missing data. Multiple shares on one unavailable claim are all omitted.
The exclusions are stored on the Deployment, separately from saved share settings,
so restarting Homestead, editing another share or repairing mappings cannot silently
re-add missing storage.

When Longhorn reports recoverable storage on a Ready node for at least two minutes,
the shares are re-added automatically. Removing or re-adding mounts **restarts SMB
and briefly interrupts all client connections**. If restoring a share fails the
rollout check while SMB was serving, the working subset is restored and automatic
retries stop. Resolve the storage/mount problem, then use **Repair / retry recovery**.
This starts a new stability check instead of bypassing it. Polling is normally once
a minute, so these are minimum stability windows, not outage-time guarantees.

Unknown health, API errors, new/rebuilding replicas, and non-Longhorn storage do
not trigger automatic exclusions or restoration. Detached/stopped replicas are
not assumed lost. A missing PVC or a faulted Longhorn volume is explicitly unavailable.
Homestead never force-deletes pods, detaches storage, salvages replicas, changes
replica counts, or recreates a missing claim as part of this recovery.

This requires a functioning control plane, a running Homestead leader, schedulable
replacement capacity, and storage that can safely attach. Kubernetes/Longhorn
may still wait for failed-node fencing or attachment cleanup. It does not make a
single-copy volume available while its only data host is down, resolve conflicting
RWO attachments, or provide seamless SMB session failover. This partial-service
policy currently applies to SMB, not the separate NFS gateway.

## Optional NFSv4 server

NFS is a **separate container** (`homestead-nfs`), not a service inside Samba.
It is off until you choose an export on a share and enable **NFSv4 network
shares** under **Settings → Hardware and storage → Add-ons**. Each export requires a Bound
ReadWriteMany (RWX) claim and an explicit IPv4 client or CIDR; an unrestricted
export is refused. Exports default to read-only and use root squashing. Enable
write access per share only when needed. Clients mount `<VIP>:/<share>` over
NFSv4/TCP port 2049.

The NFS image needs the host's `nfs` and `nfsd` kernel support and `SYS_ADMIN`
inside its container. The node probe checks NFS server support and Homestead
labels eligible nodes automatically; the scheduler places NFS only on those
nodes. Enable `nfsd` on the intended Linux hosts and update/install the node
probe before enabling the add-on. Homestead does not load host kernel modules.

SMB uses port 445 and NFS uses 2049, so the protocols can share one IP. This
implementation uses independently placed pods, however. NFS's client allowlist
needs the real source IP and therefore Local traffic routing. Its VIP must
follow the NFS pod; a separately placed SMB pod cannot safely use that same
VIP across all supported load balancers. Homestead reserves the NFS VIP against
sharing in both directions. A combined file-server pod would allow a shared
IP in a future implementation. k3s ServiceLB alone is not suitable for this
gateway; use kube-vip with per-Service election or MetalLB.

### Recovery when a host fails

Homestead keeps a single NFS server with a stable hostname and stable export
identities. Existing numeric export identities from v2.8.157 are saved before
upgrading them, so adding or removing a preceding share does not change file
handles for the remaining exports. Disabling/reinstalling the server keeps
these identities with the share settings. Startup and readiness checks wait
for the NFS TCP listener; the VIP only serves Ready endpoints. The Deployment
requests replacement after 15 seconds of a NotReady/unreachable taint, **in
addition to** Kubernetes' node-failure detection and storage recovery time.

**Recovery checks** in Network Shares and Add-ons list eligible replacement
hosts, control-plane/etcd quorum, actual healthy replica hosts and load-balancer
prerequisites. A replica count configured as two is not sufficient: two
healthy copies on different Ready nodes are required. One control-plane host
cannot reschedule work after its own failure. Longhorn's pod deletion policy
may also make a failed-node recovery wait for intervention. The checker never
claims a tested recovery time or uninterrupted availability.

Longhorn RWX volumes already use NFS. Linux NFS re-export does **not** support
normal file-lock/delegation recovery. This gateway is for ordinary file access,
not VM disks or databases that require those guarantees. Client access can
pause during recovery and a remount may be needed. See the upstream
[NFS re-export limitations](https://docs.kernel.org/filesystems/nfs/reexport.html)
and [load-balancer sharing rules](https://metallb.io/usage/#ip-address-sharing).

Before production use, test with a disposable RWX volume and a client mounted
to its share path: verify read/write checksums, restart the gateway, move it
to a different eligible node, and check access and export identity again.
A real host-loss test must also verify the storage server and VIP move while
the API and etcd retain quorum; a pod restart alone does not prove that.

Stopping or removing `homestead-nfs` deletes neither the SMB server nor share
definitions, credentials, claims or files. Removing a share itself first drops
its NFS export; the volume still remains. Homestead owns the NFS workload's
image and mounts, so change them from Network Shares and Add-ons rather than
through the generic container editor.

## SMB users

Use **SMB users** beside **New share** to add accounts, see their private shares,
change a password, or remove an unused user. SMB users are separate from
Homestead sign-ins. These controls require administrator access.

In **New share**, select an existing user to reuse its saved password, or choose
**Add a new user** and enter a username and password. Selecting an existing user
does not reveal or reset its password. Guest shares do not need an account.

Samba has one password per user across all its shares. Changing it in **SMB users**
updates every private share assigned to that user and restarts Samba, briefly
disconnecting clients. Passwords stay in the Kubernetes Secret and are never
returned by the user inventory. Existing share credentials appear automatically.

Removing a share keeps its user available for later shares. A user can only be
removed after its private shares have been reassigned or removed; removing the
user deletes its saved credential, not share data.
