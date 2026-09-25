# Settings

| Tab | What is there |
|---|---|
| **Health** | when bars and node cards turn yellow or red; the drive health policy |
| **Updates** | the image update policy: notify only, approve each, or a maintenance window |
| **Cluster** | Add-ons - Longhorn and KubeVirt installed where the cluster lacks them; Longhorn over-provisioning, minimum free space and the V2 engine; every node's disks |
| **Hardware** | hardware features (a Coral, an iGPU, a Zigbee stick) and **Rescan hosts** |
| **Access** | your password, **Manage users**, sign out everywhere |
| **Apps** | the App Store catalogue, Portal links, UniFi, namespaces |
| **MQTT** | cluster and node stats to an MQTT broker, with Home Assistant discovery |
| **This device** | notifications on this phone or computer, installing the app |
| **About** | Homestead's own health, Samba, redundancy, permissions, the node probe |

![Settings - Cluster](https://github.com/wjcloudy/homestead/releases/latest/download/homestead-settings-cluster.png)

## Add-ons

On k3s and RKE2, **Settings → Cluster → Add-ons** installs what the cluster
lacks, through the Helm controller both distributions run - so each is an
ordinary HelmChart afterwards, on the Helm page:

- **Longhorn** - volumes, snapshots and backups. It keeps one copy of each
  volume per node, up to three. Each node needs open-iscsi and an NFS client
  first (the k3s script installs them).
- **KubeVirt** - virtual machines, with CDI to fill their disks from images:
  the newest release of each. The card says which nodes have hardware
  virtualisation; with none, KubeVirt emulates, and VMs run slowly.

A page that needs one offers the same install. Harvester has both built in,
so the card does not show there.

## Homestead's own health

**About** shows whether the parts of Homestead that work in the background are
working, refreshed every 15 seconds:

![Settings - About](https://github.com/wjcloudy/homestead/releases/latest/download/homestead-settings-health.png)

- how fast the Kubernetes API answers;
- each copy of Homestead, and which one leads;
- each background task - live charts, alerts, long-term stats, hardware
  detection, moves - with when it last worked and its last error;
- the node probe: how many hosts run it, report, and have drive health;
- **Samba** - running, and on which address. Switch it off here (shares stop
  being served; volumes, settings and passwords are kept) and on again, which
  installs it if needed;
- the permissions check, backup storage and MQTT.

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
The topics and entity ids match the older hv-exporter, so Home Assistant keeps
its entities and history. **Test connection** checks the broker.

## Notifications

Opened over HTTPS, Homestead can be installed as an app and send push
notifications - outages, degraded storage and workloads, failed jobs, hosts
joining, image updates - even while closed. **This device → Turn on
notifications**. Pushes carry nothing: the app fetches what happened over its own
signed-in connection. On iPhone, add Homestead to the Home Screen first.

Browsers allow neither on a plain-HTTP address; see
[Installing Homestead](Installing-Homestead#reaching-it-from-outside).
