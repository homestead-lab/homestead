# Data protection

**Data protection** runs Longhorn's own recurring jobs - snapshots, backups,
trims and cleanups - so they keep running whether or not Homestead is.

![Data protection](https://github.com/wjcloudy/homestead/releases/latest/download/homestead-data-protection.png)

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
   share. On Harvester this is **Settings → backup-target** in Harvester's
   dashboard; elsewhere **Backup target** here. For S3, type the access key,
   secret key and - for anything but AWS - the endpoint, and Homestead keeps
   them in a Secret for Longhorn. If that Secret goes missing, the target card
   says so.

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

## Restoring

**Backups** lists every volume the backup target holds backups of, including
volumes that have since been deleted - which is when you most need them.
**Backups** on a row lists them, each with **Restore** and **✕** (delete it
from the backup target; it cannot be restored afterwards).

A completed backup has **Restore**. It always restores into a **new** volume -
it never overwrites one - at least as big as the backup, with the copies you
choose. Point the app at the restored volume (**Edit** on its container), or
restore under the original name once the old volume is deleted.

To go back to a snapshot, use Longhorn's own UI (the volume must be detached
first); Homestead lists snapshots but does not roll a volume back.
