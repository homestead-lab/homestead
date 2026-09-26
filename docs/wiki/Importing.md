# Importing

**Import** brings things in from elsewhere: containers and their data from an
Unraid (or any Docker) server, a Docker Compose file, a VM disk image, or a
workload from another Homestead cluster.

![Import](https://github.com/wjcloudy/homestead/releases/latest/download/homestead-import.png)

## From Unraid or a Docker host

This moves a running container across - its settings *and* its appdata.

1. **Add the source.** **Import → ＋ Container source** takes the server's
   address, its type (Unraid, Proxmox or any SSH host), an SSH username and
   password, and where its appdata lives (`/mnt/user/appdata` on Unraid).
   Homestead lists its containers from Docker itself.
2. **Pick a container.** Its image, ports, variables, devices and privileges
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
4. **Measure sizes.** Homestead runs `du` on the source and sizes each volume
   from what is really there, and checks each fits on Longhorn.
5. **Import.** The copy keeps owners and permissions by number, so the app
   finds its files as it left them. Progress is in bytes, in the job tray.

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

## A VM disk

**Import → VM disk** downloads a disk image from a URL - qcow2, vmdk, raw, vdi,
vhd(x) - into a new volume, converting it as it goes, and can check its
SHA-256. When it finishes, **Create VM** boots from it. This needs CDI, which
Harvester includes. See [Virtual machines](Virtual-machines).

## From another Homestead

**Import → ＋ Homestead cluster** adds another cluster to browse and move
workloads from. See [Moving between clusters](Moving-between-clusters).
