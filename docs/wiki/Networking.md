# Networking

**Networking** shows how everything is reached: each address, the Service
behind it, the pods it leads to, and whether they answer.

![Networking](https://github.com/wjcloudy/homestead/releases/latest/download/homestead-networking.jpg)

## Services & VIPs

Every LAN address in use, with the ports on it and the app behind each. A VIP
is an address a load balancer announces on your network - kube-vip on
Harvester, or kube-vip/MetalLB on k3s - rather than a host's own address.

**Expose workload** publishes an app's port, either inside the cluster only or
on the LAN. On the LAN it takes an address:

- **Automatic** - the next free one from your VIPs, then from Harvester's IP
  pools;
- **Specific VIP** - one you choose from your list, by label;
- **Default workload VIP** - the address selected under **Workload VIPs → Make default**,
  shared on free ports. This is separate from the cluster API and can differ from
  Homestead's access address. Changing the default does not move existing Services.

Every change shows its plan and checks for clashes before anything is made.

### Nodes & addresses

One card per node: its own address, then each VIP it answers for right now,
and under every address the ports on it, the Service behind each port and
the app or VM it reaches. A VIP with no running app behind it is under
**No node** - nothing answers for it until one starts.

An address works only when both halves are there: a node answers for it on
the LAN (kube-vip adds it to one node, ServiceLB listens on every node's own
address), and the Services on it carry it - kube-proxy forwards only the
addresses a Service lists. Each address says which half is missing:

| State | Means |
|---|---|
| working | A node answers and its Services carry it |
| not reachable | A node answers - ping works - but a Service does not carry it, so its ports are refused |
| no node answers | Its apps are running but the load balancer is not announcing it |
| nothing running | Nothing behind it is running, so nothing announces it; not a fault |
| port taken | ServiceLB could not publish a Service on the nodes' addresses because another holds the port |

**Not reachable** is a kube-vip fault: with several Services sharing one
address it can announce the address and never record it on them. Homestead
checks every 30 seconds and records it itself - what kube-vip would have
written - and says so above the cards. If it stays unreachable for a minute,
you get an alert.

Node cards (Overview, Nodes) list the same addresses, and the Architecture
view marks each address as a VIP or a node's own, with the node answering for
it; hovering a node lights its addresses, and an address its node.

### Container and VM editors

Container **Edit → Service VIP → Configure default / selected VIP** opens the same
labelled address picker. Save changed container ports first. Other unsaved fields
are kept when you return; Service changes apply separately and do not restart the pod.
Select an existing Service to change its VIP in place, or create an additional one.
The review checks collisions and resource identity; changes to a Service's immutable
load-balancer class are refused rather than deleting/recreating it. Unrelated settings
and its ClusterIP are retained. Existing connections may reconnect when the VIP changes.

For a **new VM**, choose the pod network, then **Default workload VIP** or
**Selected VIP**. This is a two-step flow: create the VM, then review and publish its
port mappings. Cancelling or failing the second step leaves the created VM in place;
continue from **Edit → Network → Configure default / selected VIP** without recreating it.
The same control is available when editing an existing VM with a masquerade pod interface.

A bridged **direct LAN interface** is different: its MAC identifies the NIC, not
an IP address. Its guest address comes from DHCP (optionally a router reservation)
or configuration inside the guest. New VMs can receive static settings through cloud-init;
editing cloud-init on an existing guest does not guarantee that it will run again.

Pod-network VMs also appear in **Expose workload**. From a VM, open **Network →
Configure VIP / ports**. The guest must use masquerade networking and permit the
target ports through its firewall; a bridged VM instead uses DHCP/static addressing.
The Service follows the VM's persistent template labels when its launcher pod changes.

With per-service kube-vip elections, sharing an IP requires a common lease. Homestead
adds a per-address lease annotation on supported kube-vip releases (1.2.3+), and refuses
unsafe sharing with older/unmigrated Services or across namespaces. Use separate VIPs
for independently placed Local-traffic Services such as NFS.

## Availability is more than a VIP

Multus adds interfaces; it does not allocate or fail over Service VIPs. ServiceLB uses
node addresses; kube-vip or MetalLB advertises reserved workload addresses. Do not
assign a node IP or the control-plane VIP as a workload VIP.

A movable IP alone does not make a cluster fault tolerant. Production recovery needs
a surviving control-plane quorum (normally three k3s servers for embedded etcd),
multiple eligible workload hosts with spare capacity and matching hardware/network
interfaces, healthy replicated portable storage, and appropriate restart policies.
Single-node clusters cannot survive host failure. Test host loss and recovery before
relying on the setup; existing connections may be interrupted during failover.

See the upstream [k3s network guide](https://docs.k3s.io/networking/networking-services),
[kube-vip Service leases](https://kube-vip.io/docs/usage/services/), and
[KubeVirt Service networking](https://kubevirt.io/user-guide/network/service_objects/).

## Add, default and choose workload VIPs

Harvester announces whatever address a Service asks for, but nothing hands
addresses out. So Homestead keeps a list for itself: **Services & VIPs →
＋ Add VIP** takes one address or a range (up to 64), with a label saying what
they are for - "media apps", "DNS".

1. Choose **One VIP** or **A range of VIPs**, enter the address(es), and add an
   optional label. Check the router's DHCP range and verify that no other LAN
   device uses these addresses; Homestead only knows about cluster/IP inventory conflicts.
2. Optionally check **Make this the default workload VIP**. For a range, this
   means the first address you entered. You can also choose **Make default** on a
   saved address's card later. Existing Services do not move.
3. Choose **Use this VIP** on its card to open the workload/port form with that
   address selected. Or choose **Default workload VIP** or **Specific VIP** in
   Deploy's networking section. Specific VIP offers a labelled dropdown, including
   in **Expose workload** and VM port exposure. Review the plan before creating the Service.

Saving a VIP does not alter the router or announce the address immediately. A
workload Service must request it. MetalLB addresses must also be included in its
configured pools; adding them here does not change those pools. Multiple workloads
may share a VIP on different ports, subject to the provider's sharing constraints.
Partial range additions and default-setting errors are reported separately; addresses
already saved are retained if setting the default fails.

![Add VIPs](https://github.com/wjcloudy/homestead/releases/latest/download/homestead-vip-add.jpg)

- Keep them **outside your router's DHCP range**, or the router may hand the
  same address to a phone.
- Automatic addresses come from this list first. The **Specific VIP** picker -
  in Deploy, Edit, Import, Shares and moves - lists them by label, free ones
  first.
- Node addresses and addresses recorded as a device under IP addresses are
  refused. A VIP can be removed only while nothing uses it; choose another default
  before removing the current default.

On k3s, ServiceLB puts every Service on the hosts' own addresses, so the forms
offer **Every node's own address**. What must be free there is the port: two
Services cannot share one, and Traefik already has 80 and 443. Homestead
refuses a port that is taken before it changes anything. For VIPs as well,
add **kube-vip** under Settings → Cluster → Add-ons; see
[Installing on k3s](Installing-on-k3s#4-addresses-for-apps).

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
  leave out: Homestead installs it after it starts on a new installation, and
  an older one shows **Required components not installed** under
  **Settings → Cluster → Add-ons** and here, with **Install components**.

## IP addresses

### Choosing an app or VM address

The **Network access** editor separates the current connection from the address
you want to use. Choose **Use the default VIP**, **Choose a VIP**, or automatic
allocation, then review the address and ports before applying. Kubernetes
Service names and connection selection are under **Advanced**.

On k3s, ServiceLB can report a host's own address for a LoadBalancer Service.
The editor labels this **Node address — not a VIP** and never offers that host
address as a selectable VIP. Moving from this connection to a VIP adds a separate
Service, leaving the working node connection intact. It does not remove the old
connection automatically. A single-node cluster cannot provide host failover.

**IP addresses** documents your network, a subnet at a time: what lives at each
address, its MAC, how it gets it (static, DHCP reservation, DHCP), a category
(router, switch, access point, NAS, camera...) and notes. What the cluster uses -
nodes, VIPs, IP pools - is filled in live, named for what holds it: each node by
its name, a VIP by the apps on it, the cluster's own address by its owner. A
name you give an address is kept over these.

The subnet the cluster's nodes are on is added by itself, as **Cluster LAN**, if
no subnet covers it yet - so a new cluster's addresses are listed from the
start. Widen it, rename it or give it its DHCP range like any other.

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
