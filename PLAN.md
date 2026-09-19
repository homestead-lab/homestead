# HarvUI roadmap

Tracking the feature set requested 2026-09-19. Ordered by value ÷ risk, not by
the order asked — the cheap structural fixes come first because everything else
sits on top of them.

Legend: **✅ done** · **🔨 in progress** · **⬜ queued** · **⚠️ needs a decision**

---

## Phase 1 — foundations (do first, everything depends on these)

| # | Item | Status | Notes |
|---|---|---|---|
| 1.1 | GitHub repo, private, under `wjcloudy` | ✅ | this repo |
| 1.2 | Split frontend into modules | ✅ | `core.js` + `views-overview/workloads/storage/lifecycle.js`; backend split into `harvui_lifecycle.py` + `harvui_imports.py` |
| 1.3 | **No-flash refresh** — diff state, patch the DOM, never re-render the view | ✅ | today every poll rebuilds `innerHTML`, which is what makes it flash |
| 1.4 | Mobile: sidebar collapses to a hamburger drawer; tables become cards | ✅ | below 900 px |
| 1.5 | Remove sparkline band + mid dot | ✅ | as requested |

### On websockets (1.3)

Asked for, but I'd argue **against** them here and use smarter polling instead:

- The flashing is *not* caused by polling — it's caused by us replacing
  `innerHTML` wholesale on every tick. Fixing the render loop fixes the flash,
  and it fixes it for the manual refresh button too.
- A websocket needs a persistent connection through kube-vip, reconnect/backoff
  logic, and a server that can push — our stdlib `ThreadingHTTPServer` has no
  websocket support, so it would mean hand-rolling RFC 6455 framing or taking a
  dependency, which is the thing this project deliberately avoids.
- Kubernetes already gives us `?watch=true` streaming if we want push later.

**Decision:** fix the render loop first (1.3). Revisit websockets only if a
polling interval under ~5 s is genuinely needed.

---

## Phase 2 — information the UI is currently missing

| # | Item | Status |
|---|---|---|
| 2.1 | Volumes: attached-to + last-used | ✅ |
| 2.2 | Dashboard: merge CPU + memory | ✅ |
| 2.3 | Dashboard: network + disk throughput | ✅ |
| 2.4 | Storage breakdown replaces pod-distribution donut | ✅ |
| 2.5 | Node cards: network traffic | ✅ (temps ⬜, see 2.5a) |
| 2.6 | Click a node → detail modal | ✅ |
| 2.7 | Uptime on containers and in the architecture view | ✅ |
| 2.8 | Clickable access links to each service UI | ✅ |
| 2.9 | Architecture hover dims everything not on the path | ✅ |

**Temperatures caveat:** Kubernetes exposes no thermal data. This needs
`node_exporter`'s `hwmon` collector, or reading `/sys/class/thermal` from a
privileged DaemonSet. Tracked as 2.5a — the DaemonSet is the lighter option and
avoids pulling in Prometheus.

---

## Phase 3 — lifecycle actions

| # | Item | Status | Notes |
|---|---|---|---|
| 3.1 | Edit container settings | ✅ | image, resources, env, replicas, iGPU, node pin |
| 3.2 | Edit / delete shares | ✅ delete, ⬜ edit | |
| 3.3 | Move a container between hosts | ✅ | node pin + Recreate rollout |
| 3.4 | Move a VM between hosts | ✅ | KubeVirt live migration |
| 3.5 | **Reboot / shut down a node** | ✅ built, ⚠️ **off by default** | needs `ENABLE_NODE_POWER=true`; cordon/drain/quorum guard all live |
| 3.6 | Change pod → container groupings | ⬜ | merge/split containers across pods |

### ⚠️ On host reboot/shutdown (3.5)

This is genuinely destructive and I want it gated, not just confirmed:

- Must **cordon + drain** first, never a bare reboot — otherwise workloads are
  killed rather than moved.
- Must **refuse** if it would break etcd quorum. With 3 members, taking one node
  down is survivable; taking a second is not. The UI must know that.
- Requires either a privileged DaemonSet with host PID, or the Harvester node
  API. No RBAC we currently hold can reboot a host.
- Confirmation should require **typing the node name**, not clicking OK.

---

## Phase 4 — bigger features

| # | Item | Status | Notes |
|---|---|---|---|
| 4.1 | Deploy VMs | ✅ | VM + DataVolume, Harvester image or cloud-image URL |
| 4.2 | Image cache page | ✅ | per-node inventory + pre-pull; prune ⬜ |
| 4.3 | Schedules / jobs | ✅ | CronJob CRUD + run-now |
| 4.6 | **Longhorn data protection** | ✅ | recurring jobs, groups, snapshots, backups, backup target |
| 4.4 | Import sources | ✅ | ConfigMap + Secret, with SSH browse |
| 4.5 | Import containers **with appdata** | ✅ containers, ⬜ VMs | rsync Job into a Longhorn PVC |

### On import (4.5)

The honest scope. Importing an Unraid container means translating:

1. the container definition (image, ports, env) — straightforward, already done
   for App Store templates;
2. **the appdata** — copying `/mnt/user/appdata/<app>` into a Longhorn PVC, which
   means an rsync job with credentials for the source host;
3. path mappings — Unraid host paths have no meaning in Kubernetes and must be
   remapped per volume.

Step 2 is the real work and needs a transfer job running in-cluster. VM import
is heavier still: disk image conversion (qcow2/vmdk → PVC) via CDI.

**This is several days of work, not an afternoon.** It should land last, after
Phases 1–3 are solid.

---

## Not yet scheduled

- ~~**Authentication.**~~ ✅ Done — accounts, sessions, CSRF, rate limiting.
  Still missing: roles (every account is an admin) and TLS.
- Backups / restore of workload definitions.
- Multi-cluster.

---

## Structure as it grows

Current single-file `server.py` is ~1000 lines and still readable, but Phase 3–4
will double it. Planned split:

```
server/
  server.py        entry + routing only
  harvui/
    k8s.py         API client, auth, caching
    collect.py     read models (nodes, workloads, volumes, flow)
    mutate.py      deploy, scale, restart, delete
    shares.py      samba integration
    appstore.py    Unraid CA feed
    imports.py     Phase 4
```

Frontend splits the same way under `web/js/views/`.


---

## Status after 2026-09-19

Phases 1–4 are deployed and reachable at the cluster VIP. Remaining:

| Item | Why it is still open |
|---|---|
| ~~Authentication~~ | ✅ Done. Follow-ups: **TLS** (cookie cannot be `Secure` over HTTP) and **roles** (no read-only accounts yet). |
| 2.5a node temperatures | Needs a privileged DaemonSet reading `/sys/class/thermal`. |
| 3.6 pod↔container regrouping | Needs a clear model for what "merge two workloads" should mean. |
| 4.5b VM import | Disk conversion (qcow2/vmdk → PVC) via CDI; separate from container import. |
| Image prune | Listing and pre-pull are done; deleting cached images needs CRI access. |
| Share editing | Delete exists; editing size/permissions in place does not. |


---

## Data protection (4.6)

Built on Longhorn's own model rather than a parallel one:

* **RecurringJob** — task (snapshot / backup / trim / cleanup), cron, retain,
  concurrency, and the groups it protects.
* **Groups are labels on the volume** — `recurring-job-group.longhorn.io/<g>`
  and `recurring-job.longhorn.io/<job>`. Assigning a volume is a label patch,
  not a controller, which is why group membership is cheap.
* `default` is special: Longhorn puts every new volume in it, so one job
  targeting `default` covers the whole cluster automatically.
* **Backup target** is a `BackupTarget` CR (newer Longhorn), not the old
  `backup-target` setting. Snapshots work without one; backups do not.

Coverage is shown as a percentage with the uncovered volumes named, because
"protected" is the number that actually matters and it is easy to think you
have it when you do not.

### Still open after 2026-09-19

| Item | Notes |
|---|---|
| 2.5a node temperatures | Needs a DaemonSet with a read-only `/sys` hostPath. |
| 3.6 pod↔container regrouping | Needs a clear model for merge/split first. |
| 4.5b VM import | qcow2/vmdk → PVC via CDI. |
| Image prune | Kubernetes has no delete-image API; needs CRI access on the node. |
| Share editing | Delete exists; resize and permission edits do not. |
| Backup restore | Backups are listed but restoring to a new PVC is not wired up. |
