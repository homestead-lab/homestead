"""Who a new volume should belong to: the owner its image gives that path.

Docker fills a new, empty volume from the image, owner included, so an image
that runs as a user of its own finds its data folder writable. Kubernetes
mounts a new volume as root's, and the same image cannot write there: sambee,
whose image makes /app/data its own user's and runs as that user, stopped at
PermissionError on data/mobile_logs, and its instructions ask for a chown on
the host that a cluster has no host to run.

So when a deploy makes a volume, the image's layers are read from its registry
- tar headers only, newest layer first - for the owner and mode of each path a
new volume is mounted on. A path the image does not have belongs to the user
the image runs as, resolved through the image's own /etc/passwd when it is a
name. An image that runs as root needs nothing. A small init container then
gives each new volume that owner, and only while the volume is empty: data
already there is never touched, so it is harmless on every later start.

Everything is best effort. An image that cannot be read in time deploys as
before.
"""
import json
import tarfile
import threading
import time
import urllib.parse
import urllib.request

import homestead_updates as UPDATES

IMAGE = "alpine:3.20"
INIT = "homestead-owner"
BUDGET_SECONDS = 40
BUDGET_BYTES = 400 * 2**20
_cache = {}
_lock = threading.Lock()


class _Budget:
    def __init__(self, seconds=BUDGET_SECONDS, size=BUDGET_BYTES):
        self.deadline, self.left = time.time() + seconds, size

    def spend(self, count):
        self.left -= count
        if self.left < 0 or time.time() > self.deadline:
            raise TimeoutError("reading the image took too long")


class _Counted:
    """A response that stops the read once the budget is spent."""

    def __init__(self, response, budget):
        self.response, self.budget = response, budget

    def read(self, size=-1):
        data = self.response.read(size)
        self.budget.spend(len(data))
        return data


def _clean(path):
    return "/" + str(path or "").strip().lstrip("./").strip("/")


def _numeric(text):
    text = str("" if text is None else text).strip()
    return int(text) if text.isdigit() else None


def parse_user(user, passwd="", group=""):
    """The (uid, gid) an image's USER runs as, or None when it cannot be told."""
    name, _, group_name = str(user or "").partition(":")
    uid, gid = _numeric(name), _numeric(group_name) if group_name else None
    if uid is None and name:
        for line in passwd.splitlines():
            fields = line.split(":")
            if len(fields) >= 4 and fields[0] == name and fields[2].isdigit():
                uid = int(fields[2])
                if not group_name and fields[3].isdigit():
                    gid = int(fields[3])
                break
    if gid is None and group_name:
        for line in group.splitlines():
            fields = line.split(":")
            if len(fields) >= 3 and fields[0] == group_name and fields[2].isdigit():
                gid = int(fields[2])
                break
    if uid is None:
        return None
    return uid, uid if gid is None else gid


def is_root(user):
    name = str(user or "").partition(":")[0].strip()
    return name in ("", "0", "root")


def scan_layers(layers, paths, budget=None):
    """Owner and mode of each path, and /etc/passwd and /etc/group, from an
    image's layers given newest first as tar streams (opened on demand).

    The newest layer to mention a path decides it, as it does in the image."""
    budget = budget or _Budget()
    wanted = {_clean(p) for p in paths}
    found, files = {}, {}
    for open_layer in layers:
        if wanted <= set(found) and len(files) == 2:
            break
        with open_layer() as stream:
            with tarfile.open(fileobj=_Counted(stream, budget), mode="r|*") as tar:
                for member in tar:
                    name = _clean(member.name)
                    if name in wanted and name not in found and member.isdir():
                        found[name] = (member.uid, member.gid, member.mode & 0o7777)
                    elif name in ("/etc/passwd", "/etc/group") and name not in files and member.isfile():
                        handle = tar.extractfile(member)
                        files[name] = (handle.read(1 << 20) if handle else b"").decode("utf-8", "replace")
    return found, files.get("/etc/passwd", ""), files.get("/etc/group", "")


def _registry(image):
    parsed = UPDATES.parse_image(UPDATES.with_tag(image))
    reference = parsed["digest"] or parsed["tag"]
    body, _ = UPDATES._registry_json(parsed, "manifests/" + urllib.parse.quote(reference, safe=":"), {},
                                     UPDATES.MANIFEST_ACCEPT)
    manifest = json.loads(body.decode())
    if manifest.get("manifests"):
        child = next((m for m in manifest["manifests"]
                      if (m.get("platform") or {}).get("os", "linux") == "linux"
                      and (m.get("platform") or {}).get("architecture") == "amd64"), None) or manifest["manifests"][0]
        body, _ = UPDATES._registry_json(parsed, "manifests/" + child["digest"], {}, UPDATES.MANIFEST_ACCEPT)
        manifest = json.loads(body.decode())
    config_digest = (manifest.get("config") or {}).get("digest")
    config = json.loads(UPDATES._registry_json(parsed, "blobs/" + config_digest, {})[0].decode()) if config_digest else {}
    return parsed, manifest, config


def _blob(parsed, digest):
    def open_blob():
        url = f"https://{parsed['endpoint']}/v2/{parsed['repo']}/blobs/{digest}"
        return UPDATES._open(urllib.request.Request(url, headers={"User-Agent": "Homestead/2.0"}), timeout=20)
    return open_blob


def image_owners(image, paths):
    """{path: (uid, gid, mode)} for each path the image's user needs to own.

    Empty when the image runs as root, or when its registry cannot be read."""
    key = (image, tuple(sorted(_clean(p) for p in paths)))
    with _lock:
        if key in _cache:
            return dict(_cache[key])
    out = {}
    try:
        parsed, manifest, config = _registry(image)
        user = (config.get("config") or {}).get("User") or ""
        if not is_root(user):
            layers = [layer for layer in manifest.get("layers") or []
                      if "zstd" not in str(layer.get("mediaType", "")) and layer.get("digest")]
            found, passwd, group = scan_layers([_blob(parsed, layer["digest"]) for layer in reversed(layers)],
                                               paths)
            runs_as = parse_user(user, passwd, group)
            for path in paths:
                owner = found.get(_clean(path))
                if owner and owner[0] != 0:
                    out[path] = owner
                elif runs_as:
                    out[path] = (runs_as[0], runs_as[1], 0o755)
    except Exception:
        # Not remembered: the registry may answer next time.
        return {}
    with _lock:
        _cache[key] = dict(out)
    return out


def _explicit(cfg):
    """The user the deploy itself names, which the image's own gives way to."""
    uid = _numeric(cfg.get("run_as_user"))
    if uid is None:
        return None
    gid = _numeric(cfg.get("run_as_group"))
    if gid is None:
        gid = _numeric(cfg.get("fs_group"))
    return uid, uid if gid is None else gid


def prepare(cfg, lookup=None):
    """Note on the deploy config who each volume it makes should belong to."""
    lookup = lookup or image_owners
    fresh = [v for v in cfg.get("volumes") or []
             if v.get("type") == "pvc" and v.get("create") and v.get("path") and not v.get("read_only")]
    env = cfg.get("env") or {}
    # linuxserver-style images start as root and take PUID/PGID themselves.
    if not fresh or cfg.get("privileged") or "PUID" in env:
        return cfg
    explicit = _explicit(cfg)
    if explicit:
        if explicit[0] == 0:
            return cfg
        owners = {v["path"]: (explicit[0], explicit[1], 0o755) for v in fresh}
    else:
        owners = lookup(cfg.get("image") or "", [v["path"] for v in fresh])
    if owners:
        cfg["volume_owners"] = {path: list(owner) for path, owner in owners.items()}
    return cfg


def init_container(targets):
    """The init container that gives empty new volumes their owners.

    targets: [(volume name, sub path, uid, gid, mode)]."""
    lines, mounts, seen = [], [], {}
    for volume, sub_path, uid, gid, mode in targets:
        if volume not in seen:
            seen[volume] = f"/v/{len(seen)}"
            mounts.append({"name": volume, "mountPath": seen[volume]})
        target = seen[volume] + ("/" + sub_path.strip("/") if sub_path else "")
        lines.append(f"d='{target}'; "
                     f"if [ -z \"$(ls -A \"$d\" 2>/dev/null | grep -vx lost+found)\" ]; then "
                     f"mkdir -p \"$d\" && chown {int(uid)}:{int(gid)} \"$d\" && chmod {int(mode):o} \"$d\" && "
                     f"echo \"$d is {int(uid)}:{int(gid)}\"; fi")
    return {"name": INIT, "image": IMAGE, "imagePullPolicy": "IfNotPresent",
            "command": ["sh", "-c", "\n".join(lines)],
            "securityContext": {"runAsUser": 0},
            "resources": {"requests": {"cpu": "10m", "memory": "16Mi"}},
            "volumeMounts": mounts}
