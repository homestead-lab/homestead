# Homestead

<p align="center">
  <img src="web/assets/homestead-lockup.svg" width="360" alt="Homestead — friendly cluster management for your homelab">
</p>

**Homestead is an open-source, NAS-style homelab dashboard and container
management UI for Kubernetes - on [k3s](https://k3s.io),
[Harvester HCI](https://harvesterhci.io), RKE2 or any cluster you already
run**, with Longhorn, KubeVirt, Rancher and Fleet understood where they are
there. It runs inside your cluster, talks directly to the Kubernetes API
through a dedicated ServiceAccount, and turns workloads, storage, hardware
passthrough, image updates and failover constraints into approachable controls.

Unraid® is a registered trademark of Lime Technology, Inc. This application is
not affiliated with, endorsed, or sponsored by Lime Technology, Inc.

[![CI](https://github.com/wjcloudy/homestead/actions/workflows/ci.yml/badge.svg)](https://github.com/wjcloudy/homestead/actions/workflows/ci.yml)
[![Container](https://img.shields.io/badge/ghcr.io-homestead-2453ff?logo=docker)](https://github.com/wjcloudy/homestead/pkgs/container/homestead)
![status](https://img.shields.io/badge/status-alpha-f59e0b)
![license](https://img.shields.io/badge/license-MIT-30ba78)

![Homestead cluster dashboard](https://github.com/wjcloudy/homestead/releases/latest/download/homestead-dashboard.png)

**New here?** The [wiki](https://github.com/wjcloudy/homestead/wiki) is the
guided tour: building a cluster from nothing - Harvester, k3s, or the one you
already run - with Homestead on it, then each part of Homestead in turn.

## Where it runs

Homestead checks what the cluster has and each page works with that - nothing
is Harvester-only unless Harvester is what it is about.

| | k3s | Harvester | RKE2 or other Kubernetes |
|---|---|---|---|
| **Install** | [one line](#one-line-install-and-node-doctor) from bare Linux | [one line](#one-line-install-and-node-doctor) on a host, or Helm or the manifest | [Helm or the manifest](#installing-with-the-manifest) |
| **Containers, App Store, Compose, Portal, Networking, IP addresses, Resources, dashboard** | yes | yes | yes |
| **Volumes, data protection, disks** | yes, with Longhorn - the k3s script installs it, or Settings → Cluster → Add-ons | yes, Longhorn is built in | yes, with Longhorn - Settings → Cluster → Add-ons installs it on RKE2 |
| **Virtual machines** | yes, with [KubeVirt](https://kubevirt.io) - the k3s script's `--kubevirt`, or Add-ons | built in | yes, with KubeVirt - Add-ons installs it on RKE2 |
| **Addresses for apps** | the nodes' own addresses (k3s's ServiceLB), or a VIP per app with MetalLB | a VIP per app (kube-vip) | a VIP per app with MetalLB or kube-vip |
| **Helm charts** | yes (k3s's Helm controller) | yes (RKE2's Helm controller) | yes on RKE2; listing only without a Helm controller |
| **Adding a host** | the join command for a worker or a server | a guide to Harvester's installer | RKE2's join commands |
| **Platform upgrades** | - | followed on the Cluster page | - |

### One line: install and node doctor

On a bare Linux machine (x86-64 or 64-bit ARM - old PCs, mini PCs, VMs), or on
a Harvester host, or on any node of a cluster:

```bash
curl -sfL https://raw.githubusercontent.com/wjcloudy/homestead/main/scripts/install.sh | sudo sh
```

It says what it found on the machine and offers what fits, as menus (plain
prompts where the machine has no `whiptail` or `dialog`):

![The installer's menu](https://github.com/wjcloudy/homestead/releases/latest/download/homestead-tui-menu.png)

- **Install Homestead.** It checks the machine first: memory, disk, the
  internet, ports, the hostname, the clock, the firewall, hardware
  virtualisation, and whether the address is from DHCP. Then it asks:
  - on a **bare machine**, whether to start a new k3s cluster or join one as
    a server or a worker, which address the others reach this machine on,
    and whether to add Longhorn and KubeVirt;
  - on a **Harvester host**, which address Homestead answers on and which
    storage class holds its data;
  - on a **k3s server**, whether to add Longhorn and Homestead to it.

  It shows what it will do before anything changes, follows the install with
  a progress bar (the full log goes to `/var/log/homestead-install.log`), and
  ends with Homestead's address and the line for the next machine.
- **Check this node and fix what is wrong.** The node doctor looks at the host:
  - the Kubernetes service, disk and inodes, memory, the clock;
  - iSCSI and multipath, which Longhorn needs;
  - certificates, and containerd.

  Where kubectl reaches the cluster, it looks there too: the API, etcd, this
  node and the others, failing or stuck pods, Longhorn volumes, DNS,
  Homestead, and the age of the newest etcd snapshot. It lists what it found,
  worst first; choosing one says what is wrong and offers its fix, asking
  before every change.
- **Clean up** unused images, the journal, failed pods and old snapshots;
  **take an etcd snapshot**; **restore the cluster from one** (on a k3s
  server, after typing RESTORE).

For scripts and cron: `--report` prints the health check and exits 0, 1 or 2;
`--fix-safe` applies the fixes marked safe; `--dry-run` shows the commands and
changes nothing. An install can be unattended with its answers given ahead
(`HS_ROLE=new HS_NODE_IP=... HS_YES=1`); `--help` lists them. Underneath, k3s
installs come from [`scripts/bootstrap-k3s.sh`](scripts/bootstrap-k3s.sh),
which can also be run directly. The
[k3s guide](https://github.com/wjcloudy/homestead/wiki/Installing-on-k3s) walks
through all of it.

**Addresses on k3s.** k3s's built-in ServiceLB publishes each app on every
node's own address, so Homestead and every app work without anything else,
each on its own port. For an address per app, as Harvester gives, add
**kube-vip** from **Settings → Cluster → Add-ons**. It runs beside ServiceLB
and takes only the Services given a VIP.

**On a cluster you already run** - k3s, RKE2, kubeadm - use Helm or the
manifest below. **Settings → Cluster → Add-ons** then installs Longhorn and
KubeVirt (with CDI) where they are missing, through the Helm controller k3s and
RKE2 run, and the pages that need one offer the same install.

## Highlights

| Area | Capability |
|---|---|
| **Dashboard** | Cluster CPU/RAM/network/disk telemetry, transition-aware health, top consumers, configurable warnings, and 90 days of history with node availability, recorded with no browser open |
| **MQTT** | Cluster and node stats to an MQTT broker with Home Assistant discovery |
| **Containers** | Guided App Store and image deployment, Docker Compose import, independent or sidecar pods, guarded Kubernetes workload rename, edit/move/logs/console, autostart, LAN port and exposure editing, one storage picker for new and existing containers, hardware passthrough, update checks, monitored rollout with live image-pull state, rollback, groups with folding dividers and a filter per group, and a card or row layout |
| **Helm** | Every Helm release in the cluster with its values, notes, history and objects; charts found on Artifact Hub and installed, upgraded and uninstalled through the Helm controller k3s and RKE2 ship |
| **App Store** | The Community Applications catalogue laid out as Unraid shows it - monthly spotlights, recently added, trending and top performing - with a full page per app, from the public feed or one you set |
| **Virtual machines** | Create from a Harvester image, a download or an imported disk - on Harvester, or on k3s and RKE2 with KubeVirt installed from Settings - power actions, live migration between hosts, a console (the VM's screen or its serial port), and a k3s cluster made of VMs, with KubeVirt inside if asked |
| **Portal** | A page of tiles for every web interface - containers picked from their exposed ports with their logos, and the router, switches, access points and NAS around them - in sections, with a live reachability dot |
| **Architecture** | VIP → workload → Longhorn volume → replica dependency view, with containers and VMs separated, Homestead helper pods hidden, and unreferenced volumes behind a disconnected-data switch |
| **Networking** | Service, ClusterIP, VIP, ingress, listener ownership, orphaned-listener release, endpoint health and guided collision-free exposure; IP address management per subnet with scanning, device categories, bulk edits, CSV export and UniFi sync |
| **Cluster** | k3s, RKE2 or Harvester version, control-plane and etcd quorum, node pressure, critical services, certificate requests, adding a host (k3s and RKE2 join commands, or a guide to Harvester's installer), and removing hosts - including ones that are dead for good |
| **Between clusters** | Browse another Homestead cluster, check the two releases can talk, and move its containers and VMs here through shared backup storage |
| **Storage** | RWO/RWX volume creation, growth and guarded deletion, file browsing and editing, storage-class inventory and creation, usage, health, snapshots, backups and recurring jobs |
| **Hardware** | Host device browser and reusable mappings for iGPU, Coral, USB/PCIe and other devices |
| **Import** | Docker Compose files checked as you type, Unraid/Docker workload and appdata import, several folders across several volumes, measured sizing with a per-volume capacity preflight, byte-weighted progress, named failures, editable seed configuration |
| **Resources** | Every kind the cluster serves, custom resources included, with the API server's own columns; any object as YAML with its events, edited, deleted or created from YAML - what a Headlamp user reaches for |
| **Administration** | Direct URLs/breadcrumbs, persistent activity tray with cancel and roll back for every job, viewer/operator/admin roles, namespaces for your apps, appearance, thresholds and version details |
| **App & alerts** | Installable on phones and desktops over HTTPS, with push notifications for outages, degraded storage and workloads, failed jobs, joining hosts and image updates |

## Install with Helm

Each release publishes a chart to GitHub's registry, with the node probe
included (switch it off with `nodeprobe.enabled=false`):

```bash
helm install homestead oci://ghcr.io/wjcloudy/charts/homestead -n homestead --create-namespace --set service.loadBalancerIP=192.0.2.242
```

On k3s with its built-in ServiceLB there is no address to choose - Homestead
answers on every node's own address - and storage defaults to the cluster's
class (`local-path`, or Longhorn once installed):

```bash
helm install homestead oci://ghcr.io/wjcloudy/charts/homestead -n homestead --create-namespace --set service.kubeVip=false
```

Homestead runs in its own namespace and deploys apps, shares and the probe to
`lab` (`workloadNamespace.name`), creating it if needed and keeping it if the
chart is ever uninstalled - as it keeps its own data volume. Values worth
knowing: `service.loadBalancerIP` (with `service.kubeVip`, on by default for
Harvester), `persistence.storageClass` and `storageClass` (empty: the
cluster's default), `cloudflareAccess.*`. `helm show values
oci://ghcr.io/wjcloudy/charts/homestead` lists them all. The chart is made
from the same manifests as `deploy/` by `scripts/render_chart.py`, so both
install the same thing; there is one Homestead per cluster.

## Installing with the manifest

On k3s the [one-line install](#one-line-install-and-node-doctor) does all of this for
you. This is applying `deploy/deploy.yaml` yourself - on Harvester, RKE2, or a
k3s or other cluster you already run.

### 1. Check storage

Homestead keeps its settings, history and cache on a 2 GiB volume - Longhorn
and ReadWriteMany by default. A tightly scoped init container assigns that
volume to Homestead's non-root UID on first start; the application container
itself remains non-root with a read-only root filesystem. Confirm the
StorageClass used in `deploy/deploy.yaml` exists:

```bash
kubectl get storageclass
```

The supplied manifest uses `longhorn-r2`. Change `storageClassName` (and the
`STORAGE_CLASS` value) to a class the cluster has. Without Longhorn - k3s's
`local-path`, say - use that class and change the claim to `ReadWriteOnce`,
as the k3s script does.

### 2. Configure and install

Review these values in `deploy/deploy.yaml` before applying it:

- `image`: published image/tag to run;
- `storageClassName`: Longhorn StorageClass;
- `LB_IP` and `kube-vip.io/loadbalancerIPs`: Homestead's LAN address (with
  MetalLB, keep `LB_IP` and drop the kube-vip annotation; on k3s's ServiceLB
  drop both - Homestead answers on the nodes' own addresses);
- `DEFAULT_NS`: default namespace for newly created workloads.

Then install and wait for readiness:

```bash
kubectl apply -f deploy/deploy.yaml
kubectl -n lab rollout status deployment/homestead --timeout=5m
kubectl -n lab get deployment/homestead pvc/homestead-data service/homestead
```

Everything a new install creates is called `homestead`: the Deployment and its
containers, the ServiceAccount and roles, the `homestead-data` claim, the
Service, the ConfigMaps and Secrets it writes, and the `homestead.io/*`
annotation domain.

Open the Service address on port `8088`. The first visit creates the initial
administrator. Authentication is stored in a Kubernetes Secret, independently
of the container and Longhorn volume.

For a private fork/package, add an `imagePullSecrets` entry to the Deployment
and create a `docker-registry` secret using a classic GitHub token with only
`read:packages`. GitHub documents this in
[Working with the Container registry](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry).

### 3. Optional node telemetry and drive health

Kubernetes does not expose physical temperatures, host device inventory,
per-disk throughput, or SMART health. Install the node probe to enable those
views:

```bash
kubectl apply -f deploy/nodeprobe.yaml
kubectl -n lab rollout status daemonset/homestead-nodeprobe --timeout=5m
```

The ordinary telemetry container mounts `/sys`, `/proc`, and `/dev` read-only,
drops all capabilities, runs as a non-root user, and uses a read-only root
filesystem. An isolated `smart` sidecar runs `smartctl` against physical host
drives. Because generic Kubernetes cannot grant an unknown, changing set of
block devices individually, that sidecar alone is privileged; it still has no
host PID, IPC, or network namespace, uses a read-only root filesystem, and
accepts test requests only when they carry a short-lived signature from the
Homestead backend. Remove the sidecar if SMART access is not wanted—the normal
temperature, device, and throughput telemetry keeps working.

Node detail shows drive identity, capacity, temperature, SMART health, error
counters, power-on hours, and per-disk read/write MB/s. Administrators can run
short or extended self-tests after an explicit confirmation; tests and their
progress remain in Activity through browser refreshes and Homestead restarts.
Settings → Health thresholds controls temperature and media-error warnings.
USB/SATA bridges and virtual disks that do not expose SMART are labelled as
unsupported instead of being treated as failed drives.

Homestead works without either probe container. When the SMART sidecar is not
installed or cannot read a drive, the UI reports that state without degrading
the whole cluster.

### Import qcow2 and vmdk VM disks

Import → **VM disk** uses KubeVirt CDI to stream an HTTP(S) disk image into a
new Longhorn PVC. CDI auto-detects and converts common QEMU formats including
qcow2, vmdk, raw, vdi, vhd, and vhdx. Homestead checks both the DataVolume and
PVC name before starting and never overwrites an existing disk. Optional
SHA-256/SHA-512 verification, CDI credential Secrets, and private-CA ConfigMaps
are supported.

Download/conversion progress survives page refreshes and Homestead restarts in
the Activity tray. When the DataVolume reaches `Succeeded`, choose **Create VM**
to attach that exact disk without another copy. An imported RWO disk already
referenced by a VM is not offered for a second attachment. Source URLs (which
may contain signed query parameters) are intentionally excluded from Homestead's
inventory and operation-history responses; Kubernetes administrators can still
read the source from the DataVolume itself.

CDI must be installed in the cluster. Harvester includes it, and the supplied
RBAC permits Homestead to create and monitor DataVolumes while ordinary HTTP
credentials remain in namespace-scoped Kubernetes Secrets.

## App Store

The App Store reads the Community Applications catalogue - the templates behind
Unraid's Apps tab - and lists what can run as a container here. Plugins,
language packs and templates the catalogue has blacklisted, deprecated or
hidden are Unraid's own and are left out, so the listings match what Unraid
shows.

It opens on a front page laid out as Community Applications lays it out:

- **Spotlight** - the app the Unraid team picks each month, newest first, with
  the month and why it was picked;
- **Recently added** - the newest templates, in the order the feed added them;
- **Top trending** - the apps rising fastest in downloads;
- **Top performing** - the feed's best performers overall.

Each section has a page of its own, and search covers the whole catalogue.
Clicking an app opens its full page: the whole description, its maintainer and
whether the container is official, links to its project, support thread,
registry, read-me, video and Discord, the spotlight note, any comment from the
catalogue's moderators, what it requires, screenshots, and when it was added and
last updated. The same page shows Homestead's own review of the template - the
storage, network, dependency and hardware questions it will ask - before you
choose **Configure & deploy**.

### From template to workload

Homestead translates each template into Kubernetes ports, environment
variables, device hints, and explicit storage choices. Its compatibility
analyser is application-agnostic: port/protocol patterns determine network
intent, path and description semantics distinguish config/data/media/cache,
variable metadata identifies secrets, options, and external dependencies, and
requests for container-runtime sockets are blocked for a Kubernetes-specific
design. These rules apply to every catalogue image rather than a list of named
apps.

Public catalogue password defaults are discarded and regenerated locally.
Secret values are masked in manifest previews. Template option lists become
select controls, and the same storage, network, dependency, and hardware review
is used whether deployment begins in App Store or directly from Deploy. An image
given without a tag is deployed as `:latest`, which is what Docker would pull;
the form shows the fully resolved registry, repository and tag without rewriting
the template. This also disambiguates legitimate names such as the
`openspeedtest/latest` repository, whose default pull ends in `latest:latest`.
Plex, Pi-hole, and Nextcloud are release validation examples, not special-case
profiles.

### Choosing the catalogue

**Settings → App Store catalogue** points the App Store at any feed in the
Community Applications format - a mirror, or your own list of templates - and
**Use Community Applications** goes back to the public one. The App Store says
which feed it is reading. `COMMUNITY_CATALOG_URL` sets the default for a whole
deployment.

### Community catalogue data notice

The catalogue adapter reads the public Community Applications feed from
`Squidly271/AppFeed` at runtime, unless another feed is set; catalogue content
is not bundled into the Homestead image or repository. Requests identify
Homestead and results are cached for six hours. Homestead links to and credits
the upstream source, links each app to its own project and support pages, and
does not use Unraid logos or imply endorsement.

As of 20 September 2026, GitHub reports no declared repository license for the
`AppFeed` repository. The Community Applications plugin source contains GPLv2
headers, but that alone does not establish a license for generated feed metadata,
listing descriptions, or icons. Operators redistributing or commercially hosting
the catalogue should obtain permission from its maintainers/Lime Technology or
configure a feed whose reuse terms are explicit.

## Helm

**Helm** lists every Helm release in the cluster - chart and app version,
status, revision, when it last changed - whoever installed it. Helm keeps each
revision in a Secret of its own, and that record is the same whether a person,
Rancher, Fleet or RKE2 ran Helm, so reading it misses nothing. A release opens
to the values it was installed with, the chart's notes, its history and the
objects it made. The platform's own releases (Harvester, Rancher, Longhorn and
the like) are hidden until asked for.

**Install chart** searches Artifact Hub, shows the chart's versions and its
default values beside yours, and installs it as a `HelmChart` object for the
Helm controller RKE2 already runs: Homestead carries no Helm of its own, and a
chart it installed is an ordinary object anyone can see with kubectl. Changing
the version or values of such a release upgrades it; uninstalling removes the
object and the controller removes what the chart made. A repository can also be
entered by hand, including an `oci://` registry. Releases installed some other
way are shown, not changed - whatever installed them would change them back.
Installing, upgrading and uninstalling are admin actions, followed in the job
tray.

## Resources

Homestead's own pages cover what a homelab mostly does; **System → Resources**
covers the rest, for anyone used to Headlamp, Lens or kubectl. It lists every
kind the API server serves - built in, Harvester's, Longhorn's, KubeVirt's and
any custom resource - grouped as Headlamp groups them (Workloads, Network,
Storage, Configuration, Access, Cluster, Custom resources). Each kind is listed
with the columns the API server itself prints for it, the ones `kubectl get`
shows, so a custom resource gets its own columns too. An object opens as YAML,
without the server's bookkeeping (managed fields, last-applied), with its
recent events, and a pod with its logs.

Admins can edit an object's YAML and save it - saved as a replace carrying the
version it was read at, so a change made elsewhere meanwhile is refused rather
than overwritten - delete one after typing its name, or create objects from
pasted YAML, several documents at once. Secrets are listed with their keys;
their values are sent only when an admin asks to reveal them. To serve this,
Homestead's role reads every kind and can change any; that was already
possible through its permission to extend its own role, and is now stated
plainly in `deploy/rbac.yaml`.

## Networking and virtual IPs

Networking reconciles Kubernetes Services, EndpointSlices, Ingresses, node
addresses, kube-vip status, and Harvester IP pools in one view. It shows the
live path from each VIP and listener through its Service to ready pod endpoints,
including the owning node and a direct access link where the protocol is
browser-friendly.

**Expose workload** creates either a cluster-only Service or a LAN-facing
LoadBalancer Service. Automatic allocation chooses an unused IPv4 address from
a visible Harvester IP pool; shared allocation reuses Homestead's VIP only when
the protocol/port tuple is free; manual allocation validates the address and
warns when it is outside the configured pools. Every flow shows a review plan
and collision check before Kubernetes is changed. Homestead deliberately
reconciles the pool against live Services and node addresses because a manually
requested kube-vip address may not be reflected in an IPPool's reported free
count.

The supplied RBAC is read-only for EndpointSlices, Ingresses, and Harvester IP
pools. Service creation uses the existing Service permission and copies the
selected Deployment's selector from the server rather than trusting browser
input.

### Your VIPs

kube-vip announces whatever address a Service asks for, and a plain Harvester
install has nothing handing addresses out. **Networking → Services & VIPs →
＋ Add VIPs** keeps addresses for Homestead to give to Services - one, or a
range of up to 64, with a label saying what they are for. Keep them outside
your router's DHCP range. Automatic VIPs come from these first, then from any
Harvester IP pool, and the **Specific VIP** picker on Deploy, Import and
elsewhere lists them by label. Node addresses and addresses already recorded
as a device under IP addresses are refused, and a VIP can be let go only while
nothing uses it. They appear under IP addresses as VIPs too.

The first network share installs Samba, and asks which address it answers on;
Settings → Cluster → Add-ons manages the SMB server afterwards. Samba is put in place
before anything of the share is made, so a share that cannot be served leaves
nothing behind.

### IP addresses

Networking's **IP addresses** tab keeps, per subnet, what lives at each
address: a name, the MAC, how it gets the address (static, DHCP reservation,
DHCP, held for later, network gear), a device category (router or firewall,
switch, access point, server, NAS, IoT, CCTV, printer, computer, phone, TV or
media, other) shown as an icon, an owner, tags and notes. What the cluster
uses - node addresses, load balancer VIPs, Harvester's VIP pools - is merged
in live and cannot be edited here. Each subnet shows how much is used, its DHCP
range, and the next addresses free for static use outside DHCP and the VIP
pools. A static address or a VIP inside the DHCP range is flagged, as is a
Harvester VIP pool overlapping it: the DHCP server may hand those addresses to
something else.

**Scan now** probes every address in the subnet from Homestead's pod with TCP
connections to common ports - a refused connection proves a host as well as
an accepted one - and adds reverse DNS; a host that answers but is not
documented is flagged. Tick addresses to set a category, kind, tag or owner on
all of them at once, or forget them. The runs of free addresses between
documented ones show as dividers that open to list each free address, with
how much of the run is inside the DHCP range, and any one can be documented
from there. **Export CSV** downloads the subnet; **Import CSV** documents
addresses from a spreadsheet (a template is offered): a blank cell leaves what
is recorded, unknown columns are skipped so an export imports back, and every
row is checked before anything is written.

**UniFi** is optional: without it the tab works from scans and what you write,
and shows nothing of UniFi. Connected under **Settings → Apps → UniFi
Network**, it brings in what a UniFi Network controller knows, read-only: its
clients and devices with their MACs, the reserved (fixed) IPs even for clients
that are offline, and its networks, whose DHCP range, gateway and VLAN fill in
a matching subnet or are offered as new ones. UniFi's nickname and hostname
for each client are kept beside the name you give an address, never over it.
UniFi devices get a category from their model (switches, access points,
gateways, cameras). It uses an API key from the console (Settings → Control
Plane → Integrations), kept in the `homestead-unifi` Secret; reservations and
networks come from the controller's classic API, and the sync says so if the
key is not allowed there. **Disconnect** deletes the key and hides everything
UniFi supplied. The rest lives in the `homestead-ipam` ConfigMap.

## Cluster health and node onboarding

System → **Cluster** separates platform health from application health. It
shows the k3s, RKE2 or Harvester version, control-plane readiness, etcd
quorum and failure margin, node roles and pressure conditions, observed core
services, certificate-signing requests, and platform warning events from the
last 24 hours. Partial RBAC or API availability is reported per section instead
of hiding the rest of the page.

### Adding a host

On **k3s and RKE2**, **Add a host** on the Cluster page gives the commands to
run on the new machine - one for a worker, one for another server - with this
cluster's server address and version filled in, and where on a server to find
the join token (Homestead never reads it). A machine made with the k3s script
joins with the script's `agent` or `join`.

On **Harvester**, **Add a host** is a guide to Harvester's own installer.
An unattended install needs the new machine's install disk and network card
named in advance, and those are only known once the machine is in front of
you, so you choose them in the installer; the guide covers the rest:

1. **The ISO** - a direct link to the Harvester release this cluster runs, for
   its CPU architecture, with its checksums. Write it to a USB stick (Rufus in
   DD mode, or balenaEtcher) or mount it from the server's BMC.
2. **The cluster token** - Homestead never reads or stores it. The guide gives
   the two commands that print it on a management node, with that node's
   address filled in.
3. **Each installer screen** - join an existing cluster, the node role (with
   how many management nodes the cluster already has), disks, hostname (the
   next free name), management network, and the values Homestead reads from the
   cluster: its VIP, NTP servers and proxy.
4. **Watching it join** - the new host appears in the guide as soon as
   Harvester registers it, and again when it is Ready.

### Removing a host

**Remove from cluster** (host actions, or the cleanup list on the Cluster page)
checks first: a Ready node is refused, because a running node registers itself
again - on the host come draining it and k3s's uninstall script
(`k3s-agent-uninstall.sh`, or `k3s-uninstall.sh` on a server), RKE2's
`rke2-uninstall.sh`, or on Harvester maintenance mode and its RKE2 uninstall;
the page gives the right one. Removing
the last control-plane node, or one without which etcd loses quorum, is refused;
volumes whose only healthy copy is on the node must be given up explicitly. It
then follows the platform's order: Longhorn stops scheduling there, the
Kubernetes node is deleted (k3s and RKE2 drop its etcd membership), then - on
Harvester - its Cluster API machine, then Longhorn's node record once no
replicas are listed on it.

A host that is **gone for good** - dead and never coming back - can be removed
that way too. Nothing then waits on it: the pods and VMs still bound to it are
force-stopped so they start on other hosts, its volume attachments are
released so those volumes can attach elsewhere, the replica records Longhorn
keeps for it are deleted so it rebuilds them from the remaining copies, and a
Cluster API machine whose deletion hangs on the missing host has its finalizers
cleared. The Cluster page also lists leftovers - machines with no node or stuck
deleting, Longhorn nodes with no host - to delete, or force, one at a time.

Certificate monitoring is read-only. The supplied ClusterRole may list
Kubernetes certificate-signing requests but cannot approve them, and Homestead
never returns CSR bodies, issued certificates, or private keys to the browser.

### Harvester releases and upgrades

The Cluster page lists what there is to upgrade to: the newest stable Harvester
release and the newest test build (release candidates and development builds
newer than that), each with its release notes, from Harvester's GitHub
releases checked at most hourly. A version this cluster's Harvester itself
lists is marked **offered by Harvester** - only those get an Upgrade button in
Harvester's dashboard, often a few days after release; a test build never
does.

Homestead does not start upgrades: one rewrites every host, and that belongs
in Harvester's own dashboard. It follows one instead. While an upgrade runs,
the card shows each stage - upgrade image, package repository, node
preparation, system services, nodes - and each host's state, and the last
upgrade stays shown once it has finished or failed. With notifications on,
an upgrade starting, finishing or failing is announced, as is a new stable
release (under Updates). Reading this needs the `versions` and `upgrades`
resources in `harvesterhci.io`, which Homestead's permissions include.

## Publishing through a Cloudflare Tunnel

Homestead can deploy, delete, open shells in and power off anything in the
cluster, so treat a published hostname as a way into the whole cluster:

- **Put Cloudflare Access in front**, with an allow policy for named people and
  a second factor. Homestead's own sign-in stays on as the second lock.
- **Set `CF_ACCESS_TEAM_DOMAIN` and `CF_ACCESS_AUD`** on the Deployment. Homestead
  then checks Access's signature on every request that came through Cloudflare
  and refuses the rest, so an Access bypass rule or a policy on the wrong
  hostname does not quietly publish it.
- **Point the tunnel at the Service inside the cluster** -
  `http://homestead.lab.svc:8088` with the supplied manifest - not at the LAN
  VIP, and run `cloudflared` in the cluster.
- **Finish setup on the LAN first.** Creating the first administrator is refused
  through the tunnel.

Every response forbids framing and scripts from elsewhere, sessions are signed and
`Secure` behind TLS, requests over 8 MB are refused, and sign-in attempts are
limited per address and per account, using Cloudflare's client address rather
than a header the client can write.

## Long-term stats

The Dashboard's charts cover the last hour. Its **Over time** card covers up to
ninety days: cluster CPU and RAM (average and peak), network in and out,
workload pods, and each node's availability - the share of samples it was
Ready - with its average CPU and RAM. Homestead records a sample every five
minutes whether or not a browser is open (the leading replica does, in the
background), keeps them for two days, and keeps hourly averages and peaks for
ninety, in `history.json` on its data volume - a few hundred kilobytes at most.
It answers "was it busy last week?" and "has a node been dropping out?";
Harvester's own monitoring (Prometheus and Grafana) is there for anything
deeper.

## MQTT and Home Assistant

**Settings → MQTT** publishes cluster and node stats to an MQTT broker, with
Home Assistant discovery: nodes ready, volumes degraded or faulted, pods
running and failing, VMs, cluster health, CPU and RAM for the cluster, and for
each node its CPU, RAM, network in and out, pods, VMs, workloads and status.
The topics (`harvester/cluster/state`, `harvester/node/<node>/state`), entity
names and unique ids are the ones the standalone hv-exporter used, so Home
Assistant keeps its entities and their history when Homestead takes over; the
card says when hv-exporter is still running and gives the commands to remove
it. Availability follows Homestead: the broker marks the entities unavailable
if Homestead stops without saying goodbye. MQTT 3.1.1 is spoken directly, with
optional username, password (kept in a Secret) and TLS, and only the leading
replica publishes. **Test connection** checks the broker, and **What is
published** shows the messages.

## Installing the app and getting notifications

Opened over HTTPS — through a Cloudflare Tunnel, or a reverse proxy with a
certificate — Homestead can be installed as an app, and can notify each device
when something needs you, even with Homestead closed. Browsers offer neither on
a plain-HTTP LAN address; Settings says so there instead.

1. Open Homestead's `https://` address. Chrome and Edge offer **Install app** in
   Settings; on iPhone and iPad (iOS 16.4 or later), tap Share → **Add to Home
   Screen** and open Homestead from the Home Screen, since iOS delivers
   notifications only to installed web apps.
2. In **Settings → Notifications on this device**, choose **Turn on
   notifications** and pick what this device hears about:

| Kind | What it covers |
|---|---|
| Outages | a node not Ready, a volume faulted, a drive failing SMART |
| Degraded | a volume rebuilding a replica, a workload not ready after its start-up grace |
| Failed jobs | anything in the activity tray that ends in failure |
| Hosts joining | a new host registering with the cluster, and becoming Ready |
| Image updates | a newer image for a workload (off by default; checked every six hours) |

A problem is announced once it has lasted a minute, so restarts and rollouts do
not buzz a phone, and announced again when it is over; the second notification
replaces the first. **Send a test** checks the whole path.

How it works: pushes carry nothing. Encrypting a payload needs a crypto library
Homestead does not have, so Homestead sends an empty push signed with its VAPID
key (kept in `DATA_DIR`), and the app's service worker then fetches what
happened over its own signed-in connection. Nothing about the cluster passes
through Google's, Mozilla's or Apple's push service. If the session has expired,
the notification just says to open Homestead and sign in. Signing out on a
device stops its notifications; so does removing its user.

Homestead needs to reach the push services on the internet (for example
`fcm.googleapis.com`, `updates.push.services.mozilla.com`, `web.push.apple.com`,
`*.notify.windows.com`); it POSTs only to those hosts. Behind Cloudflare Access,
the manifest and icons are served without Access's signature so the browser can
install the app; if installation still fails, add an Access **Bypass** policy for
`/manifest.webmanifest` and `/icons/*`.

## Homestead's own health

**Settings → About** shows whether the parts that work in the background are
working, refreshed every 15 seconds while it is open: how quickly the
Kubernetes API answers, each copy of Homestead and which one leads, every
background task (live charts, alerts, long-term stats, hardware detection,
cluster moves) with when it last did its work and its last error, the node
probe (nodes running it, reporting, and with drive health), SMB status, the
permissions check, backup storage and MQTT. SMB can be switched off from Settings → Cluster → Add-ons -
shares stop being served, and their volumes, settings and passwords are kept -
and on again, which installs it if the cluster has none.

A VM whose disk has waited to start downloading for more than three minutes
says why, from CDI's importer pod and the claims it waits on.

## Redundancy: more than one Homestead

**Settings → About → Redundancy** sets how many copies of Homestead run, one
to three. With two or more, spread over different nodes where the scheduler
can, a node failure leaves another copy already answering: the Service drops
the dead one, and nothing waits for Kubernetes to start a replacement.

The copies share the RWX data volume. Work that must happen once - raising
alerts and sending push notifications, advancing moves between clusters - is
done by a leader, elected with a Kubernetes Lease (`homestead-leader`) the
way Kubernetes' own controllers elect theirs; if the leader's node dies the
lease runs out and another copy takes over within about fifteen seconds, and
a copy shutting down hands it over at once. Files on the data volume that are
read and written back - job records, alert history, push subscriptions,
moves - are changed under a lock every copy honours (an `flock` on the shared
volume, which Longhorn's NFS-backed RWX volumes carry between pods), and
written through temporary files of their own. Sign-ins are signed tokens, so
any copy accepts them. Updates roll one copy at a time, so an update no
longer takes Homestead away either.

Copies on different nodes all mount Homestead's data claim, so more than one
copy needs a claim every node can mount: ReadWriteMany on a class Longhorn
serves through its share manager. A migratable class - Harvester's own and
`longhorn-r2` - gives a VM-disk volume only one node can mount, and a copy on
a second node would wait forever. Redundancy says so and offers **Move data**:
a job on the node that has the volume attached copies it to a new claim on a
shareable class (the stock `longhorn`, say), and Homestead restarts once onto
it; the old claim is kept until you delete it. A rollout that stalls shows
why - a volume that will not attach or mount, or a pod that cannot be placed -
beside the pod.

## Updating Homestead

Homestead appears in its own Containers page. **Check images** compares the running
digest with GHCR, and a newer stable semver tag is suggested when one exists.
Installing the update pins the selected manifest digest, watches Deployment and
pod readiness, and keeps the previous digest for one-click rollback. The UI
automatically reconnects while Homestead replaces itself.

Available updates and registry-check failures also appear in the global header
without repeated alert noise. Settings → Container image update policy supports
notify-only, explicit operator approval, or a UTC maintenance window. These
rules are enforced by the server, every rollout still requires acknowledgement,
and semantic-version discovery never silently crosses a major release. Image
pulls, updates, and rollbacks remain visible in the persistent Activity tray
through browser refreshes and Homestead restarts.

Every job has **Log**: each step it has taken, with the time, and - where
there is one - the output of what does its work, following along while it
runs: an import's or storage move's copy, a Helm or backup run, a rollout's
newest pod (its events and log), CDI's importer, and each node's console as a
k3s cluster installs itself.

Every job still running has **Cancel**. It first says what cancelling would
do: what is put back, what stays as it is, and anything Kubernetes cannot take
back. Where it can, a cancel rolls back - a deploy is removed, an update or
edit returns to the version before it, a volume move starts everything again
on its untouched original, a k3s cluster's VMs, disks and addresses are
removed, a restore or disk import deletes its half-filled claim. Where what is
done cannot be undone, it stops the rest (an image cleanup keeps the nodes it
already cleaned). A step that must not be interrupted, such as a volume swap,
is refused until it has finished, and the few things Kubernetes cannot take
back once asked, such as a volume deletion, are only no longer tracked.
Cancelling something that deletes VMs or stops a volume move needs an admin.

### Permissions look after themselves

The in-app update replaces Homestead's image, and a new release can need
permissions the old one did not. Homestead carries its manifest inside the image
and, on start, makes its own ClusterRole exactly what that release describes -
adding what new features need and dropping what nothing uses any more - so an
upgrade needs no `kubectl`. Rules added to that role by hand do not survive
this; give anything extra its own role and binding.
**Settings → About this installation → Permissions** says what it last did.

That needs the right to edit its own role, which an older install does not
have yet. Grant it once, wherever you use `kubectl` (a Rancher
**Kubectl Shell** will do):

```bash
kubectl apply -f https://raw.githubusercontent.com/wjcloudy/homestead/v2.8.179/deploy/rbac.yaml
```

`deploy/rbac.yaml` holds only the permissions - the ServiceAccount, roles and
bindings from `deploy/deploy.yaml` - so it leaves your Deployment, Service,
address and storage alone. Settings shows this command whenever Homestead finds
it cannot update its role.

Command-line deployment is also available:

```bash
TAG=2.8.179 HOST=rancher@your-harvester-node ./scripts/deploy.sh
```

## Image update behaviour

- Public Docker Hub and OCI registries are checked anonymously.
- Private registries use only the workload's referenced `imagePullSecrets` (or
  its ServiceAccount pull secrets). Credentials never enter API responses or
  update-history files.
- Mutable tags such as `latest` are compared by digest.
- Stable `v1.2.3`/`1.2.3` images can move to the newest stable version in the
  same major release. Pre-releases and major-version jumps are not automatic.
- Multi-architecture index digests and their platform-specific child digests
  are treated as the same release, avoiding false update notifications.
- Rollback restores the exact previous digest rather than trusting a mutable tag.

## Image cache cleanup

Image Cache groups kubelet aliases by digest and labels images retained by a
running pod, a container scaled to zero or a scheduled job (which start from
it), or the immediate managed-update rollback. Admins can remove an
unreferenced application digest from selected nodes after an impact preview and
typed confirmation. The backend rechecks live references immediately before
starting one short-lived cleanup pod per node; active, stopped, scheduled,
rollback, and recognized Harvester/Kubernetes platform images are refused. Cleanup pods mount only the
host RKE2 `crictl` binary and containerd socket, run as root without Linux
capabilities or privilege escalation, and report progress through Activity.
Kubernetes Node status exposes only each node's fifty largest cached images, so
Homestead asks each node's containerd for all of them - whenever its last answer
is more than fifteen minutes old - and keeps that answer on its data volume,
where every replica reads it and it survives a restart. Images pulled since the
last scan are added from what Kubernetes reports.

## Safe volume deletion

Volume deletion is available only to administrators and always starts with a
fresh impact preview. Homestead checks pods and workload controllers, VM disk
references, live Longhorn attachment, replica count, written data, snapshots,
external backups, and the PV reclaim policy. Any workload reference or active
attachment blocks deletion; Homestead never stops or rewrites a workload as a
side effect of deleting storage.

After a claim is detached and unreferenced, the administrator explicitly
chooses between deleting the PVC while forcing its PV to `Retain`, or permanently
deleting the PVC and backing Longhorn data with the PV policy set to `Delete`.
The exact claim name must be typed before either action. System namespaces and
Homestead's own `homestead-data` claim are protected, and the supplied RBAC grants
only the additional PV `patch` permission required to make the chosen reclaim
behavior deterministic.

## Data protection

Data Protection drives Longhorn's own recurring jobs rather than a scheduler of
Homestead's: a job has a task (snapshot, backup, trim, cleanup), a schedule, how
many to keep, and the volume groups it covers, and Longhorn runs it. A volume
joins a group, or a single job, by a label; every volume is in `default`.

Schedules are chosen as shapes - every few minutes or hours, every day, chosen
weekdays, monthly - and written to cron for you; anything else stays editable
as cron. Longhorn keeps time in UTC, so schedules are set in UTC and the editor
shows the next three runs in your own time. Each job shows when it runs next,
when it last ran and whether that run failed, and **Run now** starts it at once
from the schedule Longhorn made, tracked in the job tray.

**Plans** set up a policy in one go - snapshots (hourly kept for a day, daily
kept for a week), snapshots and backups (adding daily and weekly backups to
the backup target), or housekeeping (weekly trim and snapshot cleanup) - for
whichever group you pick. The jobs a plan makes are ordinary jobs afterwards.

Any volume's snapshots and backups are also a click away on Volumes: take one
now, back up now, or restore.

## Restore a Longhorn backup

Completed backups have a **Restore** action in Data Protection. Homestead always
restores into a newly named PVC: it checks the namespace and destination name
first, refuses to overwrite an existing claim, and enforces a capacity at least
as large as the backed-up volume. RWO and RWX claims are supported, with an
explicit replica count.

The restore uses Longhorn's documented CSI `fromBackup` StorageClass parameter.
Homestead creates a deterministic, reusable restore class derived from the
backup URL and selected replica count, then submits an ordinary PVC to the
Longhorn CSI provisioner. It never fabricates a PV or mutates the source volume
or backup. The Activity tray persists the operation and reports provisioning,
per-replica restore percentage, replica-health waiting, and actionable Longhorn
errors across browser or Homestead restarts. See the
[Longhorn StorageClass parameters](https://longhorn.io/docs/1.12.1/references/storage-class-parameters/)
reference for the underlying mechanism.

## Moving workloads between clusters

A container or VM can move from one Harvester cluster to another when both run
Homestead. The destination does the work: it asks the other Homestead for the
workload's definition directly, and reads its volumes from the Longhorn backups
the other cluster wrote, so the source never holds credentials for the
destination.

1. **Give the source cluster backup storage.** Its card here offers **Set it
   up** once the cluster is added (it uses the admin account you gave), or on
   that cluster Data Protection ▸ **Set up storage** does the same: it runs
   an S3 server (RustFS - MinIO's images are no longer published) on a Longhorn
   volume, creates the bucket, and points Longhorn's backups at it.
   Give it a LAN address, or the other cluster cannot read from it. The address
   belongs to the source cluster - its load balancer announces the store on it -
   and the destination only connects to it. The choices offered are the source's
   own VIPs, then free addresses in its Harvester IP pools. It shares
   fate with the cluster it protects: it is for moving workloads, not for your
   only copy of anything.
2. **On the destination, add the source.** Import ▸ **＋ Homestead cluster**
   takes the other Homestead's address (for example `http://192.0.2.242:8088`)
   and a Homestead account there - not a Harvester or SSH login; operator is
   enough to browse, a move needs admin. The password is kept in a Secret. The
   cluster's card shows the other Homestead's release and whether the two can
   move workloads between them - and which side to update if not; different
   releases that speak the same move protocol work together.
3. **Browse and move.** **Browse workloads** lists what the other cluster runs,
   with the reason anything cannot move (for example a ConfigMap, Secret or
   host folder, which Homestead did not write and cannot rebuild). A passed-through
   device such as `/dev/dri` travels, with a note that this cluster needs a host
   that has it. Each cluster's card lists what a move still needs, with the fix
   beside each step, and the move review offers the same fixes. **Move to this
   cluster** reviews the namespace, address and storage here, and lists every
   blocker and warning from both clusters before anything stops.

The move stops the workload there, backs up its volumes, restores them here,
creates the workload and starts it, following each step in Activity. If the two
clusters use different backup targets, this cluster's Longhorn target is pointed
at the source's bucket, and the review says so first. The move survives a
restart of either Homestead and has no timeout that would abandon a large
volume. The original stays on the source, stopped, until you remove it, so it
can be put back at any point.

## Importing from Unraid

An Unraid container usually maps several host folders, and they do not all
belong in the same place: appdata wants a small replicated claim, recordings or
media want a large one. An import can therefore fill **several volumes at
once** — define the volumes, then point each folder at the one it belongs to.
A folder sharing a volume with others is copied into its own subdirectory; a
folder that has a volume to itself takes its root. Either way it is mounted
back at the path the container expects, through `subPath`, and the workload
ends up with one volume entry per claim rather than one per folder. Each folder
is **Copy** (its files come across), **Mount empty** (the volume is mounted at
that path with nothing copied - new recordings on a new volume, the old ones
left behind) or **Leave out**.

App Store installs lay an app out the same way: the paths an Unraid template
maps into appdata share one `<app>-appdata` volume, each in a folder named
after its path, sized for all of them. Media stays for you to point at a
library, and cache stays scratch space. Any storage row in the deploy wizard or
the container editor has a **Folder in volume** field, so the same layout can
be built by hand: give two rows the same new volume name and the second
becomes another folder in it.

The container editor can also restructure storage after the fact — combine two
volumes into folders of one, split a folder out to a volume of its own, or
rename a folder. When a path moves, the editor offers to bring its data: the
save is applied with the container stopped, a job copies each old location to
its new one (keeping owners and times), and the container starts again. A new
volume, copied into or started empty, is owned as the old location was: a new
volume is root's, and an app that runs as its own user could not write to it. The old
volumes are only read, never deleted, so pointing the paths back undoes it;
remove them from Volumes once you are happy. Progress is in the job tray, and
the steps carry on from where they were if Homestead restarts part-way.

The same RAM-backed volume is offered wherever storage is chosen — the deploy
wizard, the container editor and the import form all share one picker — so an
app that wants a cache in memory can have one without being imported first.

A tmpfs mount is not copied at all. On Unraid, Frigate's `/tmp/cache` is a RAM
disk that starts empty every boot, so it imports as a memory-backed `emptyDir`
of the same size rather than as a claim full of last week's cache: the same
speed and the same volatility, capped so it cannot eat a node's memory. They
used to be dropped silently, leaving the container to write cache onto its own
filesystem.

Measure sizes runs `du` on the source under a per-folder timeout, fills in each
volume's size from what is actually there, and lets the copy report progress in
bytes rather than folder counts. The capacity check is per volume, so a 500 GiB
recordings claim never excuses appdata that will not fit.

The copy preserves ownership and permissions by number, so an imported app
finds its appdata exactly as it left it on the source. Ownership can be
overridden during an import, for a container that should run as a different
user here than it did there. For appdata imported by an older release, which
arrived owned by root, Volumes has an **Ownership** action that hands an
existing claim to a user, filled in from whatever mounts the volume — PUID and
PGID first, then a container or pod security context — so the number is never
a guess.

## Importing a Docker Compose file

Import ▸ **Docker Compose** (also on the Deploy page) takes a pasted
`docker-compose.yml` in the same editor Volumes uses for files, and checks it as
you type. Problems are underlined on the line they are about, and each service
shows what it will become before anything is created. A `.env` file pasted
under Variables fills `${NAME}`, `${NAME:-default}` and the other forms Compose
understands.

Each service becomes its own workload, created in `depends_on` order:

- a named volume becomes a new Longhorn claim of that name, or uses the claim
  already there; one that several services mount is created as shared (RWX);
- a host folder such as `./config` becomes a new claim too, because a pod can
  land on any node. Its current contents are not copied; Import ▸ Container
  source brings data across;
- `tmpfs` and `shm_size` become RAM disks, and devices are matched to hardware
  features;
- a service other services reach by name, such as a database, gets an address
  inside the cluster so that name keeps working;
- `entrypoint`, `command`, `working_dir`, a numeric `user` and `cap_add` carry
  across. Reservations become requests; memory limits are carried across.

Some things are refused outright: `build` without an `image`, the Docker socket,
`secrets`/`configs`, and a device no hardware feature covers. Docker-only
settings such as `labels` and `logging` are listed as left out. **Edit in form**
opens one service in the Deploy form to change anything first; **Create
workloads** reviews all services together against shared capacity, declared host
ports and checked storage/placement constraints. The API checks again before
creating anything and before each later service, including earlier Deployments
whose pods have not appeared yet. A later failure stops the batch and names what
was already made, without deleting workloads or volumes. The example placement
is not a scheduler reservation; unknown metrics and conservative RAM estimates
require acknowledgement. An incomplete bounded search requires splitting the
batch. See the [batch review and recovery guide](https://github.com/wjcloudy/homestead/wiki/Importing#batch-capacity-review).

## Editing files on a volume

A claim's contents are only reachable from a pod that mounts it, so Volumes ▸
Files starts a short-lived helper pod, lists directories through it and opens
text files in an editor. The pod gives itself a thirty-minute deadline and is
removed when the browser is closed, because while it runs it holds the claim —
which also means a ReadWriteOnce volume cannot be browsed until the workload
using it is stopped, and the error says so.

Saving is two steps: the content is written to a temporary file, its length is
compared with what was sent, and only a complete write replaces the original,
with the previous contents kept alongside as `<name>.homestead-bak`. JSON is
parsed and YAML is checked for tab indentation before a save; either complaint
can be overridden, because it is your file. Binary files and anything above
1 MB are listed but not editable, and no path can address anything outside the
volume.

Files open in Monaco — the editor from VS Code — with highlighting for YAML,
JSON, INI, XML, shell, Markdown and the rest of what turns up in appdata, plus
JSON validation as you type and Ctrl/Cmd+S to save. It is the one third-party
library in the browser UI, vendored at `web/vendor/monaco` as a trimmed 4.5 MB
subset of the 14 MB distribution, fetched only when a file is opened and never
on page load. If it cannot load at all the editor falls back to a plain
textarea, which saves through exactly the same path.

`server/homestead_files.py` holds the server side, reusing the exec WebSocket the
console already speaks.

## Disks

Each node card lists every disk on the host - the system disk, the disks
Longhorn stores data on, and any nothing uses yet - with each Longhorn disk's
use. A node's page has **Disks**, and **Volumes** and **Settings → Cluster**
open every node's at once. On Harvester a disk it has found and not been given
has **Add to Longhorn**: Harvester formats it (erasing it first, if you say so,
when it already holds a filesystem) and hands it to Longhorn, as its own UI
does. Elsewhere, mount the disk on the host and give Longhorn the folder, or
the raw device for the V2 engine. A Longhorn disk can stop taking new
replicas, have its replicas moved off, and be removed once it is empty; its
files stay on the disk. Which physical disk holds which folder comes from the
node probe, which reads the host's mount table.

Hardware features - a Coral, a Zigbee stick - are checked on every host every
30 seconds, so one plugged in later is found and labelled without a restart;
**Rescan hosts** in Settings → Hardware checks at once.

### A failed drive

A Longhorn disk that is not ready says why in words - Harvester no longer
finding the drive, or nothing mounted at its folder because the drive is dead
or was missing at boot - and raises an alert. **Replace failed disk** reviews
each volume that had a copy on it (rebuilds elsewhere, waits for the new
drive, or only copy), then as a resumable job stops new replicas on the disk,
deletes its failed replicas where a healthy copy exists elsewhere, takes the
disk out of Longhorn (through Harvester where it runs) and clears Harvester's
record of the dead drive, ready for **Add to Longhorn** on the new one. A
volume's only copy is kept, and the disk with it, unless given up by typing
the disk's name: a drive that is merely unplugged comes back with its data.
Off Harvester, Add to Longhorn gives the commands to mount a drive with
`nofail`, so a host starts without a dead drive rather than stopping at an
emergency shell, and with its empty folder locked so nothing lands on the
system disk in its place.

## Longhorn allocation

Longhorn books a replica's full size on a disk when it places it, however
little the volume holds, and a disk takes no new replica once that allocation
reaches its size times the over-provisioning percentage - or once less than
the minimal-available share of it is physically free. Past that, new volumes
come up a copy short, rebuilds wait and expansions are refused; nothing
already placed moves. **Volumes** shows each node's allocation against that
limit, the room left for a replica, and the largest new volume that still
fits with one, two or three copies - each copy needs a different node, so the
tightest node decides. The dashboard names a node past 80%, and it raises a
notification like any other health problem.

**Settings → Cluster** sets over-provisioning and minimal free space, with a
live preview of what each node's limit becomes, and switches Longhorn's V2
(SPDK) data engine on or off - on Harvester through Harvester's own setting,
which prepares each host - showing which nodes are ready for it.

## Changing a volume's storage class

Kubernetes cannot change a volume's class, so **Change storage class** on a
volume copies it instead, and the copy takes the original's name - so the
containers, VMs, shares and backups that use it by name need no change. The
review first lists everything that uses it and what happens to each, the room
the new copy needs (checked against Longhorn's per-node limit for the new
class's number of copies), how much data moves and roughly how long it stops
things for. Then, as a job you can watch or leave:

1. everything using the volume stops - containers, stateful sets, cron jobs,
   VMs - and how each was running is recorded;
2. a volume the same size is made on the new class;
3. the data is copied and checked: files with rsync, owners, permissions, ACLs,
   extended attributes and links kept, then compared by checksum; a VM disk
   block for block, skipping empty space, then compared byte for byte;
4. both volumes are set to keep their data, the original is released, and a
   volume of the original name is bound to the copy;
5. everything starts again the way it was.

Anything going wrong before the swap - the copy failing, not matching, or the
new class never making a volume within ten minutes - puts everything back as
it was: the new volume removed, the workloads started on the untouched
original. The original is kept as an **old copy** on Volumes until you remove
it, so both copies take room until then. A VM on a DataVolume is switched to
the plain volume, as a moved VM is; a DaemonSet, a bare pod, or a volume a
StatefulSet's template made cannot be stopped or recreated safely, and is
said so up front. Homestead's own data moves from Settings › Redundancy.

## Storage classes

A StorageClass is the recipe Longhorn follows when it creates a volume.
Kubernetes fixes a class at creation — its parameters, provisioner and reclaim
policy cannot be edited afterwards — so Homestead offers create, make-default
and delete rather than an edit button that would silently do nothing. Deleting
a class is refused while any claim still references it, and volumes already
built from it keep working and keep their data.

A volume that is not healthy says why, taken from Longhorn's own conditions:
most often that a replica cannot be scheduled because no node has room for it.
A volume that is merely rebuilding says so too, along with the fact that it is
readable and writable meanwhile, because "degraded" on its own reads like an
emergency when usually it is not.

Choosing an existing claim shows what Longhorn knows about it — its access
mode, size, replica health and the node it is currently attached to — because
`Bound` says nothing about whether a second pod on another node can mount it.
Two cases are called out by name: a ReadWriteOnce claim already attached
elsewhere cannot attach twice, and a claim from a migratable class answers a
second node by starting a live migration rather than attaching, which the CSI
driver will not filesystem-mount while it is in flight.

One parameter decides whether a class can back container storage at all.
A class with `migratable: true` hands out two-controller volumes so a VM disk
can live-migrate between hosts, and Longhorn's CSI driver refuses to
filesystem-mount those into a pod: a ReadWriteMany claim created on such a
class binds happily and then strands whatever tries to use it in
`ContainerCreating`. Homestead therefore refuses ReadWriteMany claims on a
migratable class, naming the classes that would work, and the storage picker
narrows its class list as soon as RWX is chosen. Harvester's reserved internal
classes are never offered.

A deletion is refused while something would break: a running pod, or a
controller that still references the claim even at zero replicas, since it
would start one day and find nothing. A Job that has finished, and the pod it
left behind, are listed but do not block - the copy job from an import used to
trap the volume it had just filled. Homestead removes its own finished jobs
before deleting the claim, because Kubernetes can otherwise hold the PVC in
Terminating while one exists. A transfer can also be cleared from Import
directly: a running copy is cancelled, a failed or finished one is removed.
Because a failed import usually leaves a stopped workload and a half-filled
volume behind, removing it offers to delete those too — the volume only when
that import created it, since a claim it merely copied into belongs to
whatever was using it before.

### Longhorn V2

A storage class can be made on Longhorn's V2 data engine (SPDK), which is
faster and lighter on CPU than V1. The class table has an Engine column, a V2
volume is tagged on Volumes, and the storage classes card says whether V2 is on
and how many nodes can hold its volumes: each needs a disk given to Longhorn as
a block device and 2 GiB of hugepages. Creating a V2 class says so when it
could not schedule yet. On Harvester, V2 is switched on by Harvester's own
`longhorn-v2-data-engine-enabled` setting and V2 disks are added per host, so
Homestead reads Longhorn's settings rather than changing them.

## Network shares

Network Shares manages the `homestead-smb` Deployment and Longhorn-backed claims
without replacing their data; the first share installs Samba if the cluster
has none, at an address of its own on port 445. A new share either creates its own
Longhorn claim or publishes a volume that already exists — including one a
container is using — optionally narrowed to a single folder inside it, so
appdata can be reached from Windows without copying it. Two shares on one claim
mount it once and differ only by folder. A share can be grown in place, switched
between guest and private access, assigned a username, and made read-only or
read/write. Longhorn/Kubernetes claims cannot shrink, so the editor shows the
live PVC request as its minimum size; a borrowed volume is resized from
Volumes instead, never by the share. Size-only changes do not restart Samba;
access-policy changes use the Activity tray to follow the rolling restart.

Samba serves every share from one pod with the Recreate strategy, because its
claims are mostly ReadWriteOnce. A share whose volume cannot be mounted would
therefore take every working share down with it, so a change is guarded: the
claim must be Bound and mountable before the Deployment is written, and if the
pod does not become ready the previous shares are restored and the change is
refused with the mount error. The guard only reverts when Samba was serving
beforehand, so a repair still applies to an already-broken Samba.

A password belongs to the Samba account, not to one share, because Samba keeps
a single password per user. A second private share for an existing username
therefore reuses that account's password when the field is left blank —
Homestead never shows a stored password back, so it cannot ask you to retype
one — and setting a new password changes it for every share using that
account, which the editor says before you save. Passwords that disagreed about
the same account used to fail validation on every later change, including
changes to unrelated shares.

Share metadata is stored in the `homestead-shares` ConfigMap. Passwords are stored
separately in the `homestead-share-credentials` Kubernetes Secret and are never
returned by the Homestead API. The first successful share edit transparently
migrates credentials from older Homestead ConfigMaps and the existing Samba
arguments. Removing a share keeps its PVC and data. An older `samba` Deployment
and Service are migrated to `homestead-smb`, retaining the SMB address when
possible. Its container, mounts, image and on/off state are managed from
Settings → Cluster → Add-ons and Network Shares; ordinary workload edit,
update and delete actions are blocked. Removing the SMB server leaves the
share inventory, credentials, every PVC and their data intact.
Homestead compares its live mappings with the saved share list and repairs drift.
Rejected Kubernetes updates restore the prior share settings, so a failed share
cannot linger in the inventory and collide with the next attempt.

NFSv4 is an optional **separate container**, `homestead-nfs`, under Settings →
Cluster → Add-ons. Choose exports per share in Network Shares; each requires a
Bound RWX claim and an explicit allowed IPv4 client or CIDR. Exports default
to read-only with root squashing. A dedicated VIP preserves client IPs for the
allowlist, and clients mount `<VIP>:/<share>` over TCP 2049. NFS needs host
`nfs`/`nfsd` kernel support and `SYS_ADMIN` in its container. Longhorn RWX
claims are NFS-backed, so serving them adds a re-export layer. Removing or
stopping `homestead-nfs` never removes the SMB server or any PVC. Test a
disposable RWX share and client mount before using it for important data.
Recovery checks show standby hosts, control-plane quorum and actual healthy
replica hosts. Stable export IDs and a readiness check protect ordinary file
access during server replacement. Linux NFS re-export does not provide normal
lock recovery: do not use this gateway for VM disks or databases that need it.
The independent SMB and NFS pods keep separate VIPs for correct Local traffic
routing after a host failure; the protocols themselves can share one address
when served by a combined pod. See [NFS recovery](docs/wiki/Network-shares.md#recovery-when-a-host-fails).

## Namespaces

A Harvester cluster holds dozens of namespaces that belong to Harvester,
Rancher, Longhorn and Kubernetes - several named after generated IDs - and none
of them is a place for an app. Every namespace picker in Homestead (Deploy,
Import, Compose, Volumes, Data Protection) offers only yours. A namespace is
treated as the platform's by name (`kube-*`, `cattle-*`, `harvester-*`,
`longhorn-*`, `fleet-*` and the like), by the IDs Rancher generates (`p-xxxxx`,
`u-xxxxx`, `user-xxxxx`), or by the annotation Rancher sets on the namespaces it
considers its own.

**Settings → Namespaces** lists yours with what each holds - apps, stateful
sets, volumes and VMs - and how many platform namespaces are hidden. An admin
can create a namespace there, and delete one only when it is empty and its name
is typed; `default`, the namespace new workloads go to (`DEFAULT_NS`), and the
one Homestead runs in are kept. A namespace that Homestead created is labelled
`homestead.io/managed`.

## Portal

**Portal** is one page of links to everything with a management page: the
apps in the cluster, and the router, switches, access points and NAS around
it. Links sit in sections, each a tile with its icon and address, and a dot
says whether its host answers on its port right now - a TCP connection rather
than a page load, so a login screen or a self-signed certificate still counts.
The search box filters them.

Links are edited from the Portal page or **Settings → Apps → Portal** (admins
only). **From containers** lists every exposed port of every container at the
address it listens on, ready to tick. An icon is a built-in device glyph
(router, switch, access point, firewall, NAS, server, printer, camera, UPS),
a container's own logo - which follows the app if its logo changes - or an
image from a public URL, fetched and cached the way workload logos are. Links
are kept in the `homestead-portal` ConfigMap and are readable by every signed-in
user, so passwords are refused in addresses.

## Virtual machines

Each VM shows what it is actually doing - KubeVirt's own status: Running,
Stopped, Starting, Paused, or an error such as ErrorUnschedulable with its
reason - and offers what fits: Start a stopped VM; Shut down, Restart, Pause
or open the Console of a running one; stop one stuck starting; Force off one
that will not shut down. Harvester's run strategy is honoured, not the older
`running` flag. A VM opens to its disks (volume, size, class, boot order),
network interfaces (network, MAC, addresses), guest OS as its guest agent
reports it, conditions and events. A VM whose disk is still downloading
shows how far CDI has got, and one whose disk could not be made says why.

**Edit** covers what the VM is made of. General: CPU cores, memory, run
strategy, description, and a host to keep it on. Disks: boot order, bus,
growing a disk, detaching one (its volume is kept), adding a disk or a CD-ROM
from a Harvester image or a download, and - for a disk that was never made,
say because its URL was refused - a new source. Network: model, network (the
pod network or any Multus attachment), MAC, adding and removing interfaces.
Cloud-init: user and network data, inline or in the secret Harvester keeps
them in. **Edit YAML** opens the VM in the Resources editor for anything
else. Changes apply at the next boot, or at once with a restart.

**Delete** asks for the VM's name and can take its disks with
it, marked for removal the way Harvester's own UI does; disks that are kept
are released from the VM first, so Kubernetes does not delete them with it.

**New VM** makes the boot disk the way the cluster does. On Harvester it is a
shared block volume declared the way Harvester's UI declares it, so the VM can
live-migrate, and a disk from a Harvester image lives on that image's own
class. On k3s, RKE2 or any other cluster with KubeVirt, a new disk lands on
the class you pick - the cluster's default to begin with - and CDI picks the
access mode that class supports, so local-path works; such a VM stays on the
host its disk is on. Without CDI, a VM starts from a blank disk KubeVirt
formats itself, and downloading or importing an image asks for CDI first.

## VM console

A running VM's card has **Console**, with two views. **Screen** is the VM's
display over VNC, drawn by noVNC in the page and scaled to fit, with
Ctrl+Alt+Del, full screen, and **Type text** to send a password or a long
command key by key, since a VM has no clipboard to paste into. **Serial** is
the VM's first serial port in the same terminal view as container consoles,
for a VM that boots without a display. Both are KubeVirt subresources that
Homestead proxies, so the browser never holds the service-account token; like
container consoles they are for operators, and each session's start and end
are audited while what is typed and shown is not recorded. noVNC is vendored
under `web/vendor/novnc` and loaded only when a console opens.

## Privileges, VPNs and the main port

Some containers need more of their host than the defaults allow. A VPN client
(transmission-openvpn, gluetun and the like) opens a tunnel device and adds
routes - without that it stops at "RTNETLINK answers: Operation not
permitted". Deploy, Edit and Import each have **Privileges**: **VPN tunnel**
mounts `/dev/net/tun` and grants `NET_ADMIN`, which is all a VPN needs;
**Extra capabilities** adds others by name; **Privileged** gives everything,
as Unraid's Privileged does, and is a last resort. They are filled in for you
from an Unraid template's Privileged flag and extra parameters
(`--cap-add`, `--device=/dev/net/tun`), and from Docker's own settings on an
import.

A container listening on several ports can pick its **Main port** from its ⋯
menu - usually its web UI - and its card links to that one first.

## When a node fails

Each container chooses what happens when its node stops answering: move to
another node after about 15 seconds (new containers' default), wait for its
node to come back (for hardware-bound apps), or Kubernetes' five-minute
default. **If a node fails** on Containers sets every container in one list;
each editor has it under *Where it runs*. The choice is the pod's tolerations
for the unreachable and not-ready taints, so it shows for containers made
elsewhere too. A moved container's single-node volume follows it only if
Longhorn's *Pod Deletion Policy When Node is Down* lets go of the dead node's
pods; the dialog says whether it does and sets it, as does Settings > Cluster.

## Container groups

Homestead's own containers - Homestead itself, the Samba that serves shares,
and the backup storage moves go through - sit in a **Homestead** group of
their own, unless you put them in another.

Workloads can be gathered into groups - Media, Home, Monitoring, whatever suits
- from **Group** in a container's menu, or **Groups** on the Containers page to
tick several and move them at once. Containers then shows each group under a
divider that folds, ungrouped workloads last, and a chip per group shows that
one alone; the search box matches group names too. Sorting a column sorts
within each group. A group is the `homestead.io/group` annotation on each
Deployment, so it needs no list of its own and exists while something is in it.

## Placement rules

A container's editor has a **Where it runs** section - also reached from
**Placement** in its menu - at the three levels there are. The containers in
one pod always run together on one node; that is what a pod is, so to run one
apart it becomes a workload of its own. The workload's copies can be spread
across nodes or kept near a preferred node. And the workload can be kept with
or apart from other workloads. Each rule is a preference or a requirement:

- **Spread instances** puts its own instances on different nodes, so losing a
  host does not take every copy;
- **Run on the same node as** keeps it with another workload, for apps that
  talk constantly or share a device;
- **Keep off the node of** keeps it away from one, for two DNS servers or two
  apps that would compete for a disk.

They become pod affinity terms matched on each workload's own selector. A
preference steers the scheduler and still lets the pod start anywhere; a
requirement leaves it pending rather than break the rule. The editor warns
when a rule cannot be met - more required-apart instances than nodes, or
instances spread across nodes that share a single-node volume. The rules are
recorded in `homestead.io/placement`, so an edit replaces only the terms
Homestead wrote and leaves any added by hand or by a chart.

## Workload logos

Container create, edit, import, and App Store flows accept an optional public
HTTP(S) raster-image URL. Before changing the workload, Homestead validates the
destination, blocks private/link-local address resolution and credentialed
URLs, limits the response to 256 KiB, and stores the verified image by content
hash under `$DATA_DIR/icons`. Deployments keep both the same-origin cached URL
and the original source annotation, so cards do not depend on the remote host
and the source remains editable. SVG is deliberately not accepted. Cached
content-addressed image paths are public so browser image loads do not depend
on session cookies; workload data and original source annotations remain
authenticated.

## Configuration

| Environment variable | Default | Meaning |
|---|---|---|
| `PORT` | `8080` | HTTP listen port |
| `WEBROOT` | `/web` | bundled static UI directory |
| `DATA_DIR` | `/data` | persistent operation history, audit, and workload-icon cache directory |
| `DEFAULT_NS` | `lab` | namespace for new workloads |
| `SMB_NAMESPACE` | `lab` | namespace containing the managed Samba deployment |
| `SAMBA_IMAGE` | `dperson/samba:latest` | image Samba is installed from when the first share is created |
| `NFS_IMAGE` | `pedroetb/nfs-server:v2.4.0` | image used by the optional NFSv4 server |
| `STORAGE_CLASS` | `longhorn-r2` | default StorageClass for new volumes |
| `LB_IP` | empty | shared kube-vip address |
| `SESSION_TTL_HOURS` | `12` | idle window for an ordinary session |
| `SESSION_REMEMBER_DAYS` | `30` | idle window when "keep me signed in" is ticked |
| `SESSION_MAX_DAYS` | `90` | hard limit on a session's age, however active |
| `ENABLE_NODE_POWER` | `true` | Guarded, admin-only reboot/shutdown; set `false` to disable |
| `COMMUNITY_CATALOG_URL` | AppFeed | the App Store's default catalogue feed; Settings can override it |
| `FILES_IMAGE` | `alpine:3.20` | image of the helper pod that browses and edits files on a volume |
| `FILES_SESSION_SECONDS` | `1800` | how long that helper pod may live |
| `CF_ACCESS_TEAM_DOMAIN`, `CF_ACCESS_AUD` | empty | require Cloudflare Access's signature on requests through a tunnel |
| `PUSH_CONTACT` | project URL | the `mailto:` or `https:` contact push services see in Homestead's VAPID token |

## Local verification

The production image contains no build tools. CI performs the checks before the
image is published:

```bash
python -m unittest discover -s tests -v
for file in web/js/*.js web/sw.js; do node --check "$file"; done
node --test tests/*.test.js
docker build --build-arg VERSION=dev -t homestead:dev .
```

## Security notes

Passwords use PBKDF2-HMAC-SHA256 with per-user salts in the `homestead-auth`
Secret. Sessions are HMAC-signed, `HttpOnly`, `SameSite=Strict` cookies and all
mutations require a custom anti-CSRF header. Roles are enforced server-side.
Every response carries a Content Security Policy that forbids framing and
scripts from elsewhere; sign-in attempts are limited per address and per
account; request bodies over 8 MB are refused.

### Drive health

Each drive carries a verdict — healthy, needs attention, critical, or not
reported — drawn from its SMART counters against the thresholds in **Settings →
Drive health policy**, not from the drive's own overall-health bit, which reads
PASSED until failure is imminent. Where a drive reports a measure of remaining
life it is shown as a percentage: NVMe endurance used, a SATA SSD's life-left
attribute, or failing both, how close the worst pre-failure attribute sits to
its manufacturer threshold. A drive that reports no such figure shows nothing
rather than a made-up number.

The probe's scripts travel inside the Homestead image. On start, Homestead
compares them with the ones the installed probe is running and replaces them if
they differ, restarting the DaemonSet — so upgrading Homestead upgrades the
probe, with no manifest to re-apply. It updates whichever name the probe already
has, and never installs one that is not there: the SMART sidecar is privileged,
so installing one is asked for: **Install node probe** on a node with no
thermal data, or from **Settings → Cluster → Add-ons**. **Settings → About**
still shows what the last check decided. `kubectl apply -f
deploy/nodeprobe.yaml` still works for anyone who prefers it.

`deploy/nodeprobe.yaml` is generated from `server/homestead_probe.py` and the
scripts in `server/probe/`, so the manifest applied by hand and the objects
Homestead installs itself are the same objects. Run
`python scripts/render_nodeprobe.py` after changing either; a test fails if they
disagree.

### Naming an installation

The line under the Homestead wordmark, and the footer on a phone, show whatever
**Settings → About this installation → Site name** is set to, alongside the
running version. It starts blank, in which case only the version is shown.

### Sessions

A session has two clocks. The **idle window** is how long it survives with
nothing happening, and it restarts whenever the session is used — so nobody is
signed out in the middle of a task. The **absolute window** is how long a
session may live at all, and it does not restart, so a cookie copied off a
machine stops working whatever the holder does with it.

| | idle | absolute |
| --- | --- | --- |
| ordinary sign-in | 12 hours | 90 days |
| "keep me signed in" | 30 days | 90 days |

The cookie is refreshed once a session passes the halfway point of its idle
window, so an open browser is not handed a new one on every request. `Secure`
is set when the request arrived over TLS, directly or through a proxy that sets
`X-Forwarded-Proto`; it is left off over plain HTTP, where a `Secure` cookie
would never be sent back.

Long sessions are safe because they stay revocable. Every token carries the
account's version number, checked against the stored record on each request, so
changing a password or using **Sign out everywhere** invalidates every session
for that account immediately, on every device. The role is re-read from the
store rather than trusted from the token, so a demotion takes effect at once
rather than at the next sign-in.

Homestead's ClusterRole lets it update that role itself (with Kubernetes'
`escalate` verb), so each release can bring the permissions it needs. That is,
in effect, cluster-admin; so is starting privileged pods with host access, which
the node probe, image cleanup and node power already do. The right is limited
by name to Homestead's own role and binding.

Interactive container consoles require operator access. The supplied manifest
grants `pods/exec` only through the `homestead-console` Role in the `lab`
namespace—not through the cluster-wide role. The proxy independently restricts
sessions to `DEFAULT_NS`, validates the exact running pod and application
container, checks the browser origin, and keeps the service-account token on the
server. Session start/stop metadata is written to
`$DATA_DIR/console-audit.jsonl`; terminal input and output are not recorded.

The supplied Service is plain HTTP. Put it behind TLS before exposing Homestead
outside a trusted LAN - see [Publishing through a Cloudflare
Tunnel](#publishing-through-a-cloudflare-tunnel). Host power control is disabled
by default because it requires a short-lived privileged helper pod.

## Repository layout

```text
Dockerfile                    production container image
server/server.py              stdlib HTTP server and Kubernetes API client
server/homestead_*.py         feature modules: updates, networking, storage, imports
server/homestead_names.py     the names Homestead writes, and the ones it still reads
web/                          browser UI, no build step
web/vendor/monaco/            vendored Monaco editor subset (see its README)
web/assets/                   Homestead SVG identity
web/icons/                    installed-app icons (from scripts/render_icons.py)
web/sw.js, manifest.webmanifest  the installable app's service worker and manifest
deploy/deploy.yaml            namespace, RBAC, Longhorn PVC, Deployment, Service
deploy/nodeprobe.yaml         optional per-node telemetry and device inventory
deploy/rbac.yaml              Homestead's permissions alone, for existing installs
charts/homestead/             the Helm chart (from scripts/render_chart.py)
.github/workflows/ci.yml      tests and container build validation
.github/workflows/release.yml multi-architecture GHCR and screenshot release pipeline
scripts/deploy.sh             deploy a published image through an RKE2 host
scripts/render_nodeprobe.py   regenerate deploy/nodeprobe.yaml from the probe's source
scripts/render_icons.py       regenerate web/icons/ from the mark's geometry
scripts/bump_version.py       move every file that names the release to a new version
scripts/render_rbac.py        regenerate deploy/rbac.yaml, the permissions alone
scripts/render_chart.py       regenerate charts/homestead from the manifests
scripts/capture_screenshots.mjs  the release screenshots, from demo data
docs/wiki/                    the wiki's pages, published by .github/workflows/wiki.yml
```

## Container releases

Every `vMAJOR.MINOR.PATCH` tag runs the full test suite and publishes an
`amd64`/`arm64` image to GitHub Container Registry with SBOM and provenance.
For a release such as `v2.8.179`, the workflow publishes:

```text
ghcr.io/wjcloudy/homestead:2.8.179
ghcr.io/wjcloudy/homestead:2.8
ghcr.io/wjcloudy/homestead:2
ghcr.io/wjcloudy/homestead:latest
ghcr.io/wjcloudy/homestead:sha-<commit>
```

The workflow authenticates with its short-lived `GITHUB_TOKEN`; no registry
password is stored in the repository. Create and publish a release with:

```bash
git tag v2.8.179
git push origin v2.8.179
```

The official Homestead package is public and can be pulled without registry credentials.
The OCI source label in the image links releases back to this repository.

Each tagged release also launches Homestead against deterministic demo data,
captures every page and the main dialogs in headless Chromium
(`scripts/capture_screenshots.mjs`), and attaches them to the GitHub release.
The screenshot above and every picture in the wiki link to the latest
release's, so they follow it; no live cluster data or credentials are used.
The wiki itself is written in `docs/wiki` and published by
`.github/workflows/wiki.yml` whenever it changes on `main`.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for how the project is put together,
running it locally against demo data, the tests, and how releases are made.

## Licence

Homestead is released under the [MIT License](LICENSE). The Monaco editor it
bundles, the images it starts and the catalogue it reads are listed with their
own terms in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md), along with the
trademarks it mentions.
