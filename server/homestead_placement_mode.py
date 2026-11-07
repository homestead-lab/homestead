"""How the hosts share the work: one choice for the whole cluster.

Placement is otherwise set app by app, and with thirty apps on two or three
hosts nobody assembles a plan for the cluster that way (#347). This is the
cluster's answer, settings.placement.mode:

* default - Kubernetes as it is: nothing here changes anything.
* spread - hosts carry apps in proportion to their size. The scheduler
  already does this (it scores hosts by what is left after reservations), but
  only for apps that reserve something, so this mode says how many do not and
  offers the reservations review. No app is changed by choosing it.
* standby - the hosts named in standby_nodes take apps only when the others
  cannot: each carries the taint homestead.io/standby=true:PreferNoSchedule,
  which the scheduler avoids while it has a choice and ignores when it has
  none. Nothing in any app changes, so it works the same for apps Flux, Argo
  CD or Helm apply. When a host comes back, the rebalance moves apps off the
  standby hosts first (homestead_recovery).

PreferNoSchedule rather than NoSchedule: a hard taint would also keep
DaemonSets - Longhorn's, Homestead's probe - from their pods on that host
the next time they roll out. A host is never made standby when that would
leave no other host taking work.

Homestead keeps the taints matching the setting: it adds the taint where the
setting says, and removes any taint with its key, homestead.io/standby, where
it does not - never a taint with another key. Each change names the node's
resourceVersion, so a taint something else added since Homestead read the
node is not overwritten: Kubernetes refuses the change, and the next minute's
look tries again.
"""
import re
import urllib.error

import homestead_routes

MODES = ("", "default", "spread", "standby")
TAINT = {"key": "homestead.io/standby", "value": "true", "effect": "PreferNoSchedule"}
MAX_STANDBY = 10
NAME = re.compile(r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$")

kget = ksend = None
settings = lambda: {}            # the app settings, as server.py reads them
system_ns = frozenset()
# The last write each host refused, kept until one works, so Settings can say
# a standby host is not tainted yet and why - not only the pod's log.
_failures = {}
# Namespaces of the platform's own, as the reservations review leaves out.
SYSTEM = ("kube-", "cattle-", "harvester-", "longhorn-", "fleet-")


# What bind() puts back for an argument it is not given: the module's own default,
# not the last bind's (a test's fake must not outlive the test).
_UNBOUND = {"settings": settings, "system_ns": system_ns}


def bind(_kget, _ksend, _settings=None, _system_ns=()):
    global kget, ksend, settings, system_ns
    kget, ksend = _kget, _ksend
    if _settings:
        settings, system_ns = _settings, frozenset(_system_ns)
    else:
        settings, system_ns = _UNBOUND["settings"], _UNBOUND["system_ns"]


def standby_hosts():
    """The hosts the setting keeps in reserve: Balance hosts empties them
    first and moves nothing onto them."""
    return sorted(standby_of(settings().get("placement")))


def validate(value):
    """settings.placement's mode and standby hosts, checked; "" is a mode not
    chosen yet, which the setup guide offers and behaves as default."""
    mode = str((value or {}).get("mode", "") or "")
    if mode not in MODES:
        raise ValueError("how hosts share the work must be default, spread or standby")
    standby = (value or {}).get("standby_nodes") or []
    if not isinstance(standby, list) or len(standby) > MAX_STANDBY \
            or any(not isinstance(n, str) or not NAME.match(n) for n in standby):
        raise ValueError("standby hosts must be a list of host names")
    if mode == "standby" and not standby:
        raise ValueError("choose at least one standby host, or another way to share the work")
    return {"mode": mode, "standby_nodes": sorted(set(standby))}


def _ours(taint):
    return taint.get("key") == TAINT["key"]


def takes_work(node):
    """Ready, not cordoned, and no hard taint of its own - a tiebreaker kept
    for etcd votes with NoSchedule takes no apps, so it is neither a main host
    nor one coming back."""
    ready = any(c.get("type") == "Ready" and c.get("status") == "True"
                for c in (node.get("status") or {}).get("conditions") or [])
    spec = node.get("spec") or {}
    hard = any(t.get("effect") in ("NoSchedule", "NoExecute") and not _ours(t) for t in spec.get("taints") or [])
    return ready and not spec.get("unschedulable") and not hard



def standby_of(placement):
    """The hosts that are standby under the setting: none unless in standby mode."""
    placement = placement or {}
    return set(placement.get("standby_nodes") or []) if placement.get("mode") == "standby" else set()


def wanted(nodes, placement):
    """The hosts that should carry the taint, and why any named one does not.
    A standby host is never the last one taking work."""
    named = standby_of(placement)
    names = {(n.get("metadata") or {}).get("name") for n in nodes}
    primaries = [n for n in nodes if (n.get("metadata") or {}).get("name") not in named and takes_work(n)]
    notes = [f"{name} is not a host in this cluster" for name in sorted(named - names)]
    if named and not primaries:
        return set(), notes + ["no other host takes work, so no host is made standby"]
    return named & names, notes


def reconcile(nodes, placement):
    """Adds Homestead's standby taint where the setting says and removes it
    where it does not. Returns what changed, and notes."""
    want, notes = wanted(nodes, placement)
    changed = []
    for node in nodes:
        meta, spec = node.get("metadata") or {}, node.get("spec") or {}
        name = meta.get("name")
        taints = spec.get("taints") or []
        ours = [t for t in taints if _ours(t)]
        # Ours exactly, once: one with our key and another value or effect -
        # a NoSchedule set by hand, say - is put right, not taken as ours.
        exact = len(ours) == 1 and (ours[0].get("value"), ours[0].get("effect")) == (TAINT["value"], TAINT["effect"])
        if (exact if name in want else not ours):
            _failures.pop(name, None)
            continue
        kept = [t for t in taints if not _ours(t)]
        new = kept + [dict(TAINT)] if name in want else kept
        # A merge patch replaces the list: the node's own list with only
        # Homestead's taint added or taken out, refused if the node changed.
        try:
            ksend("PATCH", f"/api/v1/nodes/{name}",
                  {"metadata": {"resourceVersion": meta.get("resourceVersion")}, "spec": {"taints": new or None}},
                  ctype="application/merge-patch+json")
        except Exception as error:
            # Refused, timed out or unreachable: this host is tried again at
            # the next look, and the others still change now.
            _failures[name] = str(error)[:120]
            notes.append(f"{name} was not changed this time: {str(error)[:120]}")
            continue
        _failures.pop(name, None)
        changed.append(f"{name} {'is now standby' if name in want else 'takes apps again'}")
    return changed, notes



def saved(placement):
    """After the settings are saved: standby hosts take their taint now, not
    at the next minute's look. A failed write is said and left to that look."""
    try:
        reconcile(kget("/api/v1/nodes").get("items", []), placement)
    except Exception as error:
        print(f"placement: {str(error)[:160]}", flush=True)
    # After the writes: a look in between would keep the state from before.
    homestead_routes.forget("placement")


def summary(nodes, placement, unreserved=None):
    """For Settings and the setup guide: the mode, each host's part, and how
    many apps reserve nothing (which spread depends on)."""
    want, notes = wanted(nodes, placement)
    hosts = []
    for node in nodes:
        name = (node.get("metadata") or {}).get("name")
        tainted = any(_ours(t) for t in (node.get("spec") or {}).get("taints") or [])
        hosts.append({"name": name, "standby": name in want, "tainted": tainted, "takes_work": takes_work(node),
                      "error": _failures.get(name, "")})
    notes = notes + [f"{name} could not be marked standby: {why}" for name, why in sorted(_failures.items()) if name in want]
    return {"mode": (placement or {}).get("mode", ""), "hosts": sorted(hosts, key=lambda h: h["name"]),
            "notes": notes, "unreserved": unreserved,
            "applies": sum(1 for h in hosts if h["takes_work"]) >= 2 or bool(want)}


def unreserved():
    """How many apps reserve nothing at all - which spreading by capacity
    depends on, since such an app is placed as if it used nothing."""
    count = 0
    for dep in kget("/apis/apps/v1/deployments").get("items", []):
        ns = (dep.get("metadata") or {}).get("namespace", "")
        if ns in system_ns or ns.startswith(SYSTEM):
            continue
        containers = (((dep.get("spec") or {}).get("template") or {}).get("spec") or {}).get("containers") or []
        if containers and not any(((c.get("resources") or {}).get("requests") or {}) for c in containers):
            count += 1
    return count


def current():
    """How hosts share the work now, for Settings and the setup guide."""
    return summary(kget("/api/v1/nodes").get("items", []), settings().get("placement"), unreserved())


ROUTES = {
    ("GET", "/api/placement"): ("viewer", lambda request: homestead_routes.cached("placement", 15, current)),
}
