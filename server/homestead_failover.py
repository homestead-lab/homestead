"""What a container does when the node it runs on stops answering.

Kubernetes taints a node that stops answering, and a pod leaves it once it
has tolerated that taint for its tolerationSeconds - Kubernetes' default is
five minutes - after which its Deployment starts a replacement elsewhere.
Homestead used to give everything it deployed fifteen seconds, and left what
it had not deployed at the default. Which is right depends on the app:

  move     fifteen seconds, then another node - an app that should come back
           quickly wherever there is room;
  slow     ninety seconds, then another node - one that should ride out a
           short network drop where it is (#1308);
  wait     never moved - an app tied to that host's hardware (a Coral, a
           Zigbee stick), or one better restarted where it was;
  default  Kubernetes' five minutes.

An app may have another time, set outside Homestead: "custom", shown with
its seconds (seconds_of) and kept as it is until another mode is saved.

A container whose volume one node mounts at a time only really moves if
Longhorn lets go of the volume on the dead node, which its "Pod Deletion
Policy When Node is Down" setting decides; see homestead_lhcapacity.
"""
import homestead_kube as KUBE

KEYS = ("node.kubernetes.io/unreachable", "node.kubernetes.io/not-ready")
FAST_SECONDS = 15
SLOW_SECONDS = 90
DEFAULT_SECONDS = 300
MODES = ("move", "slow", "wait", "default", "custom")

kget = ksend = None


def bind(_kget, _ksend):
    global kget, ksend
    kget, ksend = _kget, _ksend


def _rows(podspec):
    return [t for t in (podspec or {}).get("tolerations") or []
            if t.get("key") in KEYS and t.get("effect", "NoExecute") in ("NoExecute", "")]


def _key_seconds(rows, key):
    """The pod's time for one taint: None when it stays for good, Kubernetes'
    five minutes when it has none (its DefaultTolerationSeconds)."""
    mine = [t for t in rows if t.get("key") == key]
    if not mine:
        return DEFAULT_SECONDS
    if any(t.get("tolerationSeconds") is None for t in mine):
        return None
    return min(int(t["tolerationSeconds"]) for t in mine)


def seconds_of(podspec):
    """How long the pod stays on a host that stopped answering (tainted
    unreachable): None when it waits for good, five minutes when nothing says."""
    return _key_seconds(_rows(podspec), KEYS[0])


def waits_when_unreachable(podspec):
    """Whether the pod stays on a host that stops answering for good. A
    behaviour question, for readers that act on it; mode_of is the Edit
    choice, which calls one key set alone "custom"."""
    return seconds_of(podspec) is None


def mode_of(podspec):
    """"move", "slow", "wait" or "default" when both node-failure keys hold exactly
    what that mode writes (no key at all is "default"); anything else is
    "custom", a time Homestead does not offer, kept as it is. seconds_of
    says which time it is."""
    rows = _rows(podspec)
    if not rows:
        return "default"
    times = {_key_seconds(rows, key) for key in KEYS}
    if len(times) != 1:
        return "custom"
    seconds = times.pop()
    return {None: "wait", FAST_SECONDS: "move", SLOW_SECONDS: "slow", DEFAULT_SECONDS: "default"}.get(seconds, "custom")


def after(seconds):
    """When a pod with this time leaves a node that stopped answering, in words."""
    if seconds >= 120 and seconds % 60 == 0:
        return f"about {seconds // 60} minutes after the host stops answering"
    return f"about {seconds} seconds after the host stops answering"


def tolerations(podspec, mode):
    """The pod's tolerations with the node-failure ones set for this mode;
    any others (a GPU taint, say) are kept as they are."""
    if mode not in MODES:
        raise ValueError(f"{mode!r} is not a failover choice. When its host fails, a container moves after "
                         f"15 or 90 seconds, waits, or keeps Kubernetes' default.")
    if mode == "custom":
        return list((podspec or {}).get("tolerations") or [])
    kept = [t for t in (podspec or {}).get("tolerations") or [] if t.get("key") not in KEYS]
    if mode in ("move", "slow"):
        kept += [{"key": key, "operator": "Exists", "effect": "NoExecute",
                  "tolerationSeconds": FAST_SECONDS if mode == "move" else SLOW_SECONDS} for key in KEYS]
    elif mode == "wait":
        kept += [{"key": key, "operator": "Exists", "effect": "NoExecute"} for key in KEYS]
    return kept


def apply(podspec, mode):
    podspec["tolerations"] = tolerations(podspec, mode)
    if not podspec["tolerations"]:
        podspec.pop("tolerations")
    return podspec


def set_many(items):
    """Set several containers' node-failure behaviour. Changing it changes the
    pod template, so each changed one restarts."""
    changed, unchanged = [], []
    for item in items or []:
        ns, name, mode = str(item.get("ns") or ""), str(item.get("name") or ""), str(item.get("mode") or "")
        KUBE.request_name(ns, "namespace", namespace=True)
        KUBE.request_name(name, "workload name")
        dep = kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")
        podspec = ((dep.get("spec") or {}).get("template") or {}).get("spec") or {}
        if mode_of(podspec) == mode:
            unchanged.append(name)
            continue
        wanted = tolerations(podspec, mode)
        ksend("PATCH", f"/apis/apps/v1/namespaces/{ns}/deployments/{name}",
              {"spec": {"template": {"spec": {"tolerations": wanted or None}}}},
              ctype="application/merge-patch+json")
        changed.append(name)
    return {"ok": True, "changed": changed,
            "detail": (f"{len(changed)} container{'s' if len(changed) != 1 else ''} changed and restarting"
                       if changed else "nothing changed")}
