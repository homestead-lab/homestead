# Networking

**Networking** shows how everything is reached: each address, the Service
behind it, the pods it leads to, and whether they answer.

![Networking](https://github.com/wjcloudy/homestead/releases/latest/download/homestead-networking.png)

## Services & VIPs

Every LAN address in use, with the ports on it and the app behind each. A VIP
is an address a load balancer announces on your network - kube-vip on
Harvester, MetalLB elsewhere - rather than a host's own address.

**Expose workload** publishes an app's port, either inside the cluster only or
on the LAN. On the LAN it takes an address:

- **Automatic** - the next free one from your VIPs, then from Harvester's IP
  pools;
- **Specific VIP** - one you choose from your list, by label;
- **Shared** - an address another app uses, on a port it does not.

Every change shows its plan and checks for clashes before anything is made.

## Your VIPs

Harvester announces whatever address a Service asks for, but nothing hands
addresses out. So Homestead keeps a list for itself: **Services & VIPs →
＋ Add VIPs** takes one address or a range (up to 64), with a label saying what
they are for - "media apps", "DNS".

![Add VIPs](https://github.com/wjcloudy/homestead/releases/latest/download/homestead-vip-add.png)

- Keep them **outside your router's DHCP range**, or the router may hand the
  same address to a phone.
- Automatic addresses come from this list first. The **Specific VIP** picker -
  in Deploy, Edit, Import, Shares and moves - lists them by label, free ones
  first.
- Node addresses and addresses recorded as a device under IP addresses are
  refused. A VIP can be removed only while nothing uses it.

On k3s with ServiceLB, every Service uses the hosts' own addresses, so VIPs do
not apply; see [Installing on k3s](Installing-on-k3s#4-addresses-for-apps).
The forms there offer just **Every node's own address**. What must be free is
the port: two Services cannot share one, and Traefik already has 80 and 443.
Homestead refuses a port that is taken before it changes anything.

## LAN networks

A LAN network puts a VM, or a container given an address of its own, on your
LAN like any other machine. **＋ LAN network** makes one:

- **On Harvester**, on one of its cluster networks - `mgmt` is the hosts' own -
  untagged, or on a VLAN. It is the same object Harvester's dashboard makes, so
  it shows there too.
- **On k3s, RKE2 and other clusters**, on a host interface, which the node
  probe lists. A **bridge** (`br0`) carries VMs and containers. A plain **NIC**
  (`eth0`) carries containers only, through macvlan, each with a MAC address of
  its own. A VM needs a bridge. On a VLAN, a NIC needs the host's VLAN
  interface (`eth0.20`) first. These networks need Multus, which k3s and RKE2
  leave out. The form says how to add it when it is missing.

## IP addresses

**IP addresses** documents your network, a subnet at a time: what lives at each
address, its MAC, how it gets it (static, DHCP reservation, DHCP), a category
(router, switch, access point, NAS, camera...) and notes. What the cluster uses -
nodes, VIPs, IP pools - is filled in live.

- Each subnet shows its DHCP range, what is used, and the next addresses free
  for static use. A static address or VIP inside the DHCP range is flagged.
- **Scan now** probes the subnet from inside the cluster and flags hosts that
  answer but are not documented.
- **Export CSV** and **Import CSV** round-trip a spreadsheet.
- **UniFi** (optional, **Settings → Apps → UniFi Network**) brings in what a UniFi
  controller knows: clients, devices, reservations and networks, read-only.

## Portal

**Portal** is a page of links to every web interface - your apps (picked from
their exposed ports, with their logos) and the router, switches and NAS around
them - each with a dot saying whether it answers right now.
