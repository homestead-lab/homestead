"""Keeping workloads together, or apart.

Three rules, each a preference or a requirement, all at the level of a node:

* spread  - this workload's own instances on different nodes, so one host
            going down does not take every copy with it;
* with    - on the same node as another workload, for two apps that talk
            constantly or share a host device;
* apart   - never on the same node as another workload, for two DNS servers
            or a pair that would fight over the same disk.

They become pod affinity terms matched on each workload's own selector. A
preference steers the scheduler and still lets a pod start anywhere; a
requirement leaves a pod pending rather than break the rule, which is the
point of asking for it and also its cost.

The rules are recorded in an annotation with the selectors they were written
with, so an edit removes exactly the terms Homestead wrote - and nothing a
chart or a person added by hand - even after a workload they named is gone.
"""
import copy
import json

import homestead_names as NAMES

kget = None
RULES = NAMES.key("placement")
HOST = "kubernetes.io/hostname"
MODES = ("prefer", "require")
WEIGHT = 100


def bind(_kget):
    global kget
    kget = _kget


def _empty():
    return {"spread": "", "with": [], "apart": []}


def read(dep):
    """The rules on a Deployment, as recorded when they were written."""
    annotations = (dep.get("metadata", {}) or {}).get("annotations", {}) or {}
    try:
        stored = json.loads(annotations.get(RULES) or "{}")
    except ValueError:
        return _empty()
    rules = _empty()
    if stored.get("spread") in MODES:
        rules["spread"] = stored["spread"]
    for key in ("with", "apart"):
        rules[key] = [row for row in stored.get(key) or []
                      if isinstance(row, dict) and row.get("mode") in MODES and row.get("name")]
    return rules


def public(dep):
    """The rules without the selectors, for the editor."""
    rules = read(dep)
    return {"spread": rules["spread"],
            **{key: [{"ns": row.get("ns", ""), "name": row["name"], "mode": row["mode"]} for row in rules[key]]
               for key in ("with", "apart")}}


def _selector(dep):
    return dict(((dep.get("spec", {}) or {}).get("selector", {}) or {}).get("matchLabels", {}) or {})


def _term(selector, ns):
    return {"labelSelector": {"matchLabels": dict(selector)}, "namespaces": [ns], "topologyKey": HOST}


def _entries(rules, ns, own):
    out = []
    if rules["spread"] and own:
        out.append(("podAntiAffinity", rules["spread"], _term(own, ns)))
    for key, kind in (("with", "podAffinity"), ("apart", "podAntiAffinity")):
        for row in rules[key]:
            if row.get("selector"):
                out.append((kind, row["mode"], _term(row["selector"], row.get("ns") or ns)))
    return out


def _slot(mode):
    return ("requiredDuringSchedulingIgnoredDuringExecution" if mode == "require"
            else "preferredDuringSchedulingIgnoredDuringExecution")


def _item(mode, term):
    return term if mode == "require" else {"weight": WEIGHT, "podAffinityTerm": term}


def _remove(affinity, kind, mode, term):
    block = affinity.get(kind) or {}
    items = block.get(_slot(mode)) or []
    wanted = _item(mode, term)
    kept = [item for item in items if item != wanted]
    if kept:
        block[_slot(mode)] = kept
    else:
        block.pop(_slot(mode), None)


def _add(affinity, kind, mode, term):
    affinity.setdefault(kind, {}).setdefault(_slot(mode), []).append(_item(mode, term))


def _tidy(spec, affinity):
    for kind in ("podAffinity", "podAntiAffinity"):
        if not affinity.get(kind):
            affinity.pop(kind, None)
    if affinity:
        spec["affinity"] = affinity
    else:
        spec.pop("affinity", None)


def apply(dep, request):
    """Replace the rules Homestead wrote on dep with request's, in place.

    request: {"spread": "prefer"|"require"|"", "with": [{"ns", "name", "mode"}],
    "apart": [...]}. Each workload named is looked up for its selector, so a
    rule never matches pods by a guess at their labels."""
    meta = dep.setdefault("metadata", {})
    ns, name = meta.get("namespace", ""), meta.get("name", "")
    own = _selector(dep)
    spec = dep["spec"]["template"]["spec"]
    affinity = copy.deepcopy(spec.get("affinity") or {})
    for kind, mode, term in _entries(read(dep), ns, own):
        _remove(affinity, kind, mode, term)

    request = request or {}
    rules = _empty()
    spread = str(request.get("spread") or "")
    if spread and spread not in MODES:
        raise ValueError("spreading is prefer or require")
    if spread and not own:
        raise ValueError(f"{name} has no simple selector, so its instances cannot be told apart")
    rules["spread"] = spread
    seen = {}
    for key in ("with", "apart"):
        for row in request.get(key) or []:
            target_ns, target = str(row.get("ns") or ns).strip(), str(row.get("name") or "").strip()
            mode = str(row.get("mode") or "prefer")
            if not target:
                continue
            if mode not in MODES:
                raise ValueError(f"{target}: the rule is prefer or require")
            if (target_ns, target) == (ns, name):
                raise ValueError("a workload is kept with or apart from others; spreading is how it keeps from itself")
            if (target_ns, target) in seen:
                raise ValueError(f"{target} is named twice" + (
                    "; it cannot be kept both with and apart" if seen[(target_ns, target)] != key else ""))
            seen[(target_ns, target)] = key
            other = kget(f"/apis/apps/v1/namespaces/{target_ns}/deployments/{target}")
            selector = _selector(other)
            if not selector:
                raise ValueError(f"{target} has no simple selector to match its pods by")
            rules[key].append({"ns": target_ns, "name": target, "mode": mode, "selector": selector})

    for kind, mode, term in _entries(rules, ns, own):
        _add(affinity, kind, mode, term)
    _tidy(spec, affinity)
    annotations = meta.setdefault("annotations", {})
    if rules["spread"] or rules["with"] or rules["apart"]:
        annotations[RULES] = json.dumps(rules, separators=(",", ":"), sort_keys=True)
    else:
        annotations.pop(RULES, None)
    return rules
