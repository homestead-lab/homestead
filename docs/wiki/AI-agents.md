# AI agents

Homestead can be driven by an AI agent: Claude Code, Claude Desktop or any
other client that speaks the [Model Context Protocol](https://modelcontextprotocol.io)
(MCP). The agent can look at your cluster, start and stop things, deploy and
edit containers and VMs, and work with volumes and backups, within what you let
its key do.

It is **off by default**, and careful by design: every change is checked first,
anything that destroys data waits for a person to approve it, and every call is
written to an audit.

For scripts, Home Assistant, or agent frameworks that do not speak MCP, use the
[API](API). MCP is the easier way for clients that support it; the API, with its
OpenAPI description, remains for the rest.

## Turning it on

An administrator opens **Settings › Access** and turns on the **AI agents (MCP)**
card. While it is off, `POST /mcp` answers `404`, as if it did not exist.

## Making a key

An agent signs in with a [Homestead API key](API#making-a-key), made in
**Settings › Users and access › API keys › ＋ New key** with **Allow AI agents (MCP)**
ticked. A key without that tick does not work on `/mcp`, and a browser session
never does.

A key acts as an **operator at most**, even when an administrator made it.
Choose scopes for what the agent should do - every scope includes `read`, and
managing an area includes controlling it - or start from the dialog's
**Read only**, **Operate** or **Manage everything**. **Edit** on a key changes
its scopes later without a new token:

| Scope | Lets the agent |
|---|---|
| `read` | See the cluster: nodes, containers, VMs, volumes, backups, alerts and jobs |
| `logs:read` | Read containers' logs (they can hold passwords apps print, so it is separate) |
| `containers:control` | Start, stop and restart containers; cancel and dismiss jobs |
| `containers:manage` | Deploy, install, edit, scale, move and update containers; their settings, schedules, uptime checks and change history; ask to delete one |
| `vms:control` | Start, stop and restart virtual machines |
| `vms:manage` | Create, edit and migrate VMs; ISOs and k3s clusters of VMs; ask to delete one |
| `storage:manage` | Create and grow volumes, snapshot and back up, backup groups and jobs, balance storage; ask to delete, revert or restore |
| `network:manage` | Expose apps, and keep the IP address plan |
| `cluster:manage` | Namespaces and raw Kubernetes objects - only an admin may change them, so each change waits for an admin to approve it |

Give the agent `read` alone first and see what it makes of your cluster. Add
scopes when you want it to act. Use HTTPS: the key travels in a header. Set
**Only from** to the client's address only where Homestead can see it (see
[API](API) - on many clusters it sees a node's address instead).

## Connecting a client

The address is `/mcp` on your Homestead, and the key goes in the
`Authorization: Bearer` header.

**Claude Code:**

```bash
claude mcp add --transport http --scope user homestead http://homestead.example.com/mcp \
  --header "Authorization: Bearer hsk_..."
```

`--scope user` makes Homestead available in every project. Without it, Claude
Code adds it only to the folder the command ran in. The desktop app's Code tab
reads the same configuration, but does not put the `claude` command on the
path: install it with `npm install -g @anthropic-ai/claude-code` to run the
line above. A session loads its servers when it starts, so start a new one and
run `/mcp` to see Homestead connected.

**Claude Desktop** reaches it through `mcp-remote`. Add this to its
configuration:

```json
{
  "mcpServers": {
    "homestead": {
      "command": "npx",
      "args": ["mcp-remote", "http://homestead.example.com/mcp",
               "--header", "Authorization: Bearer ${HOMESTEAD_KEY}"],
      "env": { "HOMESTEAD_KEY": "hsk_..." }
    }
  }
}
```

The server speaks MCP Streamable HTTP, statelessly: `initialize`, `tools/list`,
`tools/call` and `ping`, each as one JSON request and answer. `GET` is refused
with `405`. A request may be at most 256 KiB, and batches are not accepted.

The agent sees only the tools its key's scopes allow.

## The tools

Tools come in three classes:

- **read** - looks, changes nothing.
- **change** - changes something, and is checked first (see below).
- **destructive** - cannot be undone, so it only *asks*: a person approves it in Homestead.

### Cluster and jobs

| Tool | What it does | Scope | Class |
|---|---|---|---|
| `whoami` | This key: its name, its scopes and when it expires | any key | read |
| `get_status` | The cluster at a glance: nodes, containers, VMs and alerts, counted | `read` | read |
| `list_nodes` | Every host with its health and use | `read` | read |
| `list_alerts` | What is wrong right now, as the app's bell shows it | `read` | read |
| `get_job` | A background job by the id an action answered with | `read` | read |
| `host_failure_forecast` | For every host: if it failed, whether the others would have room for what it runs, tier by tier, and how many StatefulSets would wait until their pod is deleted | `read` | read |
| `host_failure_preview` | One host: if it went down, what stops, waits or moves and where, and why - apps, StatefulSets, VMs, volumes at risk and addresses | `read` | read |
| `list_jobs` | Background jobs, newest first: what runs, what finished and what failed | `read` | read |
| `get_job_log` | What a job wrote as it ran: its pod's log, or its steps | `read` | read |
| `preview_cancel_job` | What cancelling a job would undo and keep, and the options and confirmation it needs | `containers:control` | change |
| `cancel_job` | Cancels a running job as preview_cancel_job described: pass its options and, when it asked, confirm | `containers:control` | change |
| `dismiss_job` | Takes a finished job off the list, or every finished one with all: true | `containers:control` | change |
| `list_cluster_events` | Kubernetes' newest warnings and events across the cluster (up to 120, newest first), with `total` (how many it read) and `limited` (true when it stopped before the end of a very long list) | `read` | read |
| `get_placement` | The placement mode, each host's part, standby hosts, and how many apps reserve nothing | `read` | read |
| `get_agent_request` | Follows a request a destructive tool filed, until a person answers it | any key | read |

### Containers

| Tool | What it does | Scope | Class |
|---|---|---|---|
| `list_containers` | Every container (app) with its state | `read` | read |
| `get_container` | One container's state | `read` | read |
| `get_container_settings` | A container's settings as Edit shows them; priority may be group, for an app that follows its group's tier | `read` | read |
| `list_events` | Kubernetes' recent events for a container or VM, newest first | `read` | read |
| `get_container_logs` | A container's last lines of output (at most 500) | `logs:read` | read |
| `list_log_alerts` | The log alerts: what each counts, whether it is paused, and its last hour of counts. Making, pausing and deleting them stays in the app | `logs:read` | read |
| `get_container_diagnostics` | Owned pods, restarts, current and last exit details, and current/previous logs, including crash loops, with the restarts Homestead kept and why each happened | `logs:read` | read |
| `run_container_command` | Ask an administrator to approve an exact argument array in one app container; bounded output and execution time | `containers:manage` | destructive |
| `deploy_options` | What a new container can use: storage classes, networks, hosts, hardware, addresses | `read` | read |
| `search_app_store` | Apps the App Store offers, by name or category | `read` | read |
| `container_start` | Starts a stopped container, once Homestead has checked it fits | `containers:control` | change |
| `container_stop` | Stops a container (scales it to zero) | `containers:control` | change |
| `container_restart` | Restarts a container's pods | `containers:control` | change |
| `preview_deploy` | What deploying these settings would create, and its capacity review, without creating anything | `containers:manage` | change |
| `deploy_container` | Deploys a container | `containers:manage` | change |
| `install_app` | Installs an App Store app, with overrides to its defaults | `containers:manage` | change |
| `preview_compose` | What a Docker Compose file would deploy, and its review, without deploying it | `containers:manage` | change |
| `apply_compose` | Deploys a Compose file as preview_compose reviewed it | `containers:manage` | change |
| `preview_edit_container` | What saving these settings (from get_container_settings, changed) would do: what restarts, and the capacity review | `containers:manage` | change |
| `edit_container` | Saves a container's settings as preview_edit_container reviewed them: the same settings plus, inside settings, its capacity_token and confirm_capacity: true | `containers:manage` | change |
| `scale_container` | Sets how many copies of a container run | `containers:manage` | change |
| `preview_move_container` | Whether a container fits on another host, and its capacity_token for move_container | `containers:manage` | change |
| `move_container` | Moves a container to another host as preview_move_container reviewed it | `containers:manage` | change |
| `delete_container` | Asks to delete a container and its Services; its volumes are kept | `containers:manage` | destructive |
| `get_change_history` | What changed in an app and when - each change can be undone with undo_change | `read` | read |
| `preview_undo_change` | What undoing a change from get_change_history would restore | `containers:manage` | change |
| `undo_change` | Restores an app's settings from before a change, as preview_undo_change reviewed it | `containers:manage` | change |
| `list_image_updates` | Which apps have a newer image, and how each is updated (automatically, in the window, or by hand) | `read` | read |
| `update_container_image` | Updates an app to its newer image, after the capacity review; the update policy has a person approve each install, so it waits as a request | `containers:manage` | change |
| `rollback_container_image` | Puts an app back on the image it had before its last update, after the capacity review, as update_container_image | `containers:manage` | change |
| `get_image_update_progress` | How an app's image update or rollback is going | `read` | read |
| `set_image_update_mode` | How an app's image updates are made: settings with ns, name and mode, as list_image_updates shows the modes | `containers:manage` | change |
| `set_managed_by` | Marks an app as updated by something else (Flux, Argo CD, a person) so Homestead offers no updates, or clears it with an empty by | `containers:manage` | change |
| `set_primary_port` | The port the app's link opens; 0 to let Homestead choose | `containers:manage` | change |
| `set_failover` | For many apps: items, each {ns, name, mode} with mode move, wait or default | `containers:manage` | change |
| `set_container_groups` | The groups apps are shown in: settings as the Containers page sends them (items, each with ns, name and group; renamed_from, the old name, when renaming a group so its tier moves) | `containers:manage` | change |
| `set_group_tier` | The tier the apps that follow a group take: group, and tier critical, normal, low or none. Only the tier is saved and nothing restarts; preview_priority_tiers then lists the apps that need a restart | `containers:manage` | change |
| `set_container_logos` | Logos for many apps: items, each {ns, name, icon} with icon a URL or an App Store logo | `containers:manage` | change |
| `preview_priority_tiers` | Every app and VM with its tier, where it comes from (its own, its group's or the default), what it runs with and its status (in_line, restart, git with the line to commit, outside, unknown_group), and each group's tier; chosen as id=tier pairs (lab/web=critical,vm:lab/win=low) | `read` | read |
| `set_priority_tiers` | Sets tiers as preview_priority_tiers reviewed them: settings {changes (its apps that change, with id, current and tier), review_token, restart: true} | `containers:manage` | change |
| `preview_reservations` | What each app used over the past week and the reservation suggested; chosen limits it to these app ids | `read` | read |
| `set_reservations` | Applies reservations as preview_reservations reviewed them: settings {apps, review_token, restart: true} | `containers:manage` | change |
| `preview_container_rebalance` | Which containers Balance hosts would move to even the hosts out; exclude: app ids to leave alone. With picks [{id, to}]: the review of those moves - why each can't go, and the hosts' room with all of them | `read` | read |
| `rebalance_containers` | Moves containers as preview_container_rebalance reviewed them: settings {moves, review_token, restart: true, exclude}, or {picks, review_token, restart: true} for chosen moves, which stop at the first that does not go | `containers:manage` | change |
| `get_uptime` | Each app's and VM's uptime check and how it has answered | `read` | read |
| `set_uptime_check` | How an app or VM is checked: settings {kind, ns, name, mode (auto, tcp, http, off), port, path} | `containers:manage` | change |
| `set_outage_actions` | What to do when an app or VM is down: settings {kind, ns, name, actions {restart {after_min, max}, webhook, format (generic, ntfy, gotify), lan (admins only)}} | `containers:manage` | change |
| `test_outage_webhook` | Sends a test call to the webhook an app's or VM's outage actions would call: settings {kind, ns, name, webhook, format}; an admin approves it | `containers:manage` | change |

### Schedules

| Tool | What it does | Scope | Class |
|---|---|---|---|
| `list_schedules` | Scheduled jobs (CronJobs) in every namespace: when they last ran, how that run ended, a run that did not happen, and the next run | `read` | read |
| `save_schedule` | Creates or changes a scheduled job: name, image, command, schedule (cron, UTC) and suspend | `containers:manage` | change |
| `run_schedule` | Runs a scheduled job once, now | `containers:manage` | change |
| `delete_schedule` | Asks to delete a scheduled job | `containers:manage` | destructive |
| `list_power_schedules` | When apps and VMs are stopped and started on a schedule | `read` | read |
| `set_power_schedule` | When an app or VM stops and starts: settings as list_power_schedules shows one (kind, ns, name, stop, start, days) | `containers:manage` | change |

### Virtual machines

| Tool | What it does | Scope | Class |
|---|---|---|---|
| `list_vms` | Every virtual machine with its state | `read` | read |
| `get_vm` | One virtual machine's state | `read` | read |
| `vm_start` | Starts a VM, once Homestead has checked it fits | `vms:control` | change |
| `vm_stop` | Asks the guest to shut down | `vms:control` | change |
| `vm_restart` | Restarts the guest | `vms:control` | change |
| `preview_create_vm` | What creating a VM with these settings would make, and its review; pass its capacity_token to create_vm | `vms:manage` | change |
| `create_vm` | Creates a VM as preview_create_vm reviewed it | `vms:manage` | change |
| `preview_edit_vm` | What saving these VM settings would change, and its review | `vms:manage` | change |
| `edit_vm` | Saves a VM's settings as preview_edit_vm reviewed them | `vms:manage` | change |
| `migrate_vm` | Live-migrates a running VM to another host | `vms:manage` | change |
| `delete_vm` | Asks to delete a VM, and its disks when delete_disks is true | `vms:manage` | destructive |
| `get_vm_settings` | A VM's settings as Edit shows them; change them and pass them to preview_edit_vm | `read` | read |
| `preview_vm_power` | Whether a VM can start, stop, restart or pause now, and its review: settings {ns, name, action} | `vms:control` | change |
| `vm_power` | Starts, stops, restarts, pauses or resumes a VM as preview_vm_power reviewed it (settings plus its capacity_token) | `vms:control` | change |
| `set_vm_logos` | Logos for VMs, as set_container_logos | `vms:manage` | change |
| `set_vm_monitoring` | How VMs are checked: settings as the VMs page sends them | `vms:manage` | change |
| `list_isos` | ISO images VMs can install from | `read` | read |
| `prepare_iso` | Makes an ISO on a network share ready to install VMs from | `vms:manage` | change |
| `preview_k3s_cluster` | What building a k3s cluster from new VMs would make, and its review | `vms:manage` | change |
| `create_k3s_cluster` | Builds it as preview_k3s_cluster reviewed it | `vms:manage` | change |

### Storage

| Tool | What it does | Scope | Class |
|---|---|---|---|
| `storage_forecast` | When each volume, disk and the pool is expected to fill | `read` | read |
| `list_volumes` | Every volume in a line each: size, use, health, copies and what uses it | `read` | read |
| `list_snapshots` | A volume's snapshots | `read` | read |
| `list_backups` | Backups, of one volume or all | `read` | read |
| `get_filesystem_freeze` | Whether Longhorn freezes filesystems for snapshots: the cluster setting, old-kernel hosts and each volume's choice | `read` | read |
| `create_volume` | Creates an empty volume | `storage:manage` | change |
| `edit_volume` | Grows a volume, or changes how many copies Longhorn keeps | `storage:manage` | change |
| `snapshot_volume` | Takes a snapshot of a volume now | `storage:manage` | change |
| `backup_volume` | Backs a volume up to the backup target now | `storage:manage` | change |
| `run_backup_job` | Runs a recurring backup or snapshot job now | `storage:manage` | change |
| `delete_volume` | Asks to delete a volume; with delete_data its data is deleted too, not only the claim | `storage:manage` | destructive |
| `delete_snapshot` | Asks to delete a snapshot | `storage:manage` | destructive |
| `revert_to_snapshot` | Asks to roll a volume back to a snapshot: what was written since is lost, and what uses it stops meanwhile | `storage:manage` | destructive |
| `restore_backup` | Asks to restore a backup as a new volume | `storage:manage` | destructive |
| `delete_backup` | Asks to delete a backup from the backup target | `storage:manage` | destructive |
| `change_storage_class` | Asks to move a volume's data to another storage class; what uses it stops while it copies | `storage:manage` | destructive |
| `get_backup_overview` | Backup and snapshot jobs, the groups that hold volumes to them, and the backup target | `read` | read |
| `save_backup_group` | Creates or changes a group of volumes and the jobs that apply to it: settings {name, jobs, volumes, original} | `storage:manage` | change |
| `save_backup_job` | Creates or changes a recurring backup or snapshot job: settings {name, task (backup or snapshot), cron, retain, concurrency, groups} | `storage:manage` | change |
| `assign_backups` | Adds volumes to (or, with enabled false, takes them out of) a group or a job | `storage:manage` | change |
| `delete_backup_group` | Asks to delete a backup group; its volumes stop being backed up by its jobs | `storage:manage` | destructive |
| `delete_backup_job` | Asks to delete a recurring backup or snapshot job | `storage:manage` | destructive |
| `set_filesystem_freeze` | Asks to turn the freeze for snapshots on or off, for the cluster or one volume | `storage:manage` | destructive |
| `trim_volume` | Gives a volume's freed space back to its disks | `storage:manage` | change |
| `set_weekly_trim` | Asks to leave one volume out of the weekly trim, or to trim it weekly again | `storage:manage` | destructive |
| `rebuild_volume` | Rebuilds missing copies of a detached volume now | `storage:manage` | change |
| `preview_storage_rebalance` | Which volume copies would move to even out the disks; exclude: volumes to leave alone | `read` | read |
| `rebalance_storage` | Moves volume copies as preview_storage_rebalance planned: settings {moves, review_token, exclude} | `storage:manage` | change |
| `preview_volume_copy_move` | Where a Longhorn volume's copies are, and which would move to disks with these tags (comma-separated), e.g. nvme | `read` | read |
| `move_volume_copies` | Moves the copies as preview_volume_copy_move planned, while the volume stays in use: settings {volume, disk_tags, node_tags, review_token} | `storage:manage` | change |
| `remove_old_volume_copy` | Asks to delete the data a storage-class move left behind | `storage:manage` | destructive |
| `list_cached_images` | Container images pre-pulled onto the hosts | `read` | read |
| `prepull_image` | Pulls an image onto hosts ahead of time, so an app starts fast; nodes to limit which | `storage:manage` | change |
| `stop_prepull` | Stops pre-pulling an image | `storage:manage` | change |

### Networking

| Tool | What it does | Scope | Class |
|---|---|---|---|
| `get_networking` | Services, addresses (VIPs), ports and what listens where | `read` | read |
| `preview_service` | What a Service exposing an app's ports would be, and its review: settings {namespace, name, workload, ports, type, vip_mode, vip} | `network:manage` | change |
| `create_service` | Creates or updates a Service as preview_service reviewed it | `network:manage` | change |
| `get_ip_addresses` | The address plan: subnets, records and what was seen on the network | `read` | read |
| `free_ip_addresses` | Addresses that are free to use | `read` | read |
| `save_ip_record` | Notes what an address is for: settings {ip, name, notes, reserved} | `network:manage` | change |
| `save_ip_records` | Applies the same changes to many addresses | `network:manage` | change |
| `import_ip_records` | Imports address records from CSV text | `network:manage` | change |
| `scan_subnet` | Looks for what answers on a subnet | `network:manage` | change |
| `save_subnets` | The subnets the address plan covers: each {cidr, name} | `network:manage` | change |
| `sync_unifi` | Reads clients and leases from the UniFi controller now | `network:manage` | change |

### Kubernetes objects

| Tool | What it does | Scope | Class |
|---|---|---|---|
| `create_namespace` | Creates a namespace for apps | `cluster:manage` | change |
| `delete_namespace` | Asks to delete an empty namespace | `cluster:manage` | destructive |
| `create_resources` | Creates the Kubernetes objects in a YAML manifest | `cluster:manage` | change |
| `save_resource` | Replaces one object with this YAML | `cluster:manage` | change |
| `delete_resource` | Asks to delete one Kubernetes object | `cluster:manage` | destructive |

## How changes work

A change is made in steps, as in the web app:

1. **Read the settings.** For an edit, `get_container_settings` gives the
   settings as the Edit dialog shows them. `deploy_options` lists what a new
   container can use.
2. **Preview.** `preview_deploy`, `preview_compose`, `preview_edit_container`,
   `preview_move_container`, `preview_create_vm` and `preview_edit_vm` say what
   would happen - what restarts, whether it fits - and change nothing. A preview
   that checks capacity answers with a `capacity_token`.
3. **Apply.** The matching tool takes the preview's `capacity_token`, and
   `confirm_capacity: true` when the review asked for it. If the cluster has
   changed since the preview, Homestead refuses and the agent previews again.

Edits are recorded in the change history and can be undone there.

## Approvals

A destructive tool does not run. It files a **request** that names the action in
a sentence, with the reason the agent gave, and answers with a request id. The
agent follows it with `get_agent_request`.

- The request appears in the **bell** and is **pushed to devices** in the
  **AI agents** notification category.
- It is approved or denied on the **/agents** page, by anyone whose role allows
  the action. Deleting a VM or a volume, and anything else the web app keeps for
  administrators, needs an administrator.
- Changes that need an administrator (host access, privileged mode) are
  requests too.
- On approval Homestead **plans again as the approver** and runs only if the
  plan is unchanged. If the cluster moved on, the request ends as failed and the
  agent can ask again.
- A request expires after **30 minutes**, and a key may have at most **10**
  waiting.

## Security

- **Off until you turn it on**, and `404` while it is off.
- **Keys only.** A browser session never works on `/mcp`, and a key needs the
  **Allow AI agents (MCP)** tick. Like every key it expires, and can be limited
  to addresses.
- **Origin.** A request that carries an Origin must come from Homestead's own.
- **Operator at most.** Whoever made the key, it never acts as an administrator.
  Admin-only actions go through an approval.
- **Scopes decide the tool list.** An agent cannot call what it cannot see.
- **Rate limits**, per key per minute: 120 reads, 20 changes, 5 destructive.
- **Size.** Requests are at most 256 KiB, and there are no batches.
- **App text is data.** Names, events, logs and alerts are written by your apps
  and users, so the agent is told to treat them as data, not instructions. Logs
  come back marked untrusted, with control codes stripped, at most 500 lines.
- **Container commands need admin approval.** `run_container_command` accepts
  an argument array, not an implicit shell string. Its approval names the exact
  command, workload and pod. A changed workload or replacement pod invalidates
  it. Follow `get_agent_request` for stdout, stderr and the exit status; an
  interrupted connection reports unknown completion, not success. The image
  must provide `timeout`; execution is limited to 1–30 seconds, then TERM/KILL,
  and output is capped at 12,000 bytes. Detached children may survive. Commands
  run with the container's existing permissions and may change data or reveal
  secrets, so review every argument before approving.
- **The same feature is in Containers → ⋯ → Diagnostics.** Anyone who can view
  logs can inspect current and previous output and exit details. Administrators
  can expand **Run a command**, review it and run it once. No diagnostic read
  starts or restarts the app. Kubernetes cannot provide deleted pods' logs.
- **Never available to agents:** users, keys and settings; interactive shells;
  Secrets; the file manager; host power and updates; Homestead itself; and linked
  clusters. Hosts are read-only.
- **Audited.** Every call is recorded (below).

Use HTTPS, or limit the key to the client's address: a key sent over plain HTTP
can be read by anyone on the path. Limiting by address works only where
Homestead sees clients' own addresses; the API keys card says when it does not.

## The audit

Every call a key makes is kept for **30 days**: MCP calls, and the changes and
refusals on `/api/v1`. Secrets and environment values are hidden. It is shown in
Settings.

## Troubleshooting

| You see | It means |
|---|---|
| `404` | AI agents (MCP) is switched off in Settings › Access |
| `401` | No key, or an invalid, revoked or expired one |
| `403` | The key was not made with **Allow AI agents (MCP)**, or the request's Origin is not Homestead's own |
| `405` | `GET` was used; MCP clients use `POST` |
| `429` | A rate limit, or too many wrong keys from one address |
| A tool is missing | The key's scopes do not include it; make a key with the scope |
| A request ended as failed | The cluster changed between the ask and the approval, so the plan no longer matched. Ask again |
| A request expired | Nobody answered within 30 minutes |
| `401 … cannot be used from 10.42.0.0` | Homestead sees a node's address, not the client's, so **Only from** cannot match. Make a key without it (see [API](API)) |
