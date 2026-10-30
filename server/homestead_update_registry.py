"""Image updates: asking a registry about an image.

Parses image references, reads a workload's pull credentials, asks registries
for manifests and tags (with a short cache), and orders release tags by
channel. Moved unchanged from homestead_updates (#623): read and replace
these names here. Names server.py binds on homestead_updates are read
through it (U.x), so a replacement there still applies.
"""
import base64
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

import homestead_fetch as FETCH


MANIFEST_ACCEPT = ", ".join((
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.docker.distribution.manifest.v2+json",
))
_CACHE = {}
_CACHE_LOCK = threading.Lock()
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
def _secret_credentials(ns, dep):
    names = [x.get("name") for x in
             dep["spec"]["template"]["spec"].get("imagePullSecrets", []) if x.get("name")]
    service_account = dep["spec"]["template"]["spec"].get("serviceAccountName", "default")
    try:
        sa = U.kget(f"/api/v1/namespaces/{ns}/serviceaccounts/{service_account}")
        names.extend(x.get("name") for x in sa.get("imagePullSecrets", []) if x.get("name"))
    except Exception:
        pass
    auths = {}
    for name in dict.fromkeys(names):
        try:
            secret = U.kget(f"/api/v1/namespaces/{ns}/secrets/{name}")
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
class _NoAuthRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise PermissionError("registry authentication redirects are disabled")
def _auth_realm(request_url, realm, credential):
    source, target = urllib.parse.urlparse(request_url), urllib.parse.urlparse(realm)
    if target.scheme != "https" or not target.hostname or target.username or target.password:
        raise PermissionError("registry authentication requires an HTTPS realm without URL credentials")
    if credential:
        if source.scheme != "https":
            raise PermissionError("registry credentials require HTTPS")
        allowed = {source.netloc.lower()}
        if source.hostname in ("registry-1.docker.io", "docker.io"):
            allowed.add("auth.docker.io")
        allowed.update(x.strip().lower() for x in os.environ.get("REGISTRY_AUTH_HOSTS", "").split(",") if x.strip())
        if target.netloc.lower() not in allowed:
            raise PermissionError("registry authentication realm is not trusted; configure REGISTRY_AUTH_HOSTS for a separate auth server")
    return realm
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
        _auth_realm(req.full_url, realm, credential)
        query = {k: v for k, v in fields.items() if k in ("service", "scope") and v}
        separator = "&" if "?" in realm else "?"
        token_req = urllib.request.Request(realm + (separator + urllib.parse.urlencode(query) if query else ""),
                                           headers={"Accept": "application/json"})
        if credential:
            raw = base64.b64encode(f"{credential[0]}:{credential[1]}".encode()).decode()
            token_req.add_unredirected_header("Authorization", "Basic " + raw)
        opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx), _NoAuthRedirect())
        with opener.open(token_req, timeout=timeout) as response:
            token_body = json.loads(FETCH.read(response, TOKEN_MAX_BYTES, time.monotonic() + timeout,
                                               "the registry's token").decode())
        token = token_body.get("token") or token_body.get("access_token")
        if not token:
            raise PermissionError("registry authentication did not return a token")
        # Only the caller's own headers go on to a redirect. urllib records the
        # first request's Host among the headers it kept back: carried along,
        # it would reach a blob's storage host, which refuses it (403/421).
        retry = urllib.request.Request(req.full_url, method=req.get_method(), headers=dict(req.headers))
        for name, value in req.unredirected_hdrs.items():
            if name.lower() not in ("host", "authorization"):
                retry.add_unredirected_header(name, value)
        # A blob answers with a redirect to storage that has its own
        # signature and refuses a second one, so the token stays here.
        retry.add_unredirected_header("Authorization", "Bearer " + token)
        return urllib.request.urlopen(retry, context=ctx, timeout=timeout)
RATE_LIMIT_WAITS = (1.0, 3.0)    # Docker Hub and others answer 429 under load; a pause usually clears it
def _retry_after(error, default):
    try:
        return max(0.0, min(5.0, float((error.headers or {}).get("Retry-After", default))))
    except (TypeError, ValueError):
        return default
# A manifest, an index or a tag list is small: the distribution spec caps a
# manifest at 4 MiB, and 1,000 tags are tens of KiB. A token answer is a few KiB.
ANSWER_MAX_BYTES = 4 << 20
TOKEN_MAX_BYTES = 1 << 20
# The whole read - connecting, any token exchange, 429 pauses and the body -
# ends by then: urllib's timeout is per wait, so a trickling registry had none.
READ_DEADLINE = 30.0
def _read_capped(response, limit, deadline=None):
    """The answer's body; never more than limit bytes (ANSWER_MAX_BYTES when
    None), whatever its Content-Length says, and none of it after deadline."""
    try:
        return FETCH.read(response, ANSWER_MAX_BYTES if limit is None else limit, deadline, "the registry's answer")
    except FETCH.TooLarge:
        raise ValueError("the registry's answer is larger than expected") from None
def _registry_json(parsed, path, auths, accept="application/json", limit=None, deadline=None):
    url = f"https://{parsed['endpoint']}/v2/{parsed['repo']}/{path}"
    deadline = time.monotonic() + READ_DEADLINE if deadline is None else deadline
    for attempt in range(len(RATE_LIMIT_WAITS) + 1):
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("the registry took too long to answer")
        req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": "Homestead/2.0"})
        try:
            with _open(req, _credential(auths, parsed), min(12, left)) as response:
                body = _read_capped(response, limit, deadline)
                return body, response.headers
        except urllib.error.HTTPError as error:
            pause = _retry_after(error, RATE_LIMIT_WAITS[min(attempt, len(RATE_LIMIT_WAITS) - 1)])
            if error.code != 429 or attempt == len(RATE_LIMIT_WAITS) or time.monotonic() + pause >= deadline:
                raise
            time.sleep(pause)
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
def image_layers(ref, arch="amd64", auths=None):
    """The layers an image is pulled as on this architecture, with their
    sizes - what a pull's progress is measured against."""
    parsed = parse_image(ref)
    reference = parsed["digest"] or parsed["tag"]
    body, _ = _registry_json(parsed, "manifests/" + urllib.parse.quote(reference, safe=":"), auths or {}, MANIFEST_ACCEPT)
    manifest = json.loads(body.decode())
    if manifest.get("manifests"):
        child = next((m for m in manifest["manifests"]
                      if (m.get("platform") or {}).get("os", "linux") == "linux"
                      and (m.get("platform") or {}).get("architecture") == arch), None) or manifest["manifests"][0]
        body, _ = _registry_json(parsed, "manifests/" + child["digest"], auths or {}, MANIFEST_ACCEPT)
        manifest = json.loads(body.decode())
    return [{"digest": layer["digest"], "size": int(layer.get("size") or 0)}
            for layer in manifest.get("layers") or [] if layer.get("digest")]
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
RELEASE = r"v?(\d+)\.(\d+)\.(\d+)(?:-dev\.(\d+))?"
def release_key(tag):
    """A release's place in time: 2.8.317-dev.1 < 2.8.317-dev.2 < 2.8.317 <
    2.8.318-dev.1. A release comes after its own previews; None if not one."""
    match = re.fullmatch(RELEASE, tag or "")
    if not match:
        return None
    return tuple(int(match[i]) for i in (1, 2, 3)) + ((0, int(match[4])) if match[4] is not None else (1, 0))
def channel_newest(current, tags, channel):
    """This channel's newest release in the installed major version."""
    installed = re.fullmatch(RELEASE, current or "")
    if not installed:
        raise ValueError("No release version is known for Homestead; redeploy it from a numbered release tag")
    major = int(installed[1])
    releases = [(release_key(tag), tag) for tag in tags if re.fullmatch(RELEASE, tag)
                and (channel == "dev") == ("-dev." in tag) and release_key(tag)[0] == major]
    if not releases:
        raise ValueError(f"No {channel} release is published for this Homestead major version")
    return max(releases)[1]
def channel_release(current, tags, channel):
    """What to update to: this channel's newest release, if it is newer than
    the one installed - never an older one. A dev build ahead of prod, or a
    prod release ahead of the last dev build, stays put until its channel
    catches up (channel_behind says so). The major stays fixed, so choosing a
    channel cannot cross a major upgrade."""
    newest = channel_newest(current, tags, channel)
    return newest if release_key(newest) > release_key(current) else current
def channel_behind(current, tags, channel):
    """The channel's newest release when it is older than the one installed,
    else "": what the page says instead of offering it."""
    try:
        newest = channel_newest(current, tags, channel)
    except ValueError:
        return ""
    return newest if release_key(newest) < release_key(current) else ""


# Imported last, so this module can be imported before or after homestead_updates:
# both only look these names up when a function runs.
import homestead_updates as U  # noqa: E402
