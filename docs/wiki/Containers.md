# Containers

**Containers** lists every app you run, as cards or as rows, with its state,
address, image and update status. In Kubernetes terms each one is a
Deployment (or a StatefulSet or DaemonSet you made elsewhere); Homestead calls
them containers because that is what you think of them as.

![Containers](https://github.com/wjcloudy/homestead/releases/latest/download/homestead-containers.png)

## Deploying a container

**＋ Deploy** takes an image - `lscr.io/linuxserver/jellyfin:latest`, say - and
asks what Docker would: ports, environment variables, volumes, devices. Most
people start from the [App Store](App-Store) instead, which fills all of that in
from the app's template, or [import](Importing) one from Unraid or a Compose
file.

Below the image field, **Will pull** shows the fully resolved registry,
repository and tag. It does not rewrite the image: for example,
`openspeedtest/latest` is a repository actually named `latest`, so its default
tag correctly resolves to `docker.io/openspeedtest/latest:latest`.

The form asks, in order:

- **Name and namespace.** Apps go in `lab` unless you make others under
  **Settings → Apps → Namespaces**.
- **Memory reserved and Memory max.** Reserved memory is the scheduler's
  request; optional Memory max is the container's enforced limit and must be
  at least the request. Leave max blank for no container memory limit. A limit
  set too low can OOM-kill and restart the app. Edit shows each container's
  current values, including sidecars. Compose imports carry `mem_limit` and
  `deploy.resources.limits.memory` into Memory max.
- **Network.** Which ports are reachable from your LAN, and on which address:
  automatic (the next free VIP), a **Specific VIP** you picked out under
  [Networking](Networking#your-vips), or shared with other apps on one address.
  Ports that stay inside the cluster need nothing.
- **Storage.** Each path the app writes to becomes a folder in a volume: a new
  one, one that exists, a folder inside another app's volume, or a RAM disk for
  a cache. Two paths can share one volume as two folders. A new volume
  belongs to the user the image gives that path, as a new Docker volume
  would: an app that runs as a user of its own, such as sambee, can write
  to it without a `chown` first. Homestead reads that owner from the image's
  registry when it deploys, and a small first step (`homestead-owner`) sets
  it only while the volume is still empty.
- **Hardware** and **Privileges** - below.
- **Autostart.** Off leaves it stopped until you start it.

Every choice is checked before anything is made: an address already in use,
a port clash, a volume that cannot be mounted where the app will run.

## Day to day

A container's **⋯** menu has what you would expect - start, stop, restart,
logs, console, edit - and:

- **Main port** - which of its ports the card links to first, usually its web
  page.
- **Group** - put it in a group (below).
- **Placement** - where it runs (below).
- **Rename** - the Kubernetes objects are renamed with it, carefully.
- **Move** - to another namespace, or [another cluster](Moving-between-clusters).

**Edit** opens the same form as Deploy, with everything the container has now.
Storage can be restructured there: two volumes combined into folders of one, a
folder split out to its own volume. When a path moves, Homestead offers to bring
its data along - it stops the container, copies, and starts it again - and the
old volume is kept until you remove it. A new volume is given the old
location's owner and permissions, whether its data comes along or it starts
empty, so an app that runs as its own user can still write to it.

### Start and scale capacity checks

Before increasing a Deployment's replica count, Homestead reads reservations
from assigned pods across all namespaces, including system, pending-on-a-host
and terminating pods. Completed pods do not reserve capacity. CPU, memory,
pod slots, declared device resources and ephemeral-storage requests are compared
against each eligible node's remaining allocatable resources. If the checked
resources cannot fit the requested number of additional pods, the API refuses
the start even when memory warnings were acknowledged.

The review separates **live RAM**, **already reserved RAM**, **per-pod requests**
and **estimated memory usage from limits**. Resource calculations include init
stages, restartable init sidecars, pod overhead and pod-level budgets. During an
in-place resize, higher observed allocations are conservatively retained.
Projection starts with the greater of live RAM and booked requests, then adds
the estimated new pods that could fit on that host. CPU is shown as a percentage
of one core (100% = one core).

Missing reservations or metrics, unbounded memory, competing unscheduled pods,
and unverified placement constraints require review; unknown data is never a
green safety result. The API recalculates immediately before scaling. This is
a snapshot, **not a capacity reservation or OOM guarantee**. It does not yet
fully simulate dynamic resource allocation or concurrent admissions.
New workloads from **Deploy and App Store** use the same planner (see below).
The legacy `/api/appstore/install` endpoint also uses this guard. API clients
review the resolved template plus overrides via `/api/preview` and, when warnings
require acknowledgement, pass its `capacity_token` and `confirm_capacity: true`
with installation. Catalogue/override changes require a new review.
**Edit** also reviews the full replacement pod before saving. The review includes
all containers, planned PVCs, memory limits and any host selection changed in the
editor. A fresh review is required if the Deployment or edited seed ConfigMaps
change. Rejected capacity checks do not save seed configs, create PVCs, persist
icons or restart the workload. The final Deployment PUT retains its reviewed
resource version. Multi-object saves are not atomic: a later API failure can
still leave a partially applied edit.

Renames and data-copy edits review conditional post-stop workload capacity;
copy-helper placement and capacity changes during a long copy are not simulated.
Paused Deployments cannot increase replicas through Edit until resumed and
reviewed again. Compose batches, Unraid migration, standalone moves, image
updates and VM launches remain separate paths; extending the guard is planned.

The arithmetic follows Kubernetes' [resource request model](https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/)
and [init-sidecar accounting](https://kubernetes.io/docs/concepts/workloads/pods/sidecar-containers/).

The same preview also checks declared host ports (including restartable init
sidecars), bound PV node affinity, and PVC access modes. Occupied ports identify
the consuming pod. Host ports limit identical new replicas to one per host;
RWO shares must fit their replicas together on one host, and RWOP permits only
one pod. Existing consumers are checked in the claim's namespace. Missing,
Lost, deleting or incorrectly bound claims/PVs are blockers, not reasons to
create an empty replacement.

Pending `WaitForFirstConsumer` claims are allowed to reach the scheduler, with
storage-class topology checks and an explicit provisioning warning. A direct
`nodeName` assignment is refused for these claims because it bypasses the
scheduling decision needed for binding. API errors are reported as unknown,
not confused with a verified 404. These are read-only checks: they do not edit
affinity, move data, detach volumes, stop consumers, or change access modes.
See Kubernetes' [volume access modes](https://kubernetes.io/docs/concepts/storage/persistent-volumes/#access-modes)
and [delayed volume binding](https://kubernetes.io/docs/concepts/storage/storage-classes/#volume-binding-mode).

Storage-driver attachment/health, CSI capacity and attachment limits, undeclared
host-network listeners, and arbitrary hostPath contents still need operator
review; a matching topology does not prove the data is available.

Required pod affinity, incoming and existing-pod anti-affinity, and explicit
`DoNotSchedule` topology spread constraints are checked against the pod/node
snapshot. Namespace selectors, self-affinity bootstrap, label-key matching,
`minDomains`, and node-affinity/taint inclusion policies are considered. Soft
preferences are not hard blockers. Direct node assignment bypasses these
scheduler checks; custom schedulers, unavailable namespace labels, and missing
controller-generated revision labels are unverified, not reported as safe.

For multiple replicas a bounded search re-evaluates topology after each proposed
pod, including replicas that need the same RWO host. If all orders are ruled out,
the start is blocked. If the search budget expires, placement is unknown and
needs acknowledgement. Per-host resource counts remain upper bounds, not a
promise that every replica can use that host. The rejected-host list describes
the **next pod**; a host may become eligible as spread counts change. Preemption,
scheduler profiles/default constraints, admission-injected labels and concurrent
controllers are not simulated. No pods are actually placed during this check.
See Kubernetes' [pod affinity](https://kubernetes.io/docs/concepts/scheduling-eviction/assign-pod-node/#inter-pod-affinity-and-anti-affinity)
and [topology spread](https://kubernetes.io/docs/concepts/scheduling-eviction/topology-spread-constraints/).

### New deployment review

Deploy and App Store show per-host placement reasons, live/projected RAM and
existing reservations before creating a new workload. A proven capacity or
placement shortfall disables deployment and cannot be overridden through the
API. Missing data or other warnings require an explicit acknowledgement.

The server rechecks the proposed manifest before the Deploy endpoint calls any
creation helpers: no icons, volumes, secrets, network attachments or workloads
are written on a capacity rejection. The preview is read-only. It includes a
conservative allowance for the volume-ownership init stage without downloading
image layers during review. This is still a snapshot, not an atomic reservation
against other users or controllers deploying concurrently.

Claims marked **Create new** are evaluated as planned only when the API confirms
that they do not already exist. Their access mode and delayed-binding class
topology constrain placement; storage provisioning/capacity is still unverified.
Existing claims keep their real access mode and PV restrictions, regardless of
the proposed settings. An API permission error is unknown, not a missing claim.

Acknowledgements expire after ten minutes and are bound to the exact reviewed
configuration. The confirmation submits that configuration, not subsequent form
edits. A changed input, expired review, or rotated account signing key requires
reviewing again. A domain-separated key from the existing account Secret lets
reviews work across Homestead replicas and restarts without writing a new Secret.
Tokens contain no passwords or manifest contents. If capacity worsens to a hard blocker
after review, the fresh server check rejects deployment even with a valid token.

### Joining a shared pod

Adding a container to an existing workload checks the **complete updated pod**,
not just the added container. The review keeps the controller's existing rollout
strategy; it does not silently switch to Recreate or stop anything during preview.

The post-stop view conditionally removes only pods proven to belong to this
Deployment through ReplicaSet controller UIDs. Same-name or same-label pods are
not sufficient evidence. Other consumers still reserve resources, host ports
and exclusive PVCs. Failed/incomplete inventory is reported as unknown; it is
not assumed to free capacity. The projected RAM remains conservative because
observed live usage still includes the old pods.

**Recreate** warns that all old pods must terminate before replacements start,
with downtime for every container. Releasing requests in the preview is not
proof that termination, volume detach or reattachment will succeed.

**RollingUpdate** separately shows replacement overlap while old pods still
reserve capacity. Surge percentages round up; unavailable percentages round
down. For a verified stable, healthy workload with `maxUnavailable=0`, a first
replacement that cannot fit is a blocker. If old-pod removal is permitted or
the workload is already changing, a current overlap shortage is a warning—not
a claim that no valid rollout order exists. Intermediate steps, readiness and
termination timing are not fully simulated. See Kubernetes'
[Deployment strategies](https://kubernetes.io/docs/concepts/workloads/controllers/deployment/#strategy).

Both the restart and capacity warnings must be acknowledged. The signed review
also binds the current Deployment UID and resourceVersion. Changes since review
require another review; the final update uses that version so a concurrent edit
cannot be silently overwritten. A stopped workload remains stopped.
A paused workload only saves its template; prospective resume blockers remain
visible and capacity must be reviewed again before resuming it.

This coverage is for **Deploy/App Store → join existing workload**. The separate
container Edit dialog, Compose batches, image updates and migrations still need
their own guarded review paths. These are read-only preflight checks, not live
failover validation or a guarantee that a rollout will complete.

## Updates

Twice a day - or whenever you press **Check images** - Homestead checks each container's image against its registry - by digest for
tags like `latest`, by version for tags like `1.2.3` - and shows a pill on the
container and a count at the top of the page.

![Image updates](https://github.com/wjcloudy/homestead/releases/latest/download/homestead-image-updates.png)

**Update** pins the new image, watches the rollout (pulling, starting, ready),
and keeps the previous image so **Roll back** returns to exactly it. A registry
that cannot be checked is shown on that container only; the rest still show
their updates. **Settings → Updates** sets the policy: notify only, apply when
approved, or apply in a maintenance window. Updates never jump a major version
by themselves.

While a new image is fetched - on an update or a first start - the rollout
and the job tray show how far it has got: a percentage of the image's size,
from containerd's own count of the layers it has fetched against the sizes
the registry gives. Kubernetes itself reports no more than "Pulling".

### Image cache

**Image cache** lists the images on each node. Kubernetes reports only each
node's largest ones, so Homestead asks each node's containerd for all of them
(a short scan, again whenever the last is over fifteen minutes old) and keeps
the answer, adding anything pulled since. **Clean up** removes an image nothing
uses. *Active* images are what running containers use; *stopped* ones are what
a container scaled to zero starts from, and *scheduled* ones a scheduled job's,
so neither reads as unused. *Rollback* copies are
the image a container had before its last update, kept so **Roll back** can
return to it; **Forget** one and it becomes unused, to clean up like the rest.

On Harvester the page also lists **VM images** - the cloud images VMs are
made from. Each is downloaded once and kept as a Longhorn backing image, with
a copy on every node whose disks were made from it; the table says how big
each is, which nodes hold a copy, and which VMs' disks came from it. An image
nothing was made from can be deleted there; one a disk came from stays, since
the disk keeps reading from it.

## Groups

Groups gather containers under a heading - Media, Home, Monitoring - that folds
away. **Group** in a container's menu puts it in one; **Groups** at the top of
the page ticks several at once. A chip per group shows that group alone.

Homestead's own containers - Homestead, the Samba that serves your shares, and
the backup storage for moves - start in a **Homestead** group, out of the way of
your apps.

On k3s and RKE2, the platform Homestead's add-ons install - KubeVirt, CDI, the
upgrade controller - runs as containers too. They go in the **Homestead** group
and are hidden until **show N platform containers** at the top of the page, as
Harvester's own always are. Each is tagged with what it belongs to and offers
only **Logs** and **Restart**: its operator puts back anything changed by hand,
so it is not updated on its own but upgraded with KubeVirt (or CDI) under
**System → Cluster → Platform versions**.

## Privileges

Some apps need more of the host than containers get by default. A VPN client
such as transmission-openvpn or gluetun opens a tunnel and changes routes; without
permission it stops with:

```
RTNETLINK answers: Operation not permitted
```

**Privileges** in Deploy, Edit and Import:

| Option | Gives | Use for |
|---|---|---|
| **VPN tunnel** | the host's `/dev/net/tun` and `NET_ADMIN` | any VPN client - this is all it needs |
| **Extra capabilities** | named Linux capabilities, e.g. `NET_RAW`, `SYS_ADMIN` | an app whose docs list `--cap-add` |
| **Privileged** | everything the host has | a last resort, as on Unraid |

An Unraid template's Privileged switch and `--cap-add` / `--device=/dev/net/tun`
extra parameters fill these in by themselves, on install from the App Store and
on import.

## Hardware

A container given a hardware feature - an iGPU for transcoding, a Coral for
Frigate, a Zigbee stick for Zigbee2MQTT - gets the device, and is kept on a host
that has it. Features are named under **Settings → Hardware**; see
[Dashboard and nodes](Dashboard-and-nodes#hardware).

## Placement

**Where it runs** (in Edit, or **Placement** in the menu):

- **Spread instances** - its copies on different hosts, so losing one host does
  not take them all;
- **Run on the same node as** - kept with another app it talks to constantly;
- **Keep off the node of** - kept apart, like two DNS servers.

Each is a preference (steer, but start anyway) or a requirement (wait rather
than break it). The editor warns when a rule cannot be met.

## Its own LAN address

A container can have an address of its own on the LAN, beside its pod
network - for an app that wants to be found there (discovery, broadcasts), or
simply to be reached on an address that is only its. In Deploy choose
**Access mode → Its own LAN address (bridged)**, or in a container's editor
tick *Its own LAN address* under *Where it runs*; pick the VM network and one
of the free addresses [IP addresses](Networking#ip-addresses) knows of.

The container joins that network as a second interface, `lan0`, and keeps
the pod network for everything else. It answers on its address directly - no
Service or VIP - and the address is recorded under the container's name in IP
addresses. A Harvester VM network gives no addresses of its own, so the
container gets a copy of it (same bridge and VLAN) holding its one address,
named `<container>-lan`; it goes when the container does.

## If a node fails

When a node stops answering, each container does one of three things -
**If a node fails** on Containers sets them all in one list, and each
container's editor has it under *Where it runs*:

| Choice | What happens |
|---|---|
| **Move to another node** | about 15 seconds later it starts on another node - for apps that should come back quickly wherever there is room. New containers start with this |
| **Wait for its node** | it stays with that node and starts again when the node is back - for apps tied to that host's hardware (a Coral, a Zigbee stick), or better restarted where they were |
| **Kubernetes default** | Kubernetes moves it after five minutes - what anything Homestead did not deploy has |

A container whose volume one node mounts at a time only really moves if
Longhorn lets go of the volume on the dead node. The dialog says whether it
will, and **Let Longhorn release them** sets Longhorn's *Pod Deletion Policy
When Node is Down* so it does; it is also under **Settings → Cluster**.
Changing a container's choice restarts it.

## Architecture

**Architecture** draws, for every app, the path from its address through its
Service to its pods, claims, Longhorn volumes and the replicas on each disk -
the quickest way to see what a failed disk or host would touch. Virtual
machines are grouped below containers rather than mixed into them, but drawn
the same way: the ports of any Service that selects them
(such as Harvester's load balancers), their disks and those disks' replicas,
with their host and address. A stopped VM is shown faded, since its disks are
still there. Homestead's temporary browser, copy and import pods are hidden.

A volume no container or VM definition references is **disconnected**. Those
retained and old volumes are hidden by default; **Show disconnected** reveals a
red group beneath the live volumes without letting it obscure the active paths.

![Architecture](https://github.com/wjcloudy/homestead/releases/latest/download/homestead-architecture.png)
