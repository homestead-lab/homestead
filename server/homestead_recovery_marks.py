"""Whether a tool applying an app took Homestead's restart mark off it.

An app moved by restarting (homestead_recovery) stays as restarted only while the
tool that applies it leaves the mark alone: taking it off changes the pod template,
so the app restarts a second time. Homestead can't know a tool's interval or how it
treats a field it doesn't own, so it looks: for a while after each such restart.
"""
import homestead_names as NAMES

MARK = NAMES.key("restartedAt")
MARK_WATCH = 7200   # two hours: a sync is minutes away on any setup worth one


def check(marks, deployments, now):
    """marks: {app id: {"value", "by", "at"}}. Returns (still watched, undone):
    undone are the ids whose mark has gone. A different value is a person restarting
    it, and a Deployment gone is nothing to watch, so both are forgotten."""
    templates = {}
    for dep in deployments:
        meta = dep.get("metadata") or {}
        templates[f"{meta.get('namespace')}/{meta.get('name')}"] = (dep.get("spec") or {}).get("template") or {}
    still, undone = {}, {}
    for app, mark in marks.items():
        if app not in templates:
            continue
        value = ((templates[app].get("metadata") or {}).get("annotations") or {}).get(MARK)
        if value is None:
            undone[app] = mark
        elif value == mark.get("value") and now - mark.get("at", 0) < MARK_WATCH:
            still[app] = mark
    return still, undone
