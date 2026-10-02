# Browse snapshot files

In a Longhorn volume's **Snapshots** dialog, choose **Browse files** on a ready
snapshot. Review the method, then choose **Prepare browser**. The live claim
stays attached to its workload, including containers and SMB shares. This does
not roll the volume back or stop the workload.

- **V1:** Longhorn copies the selected snapshot into a temporary volume with
  one replica. Preparation consumes disk space and I/O and can take time.
- **V2:** Homestead selects a one-replica **linked clone** when the installed
  Longhorn volume schema and CSI driver version advertise support (Longhorn
  1.10 or later). This shares snapshot blocks and avoids a full data copy.
  Scheduling and mounting still take time, and the clone depends on the source
  snapshot while open. An unrecognized driver image or unsupported schema uses
  the full-copy review instead. A failed linked clone never silently switches
  to copying after approval.

The progress view follows snapshot import, clone creation/copying, and the
read-only mount. A copy percentage appears only when Longhorn reports one;
otherwise the bar is indeterminate. Scheduling, image-pull and clone errors
are shown. Readiness requires Longhorn to report cloning complete from the
selected snapshot, a bound independent volume, and a ready helper pod.

Folders open in the existing file browser. Files download individually without
the live editor's size limit; downloads stream through Homestead in bounded
chunks. There are no write actions. Symbolic links, device nodes and other
special files are excluded; a listing shows up to 500 entries. Files reflect
the snapshot's consistency, which may be crash-consistent for a running app.
This does not open a guest filesystem inside a VM disk image.

Closing the browser (including X or Escape) requests cleanup. Abandoned sessions
expire 30 minutes after preparation starts, including time spent copying.
The helper also has a Kubernetes active deadline. Cleanup resumes after a
Homestead restart and when the API becomes available, removing the helper,
temporary claim, snapshot import and temporary StorageClass in order. Normal
Kubernetes/Longhorn finalizers are respected. The original snapshot import uses
**Retain**, so removing browser resources never deletes the original snapshot.
At most four saved sessions are normally admitted at once.

This is an admin feature for unencrypted Longhorn filesystem claims outside
system namespaces. Ready CSI snapshot support must already be installed.
Raw block volumes and encrypted/custom restore classes require other tooling.
The helper uses Homestead's verified image digest, a read-only volume mount,
read-only root filesystem, no service-account token, and no privileged mode.
It must be possible to pull that image in the source claim's namespace.

Implementation references: [Longhorn CSI snapshot imports](https://longhorn.io/docs/1.13.0/snapshots-and-backups/csi-snapshot-support/csi-volume-snapshot-associated-with-longhorn-snapshot/)
and [V2 linked clones for temporary backup volumes](https://longhorn.io/blog/20250902-k8s-backup-solutions-and-longhorn/).
