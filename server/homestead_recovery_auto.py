"""What a returned host must pass before containers move back by themselves
(#588, decided on #1074): steady for 15 minutes, no Longhorn copy left to rebuild
on it, and no other host that has worked away. Pure, on API objects; a person sees
each as a reason in Settings and in the alert under Hosts joining.
"""
import calendar
import time
import urllib.error

import homestead_data_copies as DATA_COPIES
import homestead_placement_mode as PLACEMODE
import homestead_priority as PRIORITY

STEADY_SECONDS = 900
LH = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system"
VOLUMES, REPLICAS = f"{LH}/volumes", f"{LH}/replicas"


def _ready_time(node):
    for condition in (node.get("status") or {}).get("conditions") or []:
        if condition.get("type") == "Ready":
            try:
                return calendar.timegm(time.strptime(str(condition.get("lastTransitionTime")), "%Y-%m-%dT%H:%M:%SZ"))
            except ValueError:
                return None
    return None


def steady_since(node, returned_at):
    """A short flap between two looks shows only in Ready's time. A time that is
    missing or can't be read counts as the return."""
    ready = _ready_time(node)
    return returned_at if ready is None else max(returned_at, ready)


def steady_left(node, returned_at, now):
    """Seconds of steady time still to go. A Ready time ahead of this clock can't be
    trusted to say anything, so it waits in full."""
    since = steady_since(node, returned_at)
    return STEADY_SECONDS if since > now else max(0, int(STEADY_SECONDS - (now - since)))


def copies_left(host, volumes, replicas, engines):
    """The volumes with a copy on host that isn't whole: anything but RW in an engine's
    map, in any of its engines, or on an attached, degraded volume whose map doesn't have
    it yet or says it failed. Only that host's, and attached only: a volume that can
    never reach its count on a small cluster, or a detached one with offline rebuilding
    off, doesn't hold by itself."""
    by_name = {(v.get("metadata") or {}).get("name"): v.get("status") or {} for v in volumes}
    not_whole, listed = {}, {}   # by volume; one engine's RW doesn't undo another's WO
    for engine in engines:
        vol = (engine.get("spec") or {}).get("volumeName")
        modes = (engine.get("status") or {}).get("replicaModeMap") or {}
        not_whole.setdefault(vol, set()).update(name for name, mode in modes.items() if mode != "RW")
        listed.setdefault(vol, set()).update(modes)
    left = set()
    for replica in replicas:
        spec = replica.get("spec") or {}
        if spec.get("nodeID") != host:
            continue
        vol, name, status = spec.get("volumeName"), (replica.get("metadata") or {}).get("name"), by_name.get(spec.get("volumeName")) or {}
        degraded = status.get("state") == "attached" and status.get("robustness") == "degraded"
        if name in not_whole.get(vol, ()) or (degraded and (spec.get("failedAt") or name not in listed.get(vol, ()))):
            left.add(vol)
    return sorted(left)


def read_copies(kget):
    """(volumes, replicas, engines): all three, or it raises, so nothing moves on half
    a picture. Only all three answering 404 means no Longhorn here, and nothing to
    wait for; one 404 beside an answer is a cluster mid-change, or a reader's fault."""
    answers, missing = [], 0
    for path in (VOLUMES, REPLICAS, DATA_COPIES.ENGINES):
        try:
            answers.append(kget(path))
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
            missing += 1
    if missing == 3:
        return [], [], []
    if missing or any("items" not in answer for answer in answers):
        raise ValueError("Longhorn answered only some of its lists")
    return tuple(answer["items"] for answer in answers)


def away(nodes, worked, standby, returned):
    """Hosts that have worked and take no work now. A standby host isn't asked to, and
    a tiebreaker kept for etcd votes never has."""
    return sorted((n.get("metadata") or {}).get("name") for n in nodes
                  if (n.get("metadata") or {}).get("name") in set(worked)
                  and (n.get("metadata") or {}).get("name") not in set(standby)
                  and (n.get("metadata") or {}).get("name") not in set(returned)
                  and not PLACEMODE.takes_work(n))


def waits_for_window(dep, ladder):
    """Critical apps wait for the update window, and so does any whose tier can't be
    read: it might be critical."""
    template = ((dep.get("spec") or {}).get("template") or {}).get("spec") or {}
    return PRIORITY.tier_of(template, ladder)[0] not in ("normal", "low")
