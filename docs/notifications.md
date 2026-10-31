# Alerts and PWA notifications

## Acknowledge a known condition

Open **All alerts** from the notification bell, or **Settings → This device →
Review alerts**. Review the condition and choose **Acknowledge**. Acknowledgement
belongs to the signed-in account and persists across its sessions and devices.
Other accounts still see their own unacknowledged alerts. **Undo** restores the
condition to the attention list.

For example, acknowledging 24 reallocated sectors suppresses that known drive
condition. A count of 25, a new pending-sector warning, a higher severity, or a
changed drive serial brings it back. It must persist for 60 seconds before a new
push is generated. The drive's health warning remains visible throughout.
Closing an operating-system notification does not acknowledge its condition.

Acknowledged conditions remain in a collapsed section. The app badge counts all
confirmed, unacknowledged conditions for the account, independently of which
notification categories a device subscribes to. Events such as failed jobs and
new releases are history, not conditions that can be acknowledged.

Acknowledgements store the observed severity and measurements. If a warning
improves but does not clear, its acknowledged baseline remains in force; it must
exceed that baseline or gain a new problem to return. Once a condition clears
for 60 seconds, a later recurrence is a new incident. Removing an account also
removes its acknowledgements. Submitting an acknowledgement from a stale review
returns a conflict so the user can review the current finding.

## Trigger rules

The leader evaluates sources every 20 seconds. Persistent conditions, worsening
and recovery require 60 seconds of observations. An unavailable source does not
resolve its conditions or count toward that confirmation period. Missing SMART
readings retain the last warning. Unchanged conditions do not repeat on a timer.

| Source | Initial condition or event | Worsening |
| --- | --- | --- |
| Host readiness | Kubernetes reports a host not Ready | A recurrence after confirmed recovery |
| Workload readiness | Existing health classifier reports a fault; normal rollout/startup transitions retain their grace period | Higher severity |
| Volume health | Longhorn reports degraded or faulted | Degraded becomes faulted |
| SMART | Configured drive thresholds are reached; defaults remain 1 reallocated (warning), 1 pending and 1 uncorrectable (critical) | Any increased sector or NVMe media-error count, new finding, higher severity, or changed reported drive serial |
| Drive temperature | Configured warning/critical threshold reached | Higher severity or the next 5°C band |
| Drive endurance/spares | Existing rated-life or available-spare rules report a warning | Each further percentage point lost, or higher severity |
| Backup protection | Covered recurring job failed, or configured backup jobs have no usable destination | Recurrence after confirmed recovery |
| Longhorn disk | Longhorn reports a disk not ready | Recurrence after confirmed recovery |
| Longhorn capacity | Existing allocation warning or blocked scheduling | Scheduling becomes blocked or allocation enters a higher 5-percentage-point band |
| Root filesystem | Host usage reaches 90%; root guard separately reports disabled replica placement | Host usage enters a higher 5-percentage-point band; guard recurrence after recovery |
| Host services | Failed systemd units reported | A newly failed unit |
| Host updates | Security updates, required restart, or automatic restarts without drain reported | Security-update count increases; other conditions recur after recovery |
| Service address | Address has no route | Recurrence after confirmed recovery |
| Failed job | A background operation fails | Once per operation |
| Previous volume | A volume an app was moved off by Restore into app will be deleted within a day (info, under Warnings); a note when it is deleted, or kept because something mounts it again | Clears when it is kept, mounted again or deleted |
| Scheduled job | A CronJob in any namespace: its last run failed (one that runs more than once an hour: no success for 30 minutes), or a run it was due to start has not started 15 minutes after its time (or its own starting deadline, if longer). Paused CronJobs and Longhorn's recurring jobs are not watched here. Runs that could not be read neither raise nor clear it | Recurrence after confirmed recovery |
| Joining host | New Kubernetes registration; subsequently Ready | Once per host identity and stage |
| Host back | A host takes work again and moving containers back would even the hosts out (Settings → Placement → When a host comes back: Suggest or In the update window) | Clears once nothing is worth moving, or a week after the host came back |
| Image update | A new eligible image candidate is discovered | Once per candidate identity |
| Harvester upgrade | New stable release or upgrade start/completion/failure | Once per release or upgrade stage |
| Audit log | `audit.jsonl` on the data volume filled 10 MiB in under an hour (read from the files, so any copy's writes count); clears once a fill is slow again, or a day after the fast one | Recurrence after confirmed recovery |
| Certificate expiry | A TLS Secret, webhook CA, cert-manager Certificate or the API server's certificate expires within 21 days (warning) or 7 days (critical), or has expired; a cert-manager renewal more than a day overdue. One that renews itself (cert-manager, or an operator's short-lived CA such as KubeVirt's and CDI's) alerts only once it has expired. Checked about hourly on the leader; a source that could not be read keeps its alerts | Inside 7 days, inside a day, expired |
| API key expiry | Homestead API keys made to last over 30 days expire within 21 days: one alert for all of them, with no key names or owners | Inside 7 days, inside a day |

SMART findings for the same drive are combined, retaining every reason and the
highest severity. Drive identity uses its reported serial when available; without
a serial the host/device path is the available identity. Ordinary message-text
changes are not worsening signals. Numeric bands avoid temperature and capacity
notifications for every small fluctuation.

The first successful observation seeds existing **events** silently, avoiding
historical job/release floods. Existing **conditions** are still confirmed and
raised. Image checks retain their existing twice-daily background schedule.
Health suggestions (such as two-member quorum or missing backup coverage) remain
read-only guidance; this change does not turn every suggestion into a push.

## Categories and destinations

Critical conditions, warnings, failed jobs and joining hosts are enabled by
default. Updates remain opt-in, per device. Critical raises and worsening request
high push urgency and interaction; operating systems control actual presentation.
Recovery notifications are silent and do not request interaction.

Notifications name the resource, state the observed problem, and point to a
relevant review screen: hosts/drives → Nodes, volumes/capacity → Volumes,
backup protection → Data protection, service addresses → Networking, image and
host updates → Settings / Updates, Harvester upgrade → Cluster, the audit log
filling fast → Settings / About, scheduled jobs → Schedules. Failed jobs keep
their operation destination. They never claim that replicas guarantee availability
or that disappearance of a warning proves a repair was performed.

## Delivery and wording

Notification titles are bounded to 100 characters and bodies to 240, with full
context in the app. More than three pending notifications become one summary,
critical conditions first. Pending history is coalesced to each condition's
latest outcome so an obsolete failure is not displayed after its recovery.

New workers confirm the delivery cursor only after the browser accepts all
notifications for display. A display failure leaves the cursor available for a
later delivery. This is at-least-once delivery: it does not promise automatic
retry or that a person read the notification. Installed older workers retain the
previous cursor protocol during upgrades.

A session failure asks the user to sign in. A connectivity failure says details
are unavailable; neither invents a cluster incident. Recovery wording describes
what is no longer reported, rather than claiming that a host restarted, a drive
was repaired, or an update was installed without evidence.

## Outage webhooks

Push notifications reach the browsers and phones that subscribed. An app or VM
can also call a webhook of your own when it goes down, comes back, is restarted
or runs out of restarts, and when an automatic update of it finishes or is
rolled back: **Monitoring → the app or VM → When it is down → Call a webhook**.

**Format** says how the call is written:

| Format | What is sent | Address |
| --- | --- | --- |
| Generic JSON (the default) | A POST of JSON: `event`, `kind`, `namespace`, `name`, `state`, `since`, `error`, `detail`, `at`, and the sentence as `text`, `content` and `message`, which Slack, Discord and Home Assistant show as it is | The service's webhook address |
| ntfy | The sentence as the message, with `Title: Homestead`, `Priority` high when it goes down or runs out of restarts and default otherwise, and a tag | The topic, such as `https://ntfy.example.com/homestead`; an access token as `?auth=` |
| Gotify | JSON `title` (Homestead), `message` (the sentence) and `priority` (8 when it goes down or runs out of restarts, 5 otherwise) | `https://gotify.example.com/message?token=` and the application's token |

**What stays hidden.** Tokens can sit anywhere after the host: a password
before it, the path (an ntfy topic, a Slack, Discord, Telegram or Home
Assistant token), the query (`?token=`, `?auth=`) or the fragment. So once
saved, the address is only ever shown as its scheme, host and port, with the
rest as `(hidden)` - in the dialog, the app and VM lists, the Resources page,
errors and the record of the last call. Save or test the address as shown and
the kept address is used, but only while the scheme, host and port are
unchanged: change any of them and type the whole address again.

The whole address is kept in a Secret in the app's or VM's namespace,
`homestead-outage-app-<app>` (`homestead-outage-vm-<vm>` for a VM), not on the app
or VM itself, so someone who can read Deployments or VMs in Kubernetes - a
viewer on the Resources page too - never sees a token. The
`homestead.io/outage-actions` annotation keeps only the scheme, host and port,
and the Secret's name. An address with nothing after its host has nothing to
hide and stays on the annotation. Homestead reads the Secret only when it made
it for that webhook, and only while its scheme, host and port match the
annotation's; it is deleted with the app or VM. When the app or VM is deleted
outside Homestead, the leader deletes its Secret once it has been missing
for an hour, with no job on it (each delete is in the pod log and the audit
log). A webhook saved before this
release is moved into its Secret by Homestead after its next monitoring
round, and on its next save. An admin can **Reveal** the Secret on the
Resources page.

**Where a webhook may go.** Homestead looks up every address the host name
resolves to, checks them all, and calls them in turn until one answers - a
host with an IPv6 and an IPv4 address still gets the call when one of them
cannot be reached. All the tries share the ten-second limit, and an address
that answered is never called again for the same message. It never calls this host
(loopback), link-local or cloud metadata addresses. An IPv6 address that
carries an IPv4 one (NAT64, 6to4) is checked as the IPv4 address inside. A LAN or cluster address
(the RFC 1918 private ranges, 100.64.x, IPv6 unique local) - a self-hosted
ntfy or Gotify, say - needs an admin to tick **Allow a LAN address** for that
webhook. An operator can still change the rest of the dialog and keeps the
admin's allowance while the address stays the same; a new LAN address needs an
admin again. No webhook follows a redirect.

**Webhooks saved before this release** keep working. Before, a webhook could
reach a LAN address without anyone allowing it, so once, after the upgrade,
the leading copy of Homestead ticks **Allow a LAN address** for each webhook
saved the old way whose address resolves to a LAN or cluster address. That
covers ones saved from the dialog and by an AI agent alike. It grants only
the LAN: an address a webhook never reaches (loopback, link-local, cloud
metadata and the rest above) stays refused, and a public one is left as it
is. It works only from complete lists of apps and VMs: if either cannot be
read, it waits for the next round.

A name that does not resolve, or an allowance that cannot be saved, is tried
again every five minutes for a day after the first try. After that Homestead
stops and raises a warning for each webhook it left: **The outage webhook for
_app_ needs Allow a LAN address**. One whose address resolves both to the LAN
and to an address never reached gets **The outage webhook for _app_ needs a
new address** instead, since the allowance would not make it reachable. The
warning clears once an admin ticks Allow a LAN address or the webhook is
saved again. What was done, and when it first tried, is kept in the
ConfigMap `homestead-outage-carry`, in Homestead's namespace next to its settings, so a restart or
another copy taking the lead never runs it again.

Known limits of the carry-over:

- It runs once. A webhook in the old shape that arrives after it finished,
  say on an app moved or imported from a cluster on an older release, or an
  annotation that GitOps applies again, gets neither the allowance nor a
  warning. Open its Monitoring dialog and save it (an admin ticks Allow a LAN
  address if it needs one).
- GitOps that manages the `homestead.io/outage-actions` annotation can undo
  the `lan` it set; put `"lan":true` in the annotation kept in git.
- GitOps that keeps a whole webhook address, token and all, in that
  annotation puts it back after Homestead moves it into its Secret, and
  Homestead moves it again each time. Keep the annotation in git as Homestead
  writes it (the address up to its host, then `/(hidden)`, and `"secret"`)
  and the Secret wherever your other secrets live.
- A webhook whose name does not resolve when it is saved is saved as it is:
  it is checked again at each call, and a LAN address is then refused
  without the allowance. Saving it clears the warning above, so tick Allow a
  LAN address when it points at the LAN.

**Send a test** sends one call now, in the format chosen in the dialog, and
says how the service answered. Only an admin can send one, because it makes a
request from inside the cluster to the address typed; it may reach a LAN
address, and at most five a minute go out.

## Alert webhook

Alerts - a host not ready, a drive failing, a backup that failed, an app out
of restarts, and the rest above - can also go to one webhook of your own, such
as ntfy or Gotify: **Settings → Connections → Alert webhook** (admins only).
It is off until set, so nothing changes for anyone who does not set it.

Each alert is sent once, as it is raised, worsens or clears, under the same
trigger rules as push: an unchanged condition is not sent again. Unlike push,
it has no categories to choose and no acknowledgement: an alert someone
acknowledged in Homestead is still sent. **Send** chooses the alerts: critical
only, warnings and critical (the default), or everything, information too.
An alert that was sent is always followed by its clear, even if it eased below
that level first. Device tests are never sent. When more than 10 alerts arrive
at once, the first 10 are sent and one more call says how many were left out.
Calls go out one after another, in order, with `User-Agent: Homestead alerts`.

| Format | What is sent |
| --- | --- |
| Generic JSON (the default) | A POST of JSON: `event` (`alert`), `phase` (raised, worsened or resolved), `severity`, `category`, `key`, `title`, `body`, `href`, `at`, and `title: body` as `text`, `content` and `message` |
| ntfy | The alert's body as the message (its title when it has none), its title as `Title`, `Priority` high for a critical alert, default for a warning or a cleared alert and low for information, and a tag |
| Gotify | JSON `title`, `message` and `priority` (8 critical, 5 a warning or a cleared alert, 2 information) |

The address works as an outage webhook's does (above): it is shown only up to
its host once saved, it never reaches this host, link-local or metadata
addresses, a LAN address needs **Allow a LAN address**, and no redirect is
followed. It is checked again at every call. **Send a test** shares the outage
webhooks' limit of five a minute. The setting is kept in `alert-webhook.json`
on Homestead's data volume, readable only by Homestead, and the last call's
result shows on the card.
