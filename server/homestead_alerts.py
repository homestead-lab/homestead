"""Durable, debounced conditions and events, with per-account acknowledgement.

Acknowledgement suppresses the observed baseline, never the underlying health
finding. New signals, higher severity or worse measurements re-arm an incident.
Source failures retain prior observations; only confirmed absence resolves it.
"""
import hashlib
import json
import os
import time
import homestead_shared as SHARED

DATA_DIR = "/data"
HOLD = 60
LOG_SIZE = 300
_lock = SHARED.SharedLock("alerts")
SEVERITY = {"info": 0, "degraded": 1, "critical": 2}


def bind(data_dir):
    global DATA_DIR
    DATA_DIR = data_dir


def _path():
    return os.path.join(DATA_DIR, "alerts.json")


def _load():
    try:
        with open(_path(), encoding="utf-8") as handle:
            state = json.load(handle)
        if not isinstance(state, dict):
            raise ValueError("Invalid alert state")
    except (OSError, ValueError):
        state = {}
    for key, default in (("seq", 0), ("active", {}), ("log", []), ("acknowledgements", {})):
        state.setdefault(key, default)
    return state


def _save(state):
    SHARED.write_json(_path(), state)


def _snapshot(fact):
    return {"severity": SEVERITY.get(fact.get("severity"), 0),
            "signals": fact.get("signals") or {"condition": 1}}


def _worse(current, baseline):
    return current["severity"] > baseline.get("severity", 0) or any(
        value > baseline.get("signals", {}).get(key, 0)
        for key, value in current["signals"].items())


def _version(row):
    return hashlib.sha256(json.dumps([row.get("since"), _snapshot(row)],
                                     sort_keys=True).encode()).hexdigest()


def _acknowledged(state, user, row):
    ack = state["acknowledgements"].get(user, {}).get(row["key"])
    return bool(ack and ack["incident"] == row.get("since", row.get("incident"))
                and not _worse(_snapshot(row), ack["baseline"]))


def _append(state, fact, phase, now):
    state["seq"] += 1
    resolved = phase == "resolved"
    entry = {"id": state["seq"], "at": int(now), "phase": phase, "key": fact["key"],
             "category": fact["category"], "severity": fact.get("severity", "info"),
             "title": (fact.get("resolved") or f"Cleared: {fact['title']}") if resolved else fact["title"],
             "body": "This condition is no longer reported." if resolved else fact.get("body", ""),
             "href": fact.get("href", "/"), "incident": fact.get("since"),
             "signals": fact.get("signals") or {"condition": 1}, "event": bool(fact.get("event"))}
    if phase == "worsened":
        entry["title"] = "Worsened: " + entry["title"]
    state["log"] = (state["log"] + [entry])[-LOG_SIZE:]
    return entry


def _source(key, row):
    # Older releases used a singular key prefix for the addresses source.
    return row.get("source") or ("addresses" if key.startswith("address:") else key.split(":", 1)[0])


def observe(results, now=None):
    now = time.time() if now is None else now
    with _lock:
        state = _load()
        watched = set(state.get("sources") or [])
        active, fresh, seen = state["active"], [], set()
        for source, facts in results.items():
            if facts is None:
                for k, row in active.items():
                    if _source(k, row) == source:
                        seen.add(k)
                        row["missing"] = 0
                        row.pop("worsening_since", None)
                        if not row["announced"]:
                            row["since"] = now
                continue
            seeding = source not in watched
            watched.add(source)
            for fact in facts:
                if "unknown_prefix" in fact or "unknown_key" in fact:
                    for k, row in active.items():
                        if k == fact.get("unknown_key") or ("unknown_prefix" in fact and k.startswith(fact["unknown_prefix"])):
                            seen.add(k)
                            row["missing"] = 0
                            row.pop("worsening_since", None)
                            if not row["announced"]:
                                row["since"] = now
                    continue
                key = fact["key"]
                seen.add(key)
                row = active.get(key)
                if row is None:
                    row = active[key] = {**fact, "source": source, "since": now, "announced": 0}
                else:
                    # Retain a baseline before updating a legacy persisted row.
                    if row.get("missing") and not row["announced"]:
                        row["since"] = now
                    row.setdefault("notice", _snapshot({**fact, "severity": row.get("severity")})
                                   if "signals" not in row and "signals" in fact else _snapshot(row))
                    row.update(fact)
                row["source"], row["missing"] = source, 0
                if row["announced"]:
                    if row.get("event"):
                        continue
                    snap = _snapshot(row)
                    worse = _worse(snap, row.get("notice", snap)) or any(
                        ack.get("incident") == row["since"] and _worse(snap, ack["baseline"])
                        for account in state["acknowledgements"].values()
                        for ack in [account.get(key, {})] if ack)
                    if not worse:
                        row.pop("worsening_since", None)
                    elif "worsening_since" not in row:
                        row["worsening_since"] = now
                    elif now - row["worsening_since"] >= HOLD:
                        entry = _append(state, row, "worsened", now)
                        row["announced"], row["notice"] = entry["id"], snap
                        row.pop("worsening_since", None)
                        fresh.append(entry)
                    continue
                if fact.get("event") and seeding:
                    row["announced"] = -1
                # A condition may ask to be held longer before it is raised:
                # one that settles by itself on its own time (a cluster
                # coming back) is not news within it.
                elif fact.get("event") or now - row["since"] >= max(HOLD, int(fact.get("hold") or 0)):
                    entry = _append(state, row, "raised", now)
                    row["announced"], row["notice"] = entry["id"], _snapshot(row)
                    fresh.append(entry)
        for key in [k for k, row in active.items() if k not in seen and _source(k, row) in results]:
            row = active[key]
            row.setdefault("missing", 0)
            if not row["missing"]:
                row["missing"] = now
            if now - row["missing"] < HOLD and not row.get("event"):
                continue
            del active[key]
            if row["announced"] > 0 and not row.get("event"):
                fresh.append(_append(state, row, "resolved", now))
        # A worsening notification consumes only acknowledgements below its
        # new baseline. Higher acknowledged levels remain quiet.
        for entry in fresh:
            for account in state["acknowledgements"].values():
                ack = account.get(entry["key"])
                if ack and (entry["phase"] == "resolved" or (entry["phase"] == "worsened" and
                        _worse(_snapshot(entry), ack["baseline"]))):
                    account.pop(entry["key"], None)
        state["sources"] = sorted(watched)
        _save(state)
        return fresh


class AlertChanged(ValueError):
    pass


def acknowledge(user, key, version, undo=False, now=None):
    """The user comes from the authenticated session, never the request body."""
    with _lock:
        state = _load()
        row = state["active"].get(key) if isinstance(key, str) else None
        if not row or row.get("event") or row.get("announced", 0) <= 0 or _version(row) != version:
            raise AlertChanged("This alert changed. Refresh and review its current state.")
        account = state["acknowledgements"].setdefault(user, {})
        if undo:
            account.pop(key, None)
        else:
            account[key] = {"baseline": _snapshot(row), "incident": row["since"],
                            "at": int(time.time() if now is None else now)}
        _save(state)
    return {"ok": True}


def forget_user(user):
    with _lock:
        state = _load()
        state["acknowledgements"].pop(user, None)
        _save(state)


def note(fact, now=None):
    with _lock:
        state = _load()
        entry = _append(state, fact, "raised", now or time.time())
        if fact.get("to"):
            entry["to"] = fact["to"]
        _save(state)
        return entry


def log(after=0, categories=None, limit=50):
    with _lock:
        state = _load()
    rows = [e for e in state["log"] if e["id"] > after and (categories is None or e["category"] in categories)]
    return {"latest": state["seq"], "alerts": rows[-limit:] if limit else []}


def for_user(entries, user):
    with _lock:
        state = _load()
    return [e for e in entries if not e.get("key") or e.get("event") or e.get("phase") == "resolved"
            or not _acknowledged(state, user, state["active"].get(e["key"], e))]


def active(categories=None, user=None):
    with _lock:
        state = _load()
    rows = []
    for row in state["active"].values():
        if row.get("event") or (categories is not None and row["category"] not in categories):
            continue
        result = {k: v for k, v in row.items() if k not in ("missing", "notice", "worsening_since")}
        if user is not None:
            result.update(acknowledged=_acknowledged(state, user, row), version=_version(row))
        rows.append(result)
    return rows


# ---------------------------------------------------------------- sources
def health_facts(overview):
    # One condition per resource, with every finding retained. The old loop
    # emitted duplicate disk keys, allowing the last finding to hide severity.
    grouped = {}
    for issue in overview.get("health_issues") or []:
        kind, name = issue.get("kind", ""), issue.get("name", "")
        key = f"health:{kind}:{name}"
        row = grouped.setdefault(key, {"kind": kind, "name": name, "issues": []})
        row["issues"].append(issue)
    facts = []
    for key, row in grouped.items():
        kind, name, issues = row["kind"], row["name"], row["issues"]
        critical = any(i.get("severity") == "critical" for i in issues)
        href = {"Node": "/nodes", "Disk": "/nodes", "Volume": "/volumes", "Backup": "/data-protection"}.get(kind, "/containers")
        title = {"Node": f"Host {name} is not ready", "Disk": f"Drive {name} needs attention",
                 "Volume": f"Volume {name} is {'faulted' if critical else 'degraded'}",
                 "Workload": f"{name.split('/')[-1]} is not ready",
                 "Backup": f"Backup protection needs attention: {name}"}.get(kind, f"{kind} {name}")
        resolved = {"Node": f"Host readiness warning cleared: {name}", "Volume": f"Volume warning cleared: {name}",
                    "Workload": f"Workload readiness warning cleared: {name.split('/')[-1]}",
                    "Disk": f"Drive warnings cleared: {name}", "Backup": f"Backup warning cleared: {name}"}.get(kind)
        signals = {i.get("metric") or "condition": i.get("value", 1) for i in issues}
        signals.update({"device:" + i["device_identity"]: 1 for i in issues if i.get("device_identity")})
        facts.append({"key": key, "category": "outage" if critical else "degraded",
                      "severity": "critical" if critical else "degraded", "title": title,
                      "resolved": resolved, "body": "; ".join(dict.fromkeys(i.get("reason", "") for i in issues)),
                      "signals": signals, "href": href})
    # Missing SMART observations cannot prove that a disk warning cleared.
    for node in overview.get("nodes") or []:
        prefix = f"health:Disk:{node.get('name', 'unknown')}/"
        temps = node.get("temps")
        if not temps or not temps.get("disks"):
            facts.append({"unknown_prefix": prefix})
        else:
            for disk in temps["disks"]:
                if not (disk.get("smart") or {}).get("available"):
                    facts.append({"unknown_key": prefix + disk.get("name", "unknown")})
    return facts


def job_facts(operations):
    return [{"key": f"jobs:{op['id']}", "category": "jobs", "severity": "degraded", "event": True,
             "title": f"Job failed: {op.get('title', 'Unnamed job')}", "body": op.get("message", ""),
             # The job itself, with its error - not the page its work is about.
             "href": f"/?job={op['id']}"}
            for op in operations if op.get("status") == "failed"]


def join_facts(nodes):
    """A host joining: said once, when a node first appears; again once it is Ready."""
    facts = []
    for node in nodes:
        meta = node.get("metadata") or {}
        name, uid = meta.get("name", "a host"), meta.get("uid") or meta.get("name")
        ready = any(c.get("type") == "Ready" and c.get("status") == "True"
                    for c in (node.get("status") or {}).get("conditions") or [])
        facts.append({"key": f"joins:{uid}:seen", "category": "joins", "event": True, "severity": "info",
                      "title": f"{name} is joining the cluster", "body": "Kubernetes registered this host. Waiting for readiness.",
                      "href": "/nodes"})
        if ready:
            facts.append({"key": f"joins:{uid}:ready", "category": "joins", "event": True, "severity": "info",
                          "title": f"Host {name} is ready", "body": "Kubernetes reports this host as Ready.", "href": "/nodes"})
    return facts


def upgrade_facts(report):
    """A Harvester upgrade starting, finishing or failing, and a new stable
    release to upgrade to. Each is said once."""
    facts = []
    for up in (report or {}).get("history") or []:
        name, version = up.get("name", ""), up.get("version", "")
        facts.append({"key": f"platform:{name}:started", "category": "updates", "event": True, "severity": "info",
                      "title": f"Harvester upgrade to {version} started", "body": "Hosts are upgraded one at a time",
                      "href": "/system/cluster"})
        if up.get("state") in ("succeeded", "failed"):
            ok = up["state"] == "succeeded"
            facts.append({"key": f"platform:{name}:{up['state']}", "category": "updates", "event": True,
                          "severity": "info" if ok else "critical",
                          "title": f"Harvester upgrade to {version} {'completed' if ok else 'failed'}",
                          "body": "" if ok else (up.get("message") or "See the Cluster page"),
                          "href": "/system/cluster"})
    stable = (report or {}).get("stable")
    if stable and (report or {}).get("current"):
        facts.append({"key": f"platform:release:{stable['tag']}", "category": "updates", "event": True,
                      "severity": "info", "title": f"Harvester {stable['tag']} is available",
                      "body": f"This cluster runs v{report['current']}", "href": "/system/cluster"})
    return facts


def update_facts(report):
    facts = []
    for workload in (report or {}).get("workloads") or []:
        if not workload.get("available"):
            continue
        targets = sorted(i.get("remote_digest") or i.get("candidate_tag") or ""
                         for i in workload.get("images") or [] if i.get("available"))
        tags = ", ".join(sorted({i.get("candidate_tag") for i in workload.get("images") or []
                                 if i.get("available") and i.get("candidate_tag")}))
        # Homestead's own release, and the helpers it runs, are updated from
        # Settings > Updates rather than as an app.
        own = workload.get("homestead")
        facts.append({"key": f"updates:{workload['ns']}/{workload['name']}:{'|'.join(targets)[:200]}",
                      "category": "updates", "severity": "info", "event": True,
                      "title": (f"Homestead {tags} is available" if own == "self" and tags
                                else f"Update available: {workload['name']}"),
                      "body": ("Review this update in Settings › Updates." if own
                               else f"A newer image is available{f' ({tags})' if tags else ''}"),
                      "href": "/settings?tab=updates"})
    return facts
