# Settings

| Tab | What is there |
|---|---|
| **Health** | when bars and node cards turn yellow or red; the drive health policy |
| **Updates** | the image update policy: notify only, approve each, or a maintenance window |
| **Cluster** | Add-ons - the node probe on every cluster, plus Longhorn, KubeVirt, Multus and kube-vip where the cluster lacks them; Longhorn over-provisioning, minimum free space and the V2 engine; every node's disks |
| **Hardware** | hardware features (a Coral, an iGPU, a Zigbee stick) and **Rescan hosts** |
| **Access** | your password, **Manage users**, sign out everywhere |
| **Apps** | the App Store catalogue, Portal links, UniFi, namespaces |
| **MQTT** | cluster and node stats to an MQTT broker, with Home Assistant discovery |
| **This device** | notifications on this phone or computer, installing the app |
| **About** | Homestead's version and updates, its own health, Samba, redundancy, permissions, and node-probe status |

![Settings - Cluster](https://github.com/wjcloudy/homestead/releases/latest/download/homestead-settings-cluster.jpg)

## Add-ons

On k3s and RKE2, **Settings → Cluster → Add-ons** installs what the cluster
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
  address of its own from **Networking → Your VIPs**. On k3s it runs beside
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

## Homestead updates

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

![Settings - About](https://github.com/wjcloudy/homestead/releases/latest/download/homestead-settings-health.jpg)

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
user, by time, or by any text; **Settings → Users** links each person to
their own. A failed attempt records the name tried, never the password. The
most recent 5,000 entries are kept.

## Configuration backup

**Settings → About → Configuration backup** saves Homestead's own setup to a
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
