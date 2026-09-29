# Linked clusters

Link your Homestead clusters - the loft rack, the shed, a second site - and
manage all of them from any one. Only one needs to be reachable from outside:
it relays everything, consoles included, to the others.

## What you get

- **A cluster switch** at the start of the top bar. Pick a cluster and the
  whole app is that cluster's, as if you had opened it directly - your
  sign-in and your role come with you.
- **All clusters** in the same switch: Containers, Virtual Machines, Nodes and
  Volumes from every cluster on one page, each tagged with its cluster.
  Containers are grouped by cluster, with a chip to show one alone. Anything you
  do to a row - start, stop, edit, logs, a console - happens on its cluster.
- **Move to cluster** in a container's or VM's `…` menu. It opens the other
  cluster at its review of the move; nothing stops until you start it there.
- **Moving workloads** under Settings → Linked clusters: moves under way, and
  each cluster a move can come from with **Browse workloads**. See
  [Moving between clusters](Moving-between-clusters).

## Link a cluster

You need an **admin** account on the other Homestead, and both should run the
same release (or one close to it).

1. **Settings → Linked clusters → Link a cluster** (or **Link a cluster** in the
   switch, once one is linked).
2. Enter the other Homestead's address - what you open it at on your LAN, with
   the port, e.g. `http://192.168.1.250:8088` - and the admin username and
   password there.
3. Check **This Homestead's address**: how the other one reaches this one. It
   starts as this cluster's VIP on port 8088. Until Homestead is on a VIP it
   is the node's own address; when Homestead moves onto a VIP, or its VIP
   changes, the linked clusters are told the new one. An address you typed
   yourself is left as it is.
4. **Link**.

The password is used once, to sign in and exchange a shared key, and is not
kept. From then on the clusters sign every request to each other with that
key.

Link a third cluster from either of the first two: it is linked to all of
them. A Homestead already linked to others cannot be pulled into a second
group - unlink it there first, or link from within its group.

## Switching, or all together

Once a cluster is linked, its name appears at the start of the top bar (on a
phone, above the page title). Click it for:

- **All clusters** - every cluster's containers, VMs, nodes and volumes
  together;
- each cluster by name - that cluster on its own.

**Settings → Linked clusters → How they show** picks the same thing: **One
cluster at a time** or **All clusters together**. The dots in the switch say:

- a green dot: the cluster answers;
- red: it does not answer from here - it is off, or its address has changed;
- amber: it runs a release too far from this one to relay; update one of them.

Picking a cluster keeps you on the same page there. To come back, open the
switch again - it is on every cluster - and pick the one you signed in to.

If a cluster stops answering while you are on it, Homestead says so and offers
a way back.

## Updating them together

The **Homestead updates** dialog (the top bar's **Homestead** button, or
**Settings → About**) lists every linked cluster with its version and any
waiting update. Tick **Include** on each one to update with this one; see
[Settings](Settings#homestead-updates). Clusters a release or more apart can
still move workloads between them, as long as their link protocol matches.

## Roles

You keep the role you have on the cluster you signed in to. Each cluster
applies its own rules to that role, so a viewer can look but not change,
anywhere. The other cluster records what you do as `name@cluster`.

Linking itself is an admin decision: every linked Homestead can act as admin
on the others, to relay for people and to move workloads.

## Migration

Each cluster in **Settings → Linked clusters** has a **Migration** button that
says whether workloads can move from that cluster to another. It opens
**Migration from** that cluster:

- **Enable migration** runs that cluster's backup storage - an S3 store
  (RustFS) on a Longhorn volume - which a move backs volumes up to and the
  cluster they go to restores them from. The first time, choose its size;
  it goes on that cluster's shared address at port 9000 unless you choose
  another port - when an app there already uses 9000 - or give it an address
  of its own.
- While it is on, the dialog shows the store's address, whether this cluster
  can reach it, its size, and a fix for anything wrong - an address no other
  cluster can reach, say.
- **Disable migration** stops the store and keeps its volume and the backups
  on it; enabling it again starts it as it was. If that cluster's Longhorn
  backs up there too, those backups pause while it is off - the dialog says so.

You can manage migration from any linked cluster, for any of them. Below,
**Moving workloads** lists moves under way and each cluster a move can come
from, with whether it is ready and **Browse workloads**.

## Unlink

**Settings → Linked clusters** (or **Manage clusters** in the switch) lists
every linked cluster:

- **Unlink** removes one. It keeps running as it is. The rest get a new key,
  so the one unlinked can no longer act for them.
- **Leave** unlinks this Homestead from the others, which stay linked to each
  other.
- **Where the others reach this Homestead** changes this one's address - after
  a new VIP, say. The others hear straight away.

## Clusters added before linking

Clusters added on Import for moves in earlier releases - with an account and
password kept here - are listed under **Settings → Linked clusters → Added
before linking**. **Link** links each with that account, then deletes the
password; moves that named it keep working. **Forget** deletes the account and
leaves the cluster alone. One already linked under another name offers **Use
the link** instead.

## Security

- Every request between clusters is signed, carries the time, and is used
  once: it cannot be altered or replayed.
- The shared key lives in a Kubernetes Secret, `homestead-fleet-key`, in
  Homestead's namespace on each cluster.
- Requests between clusters travel over the address you give. On a LAN you do
  not trust, give each cluster an `https://` address.

## Troubleshooting

**"not answering"** - open the cluster's own address from a browser on the LAN.
If it works there, its address in **Manage clusters** may be out of date: set
it on that cluster, under **Where the others reach this Homestead**.

**"the clocks differ by more than five minutes"** - signed requests carry the
time. Set NTP on the hosts.

**A cluster that was unlinked while off** still lists the others. Unlink it
there too (**Leave**), then link it again if you want it back.
