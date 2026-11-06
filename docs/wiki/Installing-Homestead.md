# Installing Homestead

**For a new home self-hosting setup, choose [k3s](Installing-on-k3s): it is
Homestead's preferred and most widely tested platform.**

The cluster guides - [k3s](Installing-on-k3s), [RKE2](Installing-on-RKE2),
[Harvester](Installing-on-Harvester),
[an existing cluster](Installing-on-an-existing-cluster) - each end with
Homestead running. This page is what they share: what got installed, the first
sign-in, and looking after Homestead afterwards.

## What gets installed

| Object | What it is |
|---|---|
| Deployment `homestead` | Homestead itself: one small container, no database, running as a non-root user on a read-only filesystem |
| Service `homestead`, port 8088 | its address on your LAN |
| Claim `homestead-data`, 2 GiB | settings, job history, long-term stats, the audit log |
| Secret `homestead-auth` | user accounts (salted PBKDF2 password hashes), kept apart from the data volume |
| ServiceAccount, ClusterRole `homestead` | what Homestead may do in the cluster |
| DaemonSet `homestead-nodeprobe` | installed automatically, one per host: temperatures, host devices, every disk, drive health |

Most of what Homestead creates for itself is named `homestead-*` or carries
`homestead.io/*` labels and annotations, so it is easy to find with kubectl.
Apps you deploy through Homestead carry some of those labels too.
[What Homestead changes](What-Homestead-changes) lists what it makes after the
install, and when. There is one
Homestead per cluster. The plain manifest installs it into `lab`; the Helm
chart into its own namespace, with your apps in `lab`.

## Helm or manifest?

Both install the same objects - the chart is generated from the manifest.

- **Helm** (`oci://ghcr.io/homestead-lab/charts/homestead`) takes values instead of
  edits, and `helm uninstall` removes it (your apps, their namespace and
  Homestead's data volume are kept).
- **The manifest** (`deploy/deploy.yaml`) needs only kubectl, and is what the k3s
  script uses.

Updates are the same either way: Homestead updates itself, below.

## Local host console

The interactive installer enables a **Host Console** by default on k3s and RKE2
servers and workers. It replaces the first local console's login screen with
live CPU, RAM, uptime, IP addresses and local disk usage. Server nodes also show
cluster members, readiness, Kubernetes versions and Homestead's service address.

![The host console on a k3s server: CPU history and cores, memory, disks, and the cluster's nodes](https://github.com/homestead-lab/homestead/releases/latest/download/homestead-tui-host-status-logo.png)

The dashboard uses green for healthy nodes, amber for warnings or cordoned
nodes, and red for unavailable APIs, failed nodes or high resource usage.
CPU, per-core load, memory and disk usage have coloured bars, with a rolling
CPU history graph. Usage turns amber at 75% and red at 90%. Narrow terminals
stack the panels and keep the full view available by scrolling.

![The host console with a node down and memory and Longhorn's disk nearly full](https://github.com/homestead-lab/homestead/releases/latest/download/homestead-tui-host-status-alert.png)

The logo, history graph and usage bars use Unicode Braille dots, like btop.
The house outline and two white infrastructure layers are rasterised from
Homestead's logo, sized for tall terminal cells. Press **A** to switch between
dots and plain characters if the console font lacks Braille glyphs. Non-Unicode
locales use plain characters automatically; `host-console.py --ascii` also
forces that view.
Workers show local health; cluster details require a server's local kubeconfig.
Harvester keeps its native console.

Press **Enter**, **Q** or **Escape** for the normal authenticated login prompt.
Use the arrow keys or Page Up/Down to scroll. The screen returns after logout.
SSH and other consoles work as usual. Host metrics refresh every two seconds;
cluster status refreshes every fifteen seconds and shows API failures.

![The installer's Host Console question](https://github.com/homestead-lab/homestead/releases/latest/download/homestead-tui-console.png)

Choose **No** at the Host Console question, or set `HS_CONSOLE=no` for unattended
installation. To enable or disable it later, use **Configure host status console**
in the installer menu, or run the downloaded installer:

```sh
sudo sh install.sh --console
sudo sh install.sh --no-console
```

In Homestead, the host console is an add-on for the whole cluster: **Settings →
Cluster → Add-ons → Host console**, with one On/Off switch. While it's on, Homestead keeps
every Ready host on the console bundled with the running Homestead:

- it installs the console on hosts that don't have it, including hosts that
  join later
- it updates hosts when Homestead carries a newer console, so updating Homestead
  rolls the console out across the cluster

Turned off, it's removed from every host. Homestead works through a couple of
hosts every ten minutes, and the row lists each host's state. A host that fails
is tried again an hour later. On a cluster set up before this setting existed,
the console counts as on where the installer turned it on. Harvester hosts keep
their native console.

Changes take effect on the next boot or logout, so installation does not
interrupt a logged-in administrator. The console needs systemd, agetty and
Python 3 with curses; the installer installs Python if needed. It reads local
metrics and Kubernetes status without displaying credentials or providing an
unauthenticated shell. The screen is visible to anyone with physical or remote
console access.

For manual removal, delete only
`/etc/systemd/system/getty@tty1.service.d/50-homestead-console.conf`, then run
`sudo systemctl daemon-reload`. Normal login returns on the next boot or logout.

## First sign-in

Open `http://<address>:8088`. The first visit asks for an administrator's name
and password, and for the one-time **setup code**, so that nobody else who
reaches the address first can make themselves the administrator. The
installer prints the code beside the address. Otherwise it is in the Secret
`homestead-setup` in Homestead's namespace (`lab` for the installer and the
manifest, the release's namespace for Helm):

```bash
kubectl -n lab get secret homestead-setup -o jsonpath='{.data.code}' | base64 -d
```

To choose the code yourself, set `HS_SETUP_CODE=ABCD-EFGH-JK` for the installer
or `--set setupCode=ABCD-EFGH-JK` for Helm: 10 characters from 0-9 and A-Z
without I, L, O and U. The code stops working once the first administrator
exists, and Homestead then deletes the Secret; clear `setupCode` from your Helm
values after install (a code that was used once never works again anyway).

A code typed on the command line stays in your shell history, and Helm keeps
every value it is given, `--set` or a values file, in the release, where
`helm get values` shows it. To keep it out of your history, read it in
without it showing, then pass the variable on. For the installer (`sudo`
drops the environment unless told to keep it):

```bash
read -rsp 'Setup code: ' HS_SETUP_CODE; echo; export HS_SETUP_CODE
curl -sfL https://raw.githubusercontent.com/homestead-lab/homestead/main/scripts/install.sh | sudo --preserve-env=HS_SETUP_CODE sh
unset HS_SETUP_CODE
```

Or for Helm, after the same `read`:

```bash
helm upgrade --install homestead oci://ghcr.io/homestead-lab/charts/homestead -n homestead <your other flags> --set setupCode="$HS_SETUP_CODE"
unset HS_SETUP_CODE
```

While Helm runs, its command line (with the code) can be seen by other users of
that machine, as with any `--set`. Clearing `setupCode` after install (above)
takes it out of the release's current values; earlier revisions keep it until
Helm drops them, but a code that was used once never works again.

That account can then add others under **Settings → Users and access → Add user**:

| Role | Can |
|---|---|
| viewer | see everything, change nothing |
| operator | deploy, edit, move, start and stop containers and VMs, open consoles |
| admin | everything, including users, hosts, hardware, imports and cluster settings |

Sessions last 12 hours of inactivity, or 30 days with **Keep me signed in**, and
never more than 90 days. Changing a password signs that account out
everywhere.

![Settings - Cluster](https://github.com/homestead-lab/homestead/releases/latest/download/homestead-settings-cluster.jpg)

## The setup guide

The first time an administrator signs in, Homestead opens its **setup guide**
(`/setup`): seven chapters - *Cluster health*, *Access*, *LAN networking*,
*Storage and backups*, *Preferences and users*, *Connections* and
*Applications and console* - each step a sentence on why it matters, the
cluster as it is now (a diagram where it helps: quorum, the VIP, copies across
nodes, the HTTPS path from a phone), and one thing to do.

- Homestead **checks each step itself**, every time: a VIP set, backups
  configured, disks in use, the default storage class matching the nodes,
  notifications on. A step done and since undone shows again. **Skip** puts
  one aside; **Bring it back** returns it.
- **Admins** see every chapter. Everyone else sees the steps that are theirs:
  appearance, installing it on their phone, and notifications.
- It covers what new clusters most often miss: two servers (no node can fail
  until there is a third), getting **HTTPS** - needed to install Homestead on a
  phone and get notifications - with a **Cloudflare Tunnel** or **Tailscale**
  set up from the step, disks Longhorn is not using yet, a default class with
  too many or too few copies, an encrypted export of Homestead's own settings,
  and a second administrator.
- The guide has 25 steps across its seven chapters. Open it again any time,
  also after setup is complete, from the book icon in the top bar or
  **Settings → Homestead → Open setup guide**.

## Things worth doing first

The setup guide walks through all of these; in short:

1. **Settings → Homestead** - check Homestead's own health: every background task,
   the node probe, Samba, backup storage. See [Settings](Settings).
2. **Check node telemetry.** Homestead installs the node probe automatically,
   unless explicitly declined in the installer. It adds temperatures, host
   devices, disks and SMART drive health. Review its status in **Settings → Homestead**.
3. **Networking → Workload VIPs** - keep a few addresses for apps (Harvester, or
   MetalLB). See [Networking](Networking).
4. **Data protection → Plans** - snapshots and backups in one go. See
   [Data protection](Data-protection).
5. **Name it** - **Settings → Homestead → About this installation → Site name** shows under
   the logo, handy with more than one cluster.

## Updating Homestead

When a release comes out, a **Homestead** button with the new version appears
on the top bar. It, and **Settings → Updates**, show the
version you run, the new one with a link to its release notes, and the helpers
Homestead runs beside it (the SMB and NFS servers and the object store used
for moves). **Update** opens the same reviewed rollout apps get: helpers
first, Homestead last, the old image kept for rollback. The page reconnects by
itself while Homestead replaces itself. Homestead and its helpers are platform
containers, so they are hidden on **Containers** and not counted among app
updates; see [Settings](Settings#homestead-updates).

Each release carries the permissions it needs, and Homestead brings its own
role up to date when it starts. An install older than that feature needs to be
given that right once:

```bash
kubectl apply -f https://raw.githubusercontent.com/homestead-lab/homestead/main/deploy/rbac.yaml
```

Settings shows this line whenever Homestead finds it cannot update its role.
Use the version-pinned command shown there. The RBAC-only manifest does not
replace your Deployment, Service address, configuration or volumes. Avoid
reapplying the full install manifest over a customised installation just to
gain a new permission.

For a Helm-managed installation, update the existing release using its actual
release name and namespace (shown by `helm list -A`). Replace `<version>` with
the release you want; the newest is at the top of the
[releases page](https://github.com/homestead-lab/homestead/releases). Drop the
release tag's leading `v`: release `v2.8.332`, for example, is
`--version 2.8.332 --set-string image.tag=2.8.332`, as image tags carry no `v`.

```bash
helm upgrade homestead oci://ghcr.io/homestead-lab/charts/homestead -n homestead --version <version> --reuse-values --set-string image.tag=<version> --wait --timeout 5m
```

This preserves saved values while explicitly updating the image even if an older
tag was pinned. If a HelmChart controller or GitOps manages the release, update
its desired version/values there instead of competing with the controller.

After updating, check the installed version, **Permissions**, and Homestead's
ready status. From v2.8.167, host power control defaults on but still requires an
administrator's impact review and passes no safety blocker automatically. An
explicit `ENABLE_NODE_POWER=false` stays disabled; changing an environment
setting restarts Homestead, not the host.

## Reaching it from outside

The Service is plain HTTP, for your LAN. To reach Homestead from elsewhere - and
to install it as an app with push notifications on your phone - put it behind
HTTPS. The reference's
[Cloudflare Tunnel section](https://github.com/homestead-lab/homestead/blob/main/docs/reference.md#publishing-through-a-cloudflare-tunnel)
covers doing that safely: Homestead can change anything in the cluster, so it
belongs behind Cloudflare Access (or a VPN), never published bare.

## Removing Homestead

On a server node, the installer does it, for a Helm or a manifest install. It
lists every object by exact name and asks first; `--dry-run` only lists. Your
apps keep running, and your data volume, accounts and settings stay unless you
add `--everything`:

```bash
curl -sfL https://raw.githubusercontent.com/homestead-lab/homestead/main/scripts/install.sh | sudo sh -s -- --uninstall --dry-run
curl -sfL https://raw.githubusercontent.com/homestead-lab/homestead/main/scripts/install.sh | sudo sh -s -- --uninstall
```

[What Homestead changes](What-Homestead-changes#what-removing-homestead-leaves-behind)
says what it removes and what it leaves. To do it by hand instead:

```bash
helm uninstall homestead -n homestead
```

Your apps keep running, and so do the `lab` namespace and Homestead's data
volume. Delete the `homestead-auth` Secret (in `lab`) yourself if you want the
accounts gone too. What Homestead made after the install stays as well:
[What Homestead changes](What-Homestead-changes#what-removing-homestead-leaves-behind)
says what to look for.

**Do not `kubectl delete -f deploy.yaml`** to remove a manifest install: the
manifest also declares the `lab` namespace, and deleting a namespace deletes
every app and volume in it. Delete Homestead's own objects instead:

```bash
kubectl -n lab delete deployment/homestead service/homestead serviceaccount/homestead \
  role/homestead-console rolebinding/homestead-console
kubectl delete clusterrolebinding/homestead clusterrole/homestead
```

The `homestead-data` claim is kept until you delete it from Volumes.

Cluster-wide things Homestead may have made stay too, among them the
PriorityClasses of the priority tiers and the hot-spare taint on standby
hosts.

- **The hot-spare taint** is safe to remove: with Homestead gone nothing moves
  work onto a spare, so it would only keep the host empty. The command says
  `not found` for each host without it, which is expected:

  ```bash
  kubectl taint nodes --all homestead.io/standby-
  ```

- **Keep the PriorityClasses** (`homestead-critical`, `homestead-normal`,
  `homestead-low`, or `homestead-<tier>-<value>`) while anything names one.
  Pods running now carry on, but Kubernetes refuses every new pod that names
  a class that is gone, so an app, VM or Helm release still naming one cannot
  restart, update or move to another host. Set every app and VM back to
  **Normal** before uninstalling, or delete a class only once
  [nothing names it](What-Homestead-changes#still-used-by-your-apps-or-data-do-not-delete).

[What Homestead changes](What-Homestead-changes#what-removing-homestead-leaves-behind)
lists everything else that stays, what is safe to delete, and what your apps
still need.
