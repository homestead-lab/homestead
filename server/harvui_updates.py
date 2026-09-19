"""Registry update discovery and deterministic Kubernetes rollouts.

Only the Python standard library is used. Registry credentials are read from
the workload's Kubernetes imagePullSecrets and are never returned by the API or
written to the update history. Applied images are pinned by manifest digest so
rollback cannot accidentally re-pull a broken mutable tag.
"""
import base64
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
TRACKED = "harvui.io/update-sources"
PREVIOUS = "harvui.io/update-previous"
LAST_ACTION = "harvui.io/update-action"
ROLLOUT_AT = "harvui.io/update-rollout-at"
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


def _annotation_json(dep, key):
    try:
        value = (dep.get("metadata", {}).get("annotations", {}) or {}).get(key, "{}")
        parsed = json.loads(value)
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
    req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": "HarvUI/1.9"})
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
    auths = _secret_credentials(ns, dep)
    mine = _matching_pods(dep, pods)
    images = []
    for container in dep["spec"]["template"]["spec"].get("containers", []):
        deployed = container.get("image", "")
        source = tracked.get(container["name"]) or deployed
        item = {"container": container["name"], "deployed": deployed, "source": source,
                "available": False, "current_digest": "", "remote_digest": "",
                "candidate": source, "candidate_tag": "", "error": ""}
        try:
            parsed = parse_image(source)
            current = parse_image(deployed).get("digest") or _pod_digest(mine, container["name"])
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
                         "available": bool(current and remote and not matches_remote) or bool(candidate_tag)})
            if not current:
                item["error"] = "running image digest is not available yet"
        except urllib.error.HTTPError as error:
            item["error"] = ("registry authentication required" if error.code in (401, 403)
                             else f"registry returned HTTP {error.code}")
        except Exception as error:
            item["error"] = str(error)[:180]
        images.append(item)
    return {"ns": ns, "name": name, "images": images,
            "available": any(x["available"] for x in images),
            "can_rollback": bool(_annotation_json(dep, PREVIOUS)),
            "last_action": (dep["metadata"].get("annotations", {}) or {}).get(LAST_ACTION, "")}


def scan(force=False):
    deps = [d for d in kget("/apis/apps/v1/deployments").get("items", [])
            if d["metadata"]["namespace"] not in SYSTEM_NAMESPACES]
    pods = kget("/api/v1/pods").get("items", [])
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(6, max(1, len(deps)))) as pool:
        futures = [pool.submit(_check_deployment, dep, pods, force) for dep in deps]
        workloads = [future.result() for future in futures]
    workloads.sort(key=lambda x: (x["ns"], x["name"]))
    return {"checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "updates": sum(1 for x in workloads if x["available"]),
            "errors": sum(1 for x in workloads for image in x["images"] if image.get("error")),
            "workloads": workloads}


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
    before = {c["name"]: c.get("image", "")
              for c in dep["spec"]["template"]["spec"].get("containers", [])}
    # Make the rollback target immutable too. On a first managed update the
    # Deployment may still contain a mutable tag even though the pod status
    # tells us the exact manifest that is running.
    for image in check["images"]:
        if image.get("current_digest"):
            before[image["container"]] = _immutable(image["source"], image["current_digest"])
    annotations[PREVIOUS] = json.dumps({"images": before, "sources": tracked,
                                        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
                                       separators=(",", ":"))
    for container in dep["spec"]["template"]["spec"].get("containers", []):
        update = chosen.get(container["name"])
        if not update:
            continue
        tracked[container["name"]] = update["candidate"]
        container["image"] = _immutable(update["candidate"], update["remote_digest"])
        container["imagePullPolicy"] = "IfNotPresent"
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    annotations[TRACKED] = json.dumps(tracked, separators=(",", ":"))
    annotations[LAST_ACTION] = "update " + now
    dep["spec"]["template"].setdefault("metadata", {}).setdefault("annotations", {})[ROLLOUT_AT] = now
    result = ksend("PUT", f"/apis/apps/v1/namespaces/{ns}/deployments/{name}", dep)
    _history({"at": now, "action": "update", "namespace": ns, "deployment": name,
              "before": before, "after": {c["name"]: c["image"] for c in
                                            result["spec"]["template"]["spec"]["containers"]}})
    return progress(ns, name, result)


def rollback(ns, name):
    dep = kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")
    previous = _annotation_json(dep, PREVIOUS)
    restore = previous.get("images") or {}
    if not restore:
        raise ValueError("no managed update is available to roll back")
    current = {c["name"]: c.get("image", "") for c in
               dep["spec"]["template"]["spec"].get("containers", [])}
    for container in dep["spec"]["template"]["spec"].get("containers", []):
        if container["name"] in restore:
            container["image"] = restore[container["name"]]
            container["imagePullPolicy"] = "IfNotPresent"
    annotations = dep["metadata"].setdefault("annotations", {})
    annotations[PREVIOUS] = json.dumps({"images": current,
                                        "sources": _annotation_json(dep, TRACKED),
                                        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
                                       separators=(",", ":"))
    if isinstance(previous.get("sources"), dict):
        annotations[TRACKED] = json.dumps(previous["sources"], separators=(",", ":"))
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    annotations[LAST_ACTION] = "rollback " + now
    dep["spec"]["template"].setdefault("metadata", {}).setdefault("annotations", {})[ROLLOUT_AT] = now
    result = ksend("PUT", f"/apis/apps/v1/namespaces/{ns}/deployments/{name}", dep)
    _history({"at": now, "action": "rollback", "namespace": ns, "deployment": name,
              "before": current, "after": restore})
    return progress(ns, name, result)


def progress(ns, name, dep=None):
    dep = dep or kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")
    pods = _matching_pods(dep, kget("/api/v1/pods").get("items", []))
    spec, status = dep.get("spec", {}), dep.get("status", {})
    desired = int(spec.get("replicas", 0) or 0)
    updated = int(status.get("updatedReplicas", 0) or 0)
    ready = int(status.get("readyReplicas", 0) or 0)
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
        pod_rows.append({"name": pod["metadata"]["name"], "phase": pod.get("status", {}).get("phase", ""),
                         "node": pod.get("spec", {}).get("nodeName", ""), "waiting": waits})
    for condition in status.get("conditions", []) or []:
        if condition.get("type") == "Progressing" and condition.get("status") == "False":
            problems.append(condition.get("message") or condition.get("reason") or "rollout failed")
    complete = observed >= generation and updated == desired and ready == desired
    phase = "failed" if problems else ("ready" if complete else "progressing")
    return {"ns": ns, "name": name, "phase": phase, "desired": desired,
            "updated": updated, "ready": ready, "generation": generation,
            "observed_generation": observed, "pods": pod_rows, "problems": problems,
            "images": {c["name"]: c.get("image", "") for c in
                       spec.get("template", {}).get("spec", {}).get("containers", [])},
            "can_rollback": bool(_annotation_json(dep, PREVIOUS)),
            "last_action": (dep["metadata"].get("annotations", {}) or {}).get(LAST_ACTION, "")}
