# Homestead

<p align="center">
  <img src="web/assets/homestead-lockup.svg" width="360" alt="Homestead — friendly cluster management for your homelab">
</p>

**Homestead is an open-source, Unraid-style homelab dashboard and container
management UI for Harvester HCI, Rancher, Longhorn, Fleet, KubeVirt and
Kubernetes.** It runs inside your cluster, talks directly to the Kubernetes API
through a dedicated ServiceAccount, and turns workloads, storage, hardware
passthrough, image updates and failover constraints into approachable controls.

[![CI](https://github.com/wjcloudy/homestead/actions/workflows/ci.yml/badge.svg)](https://github.com/wjcloudy/homestead/actions/workflows/ci.yml)
[![Container](https://img.shields.io/badge/ghcr.io-homestead-2453ff?logo=docker)](https://github.com/wjcloudy/homestead/pkgs/container/homestead)
![status](https://img.shields.io/badge/status-alpha-f59e0b)
![license](https://img.shields.io/badge/license-MIT-30ba78)

![Homestead cluster dashboard](https://github.com/wjcloudy/homestead/releases/latest/download/homestead-dashboard.png)

## Highlights

| Area | Capability |
|---|---|
| **Dashboard** | Cluster CPU/RAM/network/disk telemetry, transition-aware health, top consumers, configurable warnings |
| **Containers** | Deploy, edit, move, start/stop, logs, logos, hardware passthrough, image update checks, monitored rollout and deterministic rollback |
| **Architecture** | VIP → workload → claim → Longhorn volume → replica dependency view |
| **Storage** | RWO/RWX volume creation and growth, usage, health, snapshots, backups and recurring jobs |
| **Hardware** | Host device browser and reusable mappings for iGPU, Coral, USB/PCIe and other devices |
| **Import** | Unraid/Docker workload and appdata import with editable seed configuration |
| **Administration** | Direct URLs/breadcrumbs, persistent activity tray, viewer/operator/admin roles, appearance, thresholds and version details |

## Repository layout

```text
Dockerfile                    production container image
server/server.py              stdlib HTTP server and Kubernetes API client
server/harvui_updates.py      OCI registry checks, rollout monitoring, rollback
web/                          dependency-free browser UI
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
For a release such as `v2.0.2`, the workflow publishes:

```text
ghcr.io/wjcloudy/homestead:2.0.2
ghcr.io/wjcloudy/homestead:2.0
ghcr.io/wjcloudy/homestead:2
ghcr.io/wjcloudy/homestead:latest
ghcr.io/wjcloudy/homestead:sha-<commit>
```

The workflow authenticates with its short-lived `GITHUB_TOKEN`; no registry
password is stored in the repository. Create and publish a release with:

```bash
git tag v2.0.2
git push origin v2.0.2
```

The official Homestead package is public and can be pulled without registry credentials.
The OCI source label in the image links releases back to this repository.

Each tagged release also launches Homestead against deterministic demo data,
captures polished Dashboard, Containers, and Architecture views in headless
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
kubectl -n lab rollout status deployment/harvui --timeout=5m
kubectl -n lab get deployment/harvui pvc/harvui-data service/harvui
```

The Kubernetes resources and `harvui.io/*` metadata intentionally keep their
original names. This is an upgrade-compatibility boundary: applying the
Homestead manifest reuses the existing PVC, authentication Secret, settings,
icons, operations, and rollback history instead of creating a parallel install.

Open the Service address on port `8088`. The first visit creates the initial
administrator. Authentication is stored in a Kubernetes Secret, independently
of the container and Longhorn volume.

For a private fork/package, add an `imagePullSecrets` entry to the Deployment
and create a `docker-registry` secret using a classic GitHub token with only
`read:packages`. GitHub documents this in
[Working with the Container registry](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry).

### 3. Optional node telemetry

Kubernetes does not expose physical temperatures, host device inventory, or
per-disk throughput. Install the non-privileged node probe to enable those views:

```bash
kubectl apply -f deploy/nodeprobe.yaml
kubectl -n lab rollout status daemonset/harvui-nodeprobe --timeout=5m
```

The probe mounts `/sys`, `/proc`, and `/dev` read-only, drops all capabilities,
and uses a read-only root filesystem. Homestead works without it.

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
TAG=2.0.2 HOST=rancher@your-harvester-node ./scripts/deploy.sh
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
