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
   dashboard; elsewhere Longhorn's **backup-target** setting.

   **Set up storage** here instead runs an S3 server in the cluster, on a
   Longhorn volume, and points Longhorn at it. That is meant for
   [moving workloads](Moving-between-clusters) - it lives and dies with the
   cluster it protects, so it is not a backup of anything by itself.
2. **Pick a plan.** **Plans** set up a policy in one go:
   - *Snapshots*: hourly, kept for a day; daily, kept for a week;
   - *Snapshots and backups*: those, plus daily and weekly backups;
   - *Housekeeping*: weekly trim and snapshot cleanup.

   The jobs a plan makes are ordinary jobs afterwards, to change as you like.
3. **Choose the volumes.** A job covers volume *groups*. Every volume is in
   `default`; put a volume in another group to give it a different plan.

## Jobs

A job has a task, a schedule, how many to keep, and the groups it covers.
Schedules are picked as shapes - every few hours, daily, weekdays, monthly -
and written to cron for you. Longhorn keeps time in UTC; the editor shows the
next three runs in your own time. **Run now** starts one at once.

## Restoring

A completed backup has **Restore**. It always restores into a **new** volume -
it never overwrites one - at least as big as the backup, with the copies you
choose. Point the app at the restored volume (**Edit** on its container), or
restore under the original name once the old volume is deleted.

To go back to a snapshot, use Longhorn's own UI (the volume must be detached
first); Homestead lists snapshots but does not roll a volume back.
