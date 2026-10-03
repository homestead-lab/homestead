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

kube-vip can also stop answering for a shared address altogether: when one
Service on it loses its pods for a moment - an update, a restart - kube-vip
gives up the address's lease and does not take it again once they are back,
and every Service on that address is unreachable. When such a lease has had
no holder for a minute while a Service on it has pods ready, Homestead
restarts kube-vip, which elects again as it starts - at most once every ten
minutes - and logs it.

On k3s and RKE2, Homestead runs the kube-vip it installed with a single
leader for every VIP (global election, the `plndr-svcs-lock` lease), the
network interface named where every node's default route agrees, and the
NET_ADMIN and NET_RAW capabilities. With per-Service election, a VIP that
two Services asked for could stay `<pending>` after a reboot. Existing
installs are moved to these settings in kube-vip's HelmChart, so they survive
reboots and upgrades. A cluster with a Service using Local traffic, such as
an NFS share, keeps per-Service election, which that Service needs for its
VIP to follow its pod. Values you wrote yourself are left alone.

A LoadBalancer Service Homestead made is removed once its workload is gone,
so it stops asking for its VIP. This applies after ten minutes, and only when
nothing in its namespace (Deployment, StatefulSet, DaemonSet, VM or pod)
could still use it. A workload stopped at zero keeps its Service.

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

### Homestead itself on a VIP

Homestead's page (port 8088; it listens on 8080 inside its container), its
backup storage and its SMB shares are services too. On Harvester they start on
a VIP. On a new k3s or RKE2 cluster they start on the nodes' own addresses,
which stop answering when that node is down. The **Homestead itself** card
above the VIPs shows where each answers. **Put Homestead on a VIP** gives each
a second connection on the VIP with the same ports (`homestead-vip`,
`homestead-objectstore-vip`, `homestead-smb-vip`). Their node addresses keep
working and nothing restarts. Longhorn's backup target moves to the backup
storage's VIP address. **＋ Add VIP** offers the same as **Put Homestead itself
here** while Homestead has no VIP. NFS always has a VIP of its own already.

The installer asks for this address when it makes a new k3s or RKE2 cluster.
Homestead then reserves it, makes it the apps' default, and puts itself on it
once kube-vip is running.

### Changing a VIP's address

**Change address** on a VIP's card gives it a new address and moves every
Service on it with it: apps, and Homestead's own if they are there. The review
lists what moves before anything changes. Each Service keeps its name, ports
and type; only its address changes, so open connections drop for a moment. The
VIP keeps its label and stays the default if it was. A new address that is a
node's, or that a Service already uses, is refused.

On k3s, ServiceLB puts every Service on the hosts' own addresses, so the forms
offer **Every node's own address**. What must be free there is the port: two
Services cannot share one, and Traefik already has 80 and 443. Homestead
refuses a port that is taken before it changes anything. For VIPs as well,
add **kube-vip** under Settings → Hardware and storage → Add-ons; see
[Installing on k3s](Installing-on-k3s#4-addresses-for-apps).

## LAN networks

A LAN network puts a VM, or a container given an address of its own, on your
LAN like any other machine. **＋ LAN network** makes one:

- **On Harvester**, on one of its cluster networks - `mgmt` is the hosts' own -
  untagged, or on a VLAN. It is the same object Harvester's dashboard makes, so
  it shows there too.
- **On k3s, RKE2 and other clusters**, on a host interface, which the node
  probe lists. A **bridge** (`br0`) carries VMs and containers. On a plain
  **NIC** (`eth0`) a network is **For** one or the other:
  - **Virtual machines (macvtap)** - each VM with its own MAC and address on
    the LAN, through the NIC itself. A VM's own host cannot reach it this way;
    every other machine on the LAN can. Untagged only: for a VLAN, use a bridge.
  - **Containers (macvlan)** - each container with a MAC address of its own. On
    a VLAN, the NIC needs the host's VLAN interface (`eth0.20`) first.

  A VM cannot use a macvlan network - macvlan passes only frames for the
  address it made itself, so the VM never hears DHCP - and Homestead no longer
  offers one to a VM. These networks need Multus, and VMs on a NIC need
  macvtap. k3s and RKE2 bring neither: Homestead installs Multus when it
  starts on a new installation, and macvtap once KubeVirt is there. An older
  installation shows **Required components not installed** under **Settings →
  Cluster → Add-ons** and here, with **Install components**. macvtap is
  upgraded under **System → Cluster → Platform versions**. Installing it also
  switches off KubeVirt's `ExternalNetResourceInjection` feature (on by default
  from KubeVirt 1.8), so KubeVirt asks for the macvtap device a VM's network
  names - without it the VM's pod never gets one and Multus reports
  `deviceID is required`. macvtap's device plugin watches files, so Homestead
  also raises each host's inotify limits (below).

### A host bridge

When a host must reach its own VMs, or a network should carry VMs and
containers together, put the host's NIC into a bridge, as Proxmox's `vmbr0`
does. Under the LAN networks, **Move into a bridge…** beside a host without
one looks at its network first and says what it would change: the interface
the default route leaves by, its address (from DHCP or static), its gateway
and the netplan file that sets it up. Only a plain wired NIC that netplan sets
up through systemd-networkd - Ubuntu Server's way - is converted; anything
else is refused and left as it is. Then, with the host's name typed to confirm:

1. `/etc/netplan` is copied to `/var/lib/homestead/netplan-<time>`;
2. the NIC's addresses, routes, DNS and DHCP move to `br0` in the same file;
   `br0` takes the NIC's MAC address, and asks DHCP as that MAC, so the router
   gives it the same address. `netplan generate` must accept the result;
3. a rollback is armed on the host - a systemd timer that puts the copied
   files back and applies them four minutes later - and only then is the new
   configuration applied;
4. Homestead checks the host's address is on `br0`, its default route leaves
   by `br0` and its gateway answers, and only then disarms the rollback. A host
   that never answers puts its old network back by itself;
5. kube-vip starts again on the host, to announce VIPs on `br0`; with more than
   one host, k3s (or RKE2) restarts on it so flannel follows. Containers keep
   running throughout.

The host drops off the network for a few seconds. Follow it in the job tray,
then make a LAN network on `br0` for VMs and containers. A host whose DHCP
reservation is by client ID rather than MAC may be given another address; the
check then fails, and the host puts itself back.

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
- **UniFi** (optional, **Settings → Connections → UniFi Network**) brings in what a UniFi
  controller knows: clients, devices, reservations and networks, read-only.

## Workload firewall

**Networking → Firewall** lists Kubernetes NetworkPolicies. An admin can create
and edit Homestead policies with a workload picker and inbound/outbound allow
rules. Other policies remain visible for inspection, including policies changed
outside the editor into a form it cannot represent.

1. Choose **New policy** and select a Deployment, StatefulSet, DaemonSet, or
   supported pod-network VM.
2. Start with **Block inbound**, **Web server**, **Allow this namespace**, or
   **Isolate · keep cluster DNS**, then adjust the rules. A peer can be anywhere,
   an IP/CIDR (IPv4 or IPv6), or pods in a named namespace. Specify TCP, UDP, or
   SCTP destination ports; blank ports means every port for that protocol.
3. Choose **Review policy** to see the matching running pods, other matching
   policies, warnings, and the generated Kubernetes policy.
4. **Apply policy**, then test both permitted and blocked connections. If the
   workload, matching pods, or policies changed after review, review again.

**Allow listed traffic only** isolates that direction; an empty rule list
allows nothing through this policy. **Unrestricted by this policy** leaves that
direction to the other policies. Kubernetes combines all matching policies:
another policy can permit a connection this one omits. Rules are not ordered,
and there is no higher-priority deny. Removing the last policy for a direction
restores default allow; removing an allow policy while others remain can block
connections.

The page reports detected provider support separately from enforcement. K3s
includes a network-policy controller by default, but it can be disabled. An API
object alone does not prove traffic is filtered. Check your provider's settings
and verify traffic before relying on a policy.

Host networking and platform workloads are excluded from the editor. Policies
cover pod-network traffic only: VM LAN/Multus interfaces and host traffic need
guest, host, or upstream firewalls. Service address translation can affect which
source IP a policy sees. The DNS checkbox permits CoreDNS/kube-dns pods in
`kube-system`; NodeLocal DNS and custom resolvers need explicit rules.

Policies are stored as Kubernetes resources, not in Homestead's configuration
backup. Include NetworkPolicies in cluster backups. See the upstream
[NetworkPolicy guide](https://kubernetes.io/docs/concepts/services-networking/network-policies/)
and [K3s networking options](https://docs.k3s.io/networking/basic-network-options/).

## Portal

**Portal** is a page of links to every web interface - your apps (picked from
their exposed ports, with their logos) and the router, switches and NAS around
them - each with a dot saying whether it answers right now.
