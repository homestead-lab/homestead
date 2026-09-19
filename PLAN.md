# HarvUI product plan

Last reviewed: 2026-09-19

HarvUI is an approachable, Unraid-style control plane for a Harvester,
Longhorn, and KubeVirt homelab. This file is the single ordered backlog. Shipped
work is summarized at the end rather than mixed into future milestones.

Legend: **🔨 active** · **⬜ queued** · **⚠️ design decision** · **✅ shipped**

## Current goal

Work through this roadmap in order, keeping the live cluster usable after each
increment. The application shell, operation model, transition-aware health,
and dashboard telemetry polish shipped in v1.10.0. Refresh-safe node,
container-edit, container-log, and volume-edit URLs plus URL-backed search ship
in v1.10.1. Consistent action icons, keyboard focus, and viewport-safe tooltips
ship in v1.10.2. The explicit Deployment → pod → container hierarchy ships in
v1.11.0. The namespace-scoped interactive container console ships in v1.12.1.
The active milestone is exposing rollback-image inventory and safe cleanup.

## Product rules

- Explain Kubernetes concepts in homelab language, while retaining the real
  resource name in details and tooltips.
- Every destructive action gets an impact preview and explicit confirmation.
- Every long-running mutation exposes progress, errors, cancellation where it
  is safe, and a recovery or rollback path where one exists.
- Hardware, storage, and failover constraints are evaluated before a workload
  is changed or a node is drained.
- Health reflects steady state, not harmless transient rollout activity.
- Features must work on narrow screens and without pointer hover.

## Ordered roadmap

### 1. Application shell and feedback

These are first because every subsequent feature needs navigable URLs, shared
status semantics, and a consistent way to report background work.

#### 1.1 Direct routes and breadcrumbs — ✅ shipped in v1.10.1

- Give every primary view a stable URL such as `/containers`, `/volumes`,
  `/networking`, `/system/cluster`, and `/settings`.
- Add browser history support, refresh-safe deep links, useful document titles,
  and breadcrumbs for detail views and modal entry points.
- Preserve search/filter state in the URL where it is useful.
- **Done when:** pasting or refreshing any primary/detail URL returns to the
  same view, Back/Forward works, and breadcrumbs never lead to a dead state.

#### 1.2 Global active-jobs tray — ✅ shipped in v1.10.0

- Add a compact top or bottom tray for image pulls, deployments, imports,
  updates, migrations, backups, SMART tests, and other long operations.
- Show state, progress, elapsed time, owning resource, and the most useful next
  action. Keep recently completed/failed jobs available until dismissed.
- Reconnect to server-side operation state after a HarvUI refresh or restart;
  do not make browser memory the source of truth.
- **Done when:** starting an image download and refreshing HarvUI does not lose
  its progress or result.

#### 1.3 Honest health during transitions — ✅ shipped in v1.10.0

- Distinguish `starting`, `updating`, `degraded`, `blocked`, and `failed`.
- A healthy cluster rolling out or starting a workload must not be labelled
  degraded unless availability actually drops below the configured policy.
- Explain the cause and affected resources from every non-healthy indicator.
- **Done when:** creating a normal container produces a transient activity
  state, not a cluster-degraded alert.

#### 1.4 Consistent actions and help — ✅ shipped in v1.10.2

- Add recognizable icons to actions such as Logs, Edit, Move, Console,
  Restart, and Delete while retaining visible labels where ambiguity is likely.
- Apply consistent, accessible tooltips throughout the UI, including settings,
  metrics, badges, and destructive actions.
- Tooltips must remain inside the viewport and above modal/dialog layers; all
  actions need keyboard labels and focus states.
- **Done when:** the same action has the same icon, label, tooltip, and disabled
  explanation everywhere it appears.

#### 1.5 Dashboard telemetry polish — ✅ shipped in v1.10.0

- Remove the stretched point marker from sparklines.
- Animate/scroll new samples smoothly without rebuilding the whole chart.
- Keep disk utilization and disk MB/s in the node stats area alongside CPU,
  memory, network, and temperature—not in hardware-feature badges.
- **Done when:** multiple polling cycles add samples with no jump, flash, or
  stretched dot on desktop and mobile widths.

### 2. Workload model and operations

#### 2.1 Make pods, workloads, and containers explicit — ✅ shipped in v1.11.0

- Present the hierarchy as workload/controller → pods → containers, with the
  common one-workload/one-container case visually compact.
- Stop using `pod`, `container`, and `workload` interchangeably in counts,
  status messages, and actions.
- Define regrouping precisely before supporting merge/split operations. In
  Kubernetes this changes the pod template and lifecycle boundary; it is not a
  cosmetic grouping operation.
- **Done when:** users can see why pod and container counts differ and which
  objects will restart together.

#### 2.2 Interactive container console — ✅ shipped in v1.12.1

- Add a browser terminal alongside Logs, with container selection for
  multi-container pods, shell discovery, resize, reconnect, and clear RBAC or
  unavailable states.
- Use Kubernetes exec streaming through a narrowly scoped backend proxy; never
  expose cluster credentials to the browser.
- Audit session start/stop and make the security boundary clear.
- **Done when:** an authorized operator can open, use, resize, and close a shell
  without affecting the running workload.

#### 2.3 Managed image updates and notifications — ✅ shipped in v1.13.0

Already shipped: registry digest/version checks, multi-architecture awareness,
private pull-secret support, monitored rollout, self-update, deterministic
rollback, and persisted history.

- Surface available-update and failed-update notifications outside the
  Containers page without creating alert noise.
- Feed update/pull/rollback state into the global jobs tray.
- Add update policy controls (notify only, approval required, maintenance
  window) without silently crossing major versions.
- **Done when:** a discovered update can be reviewed, installed, monitored, and
  rolled back from one coherent flow, including HarvUI itself.

#### 2.4 Rollback-image inventory and cleanup — 🔨 active

- Mark cached images retained for rollback and show their workload/version,
  size, nodes, and retention reason in Image Cache.
- Provide safe cleanup for unreferenced rollback images, with impact preview
  and protection for the active and immediate rollback digests.
- CRI image deletion requires a tightly scoped node-side helper; Kubernetes has
  no native delete-image API.
- **Done when:** users can reclaim cache space without removing an image needed
  by a running workload or advertised rollback.

#### 2.5 App Store logo import and persistence — ⬜ verify and finish

- Carry the source icon through preview, create, edit, and update flows.
- Persist icons with HarvUI-owned configuration so image updates and pod
  replacement cannot remove them; cache/proxy remote assets safely when needed.
- Provide a fallback and a manual override.
- **Done when:** an imported app keeps its logo after rollout, restart, managed
  image update, and HarvUI restart.

### 3. Storage and host hardware

#### 3.1 Delete volumes safely — ⬜ queued

- Add delete actions for PVCs/Longhorn volumes with attached workload, mount,
  replica, snapshot, backup, and reclaim-policy impact shown first.
- Block unsafe deletion by default. Require typing the volume name for attached
  or data-bearing volumes, and clearly distinguish detach, delete claim, and
  permanently delete data.
- **Done when:** an unused test volume can be deleted cleanly and an attached
  volume cannot be deleted accidentally.

#### 3.2 Disk health and SMART operations — ⬜ queued

- Extend node details with drive identity, capacity, temperature, SMART health,
  reallocated/pending/uncorrectable sectors, power-on hours, error history, and
  per-disk read/write MB/s where the device exposes them.
- Support short/long SMART tests with progress in the global jobs tray and a
  readable result history.
- Use the node probe with the minimum device access required; document unsupported
  USB bridges, NVMe differences, and privilege implications.
- Add configurable warning thresholds and notifications.
- **Done when:** a selected SATA/SAS/NVMe disk can report health and run a test,
  while unsupported devices explain why no test is available.

#### 3.3 Finish storage administration — ⬜ queued

- Edit Samba share size/permissions in place.
- Restore a Longhorn backup into a new PVC with conflict handling and progress.
- Import VM disks (qcow2/vmdk → PVC) through CDI.

### 4. Networking and cluster administration

#### 4.1 Networking menu — ⬜ queued

- Add a top-level Networking section for Services, cluster IPs, external/VIP
  addresses, ingress/access URLs, port/protocol ownership, free/reserved ports,
  endpoints, and load-balancer/kube-vip status.
- Support guided VIP creation/allocation with collision checks, including
  dedicated-IP workloads such as Pi-hole.
- Show traffic path and health from VIP → Service → Endpoint/Pod.
- **Done when:** a user can answer “which address and port can I use?” and create
  a non-conflicting exposed service without leaving HarvUI.

#### 4.2 System → Cluster menu — ⬜ queued

- Show Harvester/Kubernetes version, control-plane and etcd health, node roles,
  quorum margin, certificates, critical system services, capacity pressure,
  and recent cluster-level warnings.
- Provide guided node onboarding. Treat an optional PXE service as a separately
  designed managed add-on after requirements and network safety are clear.
- **Done when:** the page explains whether the platform itself is healthy and
  what must be fixed, independently of application health.

#### 4.3 Harvester update awareness — ⬜ queued

- Detect available/supported Harvester updates and surface release, compatibility,
  prerequisite, and maintenance information.
- Begin with notification and readiness checks; do not automate a platform
  upgrade until the documented Harvester upgrade path can be enforced safely.
- **Done when:** admins can see current/supportable versions and blockers without
  HarvUI implying an unsafe one-click platform upgrade.

#### 4.4 Failover and restart policies — ⬜ queued

- Add cluster defaults plus workload overrides for restart behavior, node-loss
  tolerance, drain behavior, migration preference, grace periods, and hardware
  placement requirements.
- Preflight manual migration, node drain, reboot, and shutdown against storage,
  ports, resources, and hardware features. Explicitly list workloads that
  cannot restart elsewhere and require acknowledgement.
- **Done when:** failure simulation and manual drain show the same placement
  outcome, and a hardware-bound workload is never presented as safely movable
  when no eligible host exists.

### 5. Architecture view redesign

#### 5.1 Layout and responsive routing — ⬜ queued

- Center the graph and remove the redundant Architecture shortcut from the
  dashboard; the sidebar remains the canonical entry point.
- Use layered routing and lane allocation so edges do not cross cards or bend
  through nodes above them, especially on narrow screens.
- Collapse detail progressively instead of compressing labels into unreadable
  cards.

#### 5.2 Correct topology and focus behavior — ⬜ queued

Use this taxonomy:

```text
VIP / exposed port
  → Service
    → workload/controller
      → scheduled pod (host placement)
        → container(s), shown on demand
      → attached PVC
        → Longhorn volume
          → replica hosts (storage placement)
```

Do not draw PVC → host as a single undifferentiated edge: the host running a pod
and the hosts storing Longhorn replicas answer different availability questions.
Containers can be collapsed into the workload card by default and expanded when
their ports, devices, or individual status matter.

- Hover, focus, or tap dims everything outside the full dependency path,
  including unrelated ports and edge labels.
- Highlight both upstream exposure and downstream placement/storage dependencies.
- **Done when:** selecting a port, workload, node, or volume reveals only its
  complete causal path and remains readable at narrow widths.

### 6. Managed services and integrations

#### 6.1 Samba as a managed add-on — ⬜ queued

- Add a Settings toggle and conditional sidebar item.
- Enabling creates/reconciles the managed Samba workload and configuration;
  disabling previews affected shares/clients and removes or stops only resources
  owned by HarvUI.
- Preserve share configuration and credentials separately from the container
  lifecycle, with export/import and safe secret handling.
- **Done when:** enable, configure, update, disable, and re-enable are idempotent
  and do not lose share definitions unexpectedly.

#### 6.2 MQTT bridge with Home Assistant discovery — ⬜ queued

- Bring the existing cluster MQTT bridge under HarvUI management.
- Configure broker URL/TLS/authentication, discovery prefix, base topic, entity
  naming, availability/LWT, publish intervals, retained messages, and which
  cluster/workload/storage metrics are exposed.
- Store credentials in Kubernetes Secrets, preview discovered entities, expose
  connection health, and avoid publishing credentials or sensitive event text.
- **Done when:** Home Assistant discovers stable entities, availability follows
  HarvUI/bridge state, and config survives bridge image updates.

### 7. Security, recovery, and longer-term work

- Serve HarvUI behind TLS so session cookies can be `Secure`.
- Back up and restore HarvUI settings plus workload definitions, separately from
  workload data backups.
- Consider multi-cluster only after the single-cluster resource and permission
  model is stable.

## Dependency guide

| Foundation | Enables |
|---|---|
| Direct routes and breadcrumbs | Deep links from jobs, notifications, search, and architecture |
| Global jobs tray and persisted operation state | Image pulls, imports, updates, migrations, backups, SMART tests |
| Explicit workload → pod → container model | Correct counts, console targeting, regrouping, architecture |
| Shared health-state model | Cluster summary, Harvester health, load balancers, failover |
| Network inventory | VIP allocation, port availability, architecture traffic paths |
| Extended node probe | SMART, per-disk throughput, hardware discovery |
| Managed add-on framework | Samba, MQTT bridge, optional future PXE service |

## Shipped baseline

The following is implemented and should be protected by regression coverage:

- Public GitHub repository, multi-architecture GHCR releases, SBOM/provenance,
  Longhorn-backed HarvUI state, and fresh-cluster deployment documentation.
- Responsive modular UI, no-flash polling updates, global search, dense events,
  configurable appearance/resource thresholds, accounts, and role enforcement.
- Dashboard CPU, memory, network, disk throughput, temperatures, node details,
  service links, and storage overview.
- Container deploy/edit/move/start/stop/logs, appdata import, seed configuration,
  persistent workload logos, configurable hardware features/device browsing,
  and placement-aware drain/power preflight.
- Image update discovery, managed rollout/progress, HarvUI self-update, and exact
  digest rollback. Image Cache filters Harvester/system images by default.
- RWO/RWX volume creation/growth/replica editing, usage/attachment/health,
  Longhorn snapshots, backups, recurring jobs, groups, and backup target.
- VM deployment/migration, scheduled jobs, import sources, Samba share creation
  and deletion, and the initial VIP → workload → volume architecture view.

## Delivery checklist for every roadmap item

1. Define API/resource semantics and authorization before UI controls.
2. Include loading, empty, unavailable, permission-denied, partial, and failure
   states—not only the happy path.
3. Add focused backend/frontend tests and run the complete regression suite.
4. Exercise the feature against the live homelab with a disposable resource
   before using an existing workload or volume.
5. Update this plan, README/configuration docs, version, release notes, and live
   deployment only after verification succeeds.
