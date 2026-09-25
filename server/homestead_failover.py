"""What a container does when the node it runs on stops answering.

Kubernetes taints a node that stops answering, and a pod leaves it once it
has tolerated that taint for its tolerationSeconds - Kubernetes' default is
five minutes - after which its Deployment starts a replacement elsewhere.
Homestead used to give everything it deployed fifteen seconds, and left what
it had not deployed at the default. Which is right depends on the app:

  move     fifteen seconds, then another node - an app that should come back
           quickly wherever there is room;
  wait     never moved - an app tied to that host's hardware (a Coral, a
           Zigbee stick), or one better restarted where it was;
  default  Kubernetes' five minutes.

A container whose volume one node mounts at a time only really moves if
Longhorn lets go of the volume on the dead node, which its "Pod Deletion
Policy When Node is Down" setting decides; see homestead_lhcapacity.
"""

KEYS = ("node.kubernetes.io/unreachable", "node.kubernetes.io/not-ready")
FAST_SECONDS = 15
MODES = ("move", "wait", "default")

kget = ksend = None


def bind(_kget, _ksend):
    global kget, ksend
    kget, ksend = _kget, _ksend


def mode_of(podspec):
    """"move", "wait" or "default", from the pod's tolerations."""
    rows = [t for t in (podspec or {}).get("tolerations") or []
            if t.get("key") in KEYS and t.get("effect", "NoExecute") in ("NoExecute", "")]
    if not rows:
        return "default"
    if any("tolerationSeconds" not in t or t.get("tolerationSeconds") is None for t in rows):
        return "wait"
    seconds = min(int(t.get("tolerationSeconds") or 0) for t in rows)
    return "move" if seconds <= 60 else "default"


def tolerations(podspec, mode):
    """The pod's tolerations with the node-failure ones set for this mode;
    any others (a GPU taint, say) are kept as they are."""
    if mode not in MODES:
        raise ValueError(f"when its node fails, a container moves, waits, or keeps Kubernetes' default - not {mode!r}")
    kept = [t for t in (podspec or {}).get("tolerations") or [] if t.get("key") not in KEYS]
    if mode == "move":
        kept += [{"key": key, "operator": "Exists", "effect": "NoExecute", "tolerationSeconds": FAST_SECONDS}
                 for key in KEYS]
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
