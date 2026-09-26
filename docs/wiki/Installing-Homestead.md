# Installing Homestead

The cluster guides - [Harvester](Installing-on-Harvester), [k3s](Installing-on-k3s),
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
| DaemonSet `homestead-nodeprobe` | optional, one per host: temperatures, host devices, every disk, drive health |

Everything Homestead creates is named `homestead-*` or carries `homestead.io/*`
labels and annotations, so it is easy to find with kubectl. There is one
Homestead per cluster. The plain manifest installs it into `lab`; the Helm
chart into its own namespace, with your apps in `lab`.

## Helm or manifest?

Both install the same objects - the chart is generated from the manifest.

- **Helm** (`oci://ghcr.io/wjcloudy/charts/homestead`) takes values instead of
  edits, and `helm uninstall` removes it (your apps, their namespace and
  Homestead's data volume are kept).
- **The manifest** (`deploy/deploy.yaml`) needs only kubectl, and is what the k3s
  script uses.

Updates are the same either way: Homestead updates itself, below.

## First sign-in

Open `http://<address>:8088`. The first visit asks for an administrator's name
and password, and that account can then add others under **Settings → Access → Manage users**:

| Role | Can |
|---|---|
| viewer | see everything, change nothing |
| operator | deploy, edit, move, start and stop containers and VMs, open consoles |
| admin | everything, including users, hosts, hardware, imports and cluster settings |

Sessions last 12 hours of inactivity, or 30 days with **Keep me signed in**, and
never more than 90 days. Changing a password signs that account out
everywhere.

![Settings - Cluster](https://github.com/wjcloudy/homestead/releases/latest/download/homestead-settings-cluster.png)

## Things worth doing first

1. **Settings → About** - check Homestead's own health: every background task,
   the node probe, Samba, backup storage. See [Settings](Settings).
2. **Install the node probe**, if the Helm chart did not: **Settings → Cluster
   → Add-ons → Install node probe**. It adds temperatures, host devices (a Coral, a Zigbee
   stick, an iGPU), every disk and SMART drive health.
3. **Networking → Your VIPs** - keep a few addresses for apps (Harvester, or
   MetalLB). See [Networking](Networking).
4. **Data protection → Plans** - snapshots and backups in one go. See
   [Data protection](Data-protection).
5. **Name it** - **Settings → About this installation → Site name** shows under
   the logo, handy with more than one cluster.

## Updating Homestead

Homestead is on its own **Containers** page, in the **Homestead** group. When
a release comes out it shows an update like any other container; **Update**
pins the new image, watches it roll out, and keeps the old one for one-click
rollback. The page reconnects by itself while Homestead replaces itself.

Each release carries the permissions it needs, and Homestead brings its own
role up to date when it starts. An install older than that feature needs to be
given that right once:

```bash
kubectl apply -f https://raw.githubusercontent.com/wjcloudy/homestead/main/deploy/rbac.yaml
```

Settings shows this line whenever Homestead finds it cannot update its role.
Use the version-pinned command shown there. The RBAC-only manifest does not
replace your Deployment, Service address, configuration or volumes. Avoid
reapplying the full install manifest over a customised installation just to
gain a new permission.

For a Helm-managed installation, update the existing release using its actual
release name and namespace (shown by `helm list -A`). For example:

```bash
helm upgrade homestead oci://ghcr.io/wjcloudy/charts/homestead -n homestead --version 2.8.167 --reuse-values --set-string image.tag=2.8.167 --wait --timeout 5m
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
HTTPS. The README's
[Cloudflare Tunnel section](https://github.com/wjcloudy/homestead#publishing-through-a-cloudflare-tunnel)
covers doing that safely: Homestead can change anything in the cluster, so it
belongs behind Cloudflare Access (or a VPN), never published bare.

## Removing Homestead

```bash
helm uninstall homestead -n homestead
```

Your apps keep running, and so do the `lab` namespace and Homestead's data
volume. Delete the `homestead-auth` Secret yourself if you want the accounts
gone too.

**Do not `kubectl delete -f deploy.yaml`** to remove a manifest install: the
manifest also declares the `lab` namespace, and deleting a namespace deletes
every app and volume in it. Delete Homestead's own objects instead:

```bash
kubectl -n lab delete deployment/homestead service/homestead serviceaccount/homestead
kubectl delete clusterrolebinding/homestead clusterrole/homestead
```

The `homestead-data` claim is kept until you delete it from Volumes.
