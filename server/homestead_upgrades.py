"""Harvester releases, and the upgrade in progress - shown, never started.

Starting a Harvester upgrade rewrites every host and cannot be undone from
here, so Homestead leaves that to Harvester's own dashboard and does the two
things around it:

* what there is to upgrade to: the releases Harvester publishes, split into
  stable and test (release candidates and development builds), newest first,
  with which of them this cluster's Harvester itself offers - only those
  have an Upgrade button in Harvester;
* how an upgrade is going: Harvester's Upgrade object walks through images,
  repository, node preparation, system services and then each node in turn,
  and each step is shown as it lands.
"""
import json
import re
import time
import urllib.request

kget = None
HNS = "harvester-system"
API = "/apis/harvesterhci.io/v1beta1"
RELEASES_URL = "https://api.github.com/repos/harvester/harvester/releases?per_page=40"
RELEASE_TTL = 3600
_releases = {"at": 0.0, "value": [], "error": ""}
STEPS = [("ImageReady", "Upgrade image"), ("RepoReady", "Package repository"),
         ("NodesPrepared", "Nodes prepared"), ("SystemServicesUpgraded", "System services"),
         ("NodesUpgraded", "Nodes upgraded")]
fetch = None


def bind(_kget, _fetch=None):
    global kget, fetch
    kget = _kget
    fetch = _fetch or _github


def _github(url):
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                                   "User-Agent": "homestead"})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def version_key(tag):
    """Orders v1.4.2 above v1.4.2-rc3 above v1.4.1; unknown shapes sort last."""
    match = re.match(r"^v?(\d+)\.(\d+)(?:\.(\d+))?(?:-(.+))?$", str(tag or "").strip())
    if not match:
        return (-1, -1, -1, 0, "")
    major, minor, patch, pre = match.groups()
    return (int(major), int(minor), int(patch or 0), 0 if pre else 1, pre or "")


def channel(tag, prerelease=False):
    """stable, or the kind of test build a tag is."""
    pre = (version_key(tag)[4] or "").lower()
    if not pre and not prerelease:
        return "stable"
    for kind in ("rc", "beta", "alpha", "dev", "head"):
        if kind in pre:
            return "dev" if kind == "head" else kind
    return "test"


def releases(force=False):
    """Harvester's published releases, newest first, cached for an hour."""
    if force or time.monotonic() - _releases["at"] > RELEASE_TTL or not _releases["at"]:
        try:
            rows = []
            for item in fetch(RELEASES_URL) or []:
                if item.get("draft"):
                    continue
                tag = item.get("tag_name", "")
                rows.append({"tag": tag, "name": item.get("name") or tag,
                             "channel": channel(tag, bool(item.get("prerelease"))),
                             "published": item.get("published_at", ""), "url": item.get("html_url", "")})
            rows.sort(key=lambda row: version_key(row["tag"]), reverse=True)
            _releases.update(value=rows, error="", at=time.monotonic())
        except Exception as error:
            # Keep what was known; say why it is not fresher.
            _releases.update(error=str(error)[:160], at=time.monotonic() - RELEASE_TTL + 300)
    return _releases["value"], _releases["error"]


def offered():
    """The versions this cluster's Harvester has been told it can upgrade to."""
    try:
        items = kget(f"{API}/namespaces/{HNS}/versions").get("items", [])
    except Exception:
        return []
    out = []
    for item in items:
        spec = item.get("spec", {}) or {}
        out.append({"version": item["metadata"]["name"], "released": spec.get("releaseDate", ""),
                    "min_upgradable": spec.get("minUpgradableVersion", ""), "tags": spec.get("tags") or []})
    return sorted(out, key=lambda row: version_key(row["version"]), reverse=True)


def _upgrade_row(item):
    meta, spec, status = item.get("metadata", {}), item.get("spec", {}) or {}, item.get("status", {}) or {}
    conditions = {c.get("type"): c for c in status.get("conditions", []) or []}
    completed = conditions.get("Completed", {})
    steps = []
    for key, label in STEPS:
        condition = conditions.get(key)
        state = ("done" if condition and condition.get("status") == "True"
                 else "failed" if condition and condition.get("status") == "False" and condition.get("reason")
                 else "waiting")
        steps.append({"key": key, "label": label, "state": state,
                      "message": (condition or {}).get("message", "")[:200]})
    if completed.get("status") == "True":
        state = "succeeded"
    elif completed.get("status") == "False" and (completed.get("reason") or completed.get("message")):
        state = "failed"
    else:
        state = "running"
    # The step under way is the first one not done.
    current = next((step for step in steps if step["state"] != "done"), None)
    if state == "running" and current and current["state"] == "waiting":
        current["state"] = "running"
    nodes = [{"name": name, "state": (row or {}).get("state", ""), "reason": (row or {}).get("reason", ""),
              "message": ((row or {}).get("message") or "")[:200]}
             for name, row in sorted((status.get("nodeStatuses") or {}).items())]
    done = sum(1 for step in steps if step["state"] == "done")
    return {"name": meta.get("name", ""), "version": spec.get("version", ""),
            "previous": status.get("previousVersion", ""), "started": meta.get("creationTimestamp", ""),
            "latest": (meta.get("labels") or {}).get("harvesterhci.io/latestUpgrade") == "true",
            "state": state, "message": (completed.get("message") or "")[:300], "steps": steps,
            "progress": 100 if state == "succeeded" else int(done * 100 / len(STEPS)), "nodes": nodes}


def upgrades():
    try:
        items = kget(f"{API}/namespaces/{HNS}/upgrades").get("items", [])
    except Exception:
        return []
    rows = [_upgrade_row(item) for item in items]
    return sorted(rows, key=lambda row: row["started"], reverse=True)


def report(current, force=False):
    """What the Cluster page shows: where this cluster is, what is newer, and
    any upgrade under way or just finished."""
    rows, error = releases(force)
    here = version_key(current)
    newer = [row for row in rows if version_key(row["tag"]) > here] if current else rows
    stable = next((row for row in newer if row["channel"] == "stable"), None)
    newest_stable = next((row for row in rows if row["channel"] == "stable"), None)
    test = next((row for row in newer if row["channel"] != "stable"
                 and (not newest_stable or version_key(row["tag"]) > version_key(newest_stable["tag"]))), None)
    offers = offered()
    offer_names = {row["version"].lstrip("v") for row in offers}
    # Copies: the release list is cached and shared.
    stable, test = [dict(row, offered=row["tag"].lstrip("v") in offer_names) if row else None
                    for row in (stable, test)]
    history = upgrades()
    active = next((row for row in history if row["state"] == "running"), None)
    return {"current": current, "stable": stable, "test": test, "offered": offers,
            "recent": rows[:12], "error": error, "active": active,
            "last": active or (history[0] if history else None),
            "history": history[:5]}
