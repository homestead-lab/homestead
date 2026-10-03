# Linked clusters

How several Homesteads are managed from any one of them. The user guide is
[Linked clusters](wiki/Linked-clusters.md) in the wiki; this page is how it
works, for anyone changing it. The code is `server/homestead_fleet.py`, the
relay in `server/server.py` (`_fleet_target`, `_fleet_forward`,
`_fleet_identity`) and `web/js/fleet.js`.

## What a person sees

- **A switch at the start of the breadcrumb**, once two or more clusters are
  linked. It lists every linked cluster with whether it answers and what it
  runs, and **All clusters**.
- **Picking a cluster** reloads the app from that cluster. The browser keeps
  talking to the Homestead it signed in to, which relays everything - pages,
  API, consoles - to the one picked. That cluster serves its own pages, so two
  clusters on different releases each show their own app.
- **All clusters** keeps this Homestead's app and shows Containers, Virtual
  Machines, Nodes, Volumes and Architecture from every linked cluster together, each row
  tagged with its cluster (Containers groups by cluster). An action on a row -
  or a dialog or console opened from it - goes to that row's cluster.
- **Move to cluster**, in a container's or VM's `…` menu, opens the
  destination at its review of that move: moves are still started by the
  cluster a workload goes to ([Moving between clusters](wiki/Moving-between-clusters.md)).
- **Settings → Linked clusters** links and unlinks clusters, sets this one's
  address, and picks one-at-a-time or all-together. Clusters added on Import
  for moves before linking existed are listed there to link (with their stored
  account, whose password is then deleted) or forget; the old name stays as an
  alias, so moves that recorded it carry on.
- **Moving workloads**, the second card on that tab, holds the moves under way
  and a card per cluster a move can come from - linked ones, and any added
  before linking: whether the two releases can move workloads, backup storage
  over there that this cluster can reach (setting up RustFS if it has none),
  and **Browse workloads**. The Import page no longer has any of this.

## Trust

Every member keeps the same **member list** (ConfigMap `homestead-fleet`) and
the same **key** (Secret `homestead-fleet-key`, 32 random bytes). No member
keeps a password for another.

Every call between members is signed with the key: an HMAC-SHA256 over the
sender, a time, a single-use nonce, the method, the path and query, the
person's name and role, and the SHA-256 of the body. The receiver refuses a
signature that does not match, a sender that is not a member, a time more than
five minutes away, or a nonce it has seen. Headers: `X-Homestead-Fleet`
(sender id), `-Time`, `-Nonce`, `-User`, `-Role`, `-Signature`.

A signed request acts as:

| Signed for | Acts as | Role |
|---|---|---|
| A person relayed by a member | `name@member` | Their role on the member they signed in to |
| The member itself (a move, a sync) | `homestead@member` | admin |
| Nobody (the app's pages and scripts) | not signed in | - |

The receiving cluster applies its own route rules to that role
(`needed_role`), so a viewer is a viewer everywhere. Every member is admin
over every other through the key - linking is an admin decision, and says so.

## Linking

Linking is done from one member, with an admin account on the other, once:

1. This Homestead signs in to the other with the account given, and asks
   `GET /api/fleet`. The other must stand alone: groups are joined one member
   at a time, so nothing is linked by accident.
2. It sends `POST /api/fleet/accept` with the key, the member list and the
   address the other is reached at. The other stores them, adds itself under
   a handle unique among the members, and answers with its own record.
3. This Homestead adds that record, and tells every other member
   (`POST /api/fleet/sync`, signed).

The password is used for that one sign-in and not kept.

**Changes** - a link, an unlink, an address, a renamed site - raise the list's
revision and are sent to every member. A member that missed one catches up the
next time the switch is opened: members report their revision in
`/api/fleet/hello`, and a newer list is fetched (`/api/fleet/state`) and
adopted. The newer revision wins.

**Unlinking** a member tells it first, with the key it still has; it finds
itself missing from the list and goes back to standing alone with a new key.
The others get a new key in the same sync, signed with the old one. A member
that **leaves** of its own accord gives the others a new key it then forgets.
A member that cannot be reached during a key change keeps the old key and
cannot talk to the rest until linked again.

## Relaying

For each request, `_fleet_target` asks which cluster it is for:

1. never another if it is already signed by a member (no relay loops), or is
   answered here by nature - sign-in, sign-out, password, dashboard preferences,
   push (including delivery confirmations), and the switch
   (`FLEET.LOCAL`);
2. the `X-Homestead-Cluster` header - a row's action in All clusters;
3. the `hs_cluster` query parameter - a console from such a row, since a
   WebSocket cannot send a header;
4. the `homestead_cluster` cookie - the browser switched to that cluster.

`_fleet_forward` checks the session here (and Cloudflare Access, if on),
then `FLEET.forward` opens a connection to the member, sends the request with
the person's headers less their cookies, `Cf-*` and anything about the
connection, adds the signature, and pipes the answer back. The member's
`Set-Cookie` headers are dropped - they are not this origin's - and this
Homestead's own session refresh is added. A `101 Switching Protocols` (a
console) is piped both ways until either end closes. Each relayed request
closes its connection when done.

A cluster that does not answer is reported before anything is sent: an API
call gets a 502 with the reason; a page gets a short page with a link back
(`/api/fleet/home` clears the cookie).

## All clusters

`GET /api/fleet/all/{workloads,vms,nodes,volumes}` asks every reachable member
for that list in parallel, as the person asking (their name and role), adds
this cluster's own, and tags each row `site: {id, name, handle, self}`. Members
that do not answer are named in `X-Homestead-Fleet-Missing`, and the page says
so once.

In the browser, `fleetRoute` in `api()` swaps a list for its all-clusters
version only on the page that shows it, so a deploy placing pods still reads
this cluster's nodes. A click inside an element with `data-cluster` makes
that the target cluster until another row or the navigation is clicked;
while set, writes, reads naming an object (a query string) and anything read
with a dialog open go to it.

## Endpoints

| Route | Who | What |
|---|---|---|
| `GET /api/fleet` | viewer | The members, whether each answers, what each runs, this one's address |
| `GET /api/fleet/hello` | viewer | This member's id, name, release, protocol and revision |
| `GET /api/fleet/state` | a member | The member list, for one that fell behind (never the key) |
| `GET /api/fleet/all/<list>` | viewer | One list from every member |
| `GET /api/fleet/home` | signed in | Back to this cluster (clears the cookie) |
| `POST /api/fleet/switch` | signed in | Look at another cluster (sets the cookie), or this one |
| `POST /api/fleet/join` | admin | Link another Homestead |
| `POST /api/fleet/accept` | admin | Be linked (called by the joining member) |
| `POST /api/fleet/sync` | a member | A newer member list, maybe with a new key |
| `POST /api/fleet/remove` | admin | Unlink a member |
| `POST /api/fleet/leave` | admin | Unlink this one |
| `POST /api/fleet/address` | admin | Where the others reach this one |
| `GET /api/fleet/legacy` | viewer | Clusters added for moves before linking, and whether each is linked already |
| `POST /api/fleet/link-legacy` | admin | Link one of those with its stored account, then delete the password |

## Limits

- Members reach each other directly: a member two hops away is not relayed
  through a third.
- Push notifications go to the devices registered on the member a person
  signed in to, for that member's alerts.
- All clusters covers the four lists. Image-update badges there are this
  cluster's own; other clusters' rows show none.
- Nonces are remembered per Homestead process: with several Homestead replicas,
  a signed request copied off the wire could be replayed to another replica
  within five minutes. Use HTTPS between members where the LAN is not trusted.


### Personal dashboard preferences

Dashboard layouts belong to the account on the Homestead the browser signed
into. Both reading and saving `/api/auth/preferences/dashboard` stay there,
regardless of the selected cluster. Widget data continues to come from the
selected cluster. The remote identity (`name@member`) is an audit identity,
not an account whose preferences should be created on the remote cluster.
Update the entry-point Homestead to receive this routing fix.

## Combined Architecture

`/api/fleet/all/flow` gathers each member's existing `/api/flow` response using
its signed user/role relay. It returns tagged cluster graphs and a `missing`
list. Partial results remain visible with a persistent warning naming the
clusters that could not be loaded. Resource identities are prefixed with the
cluster ID in the browser; display and action names remain unchanged. VIPs,
ports, claims and replica links therefore cannot cross cluster boundaries when
names or addresses match. Move actions carry the resource's cluster context. Each cluster has its own
labelled diagram, stacked vertically; highlighting stays within that diagram.
