"""Why each app restarted, kept past the next restart (#740).

Kubernetes keeps only a container's latest termination (lastState), and its
events for about an hour, so by morning a night of restarts has left one
reason. The leader looks at every app pod on its page-snapshot cycle, which
reads the pod list anyway, and writes one record for each restart it sees:

* restart  - a container's restartCount went up: ran out of memory (with its
             limit), failed its liveness check, crashed, its host restarted;
* replaced - a pod of the app went away and a newer one took its place:
             evicted, its host went away, or deleted;
* rollout  - the app's pod template changed: an image update, a restart from
             Homestead (or by its outage action), a rollout restart, settings.

Kept in restarts.json on the data volume: per app the newest KEEP records,
none older than KEEP_DAYS, at most MAX_APPS apps. Only the leader writes, under
a lock every replica shares, by replacing the file whole (homestead_shared). A
store that cannot be read is never written over; reading it says so.

Apps only: pods in the system namespaces are not recorded. Log tails are what
the app printed - untrusted data, which diagnostics already shows its viewers.
"""
import calendar
import json
import os
import re
import threading
import time

import homestead_outage as OUTAGE
import homestead_shared as SHARED
import homestead_workloads as WORKLOADS

FILE = "restarts.json"
# Whose each ReplicaSet is, as last read (_owners): the reader, when, and uid ->
# controller reference.
_known = {"read": None, "at": 0.0, "owners": {}}
KEEP = 20
KEEP_DAYS = 30
MAX_APPS = 300
LOG_LINES = 50
LOG_CHARS = 4000
LOG_READS = 3            # previous logs read per look; the rest say why not
EVENT_READS = 5
HASHES = 5              # templates an app is remembered to have had lately
OWNERS_FOR = 600        # whose each ReplicaSet is, read again at least this often
EVENT_WINDOW = 120      # an event this close to an exit may explain it
PENDING = 600           # how long an ended pod waits for its replacement
OUTAGE_WINDOW = 120      # an outage restart and the restartedAt it wrote
HOMESTEAD_RESTART = "homestead.io/restartedAt"
UPDATE_ROLLOUT = "homestead.io/update-rollout-at"
KUBECTL_RESTART = "kubectl.kubernetes.io/restartedAt"
SIGNALS = {137: "Killed (signal 9)", 139: "Crashed (segmentation fault)", 143: "Stopped by a signal (TERM)",
           134: "Crashed (aborted)"}
_lock = SHARED.SharedLock("restarts")


def _path():
    return os.path.join(SHARED.DIR, FILE)


def _at(text):
    try:
        return calendar.timegm(time.strptime(str(text), "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        return None


def _iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def memory(value):
    """A memory limit as a person reads it: 512Mi is 512 MiB."""
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(Ki|Mi|Gi|Ti|Pi|Ei|k|M|G|T|P|E)?", str(value or ""))
    if not m:
        return str(value or "")
    number, unit = m.groups()
    if not unit:
        return f"{round(float(number) / 1048576)} MiB"
    return f"{number} {unit[0].upper()}{'iB' if unit.endswith('i') else 'B'}"


def app_of(pod, owners):
    """(namespace, kind, name, uid) of the Deployment, StatefulSet or
    DaemonSet that controls a pod, else None. A ReplicaSet's pod belongs to
    the Deployment that controls the ReplicaSet (owners: ReplicaSet uid to its
    controller reference), never to a name that looks like one: a record is
    the workload's own, by kind and uid (security review)."""
    meta = pod.get("metadata") or {}
    ref = next((r for r in meta.get("ownerReferences") or [] if r.get("controller")), None)
    if not ref:
        return None
    if ref.get("kind") == "ReplicaSet":
        ref = owners.get(ref.get("uid"))
        if not ref or ref.get("kind") != "Deployment":
            return None
    elif ref.get("kind") not in ("StatefulSet", "DaemonSet"):
        return None
    if not ref.get("uid") or not ref.get("name"):
        return None
    return meta.get("namespace", ""), ref["kind"], ref["name"], ref["uid"]


def app_key(ns, kind, name, uid):
    return f"{ns}/{kind}/{name}/{uid}"


def _parts(key):
    ns, kind, name, uid = key.split("/", 3)
    return ns, kind, name, uid


def _template(pod):
    meta = pod.get("metadata") or {}
    labels, ann = meta.get("labels") or {}, meta.get("annotations") or {}
    return {"hash": labels.get("pod-template-hash") or labels.get("controller-revision-hash") or "",
            "images": {c["name"]: c.get("image", "") for c in (pod.get("spec") or {}).get("containers") or []},
            "ann": {k: ann[k] for k in (HOMESTEAD_RESTART, UPDATE_ROLLOUT, KUBECTL_RESTART) if k in ann}}


def reason(terminated, events):
    """A container's last termination in words. Events count only near the
    exit: a liveness failure an hour earlier is not this exit's reason."""
    code = terminated.get("exitCode")
    finished = _at(terminated.get("finishedAt"))
    events = [e for e in events if finished is None or _at(_event_time(e)) is None
              or abs(_at(_event_time(e)) - finished) <= EVENT_WINDOW]
    if terminated.get("reason") == "OOMKilled":
        return "Ran out of memory"
    if any("liveness" in str(e.get("message", "")).lower() and e.get("reason") in ("Killing", "Unhealthy")
           for e in events):
        return "Failed its liveness check"
    if terminated.get("reason") == "Unknown" or code == 255:
        return "Its host restarted"
    if code in SIGNALS:
        return SIGNALS[code]
    if code == 0:
        return "Exited"
    return f"Exited with code {code}" if code is not None else "Stopped"


def _event_time(event):
    return str(event.get("lastTimestamp") or event.get("eventTime") or event.get("firstTimestamp") or "")[:19] + "Z"


def _rollout_reason(old, new, ns, name, outage):
    """outage: the outage action's state per item ({"app:ns/name": {"last_restart"}})."""
    ann_old, ann_new = old.get("ann") or {}, new["ann"]
    if ann_new.get(HOMESTEAD_RESTART) and ann_new.get(HOMESTEAD_RESTART) != ann_old.get(HOMESTEAD_RESTART):
        at = _at(ann_new[HOMESTEAD_RESTART]) or 0
        last = ((outage or {}).get(f"app:{ns}/{name}") or {}).get("last_restart") or 0
        return "Restarted by its outage action" if abs(at - last) <= OUTAGE_WINDOW else "Restarted from Homestead"
    changed = sorted(img for c, img in new["images"].items() if (old.get("images") or {}).get(c) != img)
    if changed or ann_new.get(UPDATE_ROLLOUT) != ann_old.get(UPDATE_ROLLOUT):
        return "Updated to " + ", ".join(changed) if changed else "Updated"
    if ann_new.get(KUBECTL_RESTART) != ann_old.get(KUBECTL_RESTART):
        return "Restarted (rollout restart)"
    return "Its settings changed"


def _add(state, app, record, now):
    entry = state["apps"].setdefault(app, {"records": []})
    entry["records"] = ([record] + entry["records"])[:KEEP]
    entry["updated"] = now


def _trim(state, now):
    cutoff = now - KEEP_DAYS * 86400
    for app in list(state["apps"]):
        records = [r for r in state["apps"][app]["records"] if (_at(r.get("at")) or 0) >= cutoff]
        if records:
            state["apps"][app]["records"] = records
        else:
            del state["apps"][app]
    extra = len(state["apps"]) - MAX_APPS
    if extra > 0:
        for app in sorted(state["apps"], key=lambda a: state["apps"][a].get("updated", 0))[:extra]:
            del state["apps"][app]


def _ended(pod):
    meta = pod.get("metadata") or {}
    return (pod.get("status") or {}).get("phase") == "Failed" or bool(meta.get("deletionTimestamp"))


def _app_pods(pods, sys_ns, owners):
    current = {}
    for pod in (pods or {}).get("items") or []:
        app = app_of(pod, owners)
        if app and app[0] not in sys_ns:
            current[(pod.get("metadata") or {}).get("uid")] = (app_key(*app), pod)
    return current


def _restarted(before, current, first):
    """(uid, pod, status, went) for each container whose count went up."""
    if first:
        return []
    out = []
    for uid, (_, pod) in current.items():
        counts = ((before.get("seen") or {}).get(uid) or {}).get("counts") or {}
        for status in (pod.get("status") or {}).get("containerStatuses") or []:
            went = int(status.get("restartCount") or 0) - counts.get(status.get("name", ""), 0)
            if went > 0:
                out.append((uid, pod, status, went))
    return out


def _prefetch(before, current, first, read, tail, outage):
    """Everything slow, read before the lock is taken: events and previous
    logs of the containers that restarted, the hosts when a pod ended, and
    the outage action's restarts. Bounded by EVENT_READS and LOG_READS."""
    got = {"restarts": {}, "nodes": None, "outage": {}}
    logs, events = LOG_READS, EVENT_READS
    for uid, pod, status, _ in _restarted(before, current, first):
        meta = pod.get("metadata") or {}
        ns, name, container = meta.get("namespace", ""), meta.get("name", ""), status.get("name", "")
        last = (status.get("lastState") or {}).get("terminated") or {}
        found = {"events": []}
        if (last.get("exitCode") in (137, 143) or last.get("reason") == "Error") and events > 0:
            events -= 1
            try:
                found["events"] = read(f"/api/v1/namespaces/{ns}/events?fieldSelector=involvedObject.name%3D{name}").get("items") or []
            except Exception:
                pass
        if logs > 0:
            logs -= 1
            answer = tail(ns, name, container) or {}
            if answer.get("text") is not None:
                found["log_tail"] = "\n".join(str(answer["text"]).splitlines()[-LOG_LINES:])[-LOG_CHARS:]
            else:
                found["log_error"] = str(answer.get("error") or "not read")[:300]
        else:
            found["log_error"] = "too many restarts at once to read every log"
        got["restarts"][(uid, container, int(status.get("restartCount") or 0))] = found
    seen = before.get("seen") or {}
    if not first and any(not entry.get("done") and (uid not in current or _ended(current[uid][1]))
                         for uid, entry in seen.items()):
        try:
            got["nodes"] = {n["metadata"]["name"]: any(c.get("type") == "Ready" and c.get("status") == "True"
                            for c in (n.get("status") or {}).get("conditions") or [])
                            for n in read("/api/v1/nodes").get("items") or []}
        except Exception:
            got["nodes"] = None        # with no way to tell, no host is blamed
    try:
        got["outage"] = outage() or {}
    except Exception:
        got["outage"] = {}             # its restarts read as Homestead's own
    return got


def _owners(pods, read, now):
    """ReplicaSet uid -> its controller reference, for the ReplicaSets the
    pods name. The ReplicaSet list is the largest list Homestead reads (each
    carries a whole pod template, ten old ones per Deployment), and a
    ReplicaSet's controller is set when it is made (#980): the list is read
    only when a pod names a ReplicaSet not seen yet, or OWNERS_FOR after the
    last read, should one have been orphaned and adopted. What was learnt
    through one reader is not used for another (a test's own cluster)."""
    named = {ref.get("uid") for pod in (pods or {}).get("items") or []
             for ref in (pod.get("metadata") or {}).get("ownerReferences") or []
             if ref.get("controller") and ref.get("kind") == "ReplicaSet"}
    known = _known
    if known["read"] != read or now - known["at"] >= OWNERS_FOR or not named <= set(known["owners"]):
        sets = read("/apis/apps/v1/replicasets").get("items") or []
        owners = {(r.get("metadata") or {}).get("uid"): next((o for o in (r.get("metadata") or {}).get("ownerReferences") or []
                                                             if o.get("controller")), None) for r in sets}
        # A ReplicaSet a pod names but the list has not (made since): asked
        # again on the next look, not taken as nobody's for good.
        known = {"read": read, "at": now, "owners": owners}
        if named <= set(owners):
            _known.update(known)
    return known["owners"]


def observe(pods, now=None, read=None, tail=None, outage=None, sys_ns=None):
    """One look at the pod list (leader only). Returns how many records it wrote."""
    now = time.time() if now is None else now
    read = read or WORKLOADS.kget
    if tail is None:
        import homestead_container_diagnostics as DIAGNOSTICS
        tail = lambda ns, pod, container: DIAGNOSTICS.log(ns, pod, container, True, LOG_LINES)  # noqa: E731
    outage = outage or (lambda: OUTAGE._load().get("items") or {})
    try:
        owners = _owners(pods, read, now)
    except Exception as error:
        # Whose pod is whose cannot be told: a look on a guess could call a
        # running app's pods gone. This one changes nothing.
        print(f"restart record skipped: ReplicaSets not read: {str(error)[:160]}", flush=True)
        return 0
    current = _app_pods(pods, WORKLOADS.SYS_NS if sys_ns is None else sys_ns, owners)
    try:
        before = SHARED.read_json(_path(), {})
    except SHARED.Unreadable as error:
        print(f"restart record not kept: {error}", flush=True)
        return 0
    got = _prefetch(before, current, "since" not in before, read, tail, outage)

    def change(state):
        written = [0]
        # A look read before the last one that changed the store - a former
        # leader's thread finishing late - would put counts and templates
        # back; it changes nothing.
        if now < state.get("looked", 0):
            return 0
        was = json.dumps(state, sort_keys=True)
        first = "since" not in state
        state.setdefault("since", now)
        seen, templates = state.setdefault("seen", {}), state.setdefault("templates", {})
        state.setdefault("apps", {})
        earlier = set(seen)

        def add(key, record):
            if not first:
                _add(state, key, {"at": _iso(now), **record}, now)
                written[0] += 1

        # The app's template changes when a running pod shows one not seen for
        # it lately (a rollout: old and new pods side by side make one record,
        # and an old pod replaced mid-rollout is not news), or when every
        # running pod is back on an earlier one (a rollback).
        running = {}
        for uid, (key, pod) in current.items():
            if not _ended(pod):
                running.setdefault(key, []).append(pod)
        for key, pods_ in running.items():
            known = templates.get(key) or {}
            newest = {}
            for p in sorted(pods_, key=lambda p: (p.get("metadata") or {}).get("creationTimestamp") or ""):
                if _template(p)["hash"]:
                    newest[_template(p)["hash"]] = p
            if not newest:
                continue
            history = known.get("hashes") or []
            if not known:
                last = list(newest.values())[-1]
                templates[key] = {**_template(last), "hashes": list(newest)[-HASHES:]}
                continue
            fresh = [h for h in newest if h not in history and h != known.get("hash")]
            if fresh:
                target = newest[fresh[-1]]
            elif len(newest) == 1 and next(iter(newest)) != known.get("hash"):
                target = next(iter(newest.values()))
            else:
                continue
            tpl = _template(target)
            ns, _, name, _ = _parts(key)
            add(key, {"kind": "rollout", "pod": target["metadata"].get("name", ""),
                      "reason": _rollout_reason(known, tpl, ns, name, got["outage"]), "detail": ""})
            # Every template seen this look joins the history, the chosen one last.
            seen_hashes = [h for h in history + [known.get("hash")] + [h for h in newest if h != tpl["hash"]]
                           + [tpl["hash"]] if h]
            templates[key] = {**tpl, "hashes": list(dict.fromkeys(reversed(seen_hashes)))[:HASHES][::-1]}
        # Restarts in place.
        for uid, pod, status, went in _restarted(state, current, first):
            key, meta, spec = current[uid][0], pod.get("metadata") or {}, pod.get("spec") or {}
            container, total = status.get("name", ""), int(status.get("restartCount") or 0)
            found = got["restarts"].get((uid, container, total)) or {"events": [], "log_error": "not read on this look"}
            last = (status.get("lastState") or {}).get("terminated") or {}
            why = reason(last, found.get("events") or [])
            limit = next((((c.get("resources") or {}).get("limits") or {}).get("memory")
                          for c in spec.get("containers") or [] if c.get("name") == container), None)
            record = {"kind": "restart", "pod": meta.get("name", ""), "container": container, "reason": why,
                      "detail": f"limit {memory(limit)}" if why == "Ran out of memory" and limit else "",
                      "count": went, "total": total, "exit_code": last.get("exitCode"),
                      "finished_at": last.get("finishedAt", ""),
                      # The host it restarted on, for the timeline's Host filter (#871).
                      "node": spec.get("nodeName", ""),
                      **{k: found[k] for k in ("log_tail", "log_error") if k in found}}
            if went > 1:
                record["note"] = f"{went} restarts since the last look; Kubernetes kept the reason for the last one only"
            add(key, record)
        for uid, (key, pod) in current.items():
            meta, spec = pod.get("metadata") or {}, pod.get("spec") or {}
            entry = seen.setdefault(uid, {})
            entry.update(app=key, pod=meta.get("name", ""), hash=_template(pod)["hash"], node=spec.get("nodeName", ""),
                         created=meta.get("creationTimestamp", ""),
                         counts={s.get("name", ""): int(s.get("restartCount") or 0)
                                 for s in (pod.get("status") or {}).get("containerStatuses") or []})
        # A pod that ended (gone, evicted, being deleted) and a pod of the same
        # template that was not running beside it when it ended: replaced. A
        # stop or scale-down has no such pod, and is forgotten after PENDING.
        for uid in list(seen):
            old, live = seen[uid], current.get(uid)
            if live is not None and not _ended(live[1]):
                continue
            if not old.get("done"):
                if "ended_at" not in old:
                    old["ended_at"] = now
                    old["siblings"] = sorted(u for u, (k, p) in current.items()
                                             if k == old.get("app") and u != uid and u in earlier and not _ended(p))
                newer = [p for u, (k, p) in current.items() if k == old.get("app") and u != uid and not _ended(p)
                         and u not in old["siblings"] and _template(p)["hash"] == old.get("hash")]
                if newer:
                    status = (live[1].get("status") or {}) if live else {}
                    nodes = got["nodes"]
                    if status.get("reason") == "Evicted":
                        why, detail = "Evicted", str(status.get("message") or "")[:300]
                    elif old.get("node") and nodes is not None and not nodes.get(old["node"]):
                        why, detail = f"Its host went away ({old['node']})", ""
                    else:
                        why, detail = "Pod replaced", ""
                    add(old["app"], {"kind": "replaced", "reason": why, "detail": detail,
                                     "pod": old.get("pod", ""), "node": old.get("node", ""),
                                     "replaced_by": (newer[0].get("metadata") or {}).get("name", "")})
                    old["done"] = True
                elif now - old["ended_at"] > PENDING:
                    old["done"] = True
            if live is None and old.get("done"):
                del seen[uid]
        apps_now = {k for k, _ in current.values()}
        for key in [k for k in templates if k not in apps_now and k not in state["apps"]]:
            templates.pop(key, None)
        _trim(state, now)
        if json.dumps(state, sort_keys=True) != was:
            state["looked"] = now
        return written[0]

    try:
        return SHARED.update_file(_path(), change, _lock, {})
    except SHARED.Unreadable as error:
        print(f"restart record not kept: {error}", flush=True)
        return 0


_busy = threading.Lock()


def observe_later(pods):
    """observe(pods) on a thread of its own, so the page-snapshot cycle never
    waits for its log and event reads. One look at a time: while one runs,
    this one is skipped (False); restart counts add up, so the next look
    catches up."""
    if not _busy.acquire(blocking=False):
        return False
    read_at = time.time()        # when the pods were read, not when the thread runs

    def run():
        try:
            observe(pods, now=read_at)
        except Exception as error:
            print(f"restart record: {str(error)[:160]}", flush=True)
        finally:
            _busy.release()
    try:
        threading.Thread(target=run, name="restart-record", daemon=True).start()
    except Exception:
        _busy.release()
        raise
    return True


def _load():
    return SHARED.read_json(_path(), {})


def for_app(ns, name, kind="Deployment", uid=""):
    """The app's restart records, newest first, and since when Homestead has
    looked (None before its first look): those of this workload - kind and
    uid - only, so an app made again under a name starts with none. Raises
    SHARED.Unreadable."""
    state = _load()
    cutoff = time.time() - KEEP_DAYS * 86400
    records = [r for r in ((state.get("apps") or {}).get(app_key(ns, kind, name, uid)) or {}).get("records") or []
               if (_at(r.get("at")) or 0) >= cutoff]
    return {"since": state.get("since"), "records": records}


def recent(since):
    """Every app's records after since (epoch), newest first, without log
    tails (#748's timeline). Raises SHARED.Unreadable."""
    out = []
    for key, entry in ((_load().get("apps")) or {}).items():
        ns, kind, name, _ = _parts(key)
        out += [{"ns": ns, "kind": kind, "name": name, **{k: v for k, v in r.items() if k != "log_tail"}}
                for r in entry.get("records") or [] if (_at(r.get("at")) or 0) > since]
    return sorted(out, key=lambda r: r["at"], reverse=True)
