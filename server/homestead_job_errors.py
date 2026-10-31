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
    "cluster-cleanup": "it holds nothing once it has started",
    "deployment": "it only follows a rollout Kubernetes runs",
    "harvester-upgrade": "Harvester runs and follows its own upgrade",
    "harvester-uplink": "Harvester makes the network change; Homestead only follows it",
    "helm": "it only follows a job the Helm controller runs",
    "host-console": "it installs or removes the host console add-on, which holds no data",
    "host-os": "the host installs its updates by itself, and the host hold lapses on its own",
    "image-cleanup": "it removes images nothing uses",
    "image-pull": "it only follows image pulls; cancelling it removes its pull helpers",
    "image-rollback": "it only follows a rollout Kubernetes runs",
    "image-update": "it only follows a rollout Kubernetes runs",
    "multus": "it only follows an add-on install",
    "network-service": "it only follows a Service's endpoints",
    "onboard": "it holds nothing once it has started",
    "os-rollout": "the rollout ends itself and lets go of its holds (homestead_os_rollout)",
    "set-priority": "each app's change stands alone; the rest are left as they are",
    "set-requests": "each app's change stands alone; the rest are left as they are",
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
    "vm-edit": "a VM's disks may be changing",
    "volume-copy-move": "volume data is being copied",
    "volume-delete": "a volume is being deleted",
    "volume-rebalance": "volume replicas are being moved",
    "volume-restore": "a volume is being restored",
    "workload-copy": "volume data is being copied",
    "workload-rename": "an app is being renamed with its volumes",
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
