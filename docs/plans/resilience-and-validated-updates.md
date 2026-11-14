# Spec: Homestead always comes back, and risky updates are validated first

Status: agreed, decisions recorded in section 8. Written 2026-11-10 after an
outage on a single-host test cluster.

## 1. Background

Two separate failures happened on the same day.

1. **A kernel update broke VMs.** Ubuntu's kernel `7.0.0-43` started converting
   unprivileged `change_profile` calls to AppArmor stacks
   (`change_profile unprivileged unconfined converted to stacking`). Processes
   in the virt-launcher pod ended up with two different labels
   (`cri-containerd.apparmor.d` and `cri-containerd.apparmor.d//&unconfined`),
   and containerd's profile refused signals between them. QEMU could not signal
   its own vCPU threads (`qemu:cpus_kick_thread: Permission denied`) and exited.
   KubeVirt recreated the instance for ever, and Homestead's start job cycled
   between 35% and 60% without showing the error. Booting `7.0.0-38` fixed it.
   The trigger is the sysctl `kernel.apparmor_restrict_unprivileged_unconfined`,
   which Ubuntu sets to `1` in `/usr/lib/sysctl.d/10-apparmor.conf`. `-38`
   ignores it in this case, and `-43` acts on it.
2. **An undrained restart left Homestead down.** The host was restarted with a
   plain `reboot`, so shared (RWX) volumes went down attached. Longhorn then
   marked their only copies as failed and needed auto-salvage. Auto-salvage only
   uses copies on a *schedulable* disk, and the disk holding Homestead's data had
   been under Longhorn DiskPressure (less than 25% free) for a day. Auto-salvage
   logged "Bringing up 0 replicas" for ever, and Homestead's pod waited on its
   mount. Lowering `storage-minimal-available-percentage` to 20% let the salvage
   run. Homestead's data was then moved to the NVMe system disk with **Move
   copies**, live.

Nothing told the operator either problem was coming, and nothing outside
Homestead could explain or fix the second one while Homestead was down.

## 2. Goals

- **Homestead always starts** after any restart, clean or not, including when a
  disk is under pressure or Longhorn itself is unhealthy.
- When it cannot, **something on the host says why** in one sentence and offers
  the fix, and in the narrow, safe cases fixes it without a person.
- **Risky updates are validated first.** Kernels and platform components can be
  limited to versions that have passed Homestead's own tests, the list of those
  versions stays current without manual weekly edits, and a bad version that
  slips through is recognised and reversible.
- All of this works on **every distribution Homestead supports**, with clear
  tiers where a distribution is not yet validated.

## 3. Homestead's own data

### 3.1 Placement and volume type

Reuses the self-data move (`server/homestead_self_data_*`,
`server/homestead_data_copies.py`) and the volume copy move
(`server/homestead_copy_move.py`).

| Cluster | Homestead's data | Why |
|---|---|---|
| One host, new install | `local-path` on the system disk (the default), or Longhorn RWO pinned to the system disk | Homestead then depends only on the host and k3s, and starts even when Longhorn is broken |
| One host, Longhorn chosen | Longhorn **ReadWriteOnce**, disk selector for the system disk (tagged `os` by the installer) | No share-manager, and the copy shares fate with the host rather than with a second disk |
| Two or three copies of Homestead | Longhorn **ReadWriteMany**, two or three copies on different hosts | Sharing is needed only when several copies run |

- **Growing the cluster (automatic):** once a second host is Ready and Longhorn
  can store a copy on it, Homestead moves its data from `local-path` to Longhorn
  by itself, with the self-data move. It waits for a quiet moment (no jobs
  running, outside the maintenance window's work), takes an encrypted
  configuration backup first, and restarts once, so Homestead is unavailable for
  about a minute. An alert says before and after what it did. If the move cannot
  run (no room, Longhorn not healthy), it raises an alert with the reason and
  tries again later. Raising **Redundancy** above one copy moves the data to a
  shareable Longhorn class the same way, with the usual review.
- **Trade-off of `local-path`:** Longhorn snapshots and backups do not cover
  Homestead's data. Its encrypted configuration backup still does.
- **Existing installs:** a health check flags Homestead's data when it is RWX with
  one copy, or has its only copy on a disk that is not the system disk or is under
  pressure. It offers **Move copies** to the system disk (proven live on
  2026-11-10), the RWO switch, or `local-path` on a single host, each through a
  reviewed move. Apart from the automatic move to Longhorn when the cluster grows,
  Homestead never moves its own data without a person's review.
- Files: `deploy/deploy.yaml`, the Helm chart, `scripts/install.sh`,
  `scripts/bootstrap-k3s.sh`. Tests: `test_manifest_class`, `test_deploy`.

### 3.2 Warning before an outage

`server/homestead_lhcapacity.py`, alerts and the Dashboard health list.

- An alert when a volume's only healthy copy is on a disk where Longhorn's
  auto-salvage would refuse it (`Schedulable=False`, including DiskPressure).
  Critical for Homestead's own data, a warning for app and VM volumes.
- First sentence: "After an unclean restart, *volume* will not come back on its
  own." The full text names the disk, the free space and Longhorn's threshold,
  and links to freeing space, **Move copies**, or the threshold setting.
- The OS update rollout and Host actions' restart review list these volumes, so a
  planned restart never surprises anyone.

### 3.3 Watchdog: recovery without a person

Placement removes the 2026-11-10 path, but not every path: the system disk can be
under DiskPressure too.

- **Watcher:** the node probe DaemonSet, which has no Longhorn volume and kept
  running through the outage.
- **Trigger, and nothing else:** Homestead's pod waiting on its data volume, that
  volume faulted, and Longhorn's auto-salvage refusing it.
- **Action:** salvage that one volume with Longhorn's own salvage action, which
  does not apply the free-space rule. No cluster-wide setting is changed, and no
  other volume is touched.
- **Record:** a line in the node probe's log, a Kubernetes event, and an alert
  once Homestead is back ("Homestead's data volume was salvaged after a restart:
  disk *path* is below Longhorn's free space threshold").
- **Limits:** at most once per boot of the host, and never while a self-data move
  or restore holds the volume.

### 3.4 Recovery by a person

- **Node doctor** (`scripts/install.sh`, the `doctor_*` checks) finds Homestead's
  pod waiting on its volume and reads why:
  - volume faulted, and auto-salvage blocked by a disk under pressure: offer to
    salvage it, or to lower the threshold until the salvage finishes and then
    restore it;
  - engine or copy stuck in `unknown` after a restart: offer to scale Homestead
    to zero, wait for the detach, and scale it back.

  Each fix is shown in full and runs only after a yes. Unattended runs print the
  commands instead.
- **Host console:** one line when Homestead's data volume is faulted: "Homestead
  cannot start: its data volume is faulted. Run Check node health."
- **Troubleshooting wiki:** a section, "Homestead does not come back after a
  restart", with the same cases and commands.

## 4. VM start fails fast

`server/homestead_vm_power_job.py`.

- On the first `The VirtualMachineInstance crashed` event, or the VM's
  `CrashLoopBackOff` status, the job stops, puts the VM's run strategy back as the
  job found it, and fails with KubeVirt's `SyncFailed` message: a short first
  sentence, with the full QEMU text behind the expand.
- Known patterns get a cause. `Permission denied` together with AppArmor
  `DENIED … signal` lines for `qemu-kvm` names the kernel and points to the
  kernel's status (section 5) and the workaround (section 6).

## 5. Validated versions of risky updates

### 5.1 What counts as risky

- **Host:** kernels, and the packages that change kernel or confinement
  behaviour: `apparmor`, the package that ships
  `/usr/lib/sysctl.d/10-apparmor.conf`, `linux-firmware`, and their equivalents
  on other distributions (for example `container-selinux`).
- **Platform:** k3s and RKE2 (which bundle containerd), Longhorn, KubeVirt, CDI.

Ordinary packages keep updating as they do now.

### 5.2 The validated-versions list

A signed JSON document, `validated-versions.json`:

```json
{
  "generated": "2026-11-10T00:00:00Z",
  "signature": "…",
  "host": {
    "ubuntu-26.04-amd64": [
      {"package": "linux-image-7.0.0-38-generic", "version": "7.0.0-38.38",
       "status": "validated", "tested": "2026-10-03", "evidence": "<CI run URL>"},
      {"package": "linux-image-7.0.0-43-generic", "version": "7.0.0-43.43",
       "status": "bad", "affects": ["kubevirt"],
       "reason": "AppArmor stacking breaks QEMU vCPU signals",
       "workaround": {"sysctl": {"kernel.apparmor_restrict_unprivileged_unconfined": "0"}},
       "evidence": "<issue URL>"}
    ]
  },
  "platform": {
    "k3s": [{"version": "v1.37.1+k3s1", "status": "validated", "evidence": "…"}],
    "rke2": [], "longhorn": [], "kubevirt": [], "cdi": []
  },
  "images": {"talos": [], "flatcar": []}
}
```

- **Statuses:** `validated` (passed the suite), `bad` (known to break something,
  with the reason, what it `affects`, and any workaround), `untested` (seen
  upstream, not yet run). A version missing from the list is `untested`, never
  `validated`, as in `server/homestead_upgrade_known.py`.
- **Keys:** host entries are per distribution, release and architecture.
  Immutable OSes are listed by OS image version under `images`.

### 5.3 How the update mechanisms use it

**Settings → Updates** gets one choice per kind: **Validated versions only**
(recommended default for new installs) or **Any version** (today's behaviour).

- **Host kernels** (`server/homestead_os_rollout.py`):
  - Homestead holds the kernel and the other risky host packages, so that nothing
    else installs them (unattended-upgrades, dnf-automatic, a plain upgrade).
  - A rollout installs the newest *validated* version by exact name, keeps the
    previous validated kernel installed as the fallback, and sets the boot default
    to the validated kernel.
  - The Host OS card shows the running kernel's status: validated, untested or
    bad.
- **A bad version that is already installed** (a host on **Any version**, a
  distribution outside the validated tier, or a version found bad after it was
  installed):
  - an alert names the version, the reason and what it affects;
  - **Boot the last validated kernel** sets the boot default and restarts through
    the normal drain;
  - a listed workaround is offered on the Host OS card, and applied by the leader
    once a person accepts it.
- **Platform components** (`server/homestead_upgrades.py`,
  `server/homestead_upgrade_check.py`): with **Validated versions only**, the
  next-version offer skips anything not `validated`, and names the newer versions
  that exist but are untested or bad.
- **Security fixes:** with **Validated versions only**, a security update waits
  for the harness. Target: a verdict within 24 hours of the package being
  published. A version that failed only for a feature the cluster does not use
  (for example, `affects: ["kubevirt"]` on a cluster with no VMs) can be offered
  with a clear note.
- **Canary on multi-host clusters:** the first host updated runs the local smoke
  checks (5.4, steps that need no CI) before the rollout continues. A failure
  stops the rollout and leaves the other hosts on their current versions.

### 5.4 Validating a version

A test harness, run in CI on a schedule and on demand:

1. Boots a fresh VM image of each validated distribution with the candidate host
   version, or a cluster with the candidate platform version.
2. Installs with **the same installer, defaults and versions that users get**,
   never a hand-built cluster. (The 2026-11-10 failure needed KubeVirt's non-root
   launcher, containerd's AppArmor profile and Ubuntu's sysctl defaults together.)
3. Runs the smoke suite:
   - k3s or RKE2 Ready, Homestead Ready;
   - Longhorn RWO and RWX volumes attach, write and read back;
   - an **unclean restart** (power off without a drain), then Homestead and every
     volume back without help, including with a disk under DiskPressure;
   - a KubeVirt VM boots and answers on its console, which catches the QEMU,
     AppArmor and SELinux class of failure;
   - a CDI disk import;
   - snapshot, backup and restore of one volume;
   - the node probe reports temperatures and drives.
4. Records the result with a link to the run and the features each failed check
   affects, and opens a pull request that changes the version's status.

The harness runs in CI with nested virtualisation only. GPU passthrough and
real-disk behaviour are not covered: the list says so for every version
(`"not_covered": ["gpu-passthrough", "real-disks"]`), and the Host OS card
shows it beside a validated kernel.

### 5.5 Keeping the list current

- A scheduled workflow (daily) reads new candidates: each validated distribution's
  archive (kernel and the other risky host packages), the k3s and RKE2 release
  channels, and the GitHub releases of Longhorn, KubeVirt and CDI.
- Each new version is added as `untested`, and the harness runs it. A pass opens
  a pull request to mark it `validated`. A failure opens an issue with the output
  and marks it `bad`.
- A maintainer merges. The workflow then publishes the signed list as a release
  asset (`latest/download/validated-versions.json`), separately from Homestead
  releases, so a newly validated kernel does not need a Homestead release.
- Homestead fetches the list twice a day, checks the signature, caches it on its
  data volume, and falls back to the copy bundled in its image when offline. A
  list older than 30 days is shown as stale on **Settings → Updates**.
- Optional later: clusters may share anonymous results of their own canary checks
  (version, pass or fail, no names or addresses), to flag a bad version sooner.

### 5.6 Distributions

- **One adapter per package manager** for the host steps. Homestead's OS updates
  already support apt, dnf and zypper.

  | Step | Ubuntu/Debian | Rocky/Alma | openSUSE/SLES |
  |---|---|---|---|
  | Hold | `apt-mark hold`, unattended-upgrades `Package-Blacklist` | `dnf versionlock`, `exclude=kernel*` for dnf-automatic | `zypper addlock` |
  | Install an exact version | `apt-get install linux-image-<ver>` | `dnf install kernel-<ver>` | `zypper install kernel-default-<ver>` |
  | Keep the fallback installed | `apt-mark manual` | `installonly_limit` | `multiversion.kernels` |
  | Set the boot default | `/etc/default/grub` + `update-grub` | `grubby --set-default` | `grub2-set-default` (mind snapper) |
  | Read the running version | `uname -r` + `dpkg` | `uname -r` + `rpm` | `uname -r` + `rpm` |

- **Support tiers.** A distribution the harness covers can use **Validated
  versions only** for host packages. On any other distribution, Homestead says
  "kernel versions are not validated on this distribution" and uses the **Any
  version** behaviour for host packages, while platform versions stay validated.
  Ubuntu LTS is the first validated tier. Debian and Rocky follow once the harness
  covers them.
- **Immutable OSes** (Talos, Flatcar, Fedora CoreOS, SLE Micro): the kernel comes
  with the OS image and Homestead installs no packages. "Validated" means a
  validated OS image version, and Homestead reports its status only.

## 6. Confinement workarounds

- **AppArmor** (Ubuntu, Debian, openSUSE): confirm on a test host by booting the
  affected kernel with `kernel.apparmor_restrict_unprivileged_unconfined = 0` and
  starting a VM. If it works, Homestead writes it to
  `/etc/sysctl.d/90-homestead.conf` on hosts that run KubeVirt (the leader already
  writes this file for inotify limits), and the node doctor checks for it. It is
  recorded as the `workaround` on the bad version's entry, and reported upstream
  (Launchpad, `linux` and `apparmor`).
- **SELinux** (Rocky, Alma): its own failure modes for QEMU in containers (for
  example `container-selinux` policy changes) get their own checks and fixes. The
  harness's VM-boot check catches both.

## 7. Order of work

| Step | What | Size |
|---|---|---|
| 1 | VM fail-fast (4), warning (3.2) | Small |
| 2 | Placement for new and existing installs (3.1), watchdog (3.3), node doctor and host console (3.4) | Medium |
| 3 | Confinement workarounds (6), after the test on the affected kernel | Small |
| 4 | `local-path` for single hosts (3.1) | Medium |
| 5 | Validated versions (5.1 to 5.5) with the Ubuntu tier first (5.6) | Large |
| 6 | Canary checks on multi-host clusters (5.3) | Medium |

## 8. Decisions

Agreed on 2026-11-10.

| # | Question | Decision |
|---|---|---|
| 1 | Existing installs: move Homestead's data automatically, or offer it? | **Offer** the move, with a review |
| 2 | Node doctor: run the fix, or only print the commands? | **Run it after a yes**. Unattended runs print the commands |
| 3 | Default for new installs | **Validated versions only** |
| 4 | Harness | **CI with nested virtualisation only**. GPU passthrough and real disks are marked as not covered |
| 5 | Signing the list | **Reuse the release signing key** |
| 6 | Watchdog | **Allowed** to salvage Homestead's data volume without a person, within the limits in 3.3 |
| 7 | Single host | **`local-path` by default**, moved to Longhorn **automatically** once the cluster grows past one host (3.1) |
| 8 | Security fixes | **24-hour turnaround accepted**, and clusters that do not use the affected feature may take a version that failed only for that feature |
