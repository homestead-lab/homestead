"""Registry update discovery and deterministic Kubernetes rollouts.

Only the Python standard library is used. Registry credentials are read from
the workload's Kubernetes imagePullSecrets and are never returned by the API or
written to the update history. Applied images are pinned by manifest digest so
rollback cannot accidentally re-pull a broken mutable tag.
"""
import base64
import homestead_names as NAMES
import calendar
import concurrent.futures
import hashlib
import json
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


kget = ksend = None
DEFAULT_NS = "lab"
DATA_DIR = "/data"
SYSTEM_NAMESPACES = set()
TRACKED = NAMES.key("update-sources")
PREVIOUS = NAMES.key("update-previous")
LAST_ACTION = NAMES.key("update-action")
# The digest each container last ran on, so a stopped workload can still be
# compared with the registry: without a pod there is nothing else to ask.
RAN = NAMES.key("ran-digests")
ROLLOUT_AT = NAMES.key("update-rollout-at")
MANIFEST_ACCEPT = ", ".join((
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.docker.distribution.manifest.v2+json",
))
_CACHE = {}
_CACHE_LOCK = threading.Lock()


def bind(_kget, _ksend, default_ns="lab", data_dir="/data", system_namespaces=None):
    global kget, ksend, DEFAULT_NS, DATA_DIR, SYSTEM_NAMESPACES
    kget, ksend, DEFAULT_NS, DATA_DIR = _kget, _ksend, default_ns, data_dir
    SYSTEM_NAMESPACES = set(system_namespaces or ())


def parse_image(ref):
    """Normalize a Docker/OCI reference without accepting URL syntax."""
    ref = (ref or "").strip()
    if not ref or "://" in ref or re.search(r"\s", ref):
        raise ValueError("invalid container image reference")
    name, digest = (ref.split("@", 1) + [""])[:2] if "@" in ref else (ref, "")
    slash, colon = name.rfind("/"), name.rfind(":")
    tag = name[colon + 1:] if colon > slash else "latest"
    if colon > slash:
        name = name[:colon]
    first = name.split("/", 1)[0]
    if "." in first or ":" in first or first == "localhost":
        registry, repo = (name.split("/", 1) + [""])[:2]
    else:
        registry, repo = "docker.io", name
    if not repo:
        raise ValueError("image repository is missing")
    if registry == "docker.io" and "/" not in repo:
        repo = "library/" + repo
    endpoint = "registry-1.docker.io" if registry == "docker.io" else registry
    base = f"{registry}/{repo}"
    return {"original": ref, "registry": registry, "endpoint": endpoint,
            "repo": repo, "base": base, "tag": tag, "digest": digest}


def with_tag(ref):
    """An image reference that says which tag it means: none is Docker's latest."""
    ref = (ref or "").strip()
    if not ref or "@" in ref:
        return ref
    name = ref.rsplit("/", 1)[-1]
    return ref if ":" in name else ref + ":latest"


def _suffix(key):
    return key.split("/", 1)[-1]


def _annotation(dep, key, default=""):
    return NAMES.annotation_of(dep.get("metadata", {}), _suffix(key), default)


def _write(annotations, key, value):
    annotations[key] = value


def _annotation_json(dep, key):
    try:
        parsed = json.loads(_annotation(dep, key, "{}") or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def _secret_credentials(ns, dep):
    names = [x.get("name") for x in
             dep["spec"]["template"]["spec"].get("imagePullSecrets", []) if x.get("name")]
    service_account = dep["spec"]["template"]["spec"].get("serviceAccountName", "default")
    try:
        sa = kget(f"/api/v1/namespaces/{ns}/serviceaccounts/{service_account}")
        names.extend(x.get("name") for x in sa.get("imagePullSecrets", []) if x.get("name"))
    except Exception:
        pass
    auths = {}
    for name in dict.fromkeys(names):
        try:
            secret = kget(f"/api/v1/namespaces/{ns}/secrets/{name}")
            raw = (secret.get("data") or {}).get(".dockerconfigjson")
            if not raw:
                continue
            cfg = json.loads(base64.b64decode(raw).decode())
            for host, value in (cfg.get("auths") or {}).items():
                host = host.replace("https://", "").replace("http://", "").rstrip("/")
                if host == "index.docker.io/v1":
                    host = "docker.io"
                token = value.get("auth", "")
                if token:
                    user, password = base64.b64decode(token).decode().split(":", 1)
                else:
                    user, password = value.get("username", ""), value.get("password", "")
                if user or password:
                    auths[host] = (user, password)
        except Exception:
            continue
    return auths


def _credential(auths, parsed):
    for key in (parsed["registry"], parsed["endpoint"],
                "docker.io" if parsed["registry"] == "docker.io" else ""):
        if key and key in auths:
            return auths[key]
    return None


def _open(req, credential=None, timeout=12):
    ctx = ssl.create_default_context()
    try:
        return urllib.request.urlopen(req, context=ctx, timeout=timeout)
    except urllib.error.HTTPError as error:
        if error.code != 401:
            raise
        challenge = error.headers.get("WWW-Authenticate", "")
        if not challenge.lower().startswith("bearer "):
            raise
        fields = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
        realm = fields.get("realm")
        if not realm:
            raise
        query = {k: v for k, v in fields.items() if k in ("service", "scope") and v}
        separator = "&" if "?" in realm else "?"
        token_req = urllib.request.Request(realm + (separator + urllib.parse.urlencode(query) if query else ""),
                                           headers={"Accept": "application/json"})
        if credential:
            raw = base64.b64encode(f"{credential[0]}:{credential[1]}".encode()).decode()
            token_req.add_header("Authorization", "Basic " + raw)
        with urllib.request.urlopen(token_req, context=ctx, timeout=timeout) as response:
            token_body = json.loads(response.read().decode())
        token = token_body.get("token") or token_body.get("access_token")
        if not token:
            raise PermissionError("registry authentication did not return a token")
        retry = urllib.request.Request(req.full_url, method=req.get_method(),
                                       headers=dict(req.header_items()))
        retry.add_header("Authorization", "Bearer " + token)
        return urllib.request.urlopen(retry, context=ctx, timeout=timeout)


def _registry_json(parsed, path, auths, accept="application/json"):
    url = f"https://{parsed['endpoint']}/v2/{parsed['repo']}/{path}"
    req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": "Homestead/2.0"})
    with _open(req, _credential(auths, parsed)) as response:
        body = response.read()
        return body, response.headers


def manifest_info(ref, auths=None, force=False):
    parsed = parse_image(ref)
    if parsed["digest"]:
        return {"digest": parsed["digest"], "children": []}
    key = "manifest:" + parsed["base"] + ":" + parsed["tag"]
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
    if cached and not force and time.time() - cached[0] < 300:
        return cached[1]
    body, headers = _registry_json(parsed, "manifests/" + urllib.parse.quote(parsed["tag"], safe=""),
                                   auths or {}, MANIFEST_ACCEPT)
    digest = headers.get("Docker-Content-Digest") or "sha256:" + hashlib.sha256(body).hexdigest()
    children = []
    try:
        manifest = json.loads(body.decode())
        children = [x.get("digest") for x in manifest.get("manifests", []) if x.get("digest")]
    except (UnicodeDecodeError, json.JSONDecodeError):
        pass
    info = {"digest": digest, "children": children}
    with _CACHE_LOCK:
        _CACHE[key] = (time.time(), info)
    return info


def manifest_digest(ref, auths=None, force=False):
    return manifest_info(ref, auths, force)["digest"]


def registry_tags(ref, auths=None, force=False):
    parsed = parse_image(ref)
    key = "tags:" + parsed["base"]
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
    if cached and not force and time.time() - cached[0] < 900:
        return cached[1]
    body, _ = _registry_json(parsed, "tags/list?n=1000", auths or {})
    tags = json.loads(body.decode()).get("tags") or []
    with _CACHE_LOCK:
        _CACHE[key] = (time.time(), tags)
    return tags


def semver(tag):
    match = re.fullmatch(r"(v?)(\d+)\.(\d+)\.(\d+)", tag or "")
    return (int(match.group(2)), int(match.group(3)), int(match.group(4))) if match else None


def newer_semver(current, tags):
    version = semver(current)
    if not version:
        return None
    valid = [(semver(tag), tag) for tag in tags]
    valid = [(v, tag) for v, tag in valid if v and v[0] == version[0] and v > version]
    return max(valid, default=(None, None))[1]


def _pod_digest(pods, container):
    for pod in pods:
        for status in pod.get("status", {}).get("containerStatuses", []) or []:
            if status.get("name") != container:
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


def _check_deployment(dep, pods, force=False):
    ns, name = dep["metadata"]["namespace"], dep["metadata"]["name"]
    tracked = _annotation_json(dep, TRACKED)
    ran = _annotation_json(dep, RAN)
    ran_now = dict(ran)
    auths = _secret_credentials(ns, dep)
    mine = _matching_pods(dep, pods)
    # A stopped workload has no running digest to compare, which is expected
    # rather than a failed check: imports land scaled to zero on purpose.
    running = bool(mine) and int(dep["spec"].get("replicas", 1) or 0) > 0
    images = []
    for container in dep["spec"]["template"]["spec"].get("containers", []):
        deployed = container.get("image", "")
        source = tracked.get(container["name"]) or deployed
        item = {"container": container["name"], "deployed": deployed, "source": source,
                "available": False, "current_digest": "", "remote_digest": "",
                "candidate": source, "candidate_tag": "", "error": "", "running": running}
        try:
            parsed = parse_image(source)
            running_digest = _pod_digest(mine, container["name"]) if running else ""
            if running_digest:
                ran_now[container["name"]] = running_digest
            current = (parse_image(deployed).get("digest") or running_digest
                       or ran.get(container["name"], ""))
            candidate_tag = None
            try:
                candidate_tag = newer_semver(parsed["tag"], registry_tags(source, auths, force))
            except Exception:
                candidate_tag = None
            if candidate_tag:
                candidate = parsed["base"] + ":" + candidate_tag
            else:
                candidate = source
            manifest = manifest_info(candidate, auths, force)
            remote = manifest["digest"]
            matches_remote = current in ([remote] + manifest["children"])
            item.update({"current_digest": current, "remote_digest": remote,
                         "candidate": candidate, "candidate_tag": candidate_tag or parsed["tag"],
                         # A newer tag is only an update while the image it
                         # points at is not the one already running. Offering
                         # the version you are on reads as a broken checker.
                         "available": bool(remote) and not matches_remote
                                      and bool(current or candidate_tag)})
            if not current and running:
                # Pulling or starting: nothing has run yet to compare with the
                # registry. That is a check still to come, not a failed one.
                item["unchecked"] = True
                item["starting"] = True
            elif not current and not candidate_tag:
                # Stopped, and never seen running here: nothing to compare the
                # registry with. It is not "current" - it is unchecked.
                item["unchecked"] = True
            elif not semver(parsed["tag"]) and parsed["digest"]:
                # Pinned to a digest with no release recorded to follow. Saying
                # nothing here reads as "up to date", which is not what it means.
                item["error"] = ("no release tag recorded for this image, so newer "
                                 "versions cannot be found — redeploy it from a tag")
        except urllib.error.HTTPError as error:
            item["error"] = ("registry authentication required" if error.code in (401, 403)
                             else f"registry returned HTTP {error.code}")
        except Exception as error:
            item["error"] = str(error)[:180]
        images.append(item)
    if ran_now != ran:
        try:
            ksend("PATCH", f"/apis/apps/v1/namespaces/{ns}/deployments/{name}",
                  {"metadata": {"annotations": {RAN: json.dumps(ran_now, sort_keys=True)}}},
                  ctype="application/merge-patch+json")
        except Exception:
            pass
    return {"ns": ns, "name": name, "images": images,
            "unchecked": any(x.get("unchecked") for x in images),
            "available": any(x["available"] for x in images),
            "can_rollback": bool(_annotation_json(dep, PREVIOUS)),
            "last_action": _annotation(dep, LAST_ACTION)}


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


def scan(force=False):
    deps = [d for d in kget("/apis/apps/v1/deployments").get("items", [])
            if d["metadata"]["namespace"] not in SYSTEM_NAMESPACES]
    pods = kget("/api/v1/pods").get("items", [])
    _scan_note(running=True, done=0, total=len(deps), current="", updates=0,
               started_at=time.time(), finished_at=0.0)
    workloads = []
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(6, max(1, len(deps)))) as pool:
            pending = {pool.submit(_check_deployment, dep, pods, force): dep for dep in deps}
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
                           updates=sum(1 for x in workloads if x["available"]))
    finally:
        _scan_note(running=False, current="", finished_at=time.time())
    workloads.sort(key=lambda x: (x["ns"], x["name"]))
    return {"checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "updates": sum(1 for x in workloads if x["available"]),
            "errors": sum(1 for x in workloads for image in x["images"] if image.get("error")),
            "workloads": workloads}


# The last finished scan, which every request shares. Before this, a quiet
# check could start a second scan beside a forced one, reset its progress,
# and hand the page an older answer than the one the button just found.
_LATEST = {"report": None, "number": 0, "finished": 0.0}
_STARTED = [0]      # scans begun so far; a clock is too coarse to order them
_RUN_LOCK = threading.Lock()
FRESH_FOR = 600


def invalidate():
    """Something changed an image: the next quiet request scans again."""
    with _SCAN_LOCK:
        _LATEST.update(report=None, number=0, finished=0.0)


def report(force=False):
    """The update report: one scan at a time, and the newest wins for everyone.

    A quiet request takes any report younger than FRESH_FOR. A forced one wants
    a scan that began after it asked, so it waits out one already running
    rather than trusting its older answers. Whoever waits reuses what the
    running scan found if that is good enough, instead of starting another.
    """
    with _SCAN_LOCK:
        latest, begun = dict(_LATEST), _STARTED[0]
    if not force and latest["report"] and time.time() - latest["finished"] < FRESH_FOR:
        return latest["report"]
    with _RUN_LOCK:
        with _SCAN_LOCK:
            latest = dict(_LATEST)
        if latest["report"]:
            if force and latest["number"] > begun:
                return latest["report"]
            if not force and time.time() - latest["finished"] < FRESH_FOR:
                return latest["report"]
        with _SCAN_LOCK:
            _STARTED[0] += 1
            number = _STARTED[0]
        try:
            found = scan(force)
        except Exception:
            if latest["report"] and not force:
                return latest["report"]
            raise
        with _SCAN_LOCK:
            _LATEST.update(report=found, number=number, finished=time.time())
        return found


# An env var pinning the version in the Deployment outlives the image it
# described: the image bakes HOMESTEAD_VERSION in itself, so a copy written
# into the pod spec only ever goes stale. Updating an image drops it.
PINNED_VERSION_ENV = ("HOMESTEAD_VERSION",)


def _pod_containers(dep):
    """Every container in the pod template, init containers included.

    An init container that shares an image with the app - the one that fixes
    ownership on the data volume before Homestead starts - has to move with it.
    Kubernetes keeps their names unique across both lists, so one map covers
    both.
    """
    spec = dep["spec"]["template"]["spec"]
    return list(spec.get("initContainers", []) or []) + list(spec.get("containers", []) or [])


def _same_repository(one, other):
    try:
        return parse_image(one)["base"] == parse_image(other)["base"]
    except ValueError:
        return False


def _drop_pinned_version(container):
    env = container.get("env")
    if not env:
        return
    kept = [item for item in env if item.get("name") not in PINNED_VERSION_ENV]
    if len(kept) == len(env):
        return
    if kept:
        container["env"] = kept
    else:
        container.pop("env", None)


def _immutable(ref, digest):
    parsed = parse_image(ref)
    return parsed["base"] + "@" + digest


def _history(event):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        path = os.path.join(DATA_DIR, "image-update-history.jsonl")
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, separators=(",", ":")) + "\n")
    except OSError:
        pass


def apply_update(ns, name):
    dep = kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")
    pods = kget("/api/v1/pods").get("items", [])
    check = _check_deployment(dep, pods, True)
    chosen = {x["container"]: x for x in check["images"] if x["available"]}
    if not chosen:
        raise ValueError("no image update is currently available")
    annotations = dep["metadata"].setdefault("annotations", {})
    tracked = _annotation_json(dep, TRACKED)
    before = {c["name"]: c.get("image", "") for c in _pod_containers(dep)}
    # Make the rollback target immutable too. On a first managed update the
    # Deployment may still contain a mutable tag even though the pod status
    # tells us the exact manifest that is running.
    for image in check["images"]:
        if image.get("current_digest"):
            before[image["container"]] = _immutable(image["source"], image["current_digest"])
    _write(annotations, PREVIOUS, json.dumps({"images": before, "sources": tracked,
                                        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
                                       separators=(",", ":")))
    for container in dep["spec"]["template"]["spec"].get("containers", []):
        update = chosen.get(container["name"])
        if not update:
            continue
        tracked[container["name"]] = update["candidate"]
        container["image"] = _immutable(update["candidate"], update["remote_digest"])
        container["imagePullPolicy"] = "IfNotPresent"
        _drop_pinned_version(container)
    # An init container built from the same image is the same release, so it
    # follows the container it belongs to rather than staying on the tag the
    # workload was first installed with.
    for init in dep["spec"]["template"]["spec"].get("initContainers", []) or []:
        update = next((chosen[name] for name, item in chosen.items()
                       if _same_repository(init.get("image", ""), before.get(name, ""))), None)
        if not update:
            continue
        tracked[init["name"]] = update["candidate"]
        init["image"] = _immutable(update["candidate"], update["remote_digest"])
        init["imagePullPolicy"] = "IfNotPresent"
        _drop_pinned_version(init)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _write(annotations, TRACKED, json.dumps(tracked, separators=(",", ":")))
    _write(annotations, LAST_ACTION, "update " + now)
    dep["spec"]["template"].setdefault("metadata", {}).setdefault("annotations", {})[ROLLOUT_AT] = now
    result = ksend("PUT", f"/apis/apps/v1/namespaces/{ns}/deployments/{name}", dep)
    _history({"at": now, "action": "update", "namespace": ns, "deployment": name,
              "before": before,
              "after": {c["name"]: c["image"] for c in _pod_containers(result)}})
    return progress(ns, name, result)


def rollback(ns, name):
    dep = kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")
    previous = _annotation_json(dep, PREVIOUS)
    restore = previous.get("images") or {}
    if not restore:
        raise ValueError("no managed update is available to roll back")
    current = {c["name"]: c.get("image", "") for c in _pod_containers(dep)}
    for container in _pod_containers(dep):
        if container["name"] in restore:
            container["image"] = restore[container["name"]]
            container["imagePullPolicy"] = "IfNotPresent"
    annotations = dep["metadata"].setdefault("annotations", {})
    _write(annotations, PREVIOUS, json.dumps({"images": current,
                                        "sources": _annotation_json(dep, TRACKED),
                                        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
                                       separators=(",", ":")))
    if isinstance(previous.get("sources"), dict):
        _write(annotations, TRACKED, json.dumps(previous["sources"], separators=(",", ":")))
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _write(annotations, LAST_ACTION, "rollback " + now)
    dep["spec"]["template"].setdefault("metadata", {}).setdefault("annotations", {})[ROLLOUT_AT] = now
    result = ksend("PUT", f"/apis/apps/v1/namespaces/{ns}/deployments/{name}", dep)
    _history({"at": now, "action": "rollback", "namespace": ns, "deployment": name,
              "before": current, "after": restore})
    return progress(ns, name, result)


PULL_IMAGE = re.compile(r'image\s+"([^"]+)"')
PULL_TOOK = re.compile(r"\sin\s+([0-9hms.]+)")


def _event_age(event):
    stamp = (event.get("lastTimestamp") or event.get("eventTime") or
             (event.get("metadata") or {}).get("creationTimestamp") or "")
    try:
        moment = time.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S")
    except (TypeError, ValueError):
        return 0
    return max(0, int(time.time() - calendar.timegm(moment)))


def pull_state(namespace, pod):
    """What the kubelet says about fetching this pod's image.

    Kubernetes reports no byte or layer progress for an image pull - the
    kubelet only emits Pulling and Pulled events - so this reports the phase,
    the image, the node and how long it has been going rather than inventing a
    percentage.
    """
    try:
        events = kget(f"/api/v1/namespaces/{namespace}/events"
                      f"?fieldSelector=involvedObject.name={pod}").get("items", [])
    except Exception:
        return {}
    relevant = [event for event in events
                if event.get("reason") in ("Pulling", "Pulled", "Failed", "BackOff")]
    if not relevant:
        return {}
    relevant.sort(key=_event_age)   # smallest age first: the newest event wins
    latest = relevant[0]
    reason, message = latest.get("reason", ""), latest.get("message", "") or ""
    image = (PULL_IMAGE.search(message) or [None, ""])[1] if PULL_IMAGE.search(message) else ""
    if reason == "Pulling":
        return {"state": "pulling", "image": image, "seconds": _event_age(latest),
                "detail": message[:220]}
    if reason == "Pulled":
        took = PULL_TOOK.search(message)
        return {"state": "pulled", "image": image, "seconds": _event_age(latest),
                "took": took.group(1) if took else "", "detail": message[:220]}
    return {"state": "failed", "image": image, "seconds": _event_age(latest),
            "detail": message[:220]}


STUCK_REASONS = {"FailedAttachVolume", "FailedMount", "FailedScheduling"}
STUCK_AFTER = 180


def _why_waiting(ns, pod):
    """The newest warning about a pod that has not started, once it has been
    waiting a minute; stuck when a volume or scheduling warning has lasted
    three."""
    age = _age(pod.get("metadata", {}).get("creationTimestamp"))
    if age < 60:
        return None
    name = pod["metadata"]["name"]
    try:
        events = kget(f"/api/v1/namespaces/{ns}/events?fieldSelector="
                      f"{urllib.parse.quote(f'involvedObject.name={name},type=Warning')}").get("items", [])
    except Exception:
        return None
    if not events:
        return None
    latest = max(events, key=lambda e: e.get("lastTimestamp") or e.get("eventTime") or "")
    message = " ".join(str(latest.get("message") or latest.get("reason") or "").split())[:300]
    found = {"message": message, "stuck": latest.get("reason") in STUCK_REASONS and age >= STUCK_AFTER}
    if "invalid controller count" in message:
        # Two pods on different nodes attached a volume on a migratable class,
        # and Longhorn took that for a VM live migration. Nothing clears it but
        # every pod letting go of the volume.
        found["hint"] = ("Two pods on different nodes attached this volume, and Longhorn is treating it as "
                         "a VM migration. Scale the workload to 0, wait for the volume to detach, then scale "
                         "it back up. Recreate updates, or a shareable class, stop it happening again.")
    return found


def _age(stamp):
    try:
        return time.time() - calendar.timegm(time.strptime(str(stamp), "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        return 0


def progress(ns, name, dep=None):
    dep = dep or kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")
    pods = _matching_pods(dep, kget("/api/v1/pods").get("items", []))
    spec, status = dep.get("spec", {}), dep.get("status", {})
    desired = int(spec.get("replicas", 0) or 0)
    replicas = int(status.get("replicas", 0) or 0)
    updated = int(status.get("updatedReplicas", 0) or 0)
    ready = int(status.get("readyReplicas", 0) or 0)
    available = int(status.get("availableReplicas", 0) or 0)
    unavailable = int(status.get("unavailableReplicas", 0) or 0)
    generation = int(dep["metadata"].get("generation", 0) or 0)
    observed = int(status.get("observedGeneration", 0) or 0)
    problems = []
    pod_rows = []
    fatal = {"ImagePullBackOff", "ErrImagePull", "CrashLoopBackOff", "CreateContainerConfigError"}
    for pod in pods:
        waits = []
        for cs in pod.get("status", {}).get("containerStatuses", []) or []:
            waiting = (cs.get("state", {}).get("waiting") or {})
            if waiting:
                waits.append({"container": cs.get("name", ""), "reason": waiting.get("reason", "Waiting"),
                              "message": waiting.get("message", "")[:220]})
                if waiting.get("reason") in fatal:
                    problems.append(f"{pod['metadata']['name']}: {waiting.get('reason')}")
        row = {"name": pod["metadata"]["name"], "phase": pod.get("status", {}).get("phase", ""),
               "node": pod.get("spec", {}).get("nodeName", ""), "waiting": waits}
        running = all(cs.get("ready") for cs in
                      pod.get("status", {}).get("containerStatuses", []) or [{}])
        if not running:
            row["pull"] = pull_state(ns, row["name"])
            # PodInitializing and ContainerCreating say only that a pod is
            # waiting; its warning events say for what - most often a volume
            # it cannot attach or mount.
            blocked = _why_waiting(ns, pod)
            if blocked:
                row["blocked"] = blocked["message"]
                if blocked.get("hint"):
                    row["blocked"] += " — " + blocked["hint"]
                if blocked["stuck"] or blocked.get("hint"):
                    problems.append(f"{row['name']}: {blocked['message']}" + (f" {blocked['hint']}" if blocked.get("hint") else ""))
        pod_rows.append(row)
    for condition in status.get("conditions", []) or []:
        if condition.get("type") == "Progressing" and condition.get("status") == "False":
            problems.append(condition.get("message") or condition.get("reason") or "rollout failed")
    # During a maxSurge rollout, an old ready pod can satisfy readyReplicas while
    # the new updated pod is still pulling. Do not declare success until the
    # surge pod has replaced it and every desired updated replica is available.
    complete = (observed >= generation and replicas == desired and updated == desired and
                ready == desired and available == desired and unavailable == 0)
    phase = "failed" if problems else ("ready" if complete else "progressing")
    # The pull worth reporting is the one still running, else the last failure.
    pulls = [dict(row["pull"], node=row["node"], pod=row["name"])
             for row in pod_rows if row.get("pull")]
    pull = (next((row for row in pulls if row["state"] == "pulling"), None) or
            next((row for row in pulls if row["state"] == "failed"), None) or
            (pulls[0] if pulls else {}))
    return {"ns": ns, "name": name, "phase": phase, "desired": desired, "pull": pull,
            "replicas": replicas, "updated": updated, "ready": ready,
            "available": available, "unavailable": unavailable, "generation": generation,
            "observed_generation": observed, "pods": pod_rows, "problems": problems,
            "images": {c["name"]: c.get("image", "") for c in
                       spec.get("template", {}).get("spec", {}).get("containers", [])},
            "can_rollback": bool(_annotation_json(dep, PREVIOUS)),
            "last_action": _annotation(dep, LAST_ACTION)}
