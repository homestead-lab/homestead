# Troubleshooting

Start with **Settings → About**: it shows which background parts of Homestead are
working and the last error of any that are not. The job tray (bottom right)
keeps every job's steps and errors across restarts.

## Homestead

**The page says "Waiting for the cluster".** Homestead cannot read its user
store from Kubernetes - usually the API server is restarting (an upgrade, a host
rebooting). It retries by itself; the page comes back when the API does.

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
handing out addresses: add some under **Networking → Your VIPs**. See
[Networking](Networking#your-vips).

**New hosts cannot join, though the dashboard works.** Something else is on the
cluster's own address - the VIP hosts join through on port 9345. Homestead
names any app sitting there on **Networking** and in **Settings → About →
Addresses**; give each an address of its own (**Edit → Network**). If
Homestead's own shared address (`LB_IP` on its Deployment) is the cluster's
address, change it to a free one. Homestead no longer offers the cluster's
address, or one another program owns, anywhere it asks for one.

**An app's address does not answer.** **Networking** shows each address's
Service and whether its pods are ready. On Harvester, check the address is not
also given out by your router's DHCP.

## VMs

**A VM waits at "Provisioning" or "ImportScheduled".** The VM's card says why
after three minutes - most often the image URL is wrong or unreachable from the
cluster, or its volume cannot be scheduled. **Edit → Disks** can give a failed
disk a new source.

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
Homestead version (Settings → About), the cluster type, and the job's steps from
the job tray.
