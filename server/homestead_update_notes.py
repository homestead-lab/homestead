"""What's in an image update.

In the image-update review, beside each container with an update: where to
read about the new version (release notes, code changes, the project site),
built from the new image's OCI labels, and what changes between the running
image and the new one in exposed ports, volumes, env and the user it runs as,
read from each image's config.

The registry is the only place asked, through homestead_update_registry
(the workload's own pull credentials, its 429 back-off); the links are for
the browser to open and are never fetched. Env is reported by name only: an
image's defaults can hold a DSN or a URL with a password in it.
"""
import hashlib
import json
import re
import threading
import urllib.parse

import homestead_updates as U

SHA256 = re.compile(r"sha256:[0-9a-f]{64}")
CONFIG_MAX_BYTES = 1 << 20

# A config is named by a digest, so it never changes. Kept, up to a bound,
# under the registry, repository and manifest digest it was read for - every
# byte of the chain checked against its digest first - so a second look at
# the same update asks the registry nothing, and a repository never answers
# with what was read for another.
_CONFIGS, _CONFIGS_LOCK, _CONFIGS_MAX = {}, threading.Lock(), 256


def _verified(body, digest, what):
    if not SHA256.fullmatch(digest or "") or "sha256:" + hashlib.sha256(body).hexdigest() != digest:
        raise ValueError(f"the registry's {what} does not match its digest")
    return json.loads(body.decode())


def image_config(ref, auths, arch="amd64"):
    """The image's config (Env, ExposedPorts, Volumes, User, Labels) on this
    architecture: its manifest, the platform's own when that is an index, then
    the config blob - at most three registry requests, none when cached."""
    parsed = U.parse_image(U.with_tag(ref))
    if parsed["digest"] and not SHA256.fullmatch(parsed["digest"]):
        raise ValueError("only sha256 image digests are read")
    def manifest(reference):
        return U._registry_json(parsed, "manifests/" + urllib.parse.quote(reference, safe=":"), auths,
                                U.MANIFEST_ACCEPT)[0]
    key = lambda top: (parsed["registry"], parsed["repo"], top, arch)
    def cached(top):
        with _CONFIGS_LOCK:
            hit = _CONFIGS.get(key(top))
        return dict(hit) if hit is not None else None
    if parsed["digest"]:
        top = parsed["digest"]
        if cached(top) is not None:
            return cached(top)
        current = _verified(manifest(top), top, "manifest")
    else:
        # A tag can move: its manifest is read each time, and what it names
        # is kept only by that manifest's digest.
        body = manifest(parsed["tag"])
        top = "sha256:" + hashlib.sha256(body).hexdigest()
        if cached(top) is not None:
            return cached(top)
        current = json.loads(body.decode())
    if current.get("manifests"):
        child = next((m for m in current["manifests"]
                      if (m.get("platform") or {}).get("os", "linux") == "linux"
                      and (m.get("platform") or {}).get("architecture") == arch), None) or current["manifests"][0]
        current = _verified(manifest(child.get("digest") or ""), child.get("digest") or "", "manifest")
    descriptor = current.get("config") or {}
    config_digest = descriptor.get("digest") or ""
    if not SHA256.fullmatch(config_digest):
        raise ValueError("the image's manifest names no sha256 config")
    if int(descriptor.get("size") or 0) > CONFIG_MAX_BYTES:
        raise ValueError("the image's config is too large to read")
    config = _verified(U._registry_json(parsed, "blobs/" + config_digest, auths)[0], config_digest,
                       "config").get("config") or {}
    with _CONFIGS_LOCK:
        while len(_CONFIGS) >= _CONFIGS_MAX:
            _CONFIGS.pop(next(iter(_CONFIGS)))
        _CONFIGS[key(top)] = dict(config)
    return config


SOURCE_LABELS = ("org.opencontainers.image.source", "org.label-schema.vcs-url")
URL_LABELS = ("org.opencontainers.image.url", "org.label-schema.url")
VERSION_LABELS = ("org.opencontainers.image.version", "org.label-schema.version")
REVISION_LABELS = ("org.opencontainers.image.revision", "org.label-schema.vcs-ref")
LINK_MAX, VERSION_MAX = 2048, 64


def _label(labels, keys):
    labels = labels if isinstance(labels, dict) else {}
    return next((str(labels[k]).strip() for k in keys if str(labels.get(k) or "").strip()), "")


def _link(text):
    """A label value as a link the page may show: http(s) to a named host,
    with no credentials, no spaces or control characters, and not too long.
    Anything else - javascript:, data:, git+https:, a user:password@ - is ""."""
    text = (text or "").strip()
    if not text or len(text) > LINK_MAX or re.search(r"[\s\x00-\x1f\x7f\\]", text):
        return ""
    try:
        parsed = urllib.parse.urlsplit(text)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or "@" in parsed.netloc:
            return ""
        parsed.port  # a port that is not a number raises
    except ValueError:
        return ""
    return text


def _repo(source):
    """scheme://host/owner/repo of a source label, or ""."""
    if not _link(source):
        return ""
    parsed = urllib.parse.urlsplit(source.strip())
    path = re.sub(r"\.git$", "", parsed.path.rstrip("/"))
    return f"{parsed.scheme}://{parsed.netloc}{path}" if path.count("/") >= 2 else ""


def release_links(old_labels, new_labels, old_tag, new_tag):
    """Where to read about the new version: its release notes, the code
    changes between the two revisions where the host shows them, and the
    project site - each built from the images' labels, never fetched."""
    repo = _repo(_label(new_labels, SOURCE_LABELS))
    host = (urllib.parse.urlsplit(repo).hostname or "") if repo else ""
    new_version = (_label(new_labels, VERSION_LABELS) or new_tag or "")[:VERSION_MAX]
    old_version = (_label(old_labels, VERSION_LABELS) or old_tag or "")[:VERSION_MAX]
    links = {}
    if host == "github.com":
        # The version label is not always the git tag (1.2.0 against v1.2.0),
        # so the link searches the releases rather than guessing a tag page.
        links["release"] = (f"{repo}/releases?q={urllib.parse.quote(new_version, safe='')}&expanded=true"
                            if new_version else f"{repo}/releases")
        old_rev, new_rev = _label(old_labels, REVISION_LABELS), _label(new_labels, REVISION_LABELS)
        if all(re.fullmatch(r"[0-9a-f]{7,40}", r) for r in (old_rev, new_rev)) and old_rev != new_rev:
            links["compare"] = f"{repo}/compare/{old_rev[:12]}...{new_rev[:12]}"
    elif host == "gitlab.com" or host.startswith("gitlab."):
        links["release"] = f"{repo}/-/releases"
    elif repo:
        links["release"] = repo
    project = _link(_label(new_labels, URL_LABELS))
    if project and project != links.get("release"):
        links["project"] = project
    return {"links": links, "from_version": old_version, "to_version": new_version, "source": repo}


# Env an image's build leaves behind (REDIS_VERSION, REDIS_DOWNLOAD_SHA,
# PYTHON_SHA256, GPG_KEYS): it changes with every release and is not one of
# the app's settings, so it is counted, not listed.
BUILD_ENV = re.compile(r"(_VERSION|_SHA|_SHA1|_SHA256|_SHA512|_CHECKSUM|_DOWNLOAD_URL|_DOWNLOAD_SHA\w*"
                       r"|_GET_PIP_URL|_GET_PIP_SHA256|GPG_KEYS?|_COMMIT)$", re.I)


def _env(config):
    env = config.get("Env")
    out = {}
    for item in env if isinstance(env, list) else []:
        if isinstance(item, str):
            name, _, value = item.partition("=")
            if name:
                out[name] = value
    return out


def _keys(config, field):
    value = config.get(field)
    return set(value) if isinstance(value, dict) else set()


def _user(config):
    user = config.get("User")
    user = user.strip() if isinstance(user, str) else ""
    return "root" if user.split(":")[0] in ("", "0", "root") else user


def config_changes(old, new, overrides=None):
    """What the update changes that the app's settings may rely on: exposed
    ports, declared volumes, env (names only: values never leave the
    server) and the user it runs as. `overrides` is what the app sets
    itself ({"user": runAsUser or None, "env": names}), which wins over the
    image's defaults."""
    overrides = overrides or {}
    old, new = (old if isinstance(old, dict) else {}), (new if isinstance(new, dict) else {})
    def diff(field):
        a, b = _keys(old, field), _keys(new, field)
        return {"added": sorted(b - a), "removed": sorted(a - b)}
    old_env, new_env = _env(old), _env(new)
    differs = {k for k in set(old_env) | set(new_env) if old_env.get(k) != new_env.get(k)}
    build = {k for k in differs if BUILD_ENV.search(k)}
    env = {"added": sorted(set(new_env) - set(old_env) - build),
           "removed": sorted(set(old_env) - set(new_env) - build),
           "changed": sorted((set(old_env) & set(new_env) & differs) - build)}
    env["set_by_app"] = sorted((differs - build) & set(overrides.get("env") or ()))
    user = None
    if _user(old) != _user(new):
        user = {"was": _user(old), "now": _user(new)}
        if overrides.get("user") is not None:
            user["overridden_by"] = str(overrides["user"])
    changes = {"ports": diff("ExposedPorts"), "volumes": diff("Volumes"), "env": env, "user": user,
               "build_env_changed": len(build)}
    changes["none"] = not (any(changes["ports"].values()) or any(changes["volumes"].values())
                           or env["added"] or env["removed"] or env["changed"] or user)
    return changes
