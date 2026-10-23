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
