"""Image updates: checking workloads against their registries.

One workload's check (_check_deployment), the scan of every workload with its
progress, and the shared report with its refresh, reconcile and freshness
rules. Moved unchanged from homestead_updates (#623); import that module,
which re-exports these names. Names server.py or the tests replace on
homestead_updates are read through it (U.x), so those replacements still apply.
"""
import concurrent.futures
import json
import re
import threading
import time
import urllib.error

import homestead_forced as FORCED


def _pod_digest(pods, container):
    """The digest a container is running on: only from a container that is
    running - a Pending pod has no image yet (#380)."""
    for pod in pods:
        for status in pod.get("status", {}).get("containerStatuses", []) or []:
            if status.get("name") != container or "running" not in (status.get("state") or {}):
                continue
            image_id = status.get("imageID", "")
            match = re.search(r"(sha256:[0-9a-f]{64})", image_id)
            if match:
                return match.group(1)
    return ""
def _matching_pods(dep, pods):
    ns = dep["metadata"]["namespace"]
    labels = dep["spec"].get("selector", {}).get("matchLabels", {})
    return [p for p in pods if p["metadata"].get("namespace") == ns and
            all(p["metadata"].get("labels", {}).get(k) == v for k, v in labels.items())]
def _last_known(ns, name, container):
    """What the last scan found for this container, kept while the registry
    asks Homestead to slow down."""
    with _SCAN_LOCK:
        report = U._LATEST.get("report") or {}
    workload = next((w for w in report.get("workloads") or [] if (w.get("ns"), w.get("name")) == (ns, name)), {})
    image = next((i for i in workload.get("images") or [] if i.get("container") == container), {})
    if not image or image.get("rate_limited") or image.get("error"):
        return {}
    kept = {k: image[k] for k in ("current_digest", "remote_digest", "candidate", "candidate_tag", "available") if k in image}
    return dict(kept, stale=True) if kept else {}
def _names_tag(ref):
    """Whether a reference names a tag (postgres:16-alpine@sha256:...), not
    only a digest (postgres@sha256:...): parse_image reads none as latest."""
    return ":" in ref.split("@", 1)[0].rsplit("/", 1)[-1]


def followed(recorded, deployed):
    """The image reference a container's updates follow. The recorded tag
    holds only while the deployed image is Homestead's digest pin of that same
    repository; an image someone has since set to a tag, or to another
    repository, is what it follows, and the record is out of date - else a
    change from frigate:0.18.0 to :stable was still checked against 0.18.0,
    and its build offered as an update that rolled the app back."""
    if not recorded:
        return deployed
    try:
        now, then = parse_image(deployed), parse_image(recorded)
    except ValueError:
        return recorded
    # Only a bare digest pin (repo@sha256:...) of the same repository is
    # Homestead's own: one naming its tag (tag@digest, as git pins it) says
    # what it follows itself.
    own_pin = now["digest"] and not _names_tag(deployed) and now["base"] == then["base"]
    return recorded if own_pin else deployed


def _check_deployment(dep, pods, force=False, persist=True, channel=None):
    ns, name = dep["metadata"]["namespace"], dep["metadata"]["name"]
    tracked = U._annotation_json(dep, U.TRACKED)
    ran = U._annotation_json(dep, U.RAN)
    ran_now = dict(ran)
    auths = U._secret_credentials(ns, dep)
    mine = [p for p in _matching_pods(dep, pods) if (p.get("status") or {}).get("phase") == "Running"]
    # A stopped workload has no running digest to compare, which is expected
    # rather than a failed check: imports land scaled to zero on purpose.
    wanted = int(dep["spec"].get("replicas", 1) or 0) > 0
    running = bool(mine) and wanted
    own = U.PART(ns, name) == "self"
    channel = channel or (U.CHANNEL() if own else "prod")
    images = []
    for container in dep["spec"]["template"]["spec"].get("containers", []):
        deployed = container.get("image", "")
        source = followed(tracked.get(container["name"]), deployed)
        item = {"container": container["name"], "deployed": deployed, "source": source,
                "available": False, "current_digest": "", "remote_digest": "",
                "candidate": source, "candidate_tag": "", "error": "", "running": running}
        try:
            parsed = parse_image(source)
            running_digest = _pod_digest(mine, container["name"]) if running else ""
            if running_digest:
                ran_now[container["name"]] = running_digest
            # What last ran stands in only for a workload that is stopped. One
            # that should be running but is not yet - its host down, its pod
            # Pending - is checked once it runs: the record of what last ran
            # can be behind, and read as a "new build" that is not (#380).
            current = (parse_image(deployed).get("digest") or running_digest
                       or ("" if wanted else ran.get(container["name"], "")))
            candidate_tag = None
            if own:
                # What is installed: the newer of the tag recorded for this
                # image and the version running. An update made outside this
                # page (the installer, Helm) leaves the record behind, and
                # comparing with it offered releases older than the one running.
                known = [v for v in (parsed["tag"], U.VERSION()) if release_key(v)]
                version = max(known, key=release_key) if known else parsed["tag"]
                tags = U.registry_tags(source, auths, force)
                candidate_tag = channel_release(version, tags, channel)
                item["channel_behind"] = channel_behind(version, tags, channel)
            else:
                try:
                    candidate_tag = newer_semver(parsed["tag"], U.registry_tags(source, auths, force))
                except Exception:
                    candidate_tag = None
            if candidate_tag:
                candidate = parsed["base"] + ":" + candidate_tag
            elif not own and parsed["digest"] and _names_tag(source) and not semver(parsed["tag"]):
                # Pinned as tag@digest to a tag that is not a release
                # (postgres:16-alpine@sha256:...): follow the tag, as an
                # unpinned one is followed. Asking about the pin itself only
                # ever answers with the pin, so a rebuild went unseen (#874).
                candidate = source.split("@", 1)[0]
            else:
                candidate = source
            manifest = U.manifest_info(candidate, auths, force)
            remote = manifest["digest"]
            matches_remote = current in ([remote] + manifest["children"])
            item.update({"current_digest": current, "remote_digest": remote,
                         "candidate": candidate, "candidate_tag": candidate_tag or parsed["tag"],
                         # A newer tag is only an update while the image it
                         # points at is not the one already running. Offering
                         # the version you are on reads as a broken checker.
                         "available": bool(remote) and not matches_remote
                                      and bool(current or (candidate_tag and candidate != source))})
            if not current and wanted:
                # Pulling or starting: nothing has run yet to compare with the
                # registry. That is a check still to come, not a failed one.
                item["unchecked"] = True
                item["starting"] = True
            elif not current and candidate == source:
                # Stopped, and never seen running here: nothing to compare the
                # registry with. It is not "current" - it is unchecked.
                item["unchecked"] = True
            elif not own and parsed["digest"] and not _names_tag(source):
                # Pinned to a digest with no tag recorded to follow. Saying
                # nothing here reads as "up to date", which is not what it means.
                item["error"] = ("no release tag recorded for this image, so newer "
                                 "versions cannot be found — redeploy it from a tag")
        except urllib.error.HTTPError as error:
            if error.code == 429:
                # Rate limited: a check still to come, not a broken image. What
                # the last scan found stands meanwhile, marked as such.
                item.update(unchecked=True, rate_limited=True, **_last_known(ns, name, container["name"]))
            else:
                item["error"] = ("registry authentication required" if error.code in (401, 403)
                                 else f"registry returned HTTP {error.code}")
        except Exception as error:
            item["error"] = str(error)[:180]
        images.append(item)
    if persist and ran_now != ran:
        try:
            U.ksend("PATCH", f"/apis/apps/v1/namespaces/{ns}/deployments/{name}",
                    {"metadata": {"annotations": {U.RAN: json.dumps(ran_now, sort_keys=True)}}},
                    ctype="application/merge-patch+json")
        except Exception:
            pass
    return {"ns": ns, "name": name, "images": images,
            "unchecked": any(x.get("unchecked") for x in images),
            "available": any(x["available"] for x in images),
            "managed": U.managed_by(dep),
            # Flux or Argo CD applies it: the bump is made in git, and this is it (#735).
            "yaml": GITOPS.image_yaml(dep, {x["container"]: GITOPS.git_image(x) for x in images if x["available"]})
                    if U.managed_by(dep) and any(x["available"] for x in images) else "",
            "can_rollback": bool(U._annotation_json(dep, U.PREVIOUS)),
            "last_action": U._annotation(dep, U.LAST_ACTION)}
# A scan asks a registry about every workload, one network round trip each,
# so it takes as long as the slowest registry allows. Progress is published
# here as it goes, because a button that only goes quiet looks like one that
# did not work.
SCAN = {"running": False, "done": 0, "total": 0, "current": "",
        "started_at": 0.0, "finished_at": 0.0, "updates": 0}
_SCAN_LOCK = threading.Lock()
def scan_progress():
    with _SCAN_LOCK:
        state = dict(SCAN)
    state["elapsed"] = round(
        (time.time() if state["running"] else state["finished_at"]) - state["started_at"], 1
    ) if state["started_at"] else 0
    return state
def _scan_note(**fields):
    with _SCAN_LOCK:
        SCAN.update(fields)
def scan(force=False, homestead_only=False):
    channel = U.CHANNEL()
    deps = [d for d in U.kget("/apis/apps/v1/deployments").get("items", [])
            if d["metadata"]["namespace"] not in U.SYSTEM_NAMESPACES
            and not U._managed_smb(d["metadata"]["namespace"], d["metadata"]["name"])
            and (not homestead_only or U.PART(d["metadata"]["namespace"], d["metadata"]["name"]))]
    pods = U.kget("/api/v1/pods").get("items", [])
    _scan_note(running=True, done=0, total=len(deps), current="", updates=0,
               started_at=time.time(), finished_at=0.0)
    workloads = []
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(6, max(1, len(deps)))) as pool:
            pending = {pool.submit(U._check_deployment, dep, pods, force, channel=channel): dep for dep in deps}
            for future in concurrent.futures.as_completed(pending):
                dep = pending[future]
                try:
                    workloads.append(future.result())
                except Exception as error:
                    # One odd Deployment is that workload's problem, not a
                    # failed scan that throws away every answer beside it.
                    workloads.append({"ns": dep["metadata"]["namespace"],
                                      "name": dep["metadata"]["name"], "available": False,
                                      "can_rollback": False, "last_action": "",
                                      "images": [{"container": "", "available": False,
                                                  "error": str(error)[:180]}]})
                _scan_note(done=len(workloads), current=dep["metadata"]["name"],
                           updates=sum(1 for x in workloads if x["available"] and not x.get("homestead")))
    finally:
        _scan_note(running=False, current="", finished_at=time.time())
    for item in workloads:
        item["homestead"] = U.PART(item["ns"], item["name"])
    return _summary(workloads, channel=channel)
def _summary(workloads, checked_at=None, channel=None):
    workloads = sorted(workloads, key=lambda x: (x["ns"], x["name"]))
    apps = [x for x in workloads if not x["homestead"]]
    own = [x for x in workloads if x["homestead"]]
    return {"channel": channel or U.CHANNEL(),
            "checked_at": checked_at or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "updates": sum(1 for x in apps if x["available"]),
            # Checks a registry put off: the next quiet request tries again soon.
            "rate_limited": sum(1 for x in workloads for image in x["images"] if image.get("rate_limited")),
            "errors": sum(1 for x in apps for image in x["images"] if image.get("error")),
            "homestead": {"updates": sum(1 for x in own if x["available"]),
                          "errors": sum(1 for x in own for image in x["images"] if image.get("error"))},
            "workloads": workloads}
# The last finished scan, which every request shares. Before this, a quiet
# check could start a second scan beside a forced one, reset its progress,
# and hand the page an older answer than the one the button just found.
_LATEST = {"report": None, "number": 0, "finished": 0.0}
_STARTED = [0]      # scans begun so far; a clock is too coarse to order them
_RUN_LOCK = threading.Lock()
# How long a report stands before a quiet request checks the registries
# again: twice a day. Check images asks for a fresh one whenever it is pressed,
# and the leader checks on this same rhythm with no page open.
FRESH_FOR = 12 * 3600
RETRY_RATE_LIMITED = 1800
def _fresh_for(report):
    """A report with checks a registry put off is asked again in half an hour."""
    return RETRY_RATE_LIMITED if (report or {}).get("rate_limited") else FRESH_FOR
def invalidate():
    """Something changed an image: the next quiet request scans again."""
    FORCED.forget(*U.FORCED_KEYS)
    with _SCAN_LOCK:
        U._LATEST.update(report=None, number=0, finished=0.0)
def refresh_one(ns, name):
    """After an update or a rollback: ask again about this one app and put the
    answer in the last report, rather than throwing the whole report away -
    which left the page showing the update as waiting until every registry
    had been asked again."""
    FORCED.forget(*U.FORCED_KEYS)
    with _SCAN_LOCK:
        latest = U._LATEST.get("report")
    if not latest:
        return None
    dep = U.kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")
    pods = U.kget(f"/api/v1/namespaces/{ns}/pods").get("items", [])
    # It records what now runs, as a full scan does, so the record never
    # lags a rollout this replica saw (#380).
    item = U._check_deployment(dep, pods, True, persist=True, channel=latest.get("channel"))
    item["homestead"] = U.PART(ns, name)
    with _SCAN_LOCK:
        latest = U._LATEST.get("report")
        if not latest:
            return None
        rows = [w for w in latest.get("workloads") or [] if (w.get("ns"), w.get("name")) != (ns, name)] + [item]
        U._LATEST["report"] = _summary(rows, checked_at=latest.get("checked_at"), channel=latest.get("channel"))
    return item
RECONCILE_EVERY = 10
_RECONCILED = [0.0]
def reconcile(now=None):
    """Bring the last report in line with the cluster as it is now.

    The report is held by each replica. An update or rollback refreshes that
    app's line only on the replica that ran it (refresh_one); the other went on
    offering the update that was already installed - and its review then said
    no update was available. So before the report is handed out, each app whose
    images are not what its line was worked out from - updated, rolled back,
    edited with kubectl - is asked again, and one that is gone is dropped. One
    list of Deployments, at most every RECONCILE_EVERY seconds."""
    now = now or time.time()
    with _SCAN_LOCK:
        latest = U._LATEST.get("report")
        if not latest or now - _RECONCILED[0] < RECONCILE_EVERY:
            return []
        _RECONCILED[0] = now
    deps = {(d["metadata"]["namespace"], d["metadata"]["name"]): d
            for d in U.kget("/apis/apps/v1/deployments").get("items", [])}
    try:
        pods = U.kget("/api/v1/pods").get("items", [])
    except Exception:
        pods = None
    stale, gone = [], []
    for w in latest.get("workloads") or []:
        dep = deps.get((w.get("ns"), w.get("name")))
        if dep is None:
            gone.append((w.get("ns"), w.get("name")))
            continue
        now_images = {c["name"]: c.get("image", "") for c in dep["spec"]["template"]["spec"].get("containers", [])}
        seen = {i.get("container"): i.get("deployed") for i in w.get("images") or [] if i.get("container")}
        if seen and seen != now_images:
            stale.append((w["ns"], w["name"]))
            continue
        # Or what runs is not what the line compared: a pod that started
        # since, a restart onto a newer build, or one that was not running
        # then and is now (#380).
        if pods is not None:
            running = [p for p in _matching_pods(dep, pods) if (p.get("status") or {}).get("phase") == "Running"]
            for i in w.get("images") or []:
                digest = _pod_digest(running, i.get("container")) if i.get("container") else ""
                if digest and (digest != i.get("current_digest") or i.get("starting")):
                    stale.append((w["ns"], w["name"]))
                    break
    if gone:
        with _SCAN_LOCK:
            latest = U._LATEST.get("report")
            if latest:
                rows = [w for w in latest.get("workloads") or [] if (w.get("ns"), w.get("name")) not in gone]
                U._LATEST["report"] = _summary(rows, checked_at=latest.get("checked_at"), channel=latest.get("channel"))
    for ns, name in stale:
        try:
            U.refresh_one(ns, name)
        except Exception:
            pass                    # the line stays as it was; the next scan settles it
    return stale
def refresh_soon(ns, name):
    """refresh_one in the background; if it cannot, the next request scans again."""
    def run():
        try:
            U.refresh_one(ns, name)
        except Exception:
            invalidate()
    threading.Thread(target=run, name=f"update-refresh-{name}", daemon=True).start()
def homestead_report():
    """Homestead's own parts checked now, against their registries, and put
    into the last report in place of what it said of them - Settings ›
    Homestead's Check now asks about Homestead, not every app. With no report
    yet, the answer has Homestead's parts alone, and is not kept."""
    with _RUN_LOCK:
        fresh = U.scan(True, homestead_only=True)
        with _SCAN_LOCK:
            latest = U._LATEST["report"]
            if not latest:
                return dict(fresh, partial=True)
            merged = _summary([w for w in latest["workloads"] if not w.get("homestead")] + fresh["workloads"],
                              latest["checked_at"], channel=fresh["channel"])
            merged["homestead_checked_at"] = fresh["checked_at"]
            U._LATEST["report"] = merged
            return merged
def report(force=False):
    """The update report: one scan at a time, and the newest wins for everyone.

    A quiet request takes any report younger than FRESH_FOR. A forced one wants
    a scan that began after it asked, so it waits out one already running
    rather than trusting its older answers. Whoever waits reuses what the
    running scan found if that is good enough, instead of starting another.
    """
    if not force:
        try:
            reconcile()
        except Exception:
            pass                    # a report a little behind beats none
    with _SCAN_LOCK:
        latest, begun = dict(U._LATEST), _STARTED[0]
    if not force and latest["report"] and latest["report"].get("channel", "prod") == U.CHANNEL() and time.time() - latest["finished"] < _fresh_for(latest["report"]):
        return latest["report"]
    with _RUN_LOCK:
        with _SCAN_LOCK:
            latest = dict(U._LATEST)
        if latest["report"] and latest["report"].get("channel", "prod") == U.CHANNEL():
            if force and latest["number"] > begun:
                return latest["report"]
            if not force and time.time() - latest["finished"] < _fresh_for(latest["report"]):
                return latest["report"]
        with _SCAN_LOCK:
            _STARTED[0] += 1
            number = _STARTED[0]
        try:
            found = U.scan(force)
        except Exception:
            if latest["report"] and not force and latest["report"].get("channel", "prod") == U.CHANNEL():
                return latest["report"]
            raise
        with _SCAN_LOCK:
            U._LATEST.update(report=found, number=number, finished=time.time())
        return found


# Imported last, so this module can be imported before or after homestead_updates:
# both only look these names up when a function runs.
from homestead_update_registry import channel_behind, channel_release, newer_semver, parse_image, release_key, semver  # noqa: E402
import homestead_updates as U  # noqa: E402
import homestead_gitops as GITOPS  # noqa: E402
