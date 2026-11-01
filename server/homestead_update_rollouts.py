"""Image updates: applying an update or a rollback, and following it.

Prepares and commits an update or a rollback (pinned by digest), then reports
its rollout: image pulls, pods waiting and why. Moved unchanged from
homestead_updates (#623): read and replace these names here. Names server.py
binds on homestead_updates are read through it (U.x), so a replacement there
still applies.
"""
import calendar
import copy
import json
import os
import re
import time
import urllib.parse
import homestead_rollout_capacity as ROLLOUT
import homestead_shared as SHARED


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
        path = os.path.join(U.DATA_DIR, "image-update-history.jsonl")
        with SHARED.write_scope(path):
            os.makedirs(U.DATA_DIR, exist_ok=True)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, separators=(",", ":")) + "\n")
    except OSError:
        pass
def _review_pods(dep):
    inventory = U.kget("/api/v1/pods")
    if not isinstance(inventory.get("items"), list) or (inventory.get("metadata") or {}).get("continue"):
        raise ValueError("complete pod inventory is required before reviewing an image change")
    if not _matching_pods(dep, inventory["items"]):
        return []
    owned, known = ROLLOUT.owned_pods(dep, inventory["items"], U.kget, dep["metadata"]["namespace"])
    if not known:
        raise ValueError("pod ownership is unknown; refresh before reviewing image recovery")
    return owned
def _previous_image(dep, container, pods, fallback_digest=""):
    """Pin recovery from this container's observation, never another container."""
    ref, name = container.get("image", ""), container["name"]
    digest = parse_image(ref).get("digest", "")
    if not digest:
        observed = set()
        for pod in pods:
            spec = pod.get("spec") or {}
            actual = next((c for c in list(spec.get("containers") or []) +
                           list(spec.get("initContainers") or []) if c.get("name") == name), {})
            if actual.get("image") != ref:
                continue
            status = pod.get("status") or {}
            for row in list(status.get("containerStatuses") or []) + list(status.get("initContainerStatuses") or []):
                if row.get("name") == name:
                    match = re.search(r"(sha256:[0-9a-f]{64})", row.get("imageID", ""))
                    if match:
                        observed.add(match.group(1))
        if len(observed) > 1:
            raise ValueError(f"{name} has multiple running digests; no single immutable rollback target is known")
        digest = next(iter(observed), "") or fallback_digest
        if not digest and int(dep["spec"].get("replicas", 1) or 0) == 0:
            digest = REGISTRY.manifest_info(ref, REGISTRY._secret_credentials(dep["metadata"]["namespace"], dep), True)["digest"]
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest or ""):
        raise ValueError(f"immutable recovery image is unavailable for {name}; no image change was applied")
    return _immutable(ref, digest)
def _named_release(dep, pods, version):
    """Homestead's own containers on a release an admin names, for when its
    update check cannot offer one: in 2.8.333 a broken registry reader failed
    every check, Homestead's own included, so it could not find its own fix.
    Pinned to the release's digest when the registry answers; when it cannot
    be read, the release's tag, which the cluster pulls and checks itself."""
    ns, name = dep["metadata"]["namespace"], dep["metadata"]["name"]
    if U.PART(ns, name) != "self":
        raise ValueError("only Homestead itself can be put on a release you name")
    if not REGISTRY.release_key(version):
        raise ValueError("name a Homestead release, such as 2.8.334 or 2.8.334-dev.1")
    tracked = U._annotation_json(dep, U.TRACKED)
    containers = dep["spec"]["template"]["spec"].get("containers", [])
    own = next((c for c in containers if c["name"] == NAMES.BRAND), containers[0] if containers else None)
    if own is None:
        raise ValueError("Homestead's container was not found")
    base = parse_image(CHECKS.followed(tracked.get(own["name"]), own.get("image", "")))["base"]
    auths = REGISTRY._secret_credentials(ns, dep)
    candidate = base + ":" + version
    try:
        digest, unread = REGISTRY.manifest_digest(candidate, auths, force=True), ""
    except Exception as error:
        digest, unread = "", str(error)[:180] or type(error).__name__
    chosen = {}
    for container in containers:
        if parse_image(CHECKS.followed(tracked.get(container["name"]), container.get("image", "")))["base"] == base:
            chosen[container["name"]] = {"container": container["name"], "candidate": candidate, "remote_digest": digest,
                                         "current_digest": CHECKS._pod_digest(pods, container["name"]), "unread": unread}
    return chosen


def prepare_update(ns, name, version=None):
    """Build the exact rollout without writing workload metadata or history.
    version: Homestead itself only, the release to install (_named_release)."""
    if U._managed_smb(ns, name):
        raise ValueError("SMB is managed from Network Shares")
    current = copy.deepcopy(U.kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}"))
    if U.managed_by(current):
        raise ValueError(U.managed_message(ns, name, U.managed_by(current)))
    dep = copy.deepcopy(current)
    pods = _review_pods(current)
    if version:
        chosen = _named_release(dep, pods, version)
    else:
        check = CHECKS._check_deployment(dep, pods, True, persist=False)
        chosen = {x["container"]: x for x in check["images"] if x["available"]}
    if not chosen:
        raise ValueError("no image update is currently available")
    # A named release the registry could not be read for goes by its tag.
    pin = lambda update: _immutable(update["candidate"], update["remote_digest"]) if update["remote_digest"] else update["candidate"]
    annotations = dep["metadata"].setdefault("annotations", {})
    tracked = U._annotation_json(dep, U.TRACKED)
    before = {c["name"]: c.get("image", "") for c in _pod_containers(dep)}
    for container in dep["spec"]["template"]["spec"].get("containers", []):
        update = chosen.get(container["name"])
        if not update:
            continue
        tracked[container["name"]] = update["candidate"]
        before[container["name"]] = _previous_image(current, container, pods, update.get("current_digest", ""))
        container["image"] = pin(update)
        container["imagePullPolicy"] = "IfNotPresent"
        _drop_pinned_version(container)
    # An init container built from the same image is the same release, so it
    # follows the container it belongs to rather than staying on the tag the
    # workload was first installed with.
    for init in dep["spec"]["template"]["spec"].get("initContainers", []) or []:
        matches = [item for name, item in chosen.items()
                   if _same_repository(init.get("image", ""), before.get(name, ""))]
        if not matches:
            continue
        if len({(item["candidate"], item["remote_digest"]) for item in matches}) > 1:
            raise ValueError(f"init container {init['name']} matches conflicting image updates; update its mapping explicitly")
        update = matches[0]
        before[init["name"]] = _previous_image(current, init, pods)
        tracked[init["name"]] = update["candidate"]
        init["image"] = pin(update)
        init["imagePullPolicy"] = "IfNotPresent"
        _drop_pinned_version(init)
    # Immutable targets are checked before the prepared object can be committed.
    for container in _pod_containers(dep):
        original = next(c for c in _pod_containers(current) if c["name"] == container["name"])
        if container.get("image") != original.get("image"):
            old = before[container["name"]]
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", parse_image(old).get("digest", "")):
                raise ValueError(f"immutable rollback image is unavailable for {container['name']}; no update was applied")
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", parse_image(container["image"]).get("digest", ""))                     and not (version and container["image"] == next(iter(chosen.values()))["candidate"]):
                raise ValueError("registry returned an invalid image digest")
    # Init rollback references were resolved after the initial app mapping.
    U._write(annotations, U.PREVIOUS, json.dumps({"images": before, "sources": U._annotation_json(current, U.TRACKED)},
                                           separators=(",", ":")))
    U._write(annotations, U.TRACKED, json.dumps(tracked, separators=(",", ":")))
    prepared = {"current": current, "proposed": dep, "before": before, "action": "update"}
    unread = next((u.get("unread") for u in chosen.values() if u.get("unread")), "")
    if unread:
        prepared["unread"] = unread
    return prepared
def prepare_rollback(ns, name):
    if U._managed_smb(ns, name):
        raise ValueError("SMB is managed from Network Shares")
    original = copy.deepcopy(U.kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}"))
    if U.managed_by(original):
        raise ValueError(U.managed_message(ns, name, U.managed_by(original)))
    dep = copy.deepcopy(original)
    previous = U._annotation_json(dep, U.PREVIOUS)
    restore = previous.get("images") or {}
    if not restore:
        raise ValueError("no managed update is available to roll back")
    current = {c["name"]: c.get("image", "") for c in _pod_containers(dep)}
    pods = None
    changed = False
    for container in _pod_containers(dep):
        if container["name"] in restore:
            if container.get("image") == restore[container["name"]]:
                continue
            changed = True
            if container.get("image") != restore[container["name"]] and not re.fullmatch(
                    r"sha256:[0-9a-f]{64}", parse_image(restore[container["name"]]).get("digest", "")):
                raise ValueError(f"saved rollback image for {container['name']} is mutable; review and pin an exact digest first")
            if pods is None:
                pods = _review_pods(original)
            current[container["name"]] = _previous_image(original, container, pods)
            container["image"] = restore[container["name"]]
            container["imagePullPolicy"] = "IfNotPresent"
    annotations = dep["metadata"].setdefault("annotations", {})
    if not changed:
        raise ValueError("saved rollback does not change any current container image")
    U._write(annotations, U.PREVIOUS, json.dumps({"images": current,
                                        "sources": U._annotation_json(dep, U.TRACKED)},
                                       separators=(",", ":")))
    if isinstance(previous.get("sources"), dict):
        U._write(annotations, U.TRACKED, json.dumps(previous["sources"], separators=(",", ":")))
    return {"current": original, "proposed": dep, "before": current, "action": "rollback"}
def commit_prepared(prepared):
    """Write only the prepared manifest; its resourceVersion is the concurrency fence."""
    dep = copy.deepcopy(prepared["proposed"])
    ns, name = dep["metadata"]["namespace"], dep["metadata"]["name"]
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    annotations = dep["metadata"].setdefault("annotations", {})
    previous = U._annotation_json(dep, U.PREVIOUS)
    previous["at"] = now
    U._write(annotations, U.PREVIOUS, json.dumps(previous, separators=(",", ":")))
    U._write(annotations, U.LAST_ACTION, prepared["action"] + " " + now)
    dep["spec"]["template"].setdefault("metadata", {}).setdefault("annotations", {})[U.ROLLOUT_AT] = now
    result = U.ksend("PUT", f"/apis/apps/v1/namespaces/{ns}/deployments/{name}", dep)
    _history({"at": now, "action": prepared["action"], "namespace": ns, "deployment": name,
              "before": prepared["before"], "after": {c["name"]: c["image"] for c in _pod_containers(result)}})
    return progress(ns, name, result)
def apply_update(ns, name):
    return commit_prepared(prepare_update(ns, name))
def rollback(ns, name):
    return commit_prepared(prepare_rollback(ns, name))
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
        events = U.kget(f"/api/v1/namespaces/{namespace}/events"
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
# What a failed pull's last words mean. containerd nests its errors - "failed
# to pull and unpack image ...: failed to resolve reference ...: failed to do
# request: Head ...: <the cause>" - and the cause is the part that was cut
# off when only the start was kept.
PULL_CAUSES = (
    ("toomanyrequests", "Docker Hub's pull limit for this address is used up; it resets within a few hours"),
    ("pull access denied", "the registry refused it: the image does not exist under that name, or is private and needs credentials"),
    ("not found", "the registry has no such image or tag"),
    ("unauthorized", "the registry wants credentials for it"),
    ("no such host", "the node cannot look up the registry's name: its DNS is not answering for it"),
    ("server misbehaving", "the node cannot look up the registry's name: its DNS server is failing"),
    ("lookup ", "the node cannot look up the registry's name (DNS)"),
    ("network is unreachable", "the node has no route to the registry - often an IPv6 address with no IPv6 route"),
    ("no route to host", "the node has no route to the registry"),
    ("connection refused", "the registry refused the connection - a proxy or firewall in the way"),
    ("i/o timeout", "the node could not reach the registry in time - no internet from the node, or a firewall"),
    ("TLS handshake timeout", "the node could not reach the registry in time - no internet from the node, or a firewall"),
    ("context deadline exceeded", "the registry did not answer in time"),
    ("x509", "the registry's certificate is not trusted - a proxy intercepting HTTPS"),
    ("no space left", "the node's disk is full, so the image cannot be unpacked"),
)
def explain_pull(message):
    """A pull failure in a sentence, cause first; empty when it is not one."""
    text = " ".join(str(message or "").split())
    if not text or not any(word in text for word in ("pull", "image", "registry", "reference")):
        return ""
    for needle, meaning in PULL_CAUSES:
        if needle.lower() in text.lower():
            return meaning
    return ""
def explain_unplaced(message, own_service_ports=()):
    """What the scheduler's refusal means, and what to do about it. The
    message counts nodes by reason ("0/1 nodes are available: 1 node(s)
    didn't have free ports ..."); the reasons are what matter."""
    text = " ".join(str(message or "").split())
    if "free ports" in text:
        if own_service_ports:
            ports = ", ".join(str(p) for p in own_service_ports)
            return (f"port {ports} is taken on the node - by k3s's ServiceLB, publishing this app's own Service. "
                    "An app on the host network answers on the node itself and needs no Service: remove it under "
                    "Networking (or Edit → Network → Host network, and save)")
        return "a port it needs on the host is taken on every node that could run it - by another host-network app, or a Service ServiceLB publishes"
    if "Insufficient memory" in text:
        return "no node has enough free memory for what it asks: lower its memory under Edit, or free some up"
    if "Insufficient cpu" in text:
        return "no node has enough free CPU for what it asks: lower its CPU under Edit, or free some up"
    if "didn't match Pod's node affinity/selector" in text or "node(s) didn't match" in text:
        return "it may only run on a host - pinned, or needing hardware - that cannot take it now"
    if "unbound immediate PersistentVolumeClaims" in text or "persistentvolumeclaim" in text.lower():
        return "its volume is not ready yet"
    if "untolerated taint" in text or "had taint" in text:
        return "every node that could run it is cordoned or tainted"
    return text[:240]
def _why_waiting(ns, pod):
    """The newest warning about a pod that has not started, once it has been
    waiting a minute; stuck when a volume or scheduling warning has lasted
    three."""
    age = _age(pod.get("metadata", {}).get("creationTimestamp"))
    if age < 60:
        return None
    name = pod["metadata"]["name"]
    try:
        events = U.kget(f"/api/v1/namespaces/{ns}/events?fieldSelector="
                        f"{urllib.parse.quote(f'involvedObject.name={name},type=Warning')}").get("items", [])
    except Exception:
        return None
    if not events:
        return None
    latest = max(events, key=lambda e: e.get("lastTimestamp") or e.get("eventTime") or "")
    message = " ".join(str(latest.get("message") or latest.get("reason") or "").split())[:300]
    found = {"message": message, "stuck": latest.get("reason") in STUCK_REASONS and age >= STUCK_AFTER}
    if latest.get("reason") == "FailedScheduling":
        found["message"] = f"cannot be placed: {explain_unplaced(message)}"
        found["stuck"] = age >= STUCK_AFTER
    if "does not contain driver driver.longhorn.io" in message:
        # A node Ready before Longhorn has started on it: it clears itself
        # within minutes of a join, and never does if Longhorn fails there.
        node = (pod.get("spec") or {}).get("nodeName") or "its node"
        found["message"] = (f"Longhorn is not running on {node} yet, so its volume cannot attach there. A node "
                            "that has just joined gets it within a few minutes and this clears by itself; if not, "
                            "see why in longhorn-system on that node, or delete this pod so it starts on another")
        # Not a failure while the node may still be joining: only after ten minutes.
        found["stuck"] = age >= 600
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
    dep = dep or U.kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")
    pods = _matching_pods(dep, U.kget("/api/v1/pods").get("items", []))
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
        pod_status = pod.get("status") or {}
        init_names = {cs.get("name") for cs in pod_status.get("initContainerStatuses") or []}
        for cs in (pod_status.get("initContainerStatuses") or []) + (pod_status.get("containerStatuses") or []):
            waiting = (cs.get("state", {}).get("waiting") or {})
            if waiting:
                why = explain_pull(waiting.get("message"))
                waits.append({"container": cs.get("name", ""), "init": cs.get("name") in init_names,
                              "reason": waiting.get("reason", "Waiting"),
                              "message": (why or waiting.get("message", ""))[:400]})
                if waiting.get("reason") in fatal:
                    problems.append(f"{pod['metadata']['name']}: {'init ' if cs.get('name') in init_names else ''}{cs.get('name', '')}: {waiting.get('reason')}" + (f" - {why}" if why else ""))
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
    if pull.get("state") == "pulling" and U.pull_progress:
        try:
            pull.update(U.pull_progress(pull.get("node", ""), pull.get("image", "")))
        except Exception:
            pass
    return {"ns": ns, "name": name, "uid": dep["metadata"].get("uid", ""), "phase": phase, "desired": desired, "pull": pull,
            "replicas": replicas, "updated": updated, "ready": ready,
            "available": available, "unavailable": unavailable, "generation": generation,
            "observed_generation": observed, "pods": pod_rows, "problems": problems,
            "images": {c["name"]: c.get("image", "") for c in
                       spec.get("template", {}).get("spec", {}).get("containers", [])},
            # The stamp an update writes into the pod template: which rollout
            # the pods are from, whatever else changes on the Deployment.
            "rollout_at": ((spec.get("template", {}).get("metadata") or {}).get("annotations") or {}).get(U.ROLLOUT_AT, ""),
            "can_rollback": bool(U._annotation_json(dep, U.PREVIOUS)),
            "last_action": U._annotation(dep, U.LAST_ACTION)}


# Imported last, so this module can be imported before or after homestead_updates:
# both only look these names up when a function runs.
from homestead_update_checks import _matching_pods  # noqa: E402
from homestead_update_registry import parse_image  # noqa: E402
import homestead_updates as U  # noqa: E402
import homestead_update_registry as REGISTRY  # noqa: E402
import homestead_update_checks as CHECKS  # noqa: E402
import homestead_names as NAMES  # noqa: E402
