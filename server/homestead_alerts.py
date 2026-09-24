"""What is worth telling someone who is not looking at Homestead.

Each source reports the facts it sees now: a node down, a job failed, a host
joined. The engine compares them with what it saw before and writes what has
changed to an alert log, which installed apps read after a push wakes them.

Two kinds of fact:
  * a condition (a node down) is announced once it has lasted HOLD seconds, so
    a workload restarting does not buzz a phone, and announced again as
    resolved once it has been gone as long;
  * an event (a job failed) is announced as soon as it is seen, once.

A source that cannot answer keeps what it said last time: Kubernetes being
unreachable is not every node coming back.
"""
import homestead_shared as SHARED
import json
import os
import threading
import time

DATA_DIR = "/data"
HOLD = 60
LOG_SIZE = 300
# Shared with any other Homestead replica on the same data volume.
_lock = SHARED.SharedLock("alerts")


def bind(data_dir):
    global DATA_DIR
    DATA_DIR = data_dir


def _path():
    return os.path.join(DATA_DIR, "alerts.json")


def _load():
    try:
        with open(_path(), encoding="utf-8") as handle:
            state = json.load(handle)
        if isinstance(state, dict):
            state.setdefault("seq", 0)
            state.setdefault("active", {})
            state.setdefault("log", [])
            return state
    except (OSError, ValueError):
        pass
    return {"seq": 0, "active": {}, "log": [], "seeded": False}


def _save(state):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = SHARED.temporary(_path())
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(state, handle)
    os.replace(tmp, _path())


def _append(state, fact, phase, now):
    state["seq"] += 1
    entry = {"id": state["seq"], "at": int(now), "phase": phase, "key": fact["key"],
             "category": fact["category"], "severity": fact.get("severity", "info"),
             "title": fact["title"] if phase == "raised" else fact.get("resolved") or f"Resolved: {fact['title']}",
             "body": fact.get("body", "") if phase == "raised" else "",
             "href": fact.get("href", "/")}
    state["log"] = (state["log"] + [entry])[-LOG_SIZE:]
    return entry


def observe(results, now=None):
    """Takes {source: [fact, ...] or None-for-failed} and returns the new log entries."""
    now = now or time.time()
    with _lock:
        state = _load()
        # A source seen for the first time - on a first start, or one a new
        # release added - has history, not news: its events start silent.
        watched = set(state.get("sources") or [])
        active, fresh, seen = state["active"], [], set()
        for source, facts in results.items():
            if facts is None:
                # Could not look: whatever it said before still stands.
                seen.update(k for k in active if k.startswith(source + ":"))
                continue
            seeding = source not in watched
            watched.add(source)
            for fact in facts:
                key = fact["key"]
                seen.add(key)
                row = active.get(key)
                if row is None:
                    row = active[key] = {**fact, "since": now, "announced": 0}
                else:
                    row.update(fact)
                row["missing"] = 0
                if row["announced"]:
                    continue
                if fact.get("event") and seeding:
                    # What was already true before Homestead watched is history.
                    row["announced"] = -1
                elif fact.get("event") or now - row["since"] >= HOLD:
                    entry = _append(state, row, "raised", now)
                    row["announced"] = entry["id"]
                    fresh.append(entry)
        for key in [k for k in active if k not in seen]:
            row = active[key]
            if not row.get("missing"):
                row["missing"] = now
            if now - row["missing"] < HOLD and not row.get("event"):
                continue
            del active[key]
            if row["announced"] > 0 and not row.get("event"):
                fresh.append(_append(state, row, "resolved", now))
        state["sources"] = sorted(watched)
        _save(state)
        return fresh


def note(fact, now=None):
    """Writes one entry straight to the log: a test, or anything already decided."""
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


def active(categories=None):
    """What is wrong right now, announced or about to be."""
    with _lock:
        state = _load()
    return [{k: v for k, v in row.items() if k not in ("missing",)}
            for row in state["active"].values()
            if not row.get("event") and (categories is None or row["category"] in categories)]


# ---------------------------------------------------------------- sources
def health_facts(overview):
    facts = []
    for issue in overview.get("health_issues") or []:
        kind, name = issue.get("kind", ""), issue.get("name", "")
        critical = issue.get("severity") == "critical"
        href = {"Node": "/nodes", "Disk": "/nodes", "Volume": "/volumes"}.get(kind, "/containers")
        title = {"Node": f"Node {name} is down", "Disk": f"Disk {name} needs attention",
                 "Volume": f"Volume {name} is {'faulted' if critical else 'degraded'}",
                 "Workload": f"{name.split('/')[-1]} is not healthy"}.get(kind, f"{kind} {name}")
        resolved = {"Node": f"Node {name} is back", "Volume": f"Volume {name} is healthy again",
                    "Workload": f"{name.split('/')[-1]} is healthy again",
                    "Disk": f"Disk {name} is fine again"}.get(kind)
        facts.append({"key": f"health:{kind}:{name}", "category": "outage" if critical else "degraded",
                      "severity": "critical" if critical else "degraded", "title": title,
                      "resolved": resolved, "body": issue.get("reason", ""), "href": href})
    return facts


def job_facts(operations):
    return [{"key": f"jobs:{op['id']}", "category": "jobs", "severity": "degraded", "event": True,
             "title": f"Failed: {op.get('title', 'a job')}", "body": op.get("message", ""),
             "href": op.get("href") or "/"}
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
                      "title": f"{name} is joining the cluster", "body": "Harvester has registered the new host",
                      "href": "/nodes"})
        if ready:
            facts.append({"key": f"joins:{uid}:ready", "category": "joins", "event": True, "severity": "info",
                          "title": f"{name} joined the cluster", "body": f"{name} is Ready", "href": "/nodes"})
    return facts


def upgrade_facts(report):
    """A Harvester upgrade starting, finishing or failing, and a new stable
    release to upgrade to. Each is said once."""
    facts = []
    for up in (report or {}).get("history") or []:
        name, version = up.get("name", ""), up.get("version", "")
        facts.append({"key": f"platform:{name}:started", "category": "health", "event": True, "severity": "info",
                      "title": f"Harvester upgrade to {version} started", "body": "Hosts are upgraded one at a time",
                      "href": "/system/cluster"})
        if up.get("state") in ("succeeded", "failed"):
            ok = up["state"] == "succeeded"
            facts.append({"key": f"platform:{name}:{up['state']}", "category": "health", "event": True,
                          "severity": "info" if ok else "critical",
                          "title": f"Harvester upgrade to {version} {'finished' if ok else 'failed'}",
                          "body": "" if ok else (up.get("message") or "See the Cluster page"),
                          "href": "/system/cluster"})
    stable = (report or {}).get("stable")
    if stable and (report or {}).get("current"):
        facts.append({"key": f"platform:release:{stable['tag']}", "category": "updates", "event": True,
                      "severity": "info", "title": f"Harvester {stable['tag']} is out",
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
        facts.append({"key": f"updates:{workload['ns']}/{workload['name']}:{'|'.join(targets)[:200]}",
                      "category": "updates", "severity": "info", "event": True,
                      "title": f"Update for {workload['name']}",
                      "body": f"A newer image is available{f' ({tags})' if tags else ''}",
                      "href": "/containers"})
    return facts
