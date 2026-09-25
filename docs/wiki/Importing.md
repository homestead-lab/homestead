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

## A VM disk

**Import → VM disk** downloads a disk image from a URL - qcow2, vmdk, raw, vdi,
vhd(x) - into a new volume, converting it as it goes, and can check its
SHA-256. When it finishes, **Create VM** boots from it. This needs CDI, which
Harvester includes. See [Virtual machines](Virtual-machines).

## From another Homestead

**Import → ＋ Homestead cluster** adds another cluster to browse and move
workloads from. See [Moving between clusters](Moving-between-clusters).
