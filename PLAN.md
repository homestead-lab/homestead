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
| 1.2 | Split frontend into modules (`js/core.js`, `js/charts.js`, `js/views/*.js`) | 🔨 | `app.js` is ~44 KB and growing; ConfigMap limit is 1 MB total |
| 1.3 | **No-flash refresh** — diff state, patch the DOM, never re-render the view | 🔨 | today every poll rebuilds `innerHTML`, which is what makes it flash |
| 1.4 | Mobile: sidebar collapses to a hamburger drawer; tables become cards | 🔨 | below 900 px |
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
| 2.1 | Volumes: what they're attached to + last-used time | 🔨 |
| 2.2 | Dashboard: merge CPU + memory into one box | 🔨 |
| 2.3 | Dashboard: new box for network + disk throughput | 🔨 |
| 2.4 | Replace "Pod distribution" donut with storage breakdown, free space and replica health | 🔨 |
| 2.5 | Node cards: network traffic + temperatures | 🔨 |
| 2.6 | Click a node → detail modal | 🔨 |
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
| 3.1 | Edit container settings (image, env, ports, volumes, resources) | ⬜ | |
| 3.2 | Edit / delete shares | ✅ delete, ⬜ edit | |
| 3.3 | Move a container between hosts | ⬜ | node pin + drain-and-reschedule |
| 3.4 | Move a VM between hosts | ⬜ | KubeVirt live migration |
| 3.5 | **Reboot / shut down a node** | ⚠️ | see below |
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
| 4.1 | Deploy VMs (easy interface) | ⬜ | KubeVirt `VirtualMachine` + DataVolume; needs an image source |
| 4.2 | Image cache page (container + VM images) | ⬜ | per-node image inventory, prune, pre-pull |
| 4.3 | Schedules / jobs configuration | ⬜ | CronJob CRUD — backups, prunes, restarts |
| 4.4 | Import sources in settings | ⬜ | registered Unraid/Proxmox hosts + credentials |
| 4.5 | Import VMs / containers **with appdata** | ⬜ | the largest item — see below |

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

- **Authentication.** HarvUI is unauthenticated. Before it does anything in
  Phase 3, this matters much more than it does today.
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
