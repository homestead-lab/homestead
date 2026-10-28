# Containers

**Containers** lists every app you run, as cards or as rows, with its state,
address, image and update status. In Kubernetes terms each one is a
Deployment (or a StatefulSet or DaemonSet you made elsewhere); Homestead calls
them containers because that is what you think of them as.

![Containers](https://github.com/homestead-lab/homestead/releases/latest/download/homestead-containers.jpg)

## Troubleshooting a restarting app

Open the app's **Diagnostics** to see its pod and container state, restart
count, last exit reason and code, and current and previous logs. On its row it
is the bug button beside **⋯**, after Logs; when an update is waiting, the row
shows Update instead and Diagnostics is in the **⋯** menu.
Refresh to collect new evidence; a stopped app needs to be started before
Kubernetes can provide pod diagnostics.

Kubernetes keeps only the latest exit, so **Restarts kept** in Diagnostics holds
why the app restarted before: "Restarted 3 times in 24 h · last: ran out of
memory (limit 512 MiB)", then each restart with its time and reason - ran out of
memory, failed its liveness check, crashed, its host restarted or went away,
evicted, updated to a new image, restarted from Homestead or by its outage
action - and the last 50 lines it printed before a restart. Homestead notes them
as it looks at the cluster, about every 20 seconds, so several restarts between
two looks are one entry that says how many; it keeps the newest 20 per app, for
30 days, on its data volume. The record is the app's own: an app deployed again
under the same name starts with an empty one. Apps only: pods in the system namespaces
are not recorded.

Administrators can expand **Run a command** to inspect a running container.
Review the exact command and target before running it. Commands use the
container's permissions, can change its data, and may return secrets. They
require `timeout` in the image, have a maximum 30-second timeout and bounded
output; detached child processes may survive. Homestead's own workloads and
system namespaces are excluded. Agents can request the same capability,
but an administrator must approve each command in Homestead.

## Deploying a container

**＋ Deploy** takes an image - `lscr.io/linuxserver/jellyfin:latest`, say - and
asks what Docker would: ports, environment variables, volumes, devices. Most
people start from the [App Store](App-Store) instead, which fills all of that in
from the app's template, or [import](Importing) one from Unraid or a Compose
file.

Passwords, tokens and keys - a field the form masks, a variable named like
one (`*_PASSWORD`, `*TOKEN*`, `*SECRET*`, `*API_KEY*`, `*PEPPER*` and so on), a
value with a password or token in it (`postgres://user:pass@db`,
`https://hooks.example.com/x?token=...`), or a value the Secret already holds
under a variable you renamed - are kept in a Secret named `<app>-env`, not in
the Deployment. The container
reads them with `secretKeyRef`; **Edit** shows them as ordinary variables, typed
blind, and saving puts changes back in the Secret. A container saved before
this moves its values over the next time it is edited. The Secret is deleted
with the app.

Below the image field, **Will pull** shows the fully resolved registry,
repository and tag. It does not rewrite the image: for example,
`openspeedtest/latest` is a repository actually named `latest`, so its default
tag correctly resolves to `docker.io/openspeedtest/latest:latest`.

The form asks, in order:

- **Name and namespace.** Apps go in the default namespace (`lab` unless
  another was chosen at install) or any other you make under
  **Settings → Hardware and storage → Namespaces** - see
  [Namespaces](Settings#namespaces).
- **Containers in each pod.** **Add container** adds another program to the
  same workload. Give each its own name, image, resources, ports, variables
  and storage. **Remove container** removes a draft definition; at least one
  stays. **Pod copies** controls how many copies of the whole pod run, so two
  containers with two pod copies means two pods, each running both containers.
- **When the cluster is short of room.** Under **Optional settings**, a new
  app can start as **Critical**, **Normal** (the default) or **Low**; see
  [Priority tiers](#priority-tiers). It applies to a new workload, not to a
  container added to one that exists, and the review shows the class it will
  use. If the app is put in a group later, it follows the group's tier only
  while it was left at Normal.
- **Memory reserved and Memory max.** Reserved memory is the scheduler's
  request; optional Memory max is the container's enforced limit and must be
  at least the request. Leave max blank for no container memory limit. A limit
  set too low can OOM-kill and restart the app. Edit shows each container's
  current values, including sidecars. Compose imports carry `mem_limit` and
  `deploy.resources.limits.memory` into Memory max.
- **What it uses, and what to reserve.** Homestead records each app
  container's CPU and memory every five minutes, from metrics-server (k3s and
  RKE2 have it; without it, Settings -> Homestead says so), and keeps two
  weeks. Edit shows, beside each reserved field, what the
  container was usually at or under - 95% of readings over the last seven
  days, to within about 5% - and its peak. After a day of readings it offers
  a reservation: CPU at 1.5 times that usual use (at least 10m, a hundredth of
  a core), memory at 1.25 times (at least 32 MiB), rounded up to 0.5% of a
  core and 16 MiB, and never above a CPU limit or Memory max. A reservation
  already within 10% of that is left alone; otherwise the suggestion says, for
  CPU and memory apart, whether it raises or lowers each and from what. **Use suggested** only fills the
  fields; nothing changes until you save, which restarts the app. The
  scheduler places apps by what they reserve, not by what they use: an app
  that reserves nothing, and has no limit Kubernetes would take instead, is
  placed as if it used nothing.
- **Network.** Which ports are reachable from your LAN, and on which address:
  automatic (the next free VIP), a **Specific VIP** you picked out under
  [Networking](Networking#add-default-and-choose-workload-vips), or shared with other apps on one address.
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
- **Logo** - pick its logo from the app store, or give an address (below).
- **Monitoring** - how Homestead checks that it answers, and what to do when
  it is down (below).
- **History** - who changed it, when and what, with **Undo** (below).
- **Search logs** - find a line in its recent logs (below).
- **Schedule** - stop and start it at set times (below).
- **Updates** - whether it updates itself in the maintenance window (below).
- **Placement** - where it runs (below).
- **Rename** - replaces the Deployment under its new Kubernetes name; Services
  and volumes keep their existing names and addresses.
- **Move** - to [another cluster](Moving-between-clusters), into the namespace
  you pick there. Moving to another namespace on the same cluster is not offered.

**Edit** opens the same form as Deploy, with everything the container has now.
Storage can be restructured there: two volumes combined into folders of one, a
folder split out to its own volume. When a path moves, Homestead offers to bring
its data along - it stops the container, copies, and starts it again - and the
old volume is kept until you remove it. A new volume is given the old
location's owner and permissions, whether its data comes along or it starts
empty, so an app that runs as its own user can still write to it.

### Apps applied by Flux or Argo CD

Flux and Argo CD put back what git says at their next sync, usually within ten
minutes. So when one of them applies an app - Homestead reads the labels Flux
and the annotation Argo CD leave on it, or your own **Updated elsewhere** mark -
the **Edit** and **Move** reviews say so before you save:

> Flux applies this app (Kustomization flux-system/apps). This change lasts
> until Flux next applies it. To keep it, change it in git.

You can still save: trying a change here and then committing it is fine.
**Rename** asks for one more tick, "Flux will bring back the old name": a
rename makes the app under its new name and removes the old one, and Flux then
brings the old one back, so you would have two copies. Rename it in git
instead. An AI agent renaming such an app has to say the same, with
`confirm_managed: true`.

#### Is Flux applying it?

A note that says "change it in git" is only half the story if Flux has
stopped applying what is in git - after a bad commit, an expired deploy key or
a missing Secret. So Homestead also reads each Kustomization and HelmRelease,
once a minute:

- **An app's details** (open its row on Containers) show what applies it,
  whether that is Ready, the revision it last applied (`main@a1b2c3d`, or a
  chart and its version), when it last tried, and Flux's own message when it is
  not Ready.
- **System › Cluster** has one GitOps line, for example "2 Kustomizations,
  6 HelmReleases: all ready", and lists any that are not Ready.
- **An alert** (Degraded) is raised when one has not been Ready for 10 minutes,
  and cleared when it is again. Flux's message is on the Cluster page, not in
  the alert, since it can name repositories and Secrets. A suspended one is shown
  as suspended and never alerts: someone paused it on purpose.
- **Reconcile now** (admins, in the app's ⋯ menu) asks Flux to apply the app
  again straight away, instead of at its next interval: it sets
  `reconcile.fluxcd.io/requestedAt` on the Kustomization or HelmRelease, as
  `flux reconcile` does. Flux applies the revision it last fetched; a commit
  pushed since waits for its GitRepository's next check (`flux reconcile
  --with-source` fetches first). A HelmRelease that has used up its retries
  needs `flux reconcile helmrelease --reset`. The review names every app that
  one applies, since Flux applies them all. The dialog then follows Flux until
  it has taken the request and shows the result; the app's details say
  "waiting for Flux" until then. It is not offered for a suspended one, and the
  audit log (`audit.jsonl`) records who asked. Apps are matched as Deployments,
  so a StatefulSet or DaemonSet Flux applies has no Reconcile now.

Apart from Reconcile now, this only reads: Homestead does not suspend, resume
or change Flux objects. Argo CD's Applications are not read.


#### Copy as YAML for git

The edit review has **YAML for git**: the change as a patch, with only the
fields it sets, under the app's kind, name and namespace. **Copy as YAML for
git** puts it on the clipboard; paste it into a kustomize patch, or set the same
fields in the app's manifest. Containers, environment variables and volumes are
listed by name, so the patch merges into the ones there; one you removed is
marked `$patch: delete`. Homestead's own bookkeeping - update history, the
digests it last ran, restart stamps - is left out; settings you chose, such as
the group or logo, stay in. Services and volumes are not in it.

Secret values are moved out of the YAML, as well as Homestead can tell them:
a variable it keeps in the app's own Secret (a masked field, a name that looks
like a password or token, or one the Secret already holds, even under a new
name) and any value with a password or token in it (`postgres://user:pass@db`,
`https://hooks.example.com/x?token=...`) appear as a `secretKeyRef` to that
Secret, with the `homestead.io/env-secret` annotation a save adds. It is a
heuristic: check the YAML before committing it to a public repository.

That Secret (`<app>-env`) exists only once Homestead has saved the app, and
never in git. If Flux applies the app, create the Secret in git first - for
example encrypted with SOPS - with the keys the review lists, or save the app
here first. Otherwise the pod cannot start.

On **Image updates**, an app Flux or Argo CD updates has the same button: the
new `image:` line for each container, pinned to the new digest when only what
the tag points at moved.

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

Renames review conditional post-stop capacity, then recheck after the original
pods have terminated (see below). Data-copy edits review both the temporary helper
and the final workload, then check placement afresh before each starts. The app
stays held at zero replicas until the confirmed copy Job completes and its pods
exit. Changed workload, PVC, backing PV or helper identities stop the handoff.
**Recent jobs → Inspect outcome** lets an admin release a verified inactive copy's
hold after typing the workload name and acknowledging partial data. This does not
start anything, undo mappings or delete volumes or Jobs. Unknown creation receipts
require Kubernetes inspection; they are never adopted by name or replayed.
Destination files can be overwritten or partially copied: take a backup first.
Checks do not prove free filesystem space, application consistency, absence of
external writers, or atomicity against changes made outside Homestead. Old
`restructure` jobs without identity receipts stop for inspection; their legacy
hold needs manual Kubernetes recovery after checking the copy and data. Finish upgrading
all Homestead replicas before using the new flow.
Paused Deployments cannot increase replicas through Edit until resumed and
reviewed again. [Compose batches](Importing#batch-capacity-review) have a joint
preflight and before-each-service recheck. [Manual host moves](#moving-between-hosts)
also review the whole replacement pod and chosen destination before restarting.
Image updates and rollback also review the complete rollout. Unraid/cross-cluster
migration remain separate paths; extending durable handoff coverage is planned. VM launches have
their own [placement reviews](Virtual-machines).

### Renaming a workload

Change **Workload name** in **Edit**, then review **Rename workload**. It is a
name-only action: save any other edits separately. Expect an outage while the
old pods stop and the replacement starts. A stopped workload stays stopped.

Homestead stages the replacement at zero replicas, verifies the original pods
have gone (including terminating pods), and checks placement again using the
actual API-admitted replacement. Memory-pressure warnings can be accepted;
a hard placement blocker cannot. Every scale or deletion is fenced by the
Deployment's UID and resource version. Existing service-selector labels,
volumes and service addresses are preserved. The old Deployment is removed
only after the replacement is ready, without garbage-collecting its dependents;
stopped old ReplicaSets are also retained. Readiness is Kubernetes readiness,
not proof of application health or storage fencing after a host failure.

**Recent jobs → Log** follows the rename. Each reviewed approval is single-use
and persists across restarts. If a step fails, times out, or its response is
lost, Homestead stops making changes. It does **not** automatically restart the
old workload, delete the replacement, or claim that rollback succeeded. Check
both names before starting either: the replacement may already be running.
An admin can **Inspect outcome / Review retained resources**, type the original
name and acknowledge possible late effects to stop tracking. This changes no
cluster resources and does not make the old approval reusable. A live dispatcher
cannot be dismissed through recovery; restarting Homestead does not replay it.

Autoscaled, paused and controller/Helm-owned Deployments need their controlling
configuration handled first. An incomplete import copy also blocks rename.
Other external controllers and concurrent changes cannot be made atomic with
this workflow; capacity is a fresh estimate, not a reservation. Do not delete
job history or approval-ledger files to bypass a retained rename.

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

These rollout details apply to **Deploy/App Store → join existing workload**
and the Edit dialog. Manual host moves review the explicit Recreate change;
Compose has a separate whole-batch review. Image updates and rollback use the same
full-pod planner; cross-cluster/data migrations still need guarded review paths. These are read-only
preflight checks, not live failover validation or a guarantee that a rollout completes.

## Overriding capacity warnings

High projected RAM, low headroom, unbounded memory and missing live usage metrics
are warnings, not automatic vetoes. In Start/Scale, Deploy, Edit, Compose and
manual host-move reviews, tick **Proceed despite capacity warnings**, then
confirm the action. Estimates above 100% can also be acknowledged. This accepts
the risk of memory pressure, OOM restarts and downtime; it does not lower resource
requests, remove memory limits or reserve capacity.

Placement blockers are separate: insufficient schedulable resource requests,
incompatible hardware/topology, exclusive-volume conflicts and stale or incomplete
required reviews still need resolving. An acknowledgement cannot make Kubernetes
schedule a pod that violates those constraints. The API rechecks before writing.

## Updates

Twice a day - or whenever you press **Check images** - Homestead checks each container's image against its registry - by digest for
tags like `latest`, by version for tags like `1.2.3` - and shows a pill on the
container and a count at the top of the page.

![Image updates](https://github.com/homestead-lab/homestead/releases/latest/download/homestead-image-updates.jpg)

**Update** pins the new image, watches the rollout (pulling, starting, ready),
and keeps the previous image so **Roll back** returns to exactly it.
Homestead's own updates are not listed here: they are on the top bar's
**Homestead** button and under [Settings → Updates](Settings#homestead-updates). A registry
that cannot be checked is shown on that container only; the rest still show
their updates. **Settings → Updates** sets the policy: notify only, apply when
approved, or apply in a maintenance window. Updates never jump a major version
by themselves.

### Automatic updates

A container's **Updates** (in its menu) can let it update itself. It is off for
every app until you turn it on. Inside the cluster's maintenance window - and
never while the cluster policy is notify only - Homestead takes one such app at
a time that has an update waiting, and:

1. snapshots each of its Longhorn volumes;
2. installs the update as **Update** does - pinned to the new digest, the old
   one kept for **Roll back**;
3. waits for the new pods, then for the app to answer its
   [monitoring](#monitoring) check again;
4. succeeds, or - if the rollout stalls or the app stops answering - rolls the
   image back and says so in the job, an alert and a push.

No automatic update starts while a rebalance is moving containers. The same
window can move containers back after a host returns
([Settings](Settings#when-a-host-comes-back)).

Only a newer build of the tag the app already follows is taken (a new digest of
`:latest` or `:1.2`, never 1.2 to 2.0): a new version waits for you. An app that
is not answering or not fully running beforehand is left alone, as there would
be nothing to compare with. The snapshots stay after a rollback, since an update
can change data that rolling the image back does not; the newest two
before-update snapshots of each volume are kept after updates that worked. A
row or card shows an **Auto** tag while it is on.

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

An update keeps one previous image per container for Roll back; the one before
it becomes unused. Unused images stay on every host that ran them until they
are cleaned, or until a host's image disk is 85% full and its kubelet removes
some. Two ways clear them from every host at once:

- **Clean all unused** reviews every unused image - its size, the hosts that
  hold it and how long it has been unused - and removes them all after you
  type CLEAN. It removes exactly what the review listed: if anything changed
  since, it asks you to review again.
- **Automatic clean-up** (off until an admin turns it on) removes images that
  have been unused for a number of days, 14 by default, checking every hour on
  the leader. Days count from when Homestead first saw an image unused, since a
  host doesn't record when an image was last used. The page shows its last run.

Neither ever removes a running image, a stopped app's or scheduled job's image,
a stopped VM's container disk (a VM pool's or replica set's at zero too),
the image of a job or app waiting for its pods (a suspended or retrying Job,
a DaemonSet on no host, a Deployment that cannot start its pods), the container
disks a Harvester VM template or a VM snapshot boots from, an image a CDI import
pulls through the host (`pullMethod: node`), a Roll back copy, or Harvester's
and Kubernetes' own images. An image removed is pulled again if something needs
it later.

If a record that says which images are in use can't be read - a Roll back note
or a template that is malformed, or a VM pool list Homestead may not read -
every image is kept and tagged *unreadable* with the app it belongs to, and
nothing is cleaned up until it is fixed. An image's unused days keep counting
from when it was first seen unused; they don't start again once the record is
fixed. Reading VM pools, replica sets, VM templates, VM snapshots and CDI's
scheduled imports needs read-only `get` and `list` on `virtualmachinepools`,
`virtualmachineinstancereplicasets`, `virtualmachinetemplateversions`,
`virtualmachinesnapshotcontents` and `dataimportcrons`; Homestead adds them to its own role when
it starts, or apply the release's `deploy/rbac.yaml` (see
[updating](Installing-Homestead#updating-homestead)).

On Harvester the page also lists **VM images** - the cloud images VMs are
made from. Each is downloaded once and kept as a Longhorn backing image, with
a copy on every node whose disks were made from it; the table says how big
each is, which nodes hold a copy, and which VMs' disks came from it. An image
nothing was made from can be deleted there; one a disk came from stays, since
the disk keeps reading from it.

## Monitoring

Every minute Homestead asks each running app at its address whether it answers,
as a browser would - and VMs too (see [Virtual machines](Virtual-machines#monitoring)). Three misses in a row and it is **Down**, which raises an
alert; its first answer brings it back up. The **Monitoring** page (Apps ›
Monitoring) shows each app's last 24 hours hour by hour or 30 days day by day,
problems first; the Dashboard has a Monitoring widget in four widths; and the
Portal's dots and a row's **Down** or **Slow** tag come from the same checks. A
container's history is kept hour by hour for 30 days. and a container's **Monitoring** (in its menu,
or the Monitoring step of Edit) chooses how it is asked:

- **Automatic** - an HTTP answer below 500, or an accepted connection if the app
  does not speak HTTP.
- **A web page** - only an HTTP answer below 500 at the path you give counts.
- **A connection** - the port accepting a TCP connection is enough.
- **Not monitored** - no checks and no alerts.

An app that publishes more than one port also has **Port to ask**: its main
port (see **Main port** above) by default, or any one of the ports it publishes.
If that port is later removed from the app, it stops being checked and its
Monitoring says why, until another is chosen. None of this restarts the app.

**When it is down**, in the same dialog, does nothing unless you choose it:

- **Restart it** after it has been down for so many minutes, then again every
  that many minutes while it stays down, at most so many times. Out of tries, it
  is left alone and an alert says so; the count starts again once it has
  answered for 30 minutes. Nothing is restarted while a job is working on it -
  an automatic update watches the app itself and rolls the image back if it
  stays down, and a restart then would read as the update failing. A VM is
  rebooted cleanly, and only while it is monitored on a port you chose.
- **Call a webhook**: an HTTP POST when it goes down, comes back, is
  restarted or runs out of tries, and when an automatic update of it finishes or
  is rolled back. **Format** is Generic JSON (fields `event`, `kind`,
  `namespace`, `name`, `state`, `since`, `error`, `detail`, `at`, and a
  sentence as `text`, `content` and `message`, which Slack, Discord and Home
  Assistant show as it is), **ntfy** or **Gotify**. Once saved, the address
  shows only up to its host, the rest as `(hidden)`, and a LAN address needs
  an admin's **Allow a LAN address**. **Send a test** (admins) sends one now. More in
  [Alerts and PWA notifications](https://github.com/homestead-lab/homestead/blob/main/docs/notifications.md#outage-webhooks).

## Logos

A container's **Logo** opens a picker: logos from the app store, the one that
matches the container's own image first (marked **image match** - the same
software, so nearly always right), then those that match its name, and a Helm
chart's own icon for an app installed from a chart. Search for another, or paste
an address instead. **⋯ → Logos for apps without one** lists every app with no
logo beside its best guess, to accept the image matches in one go and choose the
rest; an app you skip is not suggested again. Edit's logo field has the same
**Find** button.

An address can be an image (PNG, JPEG, GIF, WebP, ICO or SVG) or a web page: for
a page, Homestead takes the site's own logo when it is good enough - an SVG, or
a picture at least 64 pixels across. Whatever is chosen is fetched once and kept
on Homestead's volume, so the logo stays when the original goes away. An SVG is
rebuilt from its drawing elements only - no scripts, links or embedded files -
before it is kept. Changing a logo never restarts the app.

## Change history

Every minute Homestead compares each app with what it saw last - images,
environment, CPU and memory, ports, volumes, copies, command, host network, LAN
attachment and placement - and records any difference: when, who (or **outside
Homestead**, for kubectl, Helm or GitOps), and each field before and after. A
container's **History** shows its own; **⋯ → Change history** shows every app's,
newest first. A value that may be secret - a variable from a Secret, or one
whose name looks like a password or token - only says that it changed.

**Undo** puts back the settings from before a change, after the same review as
any edit: the fields that will change, and whether there is room. It replaces
the app's pods, as any such edit does. Stops and starts on a
[schedule](#schedules) are not recorded. The history keeps 50 changes per app
for 180 days.

## Searching logs

**⋯ → Search logs** finds a line in the recent logs of every app at once -
an error, a request ID, a user - or, from a container's menu, in that app's
alone. Choose how far back (15 minutes to 24 hours); matches come newest first,
with what matched marked, and each opens that app's live logs. **Match case**
and **Regular expression** are options (an expression that repeats a repeating
group is refused, as it can take ages to run, and any expression gets 10
seconds of matching per search: when they run out, the newest lines already
checked are shown and the search names the apps it searched only in part).

It asks Kubernetes for each running pod's logs, so it reaches back only as far
as each pod still holds them; lines from pods since replaced are not searched.
It reads up to 60 containers, 2 MB from each, and shows up to 500 matches - and
says when it reached any of those, or could not read a pod. One search runs at a
time.

## Schedules

A container's **Schedule** stops it at one time and starts it at another, on the
days you choose - overnight, say, or outside working hours. Either time can be
left out, to only stop or only start it. The dialog shows the next few stops and
starts and how the last one went; **⋯ → Power schedules** lists every app and
VM on a schedule. Times are in the time zone of the browser that set them, and
follow daylight saving.

A scheduled stop and start work as **Stop** and **Start** do: the app goes back
to the copies it had, after the usual room check. A schedule acts at its times
and nothing else - starting the app by hand in between is fine; it stops again
at the next stop time. A time missed while Homestead was not running (by more
than half an hour) is skipped, not done late. A stop or start that could not be
done raises an alert until the next one works. Homestead itself cannot be put
on a schedule. VMs have the same, in their own menu.

## Scheduled jobs

**Schedules** lists the CronJobs in every namespace, Homestead's own first: an
app's nightly backup applied by Flux or Helm sits beside the ones made here.
Each row says when it last ran and how that run ended - succeeded, failed (with
Kubernetes' reason), running, never run - and when it last succeeded, from the
Jobs it started. **did not run** means a run it was due to start has not started
15 minutes after its time. When the Jobs cannot be read the result is
**unknown**, and the page says why; it is never shown as fine.

Only the CronJobs Homestead made in its own namespace can be edited or deleted
here, and Run now works in its own namespace only. The rest are read-only and say
where they came from: **Applied by Flux**, **Longhorn recurring job**, or who
manages them. For CronJobs in other namespaces Schedules shows their health only
- name, schedule, last run, result and its reason code - never their image,
command or volumes, which can carry a password; alerts name the job and the time.

A failed run raises an alert until a run succeeds - a run in progress, or the
failed run's Job removed by Kubernetes, keeps it - and a run that did not
happen, until one starts (a long run still going is late, not missed); see
[Notifications](Settings#notifications). A job that runs more
than once an hour alerts only after 30 minutes without a success. Paused
CronJobs are not watched, nor Longhorn's recurring jobs: their backups and
snapshots alert in Data protection. A CronJob with no time zone runs on the clock
of the cluster's controller, which Homestead cannot see; it is counted late only
when it is late on every clock its last run fits, and not judged before its
first run.

## Groups

Groups gather containers under a heading - Media, Home, Monitoring - that folds
away, with a chip per group at the top of the page that shows that group alone.

A group is a label on each container (the `homestead.io/group` annotation), not
a list of its own, so **a group exists while at least one container is in it**.
There is no empty group to create in advance, and moving or ungrouping the last
container in a group removes it. Changing a container's group never restarts it.

- **To create a group**, put a container in it: **Group** in the container's
  menu, then type a new name (names already in use are suggested). Names are free
  text of up to 40 characters, so a typo makes a second group.
- **To move several at once**, use **⋯ → Manage groups**. Filter the list by name
  or namespace, or show one group (or **Ungrouped**); tick containers - what you
  tick stays ticked while you change the filter, and the count says how many -
  then name a group and **Move ticked**, or **Ungroup ticked**. Each group's
  heading folds, and **Tick these** ticks all of its shown containers.
- **To rename a group**, use **Rename…** on its heading in Manage groups: every
  container in it moves to the new name. A name another group already has
  merges the two.
- **To remove a group**, use **Ungroup all…** on its heading, or **Ungroup
  ticked** with its containers ticked. For one container, **Remove from
  <group>** in its **Group** dialog.
- **To give a group a priority tier**, use **Tier…** on its heading: see
  [Group tiers](#group-tiers).

Which group's chip is chosen, and which groups are folded on the page, are kept
in the browser, so each device remembers its own.

## Platform containers

Homestead's own containers - Homestead, the SMB and NFS servers that serve your
shares, and the object store moves use - are platform containers: hidden until
**show N platform containers** at the top of the page, and tagged **Homestead**
when shown. They offer **Logs**, **Restart** and where each is looked after
(Settings › Homestead, Network Shares, Linked clusters). Their updates are not app
updates: they are not counted on the Containers page or its update button, and
are installed from the **Homestead** button on the top bar or
**Settings › Updates**.

On k3s and RKE2, the platform Homestead's add-ons install - KubeVirt, CDI, the
upgrade controller - runs as containers too. They are hidden with Homestead's,
as Harvester's own always are. Each is tagged with what it belongs to and offers
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
that has it. Features are named under **Settings → Hardware and storage**; see
[Dashboard and nodes](Dashboard-and-nodes#hardware).

## Containers sharing one pod

In **Edit > Basics**, **Add container** adds a container alongside the existing
ones. Its card appears in Hardware and access, Environment values and Storage
too; these are the same container's fields, not separate instances. Names and
images stay linked across steps. Containers share the pod's network, so use
different listening ports for different programs.

**Remove container** stages removal from every step. **Undo removal** restores
its unsaved fields. The last container cannot be removed. Nothing changes on
the cluster until **Save & restart**, the review, and confirmation. The review
names additions and removals; every pod rolls out, and persistent volumes and
their data are retained. Init containers and managed references stay as they are.

## Placement

**Placement** (a step of Edit, and **Placement** in the menu):

- **Spread instances** - its copies on different hosts, so losing one host does
  not take them all;
- **Run on the same node as** - kept with another app it talks to constantly;
- **Keep off the node of** - kept apart, like two DNS servers.

Each is a preference (steer, but start anyway) or a requirement (wait rather
than break it). The editor warns when a rule cannot be met.

### Starts after

**Starts after** (in Edit → Placement) names the apps this one needs, a
database say. Each time a pod of the app starts, it first waits until those
apps answer on their Service, then starts. Choose each app and, where it
listens on more than one port, the port to wait for. **Wait up to** sets how
long (30 seconds to 60 minutes, 5 by default), and **If it never comes up**
whether it then starts anyway or keeps waiting.

- **What counts as a start:** every new pod of the app, whatever made it: a
  cold boot after a power cut, a host restart or drain, an update, a move, a
  scale-up, a Flux reconcile once the lines are committed, and the starts
  Homestead makes. When several apps' power schedules start them in the same
  minute, the ones others need go first.
- **What it doesn't cover:** a container restarting inside a pod that is
  already running, and an app it needs going away later. It does not stop the
  app then.
- **Saving it restarts the app once**, because the wait is a small first step
  in its pod (an init container). Saving the same setting again restarts
  nothing. Edit's review says so before anything is saved.
- **Refused when saved:** a loop between apps (Edit names it, "a cycle: app-1
  → app-2 → app-1"), an app with no Service and TCP port that can show it is
  ready, and a pod that can't use cluster DNS. Homestead itself, its helpers and
  system apps can't wait for other apps; other apps can start after Homestead.
- **Apps Flux or Argo CD applies** are never changed. Edit shows the lines to
  commit where they are applied; **Show the lines to commit** gives them for the
  apps you chose. Until they are committed, Architecture draws the link dashed.
- **While it waits,** the app's row says **Waiting** and its details say for
  what and for how long ("Waiting for app-2 (3 min of 5 min)"). If it started
  anyway, its details say when and without what, and an alert says so until it
  next starts with everything it needs. **Start** on an app says which of the
  apps it needs are stopped, and how long it will wait for them.
- A wait longer than 10 minutes can show an update as "not progressing" in
  Kubernetes; it carries on once the app it waits for is up.
- **A start gate changed outside Homestead** (kubectl, say) is named on the
  app's details and in an alert; **Put the start gate back** in Placement
  renders it from the setting again, restarting the app once.

The setting, the routes and the gate are described in the
[reference](https://github.com/homestead-lab/homestead/blob/main/docs/reference.md#start-order).

### Moving between hosts

The host picker is a preliminary hardware/usage comparison. **Review move**
checks the full Deployment, including sidecars, init stages, replicas, resource
requests, host ports, PVC restrictions and checked topology rules. A selected
host must fit the whole desired replica count, even for a soft preference.
This is conservative for spread workloads: use the placement editor to change
their multi-host rules instead. Soft preferences remain soft; Kubernetes may
choose another eligible host, including the current one. Hard pins prevent failover.

Moves save the **Recreate** strategy: old pods stop before replacements start,
so every container in the workload has downtime. Clearing a host preference
can also restart pods. A stopped workload stays stopped; paused workloads and
templates with a direct `nodeName` binding must be resolved before moving.
Other required node selectors and affinity rules are preserved, not cleared.
Host-path files are not copied and pod-local `emptyDir` contents are lost.

The signed ten-minute review binds the destination, pin mode and controller
UID/resourceVersion. The server checks fresh capacity before its PUT and keeps
that resourceVersion, so a concurrent edit conflicts instead of being overwritten.
Missing/incomplete pod or ReplicaSet ownership inventory blocks a move. Missing
live RAM and other unknown scheduling constraints remain explicit warnings.
The review conditionally releases only UID-owned old pods' reservations; it
cannot guarantee termination, storage detach/reattachment or readiness. It is
not an atomic reservation against concurrent starts or a live failover rehearsal.
Follow the rollout in **Recent jobs**; a saved placement is not a completed move.

API clients first POST the move input to `/api/move/preview`, then send that
same input plus `capacity_token` and `confirm_capacity: true` to `/api/move`.
Unreviewed calls are refused. Legacy `auto` calls must select and review an
explicit suggested destination; it cannot change silently at execution time.

### Balance hosts

**Balance hosts** on Nodes, or **Rebalance containers…** in Containers' menu,
evens out a cluster where one host ended up running most of the busy apps -
after a reboot, say. It proposes the fewest moves (ten at most) that bring the
busiest host down, each one worth its restart, by what the hosts and apps use
now. Untick any move you do not want, then start it.

Only what the scheduler could already place elsewhere moves: a Deployment with
one copy, not pinned to its host, not on host-local storage, and only to hosts
its hardware and placement rules allow. An app reading its volumes locally
moves only to a host that holds a copy of them. An operator's review leaves out
apps only an admin may change, as Move does, and the job checks again before
each move.

When the one host that would even things out runs something an app is kept
apart from, that one moves first, to another host it may run on, and the app
follows into the room it leaves - only when the two moves together are worth
their restarts. Both rows say so.

**Not moved** gives a reason for every app that could move but does not: no
other host it may run on (with each host's reason - no Coral, no iGPU, no copy
of its volumes, a standby host), kept apart from what runs there, or no move of
it worth a restart, with how far apart the hosts would be after it. Apps too
small to shift a host are listed together.

The **Volume copies** step can follow the container moves: with **Move a copy
of each moved container's volumes to its new host** on (the default), a
container moving to a host with no copy of its volumes takes one with it,
moved from a host it is not attached to, so its reads stay local. Off, its
volumes stay where they are. The job moves one
container at a time - its image pulled onto the new host first, as Move does,
so a host already holding it does not win - waits until it runs on its new host, and stops if one
does not come up within ten minutes; cancelling stops after the current one. The review
holds for ten minutes, and an app edited after it is skipped.

With a hot spare ([Settings](Settings#how-hosts-share-the-work)), apps on
standby hosts are moved off first - as far as the other hosts stay under 90%
full - and nothing is moved onto them. **Settings → Placement → When a host
comes back** can suggest or schedule this when a host returns.

### Moving several at once

**Move several…** in Containers' menu, or **Select** on a host page's On this host card, moves the containers you
tick to a host you choose - one host for all of them, or another for any one.

The review shows each container's move, or why it stays: pinned to its host, storage on its host, more than one
copy, paused, a host named in its spec, spread rules, already there, a host that can't run it, kept apart from what
is there, a disruption budget that allows no restart now, or more than ten at once. The hosts' memory is shown with
every container sent there counted, so two that fit apart but not together are both marked.

One job moves them, one at a time, each restarting on its new host. It stops at the first that does not go - one
edited since the review, one that did not start within ten minutes - and says what happened to each. The review
holds for ten minutes.

### Reservations from use, for many apps

**Set reservations from use…** in Containers' **⋯** menu offers the
reservation Edit would suggest for every app at once. Apps reserving nothing
are ticked - at most 50, the most one run changes; apps whose reservation only
differs from their use are listed unticked, since it may have been set on
purpose. Ticking or unticking works the plan out again.

These are left out, with the reason:

- apps Flux or Argo CD apply, since their next sync would undo the change -
  each shows a patch to commit instead, a Deployment with only the
  containers' `resources` set;
- Homestead itself, whose reservations come from its manifest;
- apps with less than a day of readings;
- for an operator, apps only an admin may change - management, system,
  service-account or host-privileged ones, as in Edit.

Starting it runs a job, **Set reservations on N apps**, that changes one app
at a time - all its containers in one restart - and waits for it to run again
before the next. An app is skipped, with the reason in the job, if its
reservation was changed by hand since the review, if a limit lowered since
would be below the new reservation, if it is paused or not running as it
should, if no host it may run on has room for its new reservation, or if it
is gone. Only the hosts the app may run on count - its pins, node selectors and
required affinity, and the hosts' taints - so an app pinned to one full host
is skipped however empty the others are. A stopped app takes its new
reservation when it next starts. A container that reserved only through a
limit is shown with that limit; a lower reservation means it no longer
reserves all of it. One that does not come back within ten minutes stops the
job; the rest are not changed, and the job says what that app reserved
before. Follow and stop it in **Jobs**. An app scaled by a
HorizontalPodAutoscaler on CPU or memory use scales against the new
reservation, since its targets are a share of it.

### Priority tiers

When a host fails and the others cannot hold every app, the scheduler fits
them by priority. Each app has a tier:

- **Critical** fits first, and may evict any pod of a lower tier to fit - low
  apps, and anything with no tier, VMs included - whenever it does not fit:
  after a host fails, but also when it restarts, updates or grows. For what
  the house depends on: home automation, passwords, a database, DNS or a
  tunnel.
- **Normal** is no tier at all, as every app has by default, and may evict low
  apps to fit. (On a cluster with a default PriorityClass of its own, it means
  that class.)
- **Low** waits for room and evicts nothing, and is the first evicted by any
  other app that needs room - a CI job can be stopped part way. For batch
  work and CI runners.

Set one app's tier in Edit → Placement → **When the cluster is short of
room**, or many at once with **Priority tiers…** in Containers' **⋯** menu,
where apps that look like home automation, a password manager, a database,
DNS or a tunnel start at critical - not their exporters or backup helpers -
and CI runners at low, at most 50 changes a run. Changing a tier restarts the
app; the bulk job changes one app at a time, as reservations do.

VMs have tiers too, in the same **Priority tiers…** list. A VM takes its tier
when it next starts, so setting one restarts nothing; until then it keeps the
one it started with.

StatefulSets are in the list too, outside the system namespaces, as
`ns/name` with a StatefulSet chip. A StatefulSet always has a tier of its own:
groups don't apply, since it isn't a Containers app. As with an app's own
tier, one Flux or Argo CD applies is read-only (set `priorityClassName` where
it's applied), and one Homestead may change starts at its suggestion. The job
writes the class to its template and the StatefulSet restarts its pods one at
a time; it waits up to ten minutes per pod. With a `partition` below its
replicas, only the pods at or above the partition restart, and the job waits
for those. One that doesn't restart itself - `updateStrategy: OnDelete`, or a
`partition` at or above its replicas, often a database operator's choice -
takes its tier when its pods are next restarted. Homestead never deletes its
pods.

#### Group tiers

A group can have a tier too: **Tier…** on its heading in Containers sets it to
critical, normal, low or none. It takes the same role as setting an app's tier
(operator), and Homestead's own group isn't offered. Apps that follow the group
take its tier. An app's tier is, in order:

1. its own;
2. its group's, if it follows the group and the group has a tier;
3. the cluster default.

An app follows its group when it carries the annotation `homestead.io/tier:
group`. Putting an app in a group makes it follow, unless it already has a
class of its own, Flux or Argo CD applies it, or it is Homestead itself
(Homestead looks after its own tier). Moving it to another group
keeps it following; ungrouping it stops it, and it keeps the class it runs
with. Renaming a group moves its tier and its apps, and makes no app follow
it. In Edit → Placement, **From its group** (shown as, say, "critical, from
Home") follows the group, and a tier makes it the app's own. When this shipped,
every app kept the tier it had as its own. An app that was in a group before
group tiers came keeps its own tier: to make it follow, choose **From its
group** in Edit, or move it out of the group and back in.

Setting a group's tier restarts nothing. **Priority tiers…** then lists every
app with its tier, where it comes from, what it runs with now, and one status:

- **in line**: it runs with its tier;
- **needs a restart**: ticked for the reviewed job, which writes the class and
  records it in `homestead.io/tier-class`;
- **change in git**: Flux or Argo CD applies it, with the line to commit;
- **changed outside Homestead**: it follows its group, but its class isn't the
  one Homestead wrote. It's shown, not ticked, so the job never quietly undoes
  someone's change;
- **can't set**: Kubernetes' own class, or one that isn't on the cluster;
- **group tier unknown**: the group tiers couldn't be read just now. Such rows
  are never acted on until they can be.

Group tiers are kept in the ConfigMap `homestead-group-tiers` in Homestead's
namespace. They aren't in git, so a cluster rebuilt from git keeps every app's
class but loses its group tiers. A Homestead config backup brings them back.
A renamed group keeps its tier; a group that ends (its last app leaves) loses
it.

How a tier changes, whoever deployed the app. A running pod's priority can't
change, so in every case the app's template changes and its pods are replaced.

| Deployed by | Where the tier is set | Then |
|---|---|---|
| Flux (Kustomization) | Git: `spec.template.spec.priorityClassName` in the manifest, or a kustomize patch | Flux applies it at its next sync; the app restarts |
| Flux (HelmRelease) | Git: the chart's value (usually `priorityClassName`) in the HelmRelease's values | Flux upgrades the release; the app restarts |
| Argo CD | Git, as for Flux | Argo syncs; the app restarts |
| Helm | The chart's value, then `helm upgrade` | The app restarts |
| Homestead | Edit, Priority tiers…, or its group's Tier… then Priority tiers… | Homestead changes the template; the app restarts once |
| kubectl | `kubectl patch` of `spec.template.spec.priorityClassName` | A rollout; the app restarts |
| A KubeVirt VM | `spec.template.spec.priorityClassName` on the VM | It takes effect at the VM's next start; nothing restarts now |
| A StatefulSet, by Homestead | Priority tiers… | Its pods restart one at a time (with a partition, those at or above it); with `OnDelete` or a partition at or above its replicas, at their next restart |

Homestead can't tell a plain Helm install from its own apps, so it treats them
as its own. They get "needs a restart" and the reviewed job, and the next
`helm upgrade` puts back the chart's value.

Homestead is never evicted by an app: once anything ranks above the default,
Homestead puts itself at least as high as the highest app - pods of one
priority never evict each other - and restarts once to do so, waiting until
no job is running. It moves itself up if an app later ranks higher, keeps
its class through updates, and keeps any class you gave it yourself.

A tier is read from the number a class gives a pod, against the cluster's
default class (or 0 without one): above it is critical, equal normal, below
low. So a cluster's own classes - set by hand, by Flux or by a chart - show
as the tier they really are. Homestead uses those classes where they fit:
critical is the highest class above the default, low the highest below it
(preferring one that never preempts).
Only when none fits does it make `homestead-critical` or `homestead-low`,
just above or below the default (with the value in the name, such as
`homestead-critical-11000`, when the plain name is taken by an older class),
the first time a tier needs one. It never changes or removes a class, and
never makes one the cluster's default. Priority tiers says what is wrong, if
anything: a class apps name that isn't on the cluster, or an older Homestead
class apps still use.

Apps Flux or Argo CD apply show their tier read-only: set it where they're
applied. Kubernetes' own classes are left alone, and Homestead's own
tier is not offered, since it follows the apps' as above. A critical app's
suggested memory reservation has more headroom - 1.5 times its usual use -
since a host short of memory evicts pods using more than they reserve before
any within their reservation, whatever their tier; only among those does the
tier count. To remove the classes, set those apps back to Normal first
([Installing Homestead](Installing-Homestead)).

## Reviewed image updates and rollback

Image updates and rollback show the exact immutable target images and a full-pod
capacity review before changing anything. This includes matching init containers,
rollout downtime, overlapping pods and storage restrictions. High or unknown RAM
can be acknowledged; hard placement blockers cannot.

Each approval lasts ten minutes and is bound to the workload identity/version,
action, target images and recovery images. Changed registry results, an edited
workload, expired approval or newly insufficient capacity requires another review.
The server rechecks capacity and update policy before applying the change.

Staged updates run one at a time and wait for readiness, with Homestead last.
Failure, lost contact or changed rollout identity stops the remaining queue.
Closing the dialog also stops unstarted updates; submitted rollouts continue and
can be followed in Jobs. After reconnecting or self-updating, check the active job
before reviewing the remaining selection. The browser does not replay the queue.

Recovery images use each container's own observed digest, including init containers.
A stopped workload with no recorded digest resolves its old template tag at review
time; that is not claimed to be a previously running image. Legacy mutable rollback
records are refused instead of silently following a tag that may have changed.
Image rollback does not undo application data/schema changes; keep suitable backups.

API clients POST `ns`, `name`, `action` (`update` or `rollback`) and
`approved: true` to `/api/image-updates/preview`. Send the same input with
`capacity_token` and `confirm_capacity: true` to `/api/image-updates/apply`
or `/api/image-updates/rollback`. Unreviewed calls are refused.
Cancelling an image rollout from Jobs opens this same reviewed rollback flow;
it cannot bypass admission through the generic cancellation endpoint.

## Its own LAN address

A container can have an address of its own on the LAN, beside its pod
network - for an app that wants to be found there (discovery, broadcasts), or
simply to be reached on an address that is only its. In Deploy choose
**Access mode → Its own LAN address (bridged)**, or in a container's editor
choose *Its own LAN address* in the *Address* step; pick the VM network and one
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
container's editor has it under *Placement*:

| Choice | What happens |
|---|---|
| **Move to another node** | about 15 seconds later it starts on another node - for apps that should come back quickly wherever there is room. New containers start with this |
| **Wait for its node** | it stays with that node and starts again when the node is back - for apps tied to that host's hardware (a Coral, a Zigbee stick), or better restarted where they were |
| **Kubernetes default** | Kubernetes moves it after five minutes - what anything Homestead did not deploy has |

A container whose volume one node mounts at a time only really moves if
Longhorn lets go of the volume on the dead node. The dialog says whether it
will, and **Let Longhorn release them** sets Longhorn's *Pod Deletion Policy
When Node is Down* so it does; it is also under **Settings → Hardware and storage**.
Changing a container's choice restarts it.

## Architecture

**Architecture** (a tab of the **Dashboard**) draws, for every app, the path from its address through its
Service to its pods, claims, Longhorn volumes and the replicas on each disk -
the quickest way to see what a failed disk or host would touch. Virtual
machines are grouped below containers rather than mixed into them, but drawn
the same way: the ports of any Service that selects them
(such as Harvester's load balancers), their disks and those disks' replicas,
with their host and address. A stopped VM is shown faded, since its disks are
still there. Homestead's temporary browser, copy and import pods are hidden.
Each app is one card named after its Deployment, StatefulSet or DaemonSet
(a pod nothing owns is named after itself), apps of the same name in two
namespaces stay apart, and a port is drawn to the app whose pods its Service
selects - not to every app sharing an `app` label.

Pods a Job starts are not drawn as apps. Scheduled jobs (CronJobs outside the
system namespaces), such as a nightly backup per app, are listed under
**Scheduled jobs**, collapsed beneath the containers: each with its last run,
when it runs, and the volumes it uses. The count of failed last runs stays on
the collapsed heading. A schedule set to UTC shows its next run. One with no
time zone runs on the clock of the cluster's controller - on k3s often the
host's local time - so it is put in words "on the cluster's clock", with no
next run the page could get wrong. One with another time zone, or written in a
form such as `@daily` or `SUN`, is shown as written. One-off Jobs are not
shown.

Each address is marked **VIP** or **node** (a node's own address) with the
node answering for it, and an address no connection reaches is outlined in
red with the reason (see [Address map](Networking#address-map)).
Each node lists its own address and the VIPs it answers for; hovering a node
highlights its addresses, and hovering an address highlights its node.

An app that [starts after](#starts-after) others has an arrow to each of them,
pointing to the app it waits for, dashed while its start gate is not committed
yet, and says "after app-2" on its
card (on a phone, where the lines are not drawn, that is all there is).

A volume no container, VM or scheduled job references is **disconnected**. Those
retained and old volumes are hidden by default; **Show disconnected** reveals a
red group beneath the live volumes without letting it obscure the active paths.

![Architecture](https://github.com/homestead-lab/homestead/releases/latest/download/homestead-architecture.jpg)
