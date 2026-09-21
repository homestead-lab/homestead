# Homestead

<p align="center">
  <img src="web/assets/homestead-lockup.svg" width="360" alt="Homestead — friendly cluster management for your homelab">
</p>

**Homestead is an open-source, NAS-style homelab dashboard and container
management UI for Harvester HCI, Rancher, Longhorn, Fleet, KubeVirt and
Kubernetes.** It runs inside your cluster, talks directly to the Kubernetes API
through a dedicated ServiceAccount, and turns workloads, storage, hardware
passthrough, image updates and failover constraints into approachable controls.

Unraid® is a registered trademark of Lime Technology, Inc. This application is
not affiliated with, endorsed, or sponsored by Lime Technology, Inc.

[![CI](https://github.com/wjcloudy/homestead/actions/workflows/ci.yml/badge.svg)](https://github.com/wjcloudy/homestead/actions/workflows/ci.yml)
[![Container](https://img.shields.io/badge/ghcr.io-homestead-2453ff?logo=docker)](https://github.com/wjcloudy/homestead/pkgs/container/homestead)
![status](https://img.shields.io/badge/status-alpha-f59e0b)
![license](https://img.shields.io/badge/license-MIT-30ba78)

![Homestead cluster dashboard](https://github.com/wjcloudy/homestead/releases/latest/download/homestead-dashboard.png)

## Highlights

| Area | Capability |
|---|---|
| **Dashboard** | Cluster CPU/RAM/network/disk telemetry, transition-aware health, top consumers, configurable warnings |
| **Containers** | Guided App Store and image deployment, independent or sidecar pods, guarded Kubernetes workload rename, edit/move/logs/console, autostart, LAN port and exposure editing, one storage picker for new and existing containers, hardware passthrough, update checks, monitored rollout with live image-pull state, and rollback |
| **Architecture** | VIP → workload → claim → Longhorn volume → replica dependency view |
| **Networking** | Service, ClusterIP, VIP, ingress, listener ownership, orphaned-listener release, endpoint health and guided collision-free exposure |
| **Cluster** | Harvester/Kubernetes versions, control-plane and etcd quorum, node pressure, critical services, certificate requests and guided node onboarding |
| **Storage** | RWO/RWX volume creation, growth and guarded deletion, file browsing and editing, storage-class inventory and creation, usage, health, snapshots, backups and recurring jobs |
| **Hardware** | Host device browser and reusable mappings for iGPU, Coral, USB/PCIe and other devices |
| **Import** | Unraid/Docker workload and appdata import, several folders across several volumes, measured sizing with a per-volume capacity preflight, byte-weighted progress, named failures, editable seed configuration |
| **Administration** | Direct URLs/breadcrumbs, persistent activity tray, viewer/operator/admin roles, appearance, thresholds and version details |

## Repository layout

```text
Dockerfile                    production container image
server/server.py              stdlib HTTP server and Kubernetes API client
server/harvui_updates.py      OCI registry checks, rollout monitoring, rollback
server/harvui_networking.py   VIP allocation, Service planning and endpoint inventory
web/                          browser UI, no build step
web/vendor/monaco/            vendored Monaco editor subset (see its README)
web/assets/                   Homestead SVG identity
deploy/deploy.yaml            namespace, RBAC, Longhorn PVC, Deployment, Service
deploy/nodeprobe.yaml         optional per-node telemetry and device inventory
.github/workflows/ci.yml      tests and container build validation
.github/workflows/release.yml multi-architecture GHCR and screenshot release pipeline
scripts/deploy.sh             deploy a published image through an RKE2 host
```

## Container releases

Every `vMAJOR.MINOR.PATCH` tag runs the full test suite and publishes an
`amd64`/`arm64` image to GitHub Container Registry with SBOM and provenance.
For a release such as `v2.8.35`, the workflow publishes:

```text
ghcr.io/wjcloudy/homestead:2.8.35
ghcr.io/wjcloudy/homestead:2.8
ghcr.io/wjcloudy/homestead:2
ghcr.io/wjcloudy/homestead:latest
ghcr.io/wjcloudy/homestead:sha-<commit>
```

The workflow authenticates with its short-lived `GITHUB_TOKEN`; no registry
password is stored in the repository. Create and publish a release with:

```bash
git tag v2.8.35
git push origin v2.8.35
```

The official Homestead package is public and can be pulled without registry credentials.
The OCI source label in the image links releases back to this repository.

Each tagged release also launches Homestead against deterministic demo data,
captures polished Dashboard, Containers, Architecture, and Networking views in headless
Chromium, and attaches them to the GitHub release. The stable screenshot above
always follows the latest release; no live cluster data or credentials are used.

## Fresh-cluster installation

### 1. Check Longhorn storage

Homestead persists registry update history and cache on a 2 GiB Longhorn RWX
volume. A tightly scoped init container assigns that volume to Homestead's
non-root UID on first start; the application container itself remains
non-root with a read-only root filesystem. Confirm the StorageClass used in
`deploy/deploy.yaml` exists:

```bash
kubectl get storageclass
```

The supplied manifest uses `longhorn-r2`. Change `storageClassName` if the fresh
cluster uses another Longhorn class.

### 2. Configure and install

Review these values in `deploy/deploy.yaml` before applying it:

- `image`: published image/tag to run;
- `storageClassName`: Longhorn StorageClass;
- `LB_IP` and `kube-vip.io/loadbalancerIPs`: Homestead's LAN address;
- `DEFAULT_NS`: default namespace for newly created workloads.

Then install and wait for readiness:

```bash
kubectl apply -f deploy/deploy.yaml
kubectl -n lab rollout status deployment/homestead --timeout=5m
kubectl -n lab get deployment/homestead pvc/harvui-data service/harvui
```

The running Deployment, container, and generated pod names use `homestead`.
Stateful and access-bound compatibility objects such as `harvui-data`, the
authentication Secret, Service, settings, icons, and the `harvui.io/*`
annotation domain intentionally retain their original names. This lets an
upgrade adopt the existing data and VIP instead of creating a parallel install.

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
kubectl -n lab rollout status daemonset/harvui-nodeprobe --timeout=5m
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

## Guided App Store deployment

Homestead translates Unraid Community Applications templates into Kubernetes
ports, environment variables, device hints, and explicit storage choices. Its
compatibility analyser is application-agnostic: port/protocol patterns determine
network intent, path and description semantics distinguish config/data/media/cache,
variable metadata identifies secrets, options, and external dependencies, and
requests for container-runtime sockets are blocked for a Kubernetes-specific design.
These rules apply to every catalogue image rather than a list of named apps.

Public catalogue password defaults are discarded and regenerated locally.
Secret values are masked in manifest previews. Template option lists become
select controls, and the same storage, network, dependency, and hardware review
is used whether deployment begins in App Store or directly from Deploy. Plex,
Pi-hole, and Nextcloud are release validation examples, not special-case profiles.

### Community catalogue data notice

The optional catalogue adapter reads the public Community Applications feed
from `Squidly271/AppFeed` at runtime; catalogue content is not bundled into the
Homestead image or repository. Requests identify Homestead, results are cached
for six hours, and `COMMUNITY_CATALOG_URL` can point deployments at another
authorized, compatible feed. Homestead links to and credits the upstream source
and does not use Unraid logos or imply endorsement.

As of 20 September 2026, GitHub reports no declared repository license for the
`AppFeed` repository. The Community Applications plugin source contains GPLv2
headers, but that alone does not establish a license for generated feed metadata,
listing descriptions, or icons. Operators redistributing or commercially hosting
the catalogue should obtain permission from its maintainers/Lime Technology or
configure a feed whose reuse terms are explicit.

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

## Cluster health and node onboarding

System → **Cluster** separates platform health from application health. It
shows the Harvester and Kubernetes versions, control-plane readiness, etcd
quorum and failure margin, node roles and pressure conditions, observed core
services, certificate-signing requests, and platform warning events from the
last 24 hours. Partial RBAC or API availability is reported per section instead
of hiding the rest of the page.

The onboarding guide recommends a control-plane/etcd or worker role from the
current quorum layout and provides a preflight checklist without exposing the
cluster join token. The optional PXE service remains intentionally separate:
DHCP and boot-network behaviour need an explicit network-safety design before
Homestead can manage them.

Certificate monitoring is read-only. The supplied ClusterRole may list
Kubernetes certificate-signing requests but cannot approve them, and Homestead
never returns CSR bodies, issued certificates, or private keys to the browser.

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

Command-line deployment is also available:

```bash
TAG=2.8.35 HOST=rancher@your-harvester-node ./scripts/deploy.sh
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
running pod or the immediate managed-update rollback. Admins can remove an
unreferenced application digest from selected nodes after an impact preview and
typed confirmation. The backend rechecks live references immediately before
starting one short-lived cleanup pod per node; active, rollback, and recognized
Harvester/Kubernetes platform images are refused. Cleanup pods mount only the
host RKE2 `crictl` binary and containerd socket, run as root without Linux
capabilities or privilege escalation, and report progress through Activity.
Kubernetes Node status exposes only each node's largest cached images; smaller
entries may not appear in the table and Homestead will not attempt to remove them.

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
Homestead's own `harvui-data` claim are protected, and the supplied RBAC grants
only the additional PV `patch` permission required to make the chosen reclaim
behavior deterministic.

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

The copy preserves ownership and permissions by number, so an imported app
finds its appdata exactly as it left it on the source. A volume created from the
App Store is written by the container itself and is correct for the same reason;
the import used to be the odd one out, dropping ownership so every file arrived
belonging to root and the container could not write to its own data. Ownership
can still be overridden during an import, for a container that should run as a
different user here than it did there.

For appdata imported before that, Volumes has an Ownership action that hands an
existing claim to a user, filled in from whatever mounts the volume — PUID and
PGID first, then a container or pod security context — and named, so the number
is never a guess.

## Importing from Unraid

An Unraid container usually maps several host folders, and they do not all
belong in the same place: appdata wants a small replicated claim, recordings or
media want a large one. An import can therefore fill **several volumes at
once** — define the volumes, then point each folder at the one it belongs to.
A folder sharing a volume with others is copied into its own subdirectory; a
folder that has a volume to itself takes its root. Either way it is mounted
back at the path the container expects, through `subPath`, and the workload
ends up with one volume entry per claim rather than one per folder.

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

`server/harvui_files.py` holds the server side, reusing the exec WebSocket the
console already speaks.

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

## Network shares

Network Shares manages the existing `samba` Deployment and Longhorn-backed
claims without replacing their data. A new share either creates its own
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

Share metadata is stored in the `harvui-shares` ConfigMap. Passwords are stored
separately in the `harvui-share-credentials` Kubernetes Secret and are never
returned by the Homestead API. The first successful share edit transparently
migrates credentials from older Homestead ConfigMaps and the existing Samba
arguments. Removing a share keeps its PVC and data.

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
| `STORAGE_CLASS` | `longhorn-r2` | default StorageClass for new volumes |
| `LB_IP` | empty | shared kube-vip address |
| `SESSION_TTL_HOURS` | `12` | signed session lifetime |
| `ENABLE_NODE_POWER` | unset | `true` enables guarded reboot/shutdown actions |

## Local verification

The production image contains no build tools. CI performs the checks before the
image is published:

```bash
python -m unittest discover -s tests -v
for file in web/js/*.js; do node --check "$file"; done
docker build --build-arg VERSION=dev -t homestead:dev .
```

## Security notes

Passwords use PBKDF2-HMAC-SHA256 with per-user salts in the `harvui-auth`
Secret. Sessions are HMAC-signed, `HttpOnly`, `SameSite=Strict` cookies and all
mutations require a custom anti-CSRF header. Roles are enforced server-side.

Interactive container consoles require operator access. The supplied manifest
grants `pods/exec` only through the `harvui-console` Role in the `lab`
namespace—not through the cluster-wide role. The proxy independently restricts
sessions to `DEFAULT_NS`, validates the exact running pod and application
container, checks the browser origin, and keeps the service-account token on the
server. Session start/stop metadata is written to
`$DATA_DIR/console-audit.jsonl`; terminal input and output are not recorded.

The supplied Service is plain HTTP. Put it behind TLS before exposing Homestead
outside a trusted LAN. Host power control is disabled by default because it
requires a short-lived privileged helper pod.

## Licence

MIT
