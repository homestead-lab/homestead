# Importing

**Import** (a tab of **Containers**) brings things in from elsewhere: containers and their data from an
Unraid (or any Docker) server, or a Docker Compose file. VMs have an **Import**
tab of their own under **Virtual machines**: VMs from Unraid, and disk images
from a web address.
Workloads from another Homestead cluster move under **Settings → Linked
clusters**.

![Import](https://github.com/homestead-lab/homestead/releases/latest/download/homestead-import.jpg)

## From Unraid or a Docker host

This imports a container's settings and appdata. Stop its writers before copying,
or use consistent snapshot/backup paths; this is not live application migration.

1. **Add the server.** **Import → ＋ Unraid or Docker server** takes the server's
   address, its type (Unraid, Proxmox or any SSH host), an SSH username and
   password, and where its appdata lives (`/mnt/user/appdata` on Unraid).
   An optional SSH port defaults to 22. **Save and verify** first reads a public
   host key without sending a password. Compare its SHA256 fingerprint with
   `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` on the source's own console
   (the dialog supplies the command for the selected key type), then explicitly
   trust it. Use a physical console or an already trusted management session.
   Homestead can then list its containers from Docker itself.
2. **Pick a container.** **Import containers** on the server's card lists what
   Docker runs there, with its appdata folders below for anything Docker does
   not describe. The chosen container's image, ports, variables, devices and privileges
   become the Deploy form here.
3. **Decide where each folder goes.** An Unraid container maps several host
   folders, and they do not all belong in one place: appdata wants a small
   volume with copies, recordings or media a large one. Define the volumes,
   then point each folder at one:
   - **Copy** - its files come across;
   - **Mount empty** - the path gets an empty volume, nothing copied (new
     recordings here, the old ones left behind);
   - **Leave out**.

   A container that keeps nothing on disk is recognised as such: it imports
   with its image, ports and environment alone, and no volume is made.
   **Add storage anyway** is there if you want one.
4. **Measure sizes and set memory.** Homestead runs `du` on the source and
   compares the copied size with each claim's logical capacity. Existing free
   filesystem space remains unverified: Longhorn's snapshot allocation is not
   filesystem usage. Review memory reserved and the optional hard memory maximum;
   Docker's reservation and limit are carried over when available.
5. **Review import.** Review two sequential phases: the copy helper and the
   application. The helper reserves 100m CPU / 128 MiB RAM and has a 512 MiB
   memory limit. Confirm new/reused claims, possible file replacement and any
   capacity warnings. Hard placement blockers cannot be overridden. The server
   repeats admission before creating resources; review tokens expire after ten minutes.
   Choose **Source data safety**: all writers stopped, or consistent snapshot/backup
   paths. For Docker-discovered imports in stopped mode, the copy checks the original
   container ID is stopped, unpaused and not restarting before copying and after
   each folder. It never stops the source for you. Other writers and changes between
   checks remain your responsibility; snapshot mode skips the Docker check.
6. **Copy, then start.** The copy preserves numeric owners and permissions.
   Transfers shows byte progress; Recent jobs records setup steps and the copy log.
   Its percentage represents workflow milestones, not bytes. The application is created **stopped**;
   Start in Containers makes a fresh capacity check after the copy completes.
   A failed, missing or mismatched copy Job blocks starting, including through
   workload edits. Imports with nothing to copy create no phantom Job.

An existing Job or workload is never silently replaced. Existing claims must be
explicitly selected, Bound and not deleting. Stop their consumers before copying,
including consumers of RWX claims: shared access does not make overwriting live
application data safe. The review is not a distributed scheduler reservation;
external writers and concurrent starts can still race it.

### Source identity and credentials

Existing saved sources also need **Verify source** before authenticated connections.
SSH uses only the explicitly pinned host key; changed keys are never accepted
automatically. A scan alone is not identity verification ([OpenSSH guidance](https://man.openbsd.org/ssh-keyscan.1)).
Trust approval expires after ten minutes and is bound to the administrator, source,
key and source-directory version. A failed or uncertain save requires a fresh scan.

New sources receive unique immutable Kubernetes credential Secrets, not passwords
in the source ConfigMap. Restrict namespace/Secret access and enable encryption at
rest as appropriate. Passwords reach helpers through Secret-backed environment
variables, not command-line arguments. A source cannot overwrite an existing name.
Changing a pinned key affects new helpers only: already-created helpers retain
their original key and credential reference.

Removing a source removes its directory entry, **not** running copy Jobs or their
credential Secrets. This is not credential revocation. After inspecting and removing
all helpers that still reference a Secret, an administrator can remove that unused
Secret separately. An interrupted source save may leave an unreferenced credential
Secret; inspect it rather than reusing it for a different source. Revoke credentials
on the source host when required.

### Interrupted imports and cleanup

New imports record their intent in **Recent jobs before creating any Kubernetes
resources**. Each resource request records an intent followed by its confirmed
identity or an uncertain outcome. Application manifests and credentials are not
stored in this journal. A lost response never triggers an automatic repeat.

Choose **Recent jobs → Inspect import** after interrupted setup. The dialog shows
planned resources, acknowledged writes and current identities. **Resolve as unknown**
acknowledges inspection only: it does not retry, delete anything, start the app or
remove its safety hold. A fresh review is needed for any new import. An active
dispatcher or copy blocks resolution. If copy-Job creation itself is uncertain,
or the Job was replaced, Kubernetes-level inspection is required; Homestead will
not adopt a same-name Job or release its volume reservation automatically.

The stopped application is pinned to the confirmed copy Job's identity. A matching
successful Job and no live pods owned by that Job are required before Start.
Failed, missing or replacement Jobs leave the application blocked. Check copied
files before deciding how to recover; a successful rsync does not prove application
consistency or data integrity.

For new journalled imports, **Remove completed Job** removes only the verified,
successfully finished helper, using its UID as a deletion precondition. It first
preserves completion on the matching workload. The application, Services and every
volume remain, including newly created and borrowed claims. Manage unwanted resources
separately after inspection. Incomplete imports cannot use this cleanup shortcut.
The tracking receipt stays protected from history clearing until the helper is removed.
Older imports retain their legacy cleanup choices; borrowed claims are never offered
for deletion there. Do not delete Jobs or remove holds to bypass verification.

This is a one-shot setup journal, not an atomic multi-resource transaction, resumable
file transfer or distributed scheduler reservation. External writers can still race
checks. Complete the upgrade on all Homestead replicas before importing; mixed-version
and live host-loss rehearsals remain separate validation work.

CI runs a disposable real SSH/rsync fixture with no external networking: it rejects
changed keys and running/replaced Docker source identities, interrupts a copy,
checks source/borrowed files remain, and explicitly starts a fresh copy with hash
verification. This tests helper behavior, not live Kubernetes host loss, HA,
filesystem free-space guarantees or application consistency.

A tmpfs RAM disk on Unraid (Frigate's `/tmp/cache`, for example) becomes a RAM
disk here, not a volume full of old cache.

Stop the container on Unraid before importing if its data changes while it
runs (databases especially), or it will copy a moving target.

## Docker Compose

**Import → Docker Compose** takes a pasted `docker-compose.yml` (and an
optional `.env`), and checks it as you type. A file indented with tabs, which
YAML forbids, is read as if it had spaces and says so; **Use spaces in the
editor** changes the file to match. Each service becomes a container,
created in `depends_on` order:

- named volumes become Longhorn volumes; one several services share becomes
  shared (RWX);
- host folders like `./config` become new volumes - their contents are not
  copied; use a container source for that;
- a service other services reach by name, such as a database, keeps that name;
- `cap_add`, `tmpfs`, `devices`, `user`, `command` carry across.

`build:` without an image, the Docker socket, and Compose `secrets`/`configs`
are refused. **Edit in form** opens one service in the Deploy form first.

### Batch capacity review

**Create workloads** first reviews the selected services together, not against
separate copies of the same free capacity. The bounded joint-placement search
accounts for every replica's requests, declared host ports, hardware, PVC
consumer restrictions and checked pod-affinity/topology rules. It tries host
and pod-order alternatives. A known shortfall blocks creation; a search that
cannot finish within its bounds also blocks and asks you to split the batch.
The current bounds are 32 services and 64 new pod placements, with a limited
search budget. Missing/incomplete required inventory cannot become an empty
cluster with invented free capacity.

The modal shows per-service requests/estimates, conservative per-host RAM upper
estimates, unknown metrics and an example placement. The upper estimates sum
pods that could individually fit on each host; they may exceed any achievable
joint placement. The example is **not** sent as a hard pin to Kubernetes.
Missing metrics and unbounded memory remain explicit warnings requiring consent.

The signed, ten-minute review binds the exact Compose file, variables, namespace,
selection and resolved configs. Editing any input requires another review.
The API rereads the file and recomputes the joint plan before its first write.
Before each later service, it refreshes the inventory and includes earlier
created Deployments even if their pods have not appeared yet. Owned pods are
resolved by controller UID; scheduled pods keep their reservations, while only
the missing replicas are simulated. Ambiguous ownership stops the remainder.

This is not an atomic admission reservation or OOM guarantee. Other controllers'
uncreated replicas, concurrent callers, admission webhooks, storage provisioning
and app readiness remain limitations. `depends_on` controls creation order, not
application startup readiness. A later failure stops the batch and names what
was created; **no workload or volume is automatically deleted**. Inspect any new
claims, keep the created services, and remove their definitions and already
satisfied `depends_on` references before reviewing the remainder (or deploy the
remaining services individually). Drafts and `.env` values stay in browser
memory only; save your original file somewhere safe before refreshing.

## VMs from Unraid

**Virtual machines → Import** lists every VM on each Unraid server you have
added (the same servers, and the same verified SSH login, the container import
uses), with its state, cores, memory, firmware and disks.

**Import** opens three steps - Settings, Disks, Network - under a live picture
of what maps across:

- **Cores, memory and firmware** come across as they are: UEFI or BIOS, a TPM,
  Hyper-V enlightenments. The machine type becomes q35, the one KubeVirt runs.
- **Each vdisk** becomes a disk here, the size of the vdisk, on the storage
  class you choose; a second disk can be left behind. Each keeps the bus it had
  on Unraid - Windows installed on SATA cannot find its disk on VirtIO.
- **The network card** joins the LAN network you pick (br0 on Unraid), keeping
  its MAC address, so a DHCP reservation still gives it the same address.
- **What cannot come across** stays behind, and the picture says why:
  GPU and USB passthrough (add Homestead's own hardware devices in Edit VM),
  ISOs in its CD-ROM, CPU pinning, and any disk that is a whole physical disk
  rather than a file. A TPM comes across as a new one, so BitLocker, if it is
  on, asks once for its recovery key.

A VM that is **running** on Unraid shows **Shut down** instead of Import: it
asks Unraid for a clean shutdown (the power button), so the disk is copied as
it was left. The copy checks again that it is still shut off before it starts.

The disks are read over SSH and streamed into CDI's upload proxy, which turns
raw or qcow2 into a VM disk as it arrives. SSH compression reduces network
traffic for empty space; progress counts the original disk bytes. Progress
shows in the bell and under **Recent imports**, where **Job details** opens
the copy log. **Follow latest** keeps the newest step and disk output visible;
turn it off to read earlier entries without losing your place on refresh.
Each disk shows its percentage, transferred bytes, speed and estimated time
remaining in a compact copy box. Jobs started on older versions report whole
percentages, so their progress and byte totals are approximate. An unchanged
percentage does not prove a stalled copy; speed and ETA are unavailable from
those coarse samples, even when timestamps are present. Upgrading Homestead
does not replace the script of an import already running. Transferring the stream still leaves CDI
to finish the disk.

**Dismiss** removes a finished import from job history; it keeps
the VM and disk data. Running imports cannot be dismissed. Each disk uses a
filesystem volume on the storage class you choose, with CDI allowing for
filesystem overhead. This avoids inheriting a
block-device mode that the host runtime may not let CDI access.
When every disk has arrived the VM is made,
**stopped**, for you to start. The VM on Unraid is never changed or deleted; if
a copy fails, cleanup of its destination disks is requested. New imports also
reclaim their own CDI staging and scratch storage, including temporary space
on a **Retain** storage class. Completed disks keep their chosen storage policy.
If copying finishes but VM creation fails, the completed disks are kept so you
can create the VM from them under Import. The failed import can be retried
without changing the source VM once its cleanup finishes.

The job stays active while temporary import storage is being removed, and
cleanup continues after a Homestead restart. Do not retry with the same disk
names while cleanup is pending. Storage used by another workload, with changed
ownership, or left by an older import without tracked ownership is preserved;
inspect it under **Volumes** before removing anything separately.

This needs CDI, which Harvester includes.

## A VM disk image

**Virtual machines → Import → ＋ Import → A disk image from a URL** downloads a
disk image - qcow2, vmdk, raw, vdi, vhd(x) - into a new volume, converting it as
it goes, and can check its SHA-256. When it finishes, **Create VM** boots from
it. This needs CDI, which Harvester includes. See
[Virtual machines](Virtual-machines).

URL disk imports use the same tracked CDI staging and scratch cleanup described
above. Completion includes removal of the import's temporary storage; the
finished VM disk remains available for **Create VM**.

## From another Homestead

Link the other cluster under **Settings → Linked clusters**, then move its
containers and VMs from there - or with **Move to cluster** in a workload's `…`
menu. See [Linked clusters](Linked-clusters) and
[Moving between clusters](Moving-between-clusters).
