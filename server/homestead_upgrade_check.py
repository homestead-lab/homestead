"""Before an upgrade: what Homestead's table knows about the next version.

For each platform part Homestead can move on (homestead_components), this
says what the target is known to break here, from the dated table in
homestead_upgrade_known, and whether the KubeVirt-Kubernetes pair running
now is inside KubeVirt's support matrix. It reads only that table and the
versions the components report already has: no fetches.

A finding's level: block (outside what the project supports, or a known
break: the upgrade asks for a tick), warn (worth reading first), info (what
was checked and found fine), unknown (the table does not cover it). The
verdict: blocked, check, clear (only info), or unchecked (nothing checked).
"No known breaks" is not "safe": the table covers few facts.
"""
import homestead_upgrade_known as KNOWN


def _minor(version):
    """(major, minor) of a release Homestead can read, else None."""
    import homestead_components as COMPONENTS
    parsed = COMPONENTS.parse(version)
    return parsed[:2] if parsed else None


def _text(minor):
    return f"{minor[0]}.{minor[1]}"


def _key(text):
    return tuple(int(part) for part in text.split("."))


def _listing(minors):
    names = [_text(m) for m in sorted(minors)]
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


# ------------------------------------------------------------------ KubeVirt
def kubevirt_pair(kubevirt, kube_minors):
    """Where KubeVirt `kubevirt` on the Kubernetes minors `kube_minors` (every
    host's) stands in KubeVirt's support matrix: supported, eol, outside, or
    unchecked when the table or the versions don't say. "fault" lists the
    minors that make it outside or eol."""
    table, source = KNOWN.KUBEVIRT_KUBERNETES, KNOWN.KUBEVIRT_KUBERNETES["source"]
    here = _minor(kubevirt)
    kube = sorted(kube_minors)
    title = f"KubeVirt {_text(here) if here else kubevirt or '?'} on Kubernetes {_listing(kube) if kube else '?'}"

    def pair(status, detail, fault=()):
        return {"id": "kubevirt-kubernetes", "status": status, "title": title, "detail": detail,
                "fault": [_text(m) for m in fault], "source": source, "checked": KNOWN.CHECKED}

    if not here or not kube:
        return pair("unchecked", "Homestead could not read both versions, so this pair is not checked.")
    row = table["rows"].get(_text(here))
    if not row:
        return pair("unchecked", f"Homestead's table (checked {KNOWN.CHECKED}) does not list KubeVirt {_text(here)}, "
                                 "so this pair is not checked.")
    supported = {_key(v) for v in row["supported"]}
    eol = {_key(v) for v in row["eol"]}
    low, high = (_key(v) for v in table["columns"])
    # Support is a run of consecutive minors: a row whose oldest column is
    # neither supported nor EOL supports nothing older than that column.
    below_known = low not in supported and low not in eol
    says = (f"KubeVirt {_text(here)} supports Kubernetes {_listing(supported)}" if supported
            else f"KubeVirt {_text(here)} supports no Kubernetes version that is still maintained")
    says += f" (its support matrix, as of {KNOWN.CHECKED})."
    outside = [m for m in kube if m > high or (m < low and below_known)
               or (low <= m and m not in supported and m not in eol)]
    if outside:
        newer = sorted(_key(k) for k, r in table["rows"].items()
                       if all(_text(m) in r["supported"] for m in outside))
        hint = (f" KubeVirt {_text(newer[0])} supports {_listing(outside)}." if newer
                else f" No KubeVirt release in Homestead's table supports {_listing(outside)} yet.")
        return pair("outside", f"{says} It may run, but it is not tested on {_listing(outside)}.{hint}", outside)
    if any(m < low for m in kube):
        return pair("unchecked", f"KubeVirt's matrix starts at Kubernetes {_text(low)}, so this pair is not checked.")
    ended = [m for m in kube if m in eol]
    if ended:
        now = (f" Of what it supports, Kubernetes still maintains {_listing(supported)}." if supported
               else " None of the Kubernetes versions it supports is still maintained.")
        return pair("eol", f"KubeVirt {_text(here)} supports Kubernetes {_listing(ended)}, but Kubernetes no longer "
                           f"maintains {_listing(ended)} (KubeVirt's support matrix, as of {KNOWN.CHECKED}).{now}", ended)
    return pair("supported", f"{says}")


def _kubevirt_findings(kubevirt, kube_minors, add, before):
    """`before`: the pair as it is now. An upgrade that leaves a pair outside
    no more than it already was is worth a look, not a block."""
    pair = kubevirt_pair(kubevirt, kube_minors)
    here = _minor(kubevirt)
    name = f"KubeVirt {_text(here)}" if here else "KubeVirt"
    fault = _listing([_key(m) for m in pair["fault"]]) if pair["fault"] else ""
    every = _listing(sorted(kube_minors)) if kube_minors else "?"
    if pair["status"] == "outside":
        if before["status"] == "outside" and set(pair["fault"]) <= set(before["fault"]):
            # A newer KubeVirt in the table is already named in the detail; otherwise the way out is to wait for one.
            way_out = f" Upgrade KubeVirt when a release lists Kubernetes {fault}." if "No KubeVirt release" in pair["detail"] else ""
            add("already", f"{name} on Kubernetes {fault} is already outside its support matrix; this upgrade does not change that",
                pair["detail"] + way_out, pair["source"])
        else:
            add("block", f"{name} is not tested on Kubernetes {fault}", pair["detail"], pair["source"])
    elif pair["status"] == "eol":
        # Past its end of life before the upgrade, and no more so after it: nothing for the upgrade to answer for.
        level = "already" if before["status"] == "eol" and set(pair["fault"]) <= set(before["fault"]) else "warn"
        add(level, f"Kubernetes {fault} {'is' if len(pair['fault']) == 1 else 'are'} past {'its' if len(pair['fault']) == 1 else 'their'} end of life",
            pair["detail"], pair["source"])
    elif pair["status"] == "supported":
        add("info", f"{name} supports Kubernetes {every}", pair["detail"], pair["source"])
    else:
        add("unknown", f"{name} on Kubernetes {every}: not checked", pair["detail"], pair["source"])


# ------------------------------------------------------------------ Longhorn
def _longhorn_findings(installed, target, kube_minors, add):
    norm = lambda v: f"v{str(v or '').lstrip('v')}"
    row, before = KNOWN.LONGHORN_CHARTS.get(norm(target)), KNOWN.LONGHORN_CHARTS.get(norm(installed))
    if not row:
        add("unknown", f"Longhorn {target}: not checked",
            f"Homestead's table (checked {KNOWN.CHECKED}) does not list Longhorn {target} yet. Read its release notes.",
            "")
        return
    floor = _key(row["kubernetes_min"])
    if not kube_minors:
        add("unknown", f"Longhorn {target} needs Kubernetes {_text(floor)} or newer",
            "Homestead could not read the hosts' Kubernetes versions, so this is not checked.", row["source"])
    elif min(kube_minors) < floor:
        add("block", f"Longhorn {target} needs Kubernetes {_text(floor)} or newer",
            f"The oldest host runs {_text(min(kube_minors))}. Upgrade Kubernetes first.", row["source"])
    else:
        add("info", f"Kubernetes {_listing(sorted(kube_minors))} meets Longhorn {target}'s minimum ({_text(floor)})",
            "Longhorn publishes a minimum Kubernetes version only.", row["source"])
    if row["restrict_internal_traffic"] and not (before and before["restrict_internal_traffic"]):
        add("warn", "Longhorn fences its manager in with NetworkPolicies",
            (f"Longhorn {target}'s chart turns networkPolicies.restrictInternalTraffic on by default" if before else
             f"networkPolicies.restrictInternalTraffic is on by default in Longhorn {target}'s chart") + ": only Longhorn's own "
            "pods may reach longhorn-backend:9500. Homestead keeps its own narrow policy beside Longhorn's (#794), so "
            "Trim now and snapshot rollback keep working. Anything else that calls that port, such as a metrics "
            "scraper, is cut off unless the chart values set it to false or allow it.", row["source"])


# ------------------------------------------------------------------ the check
def verdict_of(findings):
    """blocked, check, unchanged (every problem found is already true and the upgrade leaves it as it is),
    clear, or unchecked."""
    levels = {f["level"] for f in findings}
    if "block" in levels:
        return "blocked"
    if "warn" in levels:
        return "check"
    if levels == {"info"}:
        return "clear"
    if "already" in levels:
        return "unchanged" if levels <= {"already", "info"} else "check"
    return "check" if "info" in levels else "unchecked"


def check(component, installed, target, kube_minors, others):
    """What the table knows about moving `component` from `installed` to
    `target`. kube_minors: every host's Kubernetes (major, minor); others:
    {component id: installed version}."""
    findings = []

    def add(level, title, detail, source):
        findings.append({"level": level, "title": title, "detail": detail, "source": source})

    if component == "cluster":
        to = _minor(target)
        if others.get("kubevirt"):
            _kubevirt_findings(others["kubevirt"], {to} if to else set(), add,
                               kubevirt_pair(others["kubevirt"], kube_minors))
    elif component == "kubevirt":
        _kubevirt_findings(target, kube_minors, add, kubevirt_pair(installed, kube_minors))
    elif component == "longhorn":
        _longhorn_findings(installed, target, kube_minors, add)
    return {"to": target, "verdict": verdict_of(findings), "findings": findings, "checked": KNOWN.CHECKED}


def annotate(rows, kube_minors):
    """Give each part Homestead upgrades its check; return the pairs running
    now, for the Platform versions card and the Updates insight."""
    others = {row["id"]: row.get("installed", "") for row in rows}
    for row in rows:
        if row.get("next") and row.get("how") in ("helmchart", "suc"):
            row["check"] = check(row["id"], row.get("installed", ""), row["next"], kube_minors, others)
    return [kubevirt_pair(others["kubevirt"], kube_minors)] if others.get("kubevirt") else []
