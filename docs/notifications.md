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
| Joining host | New Kubernetes registration; subsequently Ready | Once per host identity and stage |
| Image update | A new eligible image candidate is discovered | Once per candidate identity |
| Harvester upgrade | New stable release or upgrade start/completion/failure | Once per release or upgrade stage |

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
host updates → Settings / Updates, Harvester upgrade → Cluster. Failed jobs keep
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
