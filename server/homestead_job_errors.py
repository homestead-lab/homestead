"""How long a job may go on with a resolver that only fails.

The jobs loop asks every active job's resolver every few seconds. A resolver
that raises (anything but a 404) leaves the job running with "Status
temporarily unavailable", so a passing API error is waited out. One that
keeps raising - a host that went away, a refusal that will not clear, a bug -
used to leave the job like that for ever: it kept every start() guard, blocked
a cluster shutdown and every data move, and nothing said so.

Now, once a job's resolver has failed without a break for longer than its
kind's limit, the job ends one of two ways:

* FAILS: it fails with the last error. Failing lets go of its start() guards,
  which is safe for these kinds: what they follow runs on by itself, or holds
  nothing Homestead must put back.
* KEEPS_GUARDS: a kind that moves or deletes data, or holds a host or an app,
  keeps running, and so keeps its guards: letting go of them could start
  another job on data still in motion. Its message says it needs attention,
  why it keeps its place, and what a person can do. It is asked again once a
  minute, not every round, and carries on by itself once its resolver answers.

A kind neither table names keeps its guards (the safe side); a ratchet test
holds every kind in one of them.

That limit is for a resolver that fails. A resolver that answers "running"
for ever is another matter: DEADLINES names, for each kind, the number that
ends its wait, and NO_DEADLINE says why a kind has none. Nothing here reads
them: tests/test_job_deadlines.py holds every kind in one of the two, and
checks each named number is still there.
"""
import time

clock = time.time
# Kept on the job item, beside its ref and never inside it (refs are hashed
# into reviews and compared with plans saved on hosts). Left out of the page's
# view. The item is saved whole, so a restart keeps the count.
KEY = "resolver_errors"
DEFAULT_LIMIT = 30 * 60
# Kinds whose resolver may fail for longer than DEFAULT_LIMIT and still finish.
LIMITS = {
    # An OS update installs for up to 2 h; its resolver checks that deadline
    # only once the host answers, so it is not given up before it (and its
    # host hold, homestead_host_os.UPGRADE_HOLD).
    "host-os": 2 * 3600 + 600,
}
# A job that needs attention is asked again this often.
RETRY_EVERY = 60
# A failure this long after the one before starts a new streak: Homestead was
# down (a power cut, its pod rescheduled) or the clock stepped forward, so the
# time between says nothing about the resolver. The latest failure's time is
# saved at most once per LAST_EVERY, so a failing job is not written every round.
GAP = 5 * 60
LAST_EVERY = 60

# kind -> why failing it after its limit lets go of nothing still in motion.
FAILS = {
    "addon-remove": "each step stands alone: what was removed stays removed, and the rest stays as it is",
    "cluster-cleanup": "it holds nothing once it has started",
    "deployment": "it only follows a rollout Kubernetes runs",
    "harvester-upgrade": "Harvester runs and follows its own upgrade",
    "harvester-uplink": "Harvester makes the network change, and Homestead only follows it",
    "helm": "it only follows a job the Helm controller runs",
    "host-console": "it installs or removes the host console add-on, which holds no data",
    "host-os": "the host installs its updates by itself, and the host hold lapses on its own",
    "image-cleanup": "it removes images nothing uses",
    "image-pull": "it only follows image downloads, and cancelling it removes its download helpers",
    "image-rollback": "it only follows a rollout Kubernetes runs",
    "image-update": "it only follows a rollout Kubernetes runs",
    "multus": "it only follows an add-on install",
    "network-service": "it only follows a Service's endpoints",
    "onboard": "it holds nothing once it has started",
    "os-rollout": "the rollout ends itself and lets go of its holds (homestead_os_rollout)",
    "set-priority": "each app's change stands alone, and the rest are left as they are",
    "set-requests": "each app's change stands alone, and the rest are left as they are",
    "smart-test": "the drive runs its self-test by itself",
    "vm-migration": "KubeVirt runs and finishes the migration by itself",
    "vm-power": "its power request is sent once and its receipt is kept",
    "weekly-trim": "a trim only frees unused blocks, and next week's trims again",
}

# kind -> what is still in motion, so its guards stay (said in the message).
KEEPS_GUARDS = {
    "auto-update": "it may still roll an app back to its snapshot",
    "backup": "a backup may still be reading its snapshot",
    "cluster-shutdown": "apps and hosts are being stopped",
    "container-rebalance": "apps are being moved between hosts",
    "disk-allocation": "a disk is being grown",
    "disk-retire": "replicas are leaving a disk",
    "disk-v2-convert": "a disk's data is being moved off it",
    "host-bond": "a host's network is being changed",
    "host-bridge": "a host's network is being changed",
    "host-move": "an app is being moved to another host",
    "host-promote": "a host is being made a server",
    "import": "data is being copied into a disk",
    "import-create": "data is being copied into a disk",
    "k3s-cluster": "cluster VMs are being made",
    "longhorn-runtime": "a host's Longhorn data is being moved",
    "longhorn-v2-cleanup": "hosts may be cordoned for Longhorn V2",
    "longhorn-v2-prepare": "hosts may be cordoned for Longhorn V2",
    "move": "an app and its data are being moved to another cluster",
    "node-power": "a host is being drained and its apps may be held",
    "protect-run": "a backup may still be reading its snapshot",
    "platform-upgrade": "the cluster's hosts are being upgraded",
    "reclass": "volume data is being copied",
    "restore-swap": "an app's volumes are being restored",
    "restore-test": "a test copy of an app runs on restored volumes",
    "restructure": "volume data is being copied",
    "self-data-handoff": "Homestead's own data is being moved",
    "self-data-move": "Homestead's own data is being moved",
    "self-data-prepare": "Homestead's own data is being moved",
    "set-restart": "a pod and its volume are being moved to another host",
    "share-remove": "a share and its data are being removed",
    "snapshot-delete": "a snapshot is being removed",
    "snapshot-revert": "a volume is being put back to a snapshot",
    "unraid-vm-import": "data is being copied into a disk",
    "vm-create": "a VM and its disks are being made",
    "vm-disk-import": "data is being copied into a disk",
    "vm-disk-remove": "a disk is being removed",
    "vm-edit": "a VM's disks may be changing",
    "volume-copy-move": "volume data is being copied",
    "volume-delete": "a volume is being deleted",
    "volume-rebalance": "volume replicas are being moved",
    "volume-restore": "a volume is being restored",
    "workload-copy": "volume data is being copied",
    "workload-rename": "an app is being renamed with its volumes",
}

# kind -> the number that ends its "running": (module, constant), or
# (module, function, seconds) for a number written in that function. A
# comment names a wait the number does not cover.
DEADLINES = {
    "addon-remove": ("homestead_addon_removal", "WAIT_LIMIT"),  # each step's wait; counted while Homestead looks (GAP)
    "auto-update": ("homestead_autoupdate", "ROLLOUT_WAIT"),  # then ANSWER_WAIT
    "cluster-shutdown": ("homestead_cluster_shutdown", "LIFETIME"),
    "container-rebalance": ("homestead_container_rebalance", "status", 600),  # each app's restart; pulls: PULL_WAIT
    "disk-allocation": ("homestead_disk_allocation", "_job_body", 900),  # the helper Job's activeDeadlineSeconds
    "disk-retire": ("homestead_disks", "UNCONFIRMED_WAIT"),  # and LETTING_GO_WAIT
    # The helper Job's activeDeadlineSeconds. The erase review waits for a person.
    "disk-v2-convert": ("homestead_disk_v2", "_job_body", 600),
    "harvester-uplink": ("homestead_uplinks", "WAIT"),
    # The unconfirmed stage waits for a person (it says the job needs attention).
    "host-bond": ("homestead_host_bonds", "RESTART_SECONDS"),
    "host-bridge": ("homestead_host_bridge", "RESTART_SECONDS"),
    # The pull; then the rollout ends on the Deployment's progressDeadlineSeconds.
    "host-move": ("homestead_host_move", "PULL_WAIT"),
    "host-os": ("homestead_host_os", "_step", 7200),
    "host-promote": ("homestead_promote", "RUN_LIMIT"),  # and JOIN_LIMIT
    "image-cleanup": ("homestead_image_cache", "CLEAN_DEADLINE"),  # the pods' activeDeadlineSeconds
    "image-pull": ("homestead_image_cache", "PULL_STUCK_FOR"),  # a failing pull; a slow one is never failed
    "import-create": ("homestead_copy_job", "DEADLINE_CEILING"),  # the copy Job; setup waits for Check outcome
    "k3s-cluster": ("homestead_k3scluster", "START_LIMIT"),
    "longhorn-v2-prepare": ("homestead_lhv2_setup", "_body", 900),  # the helper Job's activeDeadlineSeconds
    "multus": ("homestead_addons", "multus_progress", 1200),
    # A host that is off with apps waiting for it waits for the host or a person.
    "node-power": ("homestead_power", "_status", 600),
    "os-rollout": ("homestead_os_rollout", "INSTALL_LIMIT"),  # and RESTART_LIMIT, ERROR_LIMIT
    "platform-upgrade": ("homestead_components", "NODE_WAIT"),  # and CONTROLLER_WAIT, JOB_FINISH_WAIT
    "reclass": ("homestead_reclass_handoff", "WAITS"),  # each wait's limit; a move from before: reclass START_LIMIT
    "restore-swap": ("homestead_restore_swap", "STOP_WAIT"),  # then autoupdate ROLLOUT_WAIT, ANSWER_WAIT
    "restore-test": ("homestead_restore_test", "resolve", 1800),  # the restore, plus 120 s a GiB; then READY_WAIT
    # A host that is down: until the start request; after it, no limit by design.
    "self-data-handoff": ("homestead_self_data_worker", "HOST_LIMIT"),
    "self-data-prepare": ("homestead_self_data_prepare", "manifests", 3600),  # the binding pod's activeDeadlineSeconds
    "set-priority": ("homestead_priority", "PER_POD"),
    "set-requests": ("homestead_requests", "status", 600),
    "set-restart": ("homestead_set_restart", "LIMIT"),
    "share-remove": ("homestead_shares", "removal_progress", 1200),
    "smart-test": ("homestead_smart", "_progress", 600),  # once the drive no longer says the test runs
    "snapshot-delete": ("homestead_snapshot_delete", "TIMEOUT"),
    "snapshot-revert": ("homestead_revert", "WAIT"),  # one limit per phase
    "unraid-vm-import": ("homestead_unraid_vms", "FINISH_LIMIT"),  # and READY_LIMIT
    "volume-copy-move": ("homestead_copy_move", "status", 3600),  # plus 120 s a GiB
    "volume-rebalance": ("homestead_rebalance", "status", 3600),  # plus 120 s a GiB
    # The copy Job. Waiting for the app's pods to stop, and to be ready after, has no limit.
    "workload-copy": ("homestead_copy_job", "DEADLINE_CEILING"),
}

# kind -> why it has no number of its own. "No limit" marks a kind that can
# wait for ever.
NO_DEADLINE = {
    "backup": "no limit: it follows Longhorn's backup state, and one Longhorn leaves pending or in progress waits",
    "cluster-cleanup": "it ends on its first round",
    "deployment": "Kubernetes ends a stalled rollout at the Deployment's progressDeadlineSeconds, read as failed",
    "harvester-upgrade": "no limit: UPGRADE_MISSING_LIMIT covers only an upgrade Harvester never took; "
                         "one Harvester has taken is followed until Harvester ends it",
    "helm": "no limit: JOB_MISSING_LIMIT covers only a missing Job; one Kubernetes keeps retrying is followed",
    "host-console": "it ends on its first round",
    "image-rollback": "Kubernetes ends a stalled rollout at the Deployment's progressDeadlineSeconds, read as failed",
    "image-update": "Kubernetes ends a stalled rollout at the Deployment's progressDeadlineSeconds, read as failed",
    "import": "no limit: its copy Job has no activeDeadlineSeconds, and the job follows it until it ends",
    "longhorn-runtime": "it waits for a person to restart the host, by design, looked at every CADENCE interval",
    "longhorn-v2-cleanup": "no limit: it waits for the V2 instance managers to stop and for hosts that are away "
                           "to return, with the hosts cordoned",
    "move": "the move engine runs it and writes its record (homestead_move_state); the job only reads that record",
    "network-service": "no limit: a LoadBalancer Service waits for kube-vip to give it an address",
    "onboard": "it ends on its first round",
    "protect-run": "no limit: Homestead gives the run's Job no activeDeadlineSeconds (the CronJob's template may)",
    "restructure": "an older job: it ends failed on its first round",
    "self-data-move": "an older job: it ends failed on its first round",
    "vm-create": "the change runs in its request; the job waits for a person's Check outcome, by design",
    "vm-disk-import": "no limit: it follows CDI's import, and its clean-up waits until the objects are gone",
    "vm-disk-remove": "no limit: its clean-up waits until the DataVolume and its storage are gone",
    "vm-edit": "the change runs in its request; the job waits for a person's Check outcome, by design",
    "vm-migration": "KubeVirt fails a migration that stalls (its progress and completion timeouts)",
    "vm-power": "no limit: it waits for the VM instance to run and be ready, or for a person to check an "
                "uncertain request",
    "volume-delete": "no limit: it waits for workloads to let go of the claim, and for the volume to go",
    "volume-restore": "no limit once started: _restore_then_tidy gives a restore 15 min to start, then it "
                      "follows Longhorn's restore",
    "weekly-trim": "one volume a round from a fixed list, then it ends",
    "workload-rename": "the rename runs in its request; the job waits for a person's Check outcome, by design",
}


def limit(kind):
    """Seconds a kind's resolver may fail without a break."""
    return LIMITS.get(kind, DEFAULT_LIMIT)


def outcome(kind):
    """"failed" or "attention": what giving up on this kind does."""
    return "failed" if kind in FAILS else "attention"


def _span(seconds):
    hours, minutes = divmod(int(seconds) // 60, 60)
    return f"{hours} h {minutes} min" if hours and minutes else f"{hours} h" if hours else f"{minutes} min"


def resting(item, now=None):
    """True while a job that needs attention waits for its next ask."""
    now = clock() if now is None else now
    state = item.get(KEY) or {}
    return bool(state.get("attention")) and 0 <= now - state.get("tried", 0) < RETRY_EVERY


def clear(item):
    """The resolver answered: the count starts again."""
    item.pop(KEY, None)


def failed(item, error, now=None):
    """Count one failed ask. None while within the kind's limit, else
    ("failed", message) or ("attention", message without its recovery)."""
    now = clock() if now is None else now
    kind = item.get("kind")
    state = item.get(KEY) or {}
    since, last = state.get("since"), state.get("last", state.get("since"))
    if (not isinstance(since, (int, float)) or not isinstance(last, (int, float))
            or since > now or last > now or now - last > GAP):
        # The first failure, a count saved by a clock ahead of this one, or a
        # gap in polling: a new streak.
        since = last = now
    elif now - last >= LAST_EVERY:
        last = now
    text = " ".join(str(error).split())[:240]
    state = {"since": since, "last": last, "error": text}
    if now - since <= limit(kind):
        item[KEY] = state
        return None
    span = _span(limit(kind))
    if outcome(kind) == "failed":
        clear(item)
        return "failed", f"Stopped after {span} without a status: {text}"
    item[KEY] = {**state, "attention": True, "tried": now}
    return "attention", (f"Needs attention: no status for over {span} ({text}). It keeps its place because "
                         f"{KEEPS_GUARDS.get(kind, 'it may still be changing something')}, and carries on once "
                         f"this clears.")
