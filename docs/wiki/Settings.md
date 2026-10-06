# Settings

Settings has eight sections: seven that change the cluster for everyone, and
**You**, which is yours and this browser's. The desktop top bar's gear opens
**You**; on a phone, open Settings from **More** or your account icon.

| Section | What is there |
|---|---|
| **Homestead** | its version and site name, its own health, how many copies run and where its data lives, configuration backup |
| **Updates** | Homestead's releases (and linked clusters' in the same update); platform versions; host OS updates and schedules; the container image update policy and its update window |
| **Monitoring** | when bars and node cards turn amber or red; drive health; publishing stats to MQTT and Home Assistant |
| **Hardware and storage** | hardware features (a Coral, an iGPU, a Zigbee stick) and **Rescan**; add-ons - the node probe, SMB and NFS shares, and Longhorn, KubeVirt, Multus and kube-vip where the cluster lacks them; Longhorn over-provisioning, minimum free space, replica rebuilds and the V2 engine; storage classes; namespaces |
| **Linked clusters** | other Homesteads managed from this one, and moving workloads between them |
| **Connections** | UniFi Network, the App Store catalogue |
| **Users and access** | users and their roles, and [API keys](API) |
| **You** | appearance, notifications on this device, refresh rate, your password and signing out |

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

## Homestead's own health

**About** shows whether the parts of Homestead that work in the background are
working, refreshed every 15 seconds:

![Settings - About](https://github.com/homestead-lab/homestead/releases/latest/download/homestead-settings-health.jpg)

- how fast the Kubernetes API answers;
- each copy of Homestead, and which one leads;
- each background task - live charts, alerts, long-term stats, hardware
  detection, moves - with when it last worked and its last error;
- the node probe: how many hosts run it, report, and have drive health;
- **Samba** - running, and on which address. Switch it off here (shares stop
  being served; volumes, settings and passwords are kept) and on again, which
  installs it if needed;
- the permissions check, backup storage and MQTT.

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

On the development branch, after a data move succeeds or recovery verifies
Homestead back on its original volume, **Jobs → Dismiss** or **Clear finished**
can hide its completed entry.
Both data volumes, the saved log and required recovery records are retained.
A move that still needs recovery cannot be cleared. See
[storage move recovery](Storage#when-a-storage-move-needs-review).

## MQTT and Home Assistant

**MQTT** publishes cluster and node stats to a broker, with Home Assistant
discovery: nodes ready, volumes degraded, pods, VMs, CPU and memory per node.
The entity ids stay the same from release to release, so Home Assistant keeps
its entities and history. **Test connection** checks the broker.

## Notifications

Opened over HTTPS, Homestead can be installed as an app and send push
notifications - outages, degraded storage and workloads, failed jobs, hosts
joining, image updates - even while closed. **This device → Turn on
notifications**. Pushes carry nothing: the app fetches what happened over its own
signed-in connection. On iPhone, add Homestead to the Home Screen first.

Browsers allow neither on a plain-HTTP address; see
[Installing Homestead](Installing-Homestead#reaching-it-from-outside).
