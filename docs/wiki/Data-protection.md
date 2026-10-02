# Data protection

When setting up backup storage or changing its address, Homestead saves the
requested Longhorn target switch and retries while the store starts. Progress
and the last error appear on Data protection and in the linked cluster's
migration controls. The request survives a Homestead restart. After ten minutes
it stops retrying and offers **Retry target switch**; **Storage settings** lets
you correct the address and port. Retrying the target switch does not redeploy
the store. Homestead verifies the actual S3 endpoint, as well as the bucket URL,
before showing that Longhorn is pointed at the store.

## Snapshot timeline and cleanup

The snapshot dialog shows local creation times, relative ages, and separate
**User**, **Scheduled** (identified by recurring-job labels), and **System**
checkpoints. Unknown sources stay unknown. The timeline is chronological, not
a claim that snapshots form a single dependency chain after rollback.
**Volume Head** is live data and has no snapshot-delete action.

Deleting a recovery point or cleaning up a system checkpoint requires an impact
review and exact-name confirmation. A persistent job follows the request,
Longhorn merge/purge progress and verified disappearance from the snapshot and
engine inventories. It survives refresh/restart, reports errors and offers
**Carry on** after a timeout; removal cannot be cancelled or undone. Reported
purge percentages are Longhorn's, not estimates of bytes reclaimed.

Longhorn can retain a removed snapshot while it is the direct parent of Volume
Head. The job explains this wait: taking a fresh snapshot creates a boundary
that can allow merging to finish. Homestead never creates one automatically,
force-detaches the volume, or strips finalizers. Purge may clean other already
removed/eligible system checkpoints on the same volume. Shared blocks can remain,
so snapshot size is not a guarantee of space freed. See the
[Longhorn snapshot controller](https://github.com/longhorn/longhorn-manager/blob/v1.12.1/controller/snapshot_controller.go).

**Data protection** runs Longhorn's own recurring jobs - snapshots, backups,
trims and cleanups - so they keep running whether or not Homestead is.

![Data protection](https://github.com/wjcloudy/homestead/releases/latest/download/homestead-data-protection.jpg)

## Snapshots or backups?

- A **snapshot** is a point in time kept *inside* the volume, on the same disks.
  It is instant and cheap, and undoes a bad update or a deleted file - but it
  dies with the volume.
- A **backup** is a copy *outside* the cluster, on backup storage (S3 or NFS).
  It survives losing the volume, the host, or the cluster.

Have both.

## Setting it up

1. **Backup storage.** Point Longhorn at somewhere to keep backups - an S3
   bucket (a NAS running MinIO, RustFS or Garage; Backblaze B2; AWS) or an NFS
   share, with **Backup target** here. For S3, type the access key, secret key
   and - for anything but AWS - the endpoint, and Homestead keeps them in a
   Secret for Longhorn. If that Secret goes missing, the target card says so.
   On Harvester it is saved as Harvester's own **backup-target** setting, the
   same one its dashboard sets. Harvester passes it to Longhorn and uses it
   for VM backups, and would reset one set in Longhorn alone. Harvester takes
   NFS or S3, and writes to the top of an S3 bucket (`s3://bucket@region`).

   **Set up storage** here instead runs an S3 server in the cluster, on a
   Longhorn volume, and points Longhorn at it. That is meant for
   [moving workloads](Moving-between-clusters) - it lives and dies with the
   cluster it protects, so it is not a backup of anything by itself.
2. **Pick a plan.** **Plans** set up a policy in one go:
   - *Snapshots*: hourly, kept for a day; daily, kept for a week;
   - *Snapshots and backups*: those, plus daily and weekly backups;
   - *Housekeeping*: weekly trim and snapshot cleanup.

   The jobs a plan makes are ordinary jobs afterwards, to change as you like.
   A plan for `default` makes jobs with plain names (`daily-snapshot`); a plan
   for another group names them after it (`media-daily-snapshot`), so it never
   takes over the jobs protecting everything else.
3. **Choose the volumes.** A job covers volume *groups*. Every volume with no
   other group is in `default`; put some in a group of their own to give them
   a different plan.

## Groups

**＋ New group** asks for a name, the volumes in it (filter by name or
namespace), the jobs that protect it, and - if you like - a plan to set up for
it. **Edit** on a group changes any of those, its name included; **Delete**
takes it off its volumes and out of its jobs, and keeps every snapshot and
backup already taken.

Longhorn puts a volume in `default` only while it has no other group or job,
and leaves it there when it joins one - so it would get both groups' jobs.
Joining a group here takes a volume out of `default` unless you untick **Take
volumes out of default**. A volume that leaves its only group goes back to
`default`; one with no other group cannot leave `default`, because Longhorn
would put it straight back.

**Protect** on a volume shows its groups and the jobs set on it directly,
and can put it in a new group. When a claim carries Longhorn's
`recurring-job.longhorn.io/source` label, Longhorn copies the claim's groups
onto the volume, so Homestead changes the claim as well.

**Coverage** counts volumes a snapshot or backup job covers - a trim or a
cleanup keeps no copy - and how many of them are backed up off the cluster.
**Protected by** in the volume list names those jobs, and says *nothing* for a
volume none of them covers.

## Jobs

A job has a task, a schedule, how many to keep, and the groups it covers.
Trims and cleanups keep nothing, so they have no count.
Schedules are picked as shapes - every few hours, daily, weekdays, monthly -
and written to cron for you. Longhorn keeps time in UTC; the editor shows the
next three runs in your own time. **Run now** starts one at once.

### Weekly trim, by default

Files deleted inside a volume leave their space allocated in Longhorn until
the filesystem is trimmed. Media, databases and VM disks can hold far more
than their files use. So Homestead sets up **homestead-weekly-trim** for every
volume: Saturdays at 04:00 UTC, in the default group and in every group a
volume is in, kept in step as groups come and go.

- You can change its schedule, and your change is kept.
- If you delete it, it isn't made again.
- If you already have a trim job for the default group, Homestead doesn't add
  a second.
- Its **Run now** trims every volume at once.
- A single volume's snapshot panel has **Trim now**.

Longhorn trims a volume only while it's attached. A stopped app's volume is
trimmed the next time it runs.

A snapshot or backup job whose last run failed, or backup jobs with no
backup target (or one that cannot be reached), make the cluster
**degraded** on the Dashboard and raise an alert. Its **Review** button leads
here.

## Restoring

**Backups** lists every volume the backup target holds backups of, including
volumes that have since been deleted - which is when you most need them.
**Backups** on a row lists them, each with **Restore** and **✕** (delete it
from the backup target; it cannot be restored afterwards).

A completed backup has **Restore**. It always restores into a **new** volume -
it never overwrites one - using the configured Longhorn storage class you
choose. Its replica count and other provisioning settings come from that class,
and the PVC keeps the same class after restoring. The backup is imported through
a retained CSI snapshot; cleanup removes import metadata, never the backup.
A larger destination restores at the original size first, then expands after
the data is healthy. Its class must allow expansion. If filesystem resizing
needs a mount, the job says that it will finish when the volume is next used.
Point the app at the restored volume (**Edit** on its container), or
restore under the original name once the old volume is deleted.

### Rolling back to a snapshot

The snapshot dialog refreshes the timeline and Longhorn cleanup status every four
seconds while open. During purge it shows the slowest active replica's percentage.
This is volume-wide merge/purge work, not a percentage for an individual snapshot,
and includes cleanup started outside Homestead. Missing status is shown as unknown,
not completed. A snapshot marked for removal may still await a new head boundary.

Deletion started in Homestead also creates a persistent cleanup job in Recent jobs.
An external deletion does not retroactively create a Homestead job; its live Longhorn
progress is still visible in the snapshot dialog.

**Snapshots** on a volume lists them, each with **Roll back**, which puts the
volume back as it was then. Longhorn only reverts a volume nothing is using,
so Homestead:

1. stops what uses it (containers, VMs, scheduled jobs), noting how each ran;
2. waits for the volume to detach, then attaches it in maintenance mode;
3. keeps the present state as a snapshot of its own (`before-rollback-…`),
   so rolling forward again is one more **Roll back**;
4. reverts to the snapshot, detaches, and starts everything again as it was.

The job tray follows each step. If the volume will not let go, nothing is
changed and everything starts again. Homestead's own data cannot be rolled
back from Homestead, as that would stop it part-way.
