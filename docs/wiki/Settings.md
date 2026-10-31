# Settings

Settings has ten sections: nine that change the cluster for everyone, and
**You**, which is yours and this browser's. The desktop top bar's gear opens
**You**; on a phone, open Settings from **More** or your account icon.

| Section | What is there |
|---|---|
| **Homestead** | its version and site name, its own health, how many copies run and where its data lives, configuration backup |
| **Updates** | Homestead's releases (and linked clusters' in the same update); platform versions; host OS updates and schedules; the container image update policy and its update window |
| **Placement** | How hosts share the work - Kubernetes' default, spread by capacity, or a hot spare; whether the other hosts have room if one fails; and whether containers move back when a host returns |
| **Monitoring** | when bars and node cards turn amber or red; drive health |
| **Hardware and storage** | hardware features (a Coral, an iGPU, a Zigbee stick) and **Rescan**; add-ons - the node probe, SMB and NFS shares, and Longhorn, KubeVirt, Multus and kube-vip where the cluster lacks them; Longhorn over-provisioning, minimum free space, replica rebuilds and the V2 engine; storage classes; namespaces |
| **Linked clusters** | other Homesteads managed from this one, and moving workloads between them |
| **Connections** | UniFi Network; MQTT and Home Assistant - stats, and power control for the containers and VMs you pick; the alert webhook (alerts to ntfy, Gotify or any webhook, see [Alerts and PWA notifications](https://github.com/homestead-lab/homestead/blob/main/docs/notifications.md#alert-webhook)); the App Store catalogue |
| **Users and access** | users and their roles, and [API keys](API) |
| **Troubleshooting** | bug reports and logs |
| **You** | appearance, notifications on this device, your summary push, refresh rate, your password and signing out |

A section saves once: change what you like and a bar at its foot offers
**Save** and **Discard**. Leaving the section or the page with changes not
saved asks first. Settings with many fields - MQTT, UniFi, the update window,
the App Store catalogue - open in a dialog of their own.

![Settings - Cluster](https://github.com/homestead-lab/homestead/releases/latest/download/homestead-settings-cluster.jpg)

## Refresh on a phone

The top bar's refresh button updates the current page without reloading the
app. In an installed app, pull down at the top of the page until **Release to
refresh** appears, then let go. Ordinary browser tabs keep their browser's
pull-to-refresh. Tables with their own scrolling, editors and consoles keep
their gestures. App refresh pauses while a dialog or form page is open, or
settings have changes not saved.

For a full reload, choose **More → Reload app**. Unsaved settings, open dialogs
and form pages ask before reloading; a full reload disconnects open consoles.

## Setup guide

Open the guide from the book icon in the top bar or **Settings → Homestead**,
including after setup is complete. Each step explains its check. **Configuration
found** means settings or resources exist; it does not confirm successful
backups, imports or connection health. Appearance requires your confirmation.
**Next** moves through the guide without completing a step. When you visit a
configuration page, **Return to setup** brings you back to the current step.

The book icon stays available on real clusters and in the demo. It pulses until
you choose **Finish guide** or **Don’t show again**. These choices are saved for
your account and do not change the checks. **Show reminders again** starts the
reminder again. The guide has no dashboard shortcut or node-probe step: Homestead
installs the node probe automatically, unless installation was explicitly declined.

**Access → HTTPS and remote access → Set up a Cloudflare Tunnel** opens six
steps: account and domain, tunnel token, connector deployment, access policy,
hostname route and browser testing. A Cloudflare account and a domain on
Cloudflare are required. The Free plan is sufficient; domain registration and
renewal are separate costs. The guide links to Cloudflare's
[domain setup](https://developers.cloudflare.com/dns/zone-setups/full-setup/setup/),
[tunnel instructions](https://developers.cloudflare.com/tunnel/get-started/)
and [Access instructions](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/self-hosted-public-app/).

The connector's route uses Homestead's cluster Service DNS name and Service
port. Configure Access for specific users before publishing the hostname.
Open the HTTPS address and sign in to test an Access-protected route; the
server's address check cannot sign in through Cloudflare Access. Only the
guide position is remembered in the browser tab, not account details or tokens.

## Add-ons

On k3s and RKE2, **Settings → Hardware and storage → Add-ons** installs what the cluster
lacks, through the Helm controller both distributions run - so each is an
ordinary HelmChart afterwards, on the Helm page. The node probe is offered here
on Harvester too:

- **Node probe** - host hardware inventory, `/dev/kvm`, temperatures, physical
  disks, SMART health and per-disk throughput. Its row also removes the probe
  when it is no longer wanted.
- **Longhorn** - volumes, snapshots and backups. It keeps one copy of each
  volume per node, up to three. Each node needs open-iscsi and an NFS client
  first (the k3s script installs them).
- **KubeVirt** - virtual machines, with CDI to fill their disks from images:
  the newest release of each. The card says which nodes have hardware
  virtualisation. A new install allows software fallback unless every probed
  node exposes `/dev/kvm`; an existing install offers **Allow software
  fallback** when VMs otherwise cannot be scheduled. Emulated VMs run slowly.
- **Multus** - second networks for pods, which
  [LAN networks](Networking#lan-networks) need, so a container or VM can
  have an address of its own on your LAN. It comes from the `rke2-multus`
  chart RKE2 uses, set up with k3s's own CNI folders on k3s. Apps already
  running are left as they are. While installation is still pending, its row
  shows one pasteable SSH command for the HelmChart, installer pod and job log,
  using the correct kubectl and kubeconfig paths for k3s or RKE2.

- **kube-vip** - VIPs for apps, as Harvester has: a container can have a LAN
  address of its own from **Networking → Workload VIPs**. On k3s it runs beside
  ServiceLB and takes only the Services given a VIP. It announces addresses
  on the interface each node's default route uses.

kube-vip and Multus are required components on k3s and RKE2: a new
installation gets both after Homestead starts, at tested chart versions
(kube-vip 0.11.1, which is kube-vip v1.2.3; Multus v4.3.102). When either is
missing, **Required components not installed** at the top of Add-ons, and on
Networking, installs it. Their chart versions are listed and upgraded under
**System → Cluster → Platform versions**.

A page or form that needs one offers the same install. Harvester has the
storage, VM and network add-ons built in, so its card shows only the node probe.

## Host updates

**Settings → Updates → Host updates** lists each host's pending packages,
security updates, restart needs and automatic-update status, with progress
for a running update. **Manage host updates** opens the same controls as
**Nodes → OS updates**: choose who installs updates, set the weekly window,
configure restarts, or review an update of every host one at a time. Only
administrators can change these settings or start an update. Opening Settings
or refreshing its status installs nothing.

On Harvester, the card links to Cluster because the hosts' operating system
is upgraded with Harvester.

## Homestead updates

**Release channel** chooses **Prod · stable** (the default) or **Dev · preview**
for this cluster. Admins can change it; the choice is saved immediately and
checks for that channel's newest release in the current major version.
Changing channel installs nothing: review and accept the offered release
under the normal update policy. Returning to Prod can offer an older stable
version. Dev uses numbered prereleases such as `2.8.291-dev.1`; app and helper
images keep their own update policies. Each linked cluster keeps its own
channel, so set it on that cluster before including it in a fleet update.

Homestead's own release, and the helpers it runs beside it - the SMB and NFS
servers and the object store moves use - are updated apart from your apps.
When one is waiting, a **Homestead** button with the new version appears on
the top bar; it and **About › Homestead updates** show the release you run,
the one on offer with a link to what's new, and each helper's image. **Update**
opens the same reviewed rollout an app gets, helpers first and Homestead last;
the page reconnects when Homestead is back. **Check now** asks the registries
again. How updates are approved is set under **Updates**.

**Linked clusters** are listed below this one's parts, each with the version
it runs and whether an update is waiting, asked through this Homestead's
relay. Tick **Include** on the ones to update as well: the same review then
covers them, each rollout checked and applied on its own cluster, the linked
clusters first and this Homestead last, since the others are reached through
it. A Homestead restarting mid-update is waited for rather than counted as a
failure. Nothing is ticked by default. Keeping every linked Homestead on one
release keeps moves between them working. A cluster that is not answering is
shown, not offered; one older than 2.8.220 is recognised by its `homestead`
Deployment.

A notification about a new Homestead release links here: `/settings?tab=about`
opens this tab, as `?tab=` does for any of them.

## How hosts share the work

**Placement → How hosts share the work** is one choice for the whole cluster,
offered in the setup guide once two hosts take work. A host with a hard taint
of its own - a tiebreaker kept only for etcd votes, say - takes no apps and is
not counted:

- **Kubernetes default** - apps are placed as Kubernetes places them; placement
  is set app by app, in Edit.
- **Spread by capacity** - each app goes to the host with most room left, so a
  32 GB host carries about twice a 16 GB one. The scheduler already does this,
  but by what apps *reserve*: an app that reserves nothing is placed as if it
  used nothing. The setting says how many apps reserve nothing and links to
  **Set reservations from use**. Choosing it changes no app.
- **Hot spare** - the standby hosts you tick take apps only when the others
  cannot. Homestead gives each the taint `homestead.io/standby=true:PreferNoSchedule`:
  the scheduler avoids it while another host has room and uses it when none
  has. No app changes, so it works for apps Flux, Argo CD or Helm apply.
  Homestead keeps the taints in step with the setting every minute: it adds
  its own and removes any taint with the key `homestead.io/standby`, leaves
  every other taint alone, and never makes standby the last host taking work.
  Balance hosts always moves apps off standby hosts first - as far as the
  other hosts stay under 90% full - and never onto them, and weighs how even
  the main hosts are without the standby ones. Ticking a host moves nothing
  by itself: apps already on it stay until Balance hosts moves them, which the
  setting offers. A host that refuses the taint is shown as not marked yet,
  with why, and tried again every minute; a standby host coming back is no
  reason to move anything, since nothing moves onto it.

The taint is soft on purpose: a hard `NoSchedule` would also keep DaemonSets -
Longhorn's, Homestead's probe - from rolling out on that host. Choosing either
of the last two turns on **Suggest** under *When a host comes back* if it was
off, visibly, since apps that moved while a host was away only come back that way.

## If a host fails

**Placement → If a host fails** takes each host in turn as failed and says
whether the others have room for what it runs - for example *If node2
fails, node1 would have 13.7 of 15 GiB of memory reserved; 3 low-tier apps
would wait for room or be evicted.* A host whose CPU runs out first is
described by its CPU instead. Each host is marked **fits**, **tight** (a host
that takes some of the apps left 90% full or more), **apps wait**, **no host
left**, or **unknown** when its preview could not be worked out. **Details** opens
that host's *If this host goes down* preview, which shows the same forecast
under **Room on the other hosts**: each remaining host's memory reserved now
and after the move, and what would wait.

The apps and VMs that would move are placed - not those that stop anyway
(pinned, on host-local storage, or using hardware only that host has), nor
those set to wait for their host to come back. A StatefulSet's pod needs room
only when the preview says it moves - when Longhorn's **Pods on a failed
node** setting deletes it; one that waits until its pod is deleted, or
couldn't be checked, needs none, and the sentence says how many wait and how many
couldn't be checked: *1 app waits until its pod is deleted and 1 app couldn't
be checked; If this host goes down says why.* When StatefulSets couldn't be
read, the sentence ends *StatefulSets couldn't be read, so they aren't
counted.* Pods the
preview does not list - a platform app's - are counted as needing room on a
host their own rules and its taints allow, should they be started elsewhere,
except a StatefulSet's. A bare pod no controller owns does not move. They are
placed the way the scheduler would: critical first,
then normal, then low, largest first, each on an allowed host with room for it
beside what is already there - the one with the most room left. Only when no
host has room does an app evict lower tiers: a critical app may evict normal
and low ones, a normal app low ones, on the host where the fewest apps must
go; low apps wait and evict nothing. Only what apps *reserve* counts, so an
app that reserves nothing fits anywhere on paper: the card and the dialog say
how many of those there are, since the figures are then a floor. Counts are of
apps, not of their copies.
Apps already Pending, with no host, are counted apart: they wait whichever
host fails. It is a forecast, not the scheduler - it does not weigh affinity
between apps or the order pods restart in. The card is worked out when
Placement is opened, and at most once a minute.

## When a host comes back

While a host is down or drained, its containers start on the others, and
Kubernetes never moves them back. **Placement → When a host comes back** says
what Homestead does once the host takes work again - Ready, and not cordoned,
after it has been seen working before (a new host finishing joining is not
"back"):

- **No** - nothing. **Balance hosts** on Nodes, or **Rebalance containers…** in
  Containers' menu, moves them when you choose.
- **Suggest** - an alert under *Hosts joining* (a notification category on by
  default) says the host is back and how many containers the rebalance would
  move. Nothing moves until someone reviews it in Balance hosts.
- **In the update window** - the same alert, then the rebalance runs by itself
  in each opening of the update window, as the job a person would start,
  titled *Rebalance N containers after host came back*. It does not start while
  another job moves or restarts apps or changes hosts - an OS rollout or a
  host's update, host power or a k3s change, a rebalance, a manual move, an
  automatic or manual image update, reservations being set, a volume restore
  or storage-class move, a VM migration, among others - and neither an
  automatic update nor a scheduled OS rollout starts while it runs. The check
  and the start are one step, so two that become due the same minute do not
  both start. The window is used whatever the image update policy, notify
  only included. The run can go on after the window closes: each move waits up
  to ten minutes for its container to start. Apps Flux or Argo CD apply are
  left out of it, since their next sync could move them back; move those by
  hand, or in Git.

In the window, an app is left where it is when a scheduled job (a CronJob)
mounts its volume and is running, or runs while the rebalance may still be
going - before the window closes, or within ten minutes a move for each
container it would move - or has a schedule Homestead cannot read: the job has
to run on the app's host while the volume is attached there. A CronJob without
a time zone of its own runs on its controller's host clock - on k3s often
local time - so Homestead allows for every clock its last run is consistent
with, an hour either way in case the clocks have changed since. An app left
where it is moves at the next opening.

Only moves worth a restart are made, with the same checks as Balance hosts. A
returned host is remembered until the rebalance finds nothing worth moving,
and for a week at most; the alert clears then.

## Homestead's own health

**About** shows whether the parts of Homestead that work in the background are
working, refreshed every 15 seconds:

![Settings - About](https://github.com/homestead-lab/homestead/releases/latest/download/homestead-settings-health.jpg)

- how fast the Kubernetes API answers;
- each copy of Homestead, and which one leads;
- each background task - live charts, alerts, long-term stats, app usage
  history, rebalance after a host returns, hardware detection, moves - with
  when it last worked and its last error;
- the node probe: how many hosts run it, report, and have drive health;
- **Samba** - running, and on which address. Switch it off here (shares stop
  being served; volumes, settings and passwords are kept) and on again, which
  installs it if needed;
- the permissions check, backup storage and MQTT;
- **Metrics for Prometheus** - off until an admin turns it on here. On,
  `/metrics` gives the same health in the Prometheus text format to anyone with
  an API key, or signed in. See
  [Monitoring Homestead itself](Troubleshooting#monitoring-homestead-itself).

## Homestead's data

**Settings › About › Homestead's data** shows what Homestead keeps on its own
volume - monitoring and stats history, change history, the storage forecast,
app usage, hosts coming back, jobs, logs, logos and so on - each store's size and how long it keeps things,
and how full the volume is. Each store trims itself; once a day Homestead also
trims its logs to a year (and 5,000 entries), removes logos nothing has used for
30 days, and removes leftovers of interrupted writes. Past 80% full it keeps half
as much of its logs, removes unused logos at once and raises an alert naming the
largest stores. **Clean up now** does the daily tidy at once (admin). Homestead
writes a file only when what it holds has changed.

## Namespaces

**Settings › Hardware and storage › Namespaces** lists the namespaces your apps
can go in - Harvester's, Rancher's and Kubernetes' own are hidden - with what each
holds. **Create** makes one, and Deploy offers it at once; **Delete** removes an
empty one: no apps, stateful sets, daemon sets, scheduled jobs, running pods,
volumes, VMs or VM disks. If Homestead can't read one of those just then, it
says so and doesn't offer Delete, rather than taking "couldn't read" for "none".

One is the **default for new apps**: `lab`, unless another was chosen when
Homestead was installed (`workloadNamespace.name` in the Helm chart, `DEFAULT_NS`
in the manifest). Deploy starts with it selected. Homestead also keeps its own
settings, the network shares and the node probe there, so the default is not
changed from the page: moving those on a running cluster would leave them behind.
To keep your apps somewhere else, create a namespace and pick it in Deploy.

Three are marked **Kept** and cannot be deleted from here: the default (new apps
go there, and so does Homestead's own state), the namespace Homestead itself runs
in, and Kubernetes' own `default`.

## Audit log

Every request that changes something - a scale, a delete, a new user, a linked
cluster, an AI agent's change - writes one line: who, how they came in (browser,
API key, MCP, a linked Homestead), what, the target, the result and how long it
took. Opening a console or a host shell writes one line when it ends.

```json
{"v":1,"at":"2026-10-21T09:14:03.512Z","replica":"homestead-6d9f-abcde","via":"browser","user":"ada","role":"operator","addr":"192.0.2.10","method":"POST","path":"/api/workload/scale","target":{"ns":"lab","kind":"Deployment","name":"app-1"},"status":200,"ms":142}
```

- **What Homestead deletes on its own** writes a line too, with `"via":"leader"`,
  `"user":"homestead"` and an `event`, but no `addr`, `target`, `status` or `ms`:
  `outage-secret-swept` when the Secret of an outage webhook is deleted because
  its app or VM was deleted outside Homestead an hour before (`"for"` names it,
  `"detail"` says for how long it was missing).
- **The pod log always has every line**, for Loki, Vector or anything else that
  reads pod logs.
- **`audit.jsonl` on Homestead's data volume** keeps them across restarts: two
  files of 10 MiB at most (about 60,000 lines). **Settings › About › Audit log**
  turns that off (admin); the pod log keeps writing.
- So that a flood of refused requests cannot push real changes out of those two
  files, the file keeps every success and every real server error, but limits
  refusals (4xx, a busy server's 503 to someone not signed in, and an AI
  agent's refused tool call). Each minute it keeps the first refusal from each
  person, or from each address for requests with no user (an IPv6 address
  counts by its /64), up to 50 such firsts; then at most 5 more from each, and
  120 in all (one ceiling for requests with no user, another for signed-in
  people). One line says how many it left out. Those lines are written when the
  minute is over, on the next request, so a pod that stops first loses them.
- With two copies of Homestead on a shared (ReadWriteMany, NFS) data volume,
  lines appended by both at the same moment can overwrite each other in
  `audit.jsonl`; the pod logs keep them all.
- If `audit.jsonl` fills 10 MiB in under an hour, something may be flooding
  Homestead with requests: the bell says so ("The audit log is filling fast"),
  and the pod log has a line about it too.
- If the volume is full, a line that could not be written is counted and the
  next line that fits says how many were lost; a line cut short is followed by
  an `audit-line-truncated` line, so a reader can skip it. Nothing already
  written is ever cut back.
- An AI agent's refused call with a bad key, or for a tool that does not
  exist, has no line here; the agent audit records it.
- Never written: passwords, setup codes, API keys, cookies, request bodies
  (only the target's namespace, kind and name) and query strings.
- Sign-ins are in the [sign-in history](#sign-in-history), and what AI agents
  asked for, with their arguments, in the agent audit.

Admins read it under **Events → Audit**, together with the agent audit (every
call an API key made, on `/api/v1` and `/mcp`): one list, newest first, each
row saying which log it came from, who, what and how it ended. Filter it by log
or by person; **Older** and **Newer** move 100 rows at a time. The two files
stay apart: an agent's arguments are shown only under **Settings → AI agent requests**.
Each page reads the logs from their newest end, so a full log stays quick; when
a filter matches little, only the newest 20,000 lines of each log are searched,
and the page says so. A change an AI agent made through `/mcp` appears in both
logs, once as the request and once as the agent's call.

## Sign-in history

Every sign-in to Homestead, failed attempt, sign-out, password change and
change to an account is kept, with the address and device it came from.
Admins read it under **Events → Sign-ins**, filtered by what happened, by
user, by time, or by any text; **Settings → Users and access** links each person to
their own. A failed attempt records the name tried, never the password. The
most recent 5,000 entries are kept.

## Configuration backup

**Settings → Homestead → Configuration backup** saves Homestead's own setup to a
file on your device: settings, users and roles, hardware features, VIPs, IP
addresses, MQTT, Portal, network shares and SMB users, import sources and the
VM image store. Pick which parts to include.

The file holds password hashes and keys, so it is always encrypted with a
passphrase you choose - at least 8 characters - which Homestead does not keep.
Without it the file cannot be read, and any change to the file is noticed.

**Restore** reads a backup with its passphrase and lists each part it holds,
saying whether it is the same as now or differs. Tick the parts to bring back;
the rest stay as they are, and nothing is deleted. **Users and roles** is left
unticked unless you choose it: restoring it replaces every account and
password with the backup's and signs everyone out.

Not in a backup: workloads and their data (volume backups under Data
protection hold those), and linked clusters - link them again after
restoring onto a new install.

## Redundancy

**About → Redundancy** runs one to three copies of Homestead. With two or more,
on different hosts, a host failing leaves another copy already answering, and
updates roll one copy at a time.

More than one copy needs Homestead's data on a volume every host can mount - a
shareable (RWX) Longhorn class. On a migratable class (Harvester's default)
Redundancy says so and offers **Move data**, which copies it to a shareable class
and restarts Homestead once onto it.

Homestead's data can also have more than one copy itself. On a one-host
Longhorn cluster it keeps one. Once two hosts Longhorn stores on have room for
another, Redundancy (and Cluster Health) says "Homestead's data has one copy;
this cluster can now keep two" and offers **Keep two copies**: it makes
`longhorn-r2` - two copies on different hosts, not migratable, kept when
deleted - if the cluster has none, and opens **Move data** with it picked. You
still prepare and confirm the move; Homestead stops briefly while its data is
copied. An existing `longhorn-r2` that keeps fewer than two copies, is
migratable, lets copies share a host, keeps them only on tagged hosts or is
not Longhorn's is not used, and Redundancy says why. When its data is already
on such a two-copy class and only kept one copy on a one-host cluster,
**Keep two copies** raises it to two in place instead: no move, no outage. On
k3s and RKE2 Homestead raises its data's copies in place as hosts join, so
nothing is offered there. If the copies cannot be read, Redundancy and
Cluster Health say so, with the reason.

On the development branch, after a data move succeeds or recovery verifies
Homestead back on its original volume, **Jobs → Dismiss** or **Clear finished**
can hide its completed entry.
Both data volumes, the saved log and required recovery records are retained.
A move that still needs recovery cannot be cleared. See
[storage move recovery](Storage#when-a-storage-move-needs-review).

## MQTT and Home Assistant

Under **Connections**, **MQTT** publishes cluster and node stats to a broker,
with Home Assistant discovery: nodes ready, volumes degraded, pods, VMs, CPU and
memory per node. The entity ids stay the same from release to release, so Home
Assistant keeps its entities and history. **Test connection** checks the broker.

### Power control

Once a broker is set up, an admin can **Turn on** power control and pick the
containers and VMs Home Assistant may control. Each one becomes a device in
Home Assistant, under the cluster's:

| Entity | Container | VM |
|---|---|---|
| **Power** switch | starts it, or stops it (scales it to 0) | starts it, or shuts it down |
| **Restart** button | restarts its pods | restarts it |
| **Force off** button | - | turns it off at once, like pulling the plug |
| **Reset** button | - | resets it at once, without shutting down |

Homestead listens on `<base>/control/<app or vm>/<namespace>/<name>/<power,
restart, force_off or reset>` and publishes each one's state, ON or OFF, to
`.../state`. Only what is picked is listened to. Homestead's own containers
and those in system namespaces cannot be picked.

A command goes through the same checks as the app's buttons, made as an
operator would make it: a VM that would not fit is not started. At most one
command every 10 seconds reaches one item, and 20 a minute in all; a retained
command message is never acted on. Each command, carried out or refused, is
listed under **AI agents** > **Recent calls** as Home Assistant (MQTT).

Anything that can publish to the broker can send these commands. Give the
broker a username and password, TLS, and an ACL so only Home Assistant can
publish to `<base>/control/#`. Homestead warns on the card until it signs in
over TLS. **Turn off** removes the switches and buttons from Home Assistant
and stops listening, and keeps what was picked for next time. The same
actions are on the [API](API) for scripts.

With [linked clusters](Linked-clusters), each Homestead has its own
MQTT settings and controls only its own containers and VMs: picking a cluster
from the top bar shows that cluster's MQTT card and its own list. If two
clusters use the same broker, give each its own base topic; the base topic is
then part of each power control entity's id, so they don't overwrite each
other in Home Assistant.

## Notifications

Opened over HTTPS, Homestead can be installed as an app and send push
notifications - outages, degraded storage and workloads, failed jobs, hosts
joining, image updates - even while closed. **This device → Turn on
notifications**. Pushes carry nothing: the app fetches what happened over its own
signed-in connection. On iPhone, add Homestead to the Home Screen first.

A push tells you when something starts. To hear how a quiet day or week went
as well, set **You → Summary push**: Off (the default), Daily, or Weekly on a
day you choose, at an hour of your time zone. At that time each of your
devices with notifications on gets one push counting what the Timeline
recorded, such as "Past week: 3 changes, 1 restart, nothing failed", and
tapping it opens the Timeline from there. It is yours alone: everyone sets
their own, and it counts what your role can see. How it is sent:
[Summary push](https://github.com/homestead-lab/homestead/blob/main/docs/notifications.md#summary-push).

To get the same alerts in ntfy, Gotify or any webhook, an admin sets
**Connections → Alert webhook**: its format, the address, which alerts
(critical only, warnings and critical, or everything) and, for a self-hosted
service on the home network, **Allow a LAN address**.

Browsers allow neither on a plain-HTTP address; see
[Installing Homestead](Installing-Homestead#reaching-it-from-outside).
