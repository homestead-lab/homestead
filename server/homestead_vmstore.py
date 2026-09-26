"""The VM image store: cloud images from the people who make them.

Most distributions publish a cloud image - a small disk with cloud-init in
it - at an address that always holds their newest build. The store lists the
common ones, each for the nodes' own architecture, and a VM can start from
any of them.

On Harvester an image is kept: downloaded once as a Harvester image, which
every disk made from it copies. A kept image can keep itself current: twice a
day Homestead asks the publisher, without downloading, whether the build has
changed (its ETag, date or size), downloads the new one beside the old, and -
once it is ready - lets go of the older builds no disk was made from. Disks
made from an older build keep it until they are gone.

Elsewhere, CDI fills each VM's disk straight from the publisher's address, so
there is nothing to keep: a new VM always starts from the newest build.

What is kept, and which Harvester image holds each build, is in the
homestead-vm-store ConfigMap.
"""
import json
import re
import threading
import time
import urllib.error
import urllib.request

kget = ksend = None
NS = "lab"
platform = None          # force -> what the cluster has
hv_download = None       # (url, display, reuse) -> Harvester image row
images = None            # () -> Harvester's images, as list_vm_images gives them
image_disks = None       # () -> {"ns/name": [disks made from it]}
delete_image = None      # (ns, name) -> None
fetch_json = None        # url -> parsed JSON
head = None              # url -> {"etag", "modified", "size"}

CONFIGMAP = "homestead-vm-store"
CHECK_EVERY = 12 * 3600
HEAD_TTL = 3600
_heads, _lock = {}, threading.Lock()

# Each publisher's own address for its newest cloud image. Fedora has no
# fixed "latest" address, so its newest release is looked up.
CATALOG = [
    {"id": "ubuntu-24.04", "name": "Ubuntu 24.04 LTS", "publisher": "Canonical", "user": "ubuntu", "min_gb": 10,
     "urls": {"amd64": "https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img",
              "arm64": "https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-arm64.img"}},
    {"id": "ubuntu-22.04", "name": "Ubuntu 22.04 LTS", "publisher": "Canonical", "user": "ubuntu", "min_gb": 10,
     "urls": {"amd64": "https://cloud-images.ubuntu.com/jammy/current/jammy-server-cloudimg-amd64.img",
              "arm64": "https://cloud-images.ubuntu.com/jammy/current/jammy-server-cloudimg-arm64.img"}},
    {"id": "debian-13", "name": "Debian 13 (trixie)", "publisher": "Debian", "user": "debian", "min_gb": 10,
     "urls": {"amd64": "https://cloud.debian.org/images/cloud/trixie/latest/debian-13-genericcloud-amd64.qcow2",
              "arm64": "https://cloud.debian.org/images/cloud/trixie/latest/debian-13-genericcloud-arm64.qcow2"}},
    {"id": "debian-12", "name": "Debian 12 (bookworm)", "publisher": "Debian", "user": "debian", "min_gb": 10,
     "urls": {"amd64": "https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-genericcloud-amd64.qcow2",
              "arm64": "https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-genericcloud-arm64.qcow2"}},
    {"id": "fedora", "name": "Fedora Cloud", "publisher": "Fedora Project", "user": "fedora", "min_gb": 10,
     "resolve": "fedora"},
    {"id": "rocky-10", "name": "Rocky Linux 10", "publisher": "Rocky Enterprise Software Foundation", "user": "rocky",
     "min_gb": 10,
     "urls": {"amd64": "https://dl.rockylinux.org/pub/rocky/10/images/x86_64/Rocky-10-GenericCloud-Base.latest.x86_64.qcow2",
              "arm64": "https://dl.rockylinux.org/pub/rocky/10/images/aarch64/Rocky-10-GenericCloud-Base.latest.aarch64.qcow2"}},
    {"id": "rocky-9", "name": "Rocky Linux 9", "publisher": "Rocky Enterprise Software Foundation", "user": "rocky",
     "min_gb": 10,
     "urls": {"amd64": "https://dl.rockylinux.org/pub/rocky/9/images/x86_64/Rocky-9-GenericCloud-Base.latest.x86_64.qcow2",
              "arm64": "https://dl.rockylinux.org/pub/rocky/9/images/aarch64/Rocky-9-GenericCloud-Base.latest.aarch64.qcow2"}},
    {"id": "almalinux-10", "name": "AlmaLinux 10", "publisher": "AlmaLinux OS Foundation", "user": "almalinux",
     "min_gb": 10,
     "urls": {"amd64": "https://repo.almalinux.org/almalinux/10/cloud/x86_64/images/AlmaLinux-10-GenericCloud-latest.x86_64.qcow2",
              "arm64": "https://repo.almalinux.org/almalinux/10/cloud/aarch64/images/AlmaLinux-10-GenericCloud-latest.aarch64.qcow2"}},
    {"id": "almalinux-9", "name": "AlmaLinux 9", "publisher": "AlmaLinux OS Foundation", "user": "almalinux",
     "min_gb": 10,
     "urls": {"amd64": "https://repo.almalinux.org/almalinux/9/cloud/x86_64/images/AlmaLinux-9-GenericCloud-latest.x86_64.qcow2",
              "arm64": "https://repo.almalinux.org/almalinux/9/cloud/aarch64/images/AlmaLinux-9-GenericCloud-latest.aarch64.qcow2"}},
    {"id": "centos-stream-10", "name": "CentOS Stream 10", "publisher": "CentOS Project", "user": "cloud-user",
     "min_gb": 10,
     "urls": {"amd64": "https://cloud.centos.org/centos/10-stream/x86_64/images/CentOS-Stream-GenericCloud-10-latest.x86_64.qcow2"}},
    {"id": "centos-stream-9", "name": "CentOS Stream 9", "publisher": "CentOS Project", "user": "cloud-user",
     "min_gb": 10,
     "urls": {"amd64": "https://cloud.centos.org/centos/9-stream/x86_64/images/CentOS-Stream-GenericCloud-9-latest.x86_64.qcow2",
              "arm64": "https://cloud.centos.org/centos/9-stream/aarch64/images/CentOS-Stream-GenericCloud-9-latest.aarch64.qcow2"}},
    {"id": "opensuse-leap-15.6", "name": "openSUSE Leap 15.6", "publisher": "openSUSE", "user": "opensuse",
     "min_gb": 10,
     "urls": {"amd64": "https://download.opensuse.org/distribution/leap/15.6/appliances/openSUSE-Leap-15.6-Minimal-VM.x86_64-Cloud.qcow2",
              "arm64": "https://download.opensuse.org/distribution/leap/15.6/appliances/openSUSE-Leap-15.6-Minimal-VM.aarch64-Cloud.qcow2"}},
    {"id": "opensuse-tumbleweed", "name": "openSUSE Tumbleweed", "publisher": "openSUSE", "user": "opensuse",
     "min_gb": 10,
     "urls": {"amd64": "https://download.opensuse.org/tumbleweed/appliances/openSUSE-Tumbleweed-Minimal-VM.x86_64-Cloud.qcow2"}},
    {"id": "arch", "name": "Arch Linux", "publisher": "Arch Linux", "user": "arch", "min_gb": 10,
     "urls": {"amd64": "https://geo.mirror.pkgbuild.com/images/latest/Arch-Linux-x86_64-cloudimg.qcow2"}},
]
BY_ID = {entry["id"]: entry for entry in CATALOG}
FEDORA = "https://fedoraproject.org/releases.json"
_fedora = {"at": 0.0, "value": {}}


def bind(_kget, _ksend, namespace, _platform, _hv_download, _images, _image_disks, _delete_image,
         _fetch_json=None, _head=None):
    global kget, ksend, NS, platform, hv_download, images, image_disks, delete_image, fetch_json, head
    kget, ksend, NS, platform = _kget, _ksend, namespace, _platform
    hv_download, images, image_disks, delete_image = _hv_download, _images, _image_disks, _delete_image
    fetch_json = _fetch_json or _get_json
    head = _head or _head_request


def _get_json(url):
    request = urllib.request.Request(url, headers={"User-Agent": "Homestead", "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode())


def _head_request(url):
    request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "Homestead"})
    with urllib.request.urlopen(request, timeout=20) as response:
        h = response.headers
        return {"etag": (h.get("ETag") or "").strip('"'), "modified": h.get("Last-Modified") or "",
                "size": int(h.get("Content-Length") or 0)}


# ------------------------------------------------------------------ addresses
def arch():
    """The architecture most nodes run: amd64 or arm64."""
    found = (platform(False) or {}).get("arch") or []
    return "arm64" if found and all(a == "arm64" for a in found) else "amd64"


def _fedora_urls():
    if time.time() - _fedora["at"] < CHECK_EVERY and _fedora["value"]:
        return _fedora["value"]
    rows = [r for r in fetch_json(FEDORA) if r.get("variant") == "Cloud" and r.get("subvariant") == "Cloud_Base"
            and str(r.get("link", "")).endswith(".qcow2") and "Generic" in r.get("link", "")
            and re.fullmatch(r"\d+", str(r.get("version", "")))]
    newest = max((int(r["version"]) for r in rows), default=0)
    urls = {{"x86_64": "amd64", "aarch64": "arm64"}.get(r.get("arch"), ""): r["link"]
            for r in rows if int(r["version"]) == newest}
    urls.pop("", None)
    _fedora.update(at=time.time(), value={"urls": urls, "version": str(newest)})
    return _fedora["value"]


def url_for(entry, on=None):
    """The publisher's address for this entry's newest build, or ""."""
    on = on or arch()
    if entry.get("resolve") == "fedora":
        try:
            return _fedora_urls()["urls"].get(on, "")
        except Exception:
            return ""
    return (entry.get("urls") or {}).get(on, "")


def upstream(url):
    """What the publisher says of its newest build, asked at most hourly."""
    with _lock:
        cached = _heads.get(url)
    if cached and time.time() - cached["at"] < HEAD_TTL:
        return cached["value"]
    try:
        value = head(url)
    except Exception as error:
        value = {"error": str(error)[:160]}
    with _lock:
        _heads[url] = {"at": time.time(), "value": value}
    return value


def _same_build(version, now):
    """Whether a kept build is the one the publisher has now: by ETag where
    both have one, else by date and size."""
    if now.get("error") or not version:
        return True
    if version.get("etag") and now.get("etag"):
        return version["etag"] == now["etag"]
    return version.get("modified") == now.get("modified") and version.get("size") == now.get("size")


# ------------------------------------------------------------------ what is kept
def _path():
    return f"/api/v1/namespaces/{NS}/configmaps/{CONFIGMAP}"


def load():
    try:
        cm = kget(_path())
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return {"kept": {}}, None
        raise
    try:
        data = json.loads((cm.get("data") or {}).get("store.json") or "{}")
    except ValueError:
        data = {}
    data.setdefault("kept", {})
    return data, cm


def save(data, cm):
    body = {"apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": CONFIGMAP, "namespace": NS, "labels": {"homestead.io/managed": "true"}},
            "data": {"store.json": json.dumps(data, sort_keys=True)}}
    if cm:
        body["metadata"]["resourceVersion"] = cm["metadata"].get("resourceVersion")
        ksend("PUT", _path(), body)
    else:
        ksend("POST", f"/api/v1/namespaces/{NS}/configmaps", body)


# ------------------------------------------------------------------ the store
def _harvester():
    return bool((platform(False) or {}).get("harvester"))


def view(check=False):
    """The catalogue, each entry with what is kept of it and whether a newer
    build is out."""
    data, _ = load()
    harvester, on = _harvester(), arch()
    have = {f"{i['namespace']}/{i['name']}": i for i in (images() if harvester else [])}
    rows = []
    for entry in CATALOG:
        url = url_for(entry, on)
        kept = data["kept"].get(entry["id"])
        versions = []
        for version in (kept or {}).get("versions") or []:
            image = have.get(version.get("image", ""))
            if harvester and not image:
                continue  # deleted outside the store
            versions.append(dict(version, ready=bool(image and image.get("ready")),
                                 failed=bool(image and image.get("failed")),
                                 progress=(image or {}).get("progress", 0)))
        newest = versions[-1] if versions else None
        now = upstream(url) if (kept and url and (check or harvester)) else {}
        rows.append({"id": entry["id"], "name": entry["name"], "publisher": entry["publisher"], "user": entry["user"],
                     "min_gb": entry["min_gb"], "url": url, "available": bool(url), "arch": on,
                     "kept": bool(kept), "auto": bool((kept or {}).get("auto")), "versions": versions,
                     "ready": bool(newest and newest["ready"]) if harvester else bool(kept),
                     "update": bool(harvester and newest and now and not _same_build(newest, now)),
                     "checked": (kept or {}).get("checked", 0), "error": now.get("error", "")})
    return {"harvester": harvester, "arch": on, "images": rows,
            "note": "" if harvester else "CDI fills each VM's disk straight from the publisher, so a new VM always "
                                         "starts from the newest build and there is nothing to keep."}


def _display(entry, now):
    """A build's name in Harvester's list: the image and the publisher's
    date for it. Harvester wants each name once, so a second build of the
    same day is told apart."""
    try:
        stamp = time.strftime("%Y-%m-%d", time.strptime(now.get("modified", ""), "%a, %d %b %Y %H:%M:%S GMT"))
    except (TypeError, ValueError):
        stamp = time.strftime("%Y-%m-%d", time.gmtime())
    name = f"{entry['name']} · {stamp}"
    taken = {i.get("display") for i in images()}
    return name if name not in taken else f"{name} ({(now.get('etag') or str(int(time.time())))[-6:]})"


def keep(image_id, auto=True):
    """Keep an image: on Harvester, download its newest build now."""
    entry = BY_ID.get(image_id)
    if not entry:
        raise ValueError(f"the store has no image {image_id}")
    url = url_for(entry)
    if not url:
        raise ValueError(f"{entry['name']} has no {arch()} image")
    data, cm = load()
    kept = data["kept"].setdefault(image_id, {"versions": []})
    kept["auto"] = bool(auto)
    detail = f"{entry['name']} is kept"
    if _harvester() and not kept["versions"]:
        now = upstream(url)
        made = hv_download(url, _display(entry, now), True)
        kept["versions"].append({"image": f"{made['namespace']}/{made['name']}", "etag": now.get("etag", ""),
                                 "modified": now.get("modified", ""), "size": now.get("size", 0),
                                 "added": int(time.time())})
        detail = f"{entry['name']} is downloading as a Harvester image" + ("" if not made.get("reused") else " (already there)")
    kept["checked"] = int(time.time())
    save(data, cm)
    return {"ok": True, "detail": detail + ("; it keeps itself current" if auto else "")}


def set_auto(image_id, auto):
    data, cm = load()
    if image_id not in data["kept"]:
        raise ValueError("that image is not kept")
    data["kept"][image_id]["auto"] = bool(auto)
    save(data, cm)
    return {"ok": True}


def _in_use(ref):
    return bool((image_disks() or {}).get(ref))


def forget(image_id):
    """Stop keeping an image: its builds no disk was made from are deleted;
    those a disk came from stay until that disk is gone."""
    data, cm = load()
    kept = data["kept"].pop(image_id, None)
    if kept is None:
        raise ValueError("that image is not kept")
    left = []
    for version in kept.get("versions") or []:
        ns, _, name = version["image"].partition("/")
        if _in_use(version["image"]):
            left.append(version["image"])
        else:
            try:
                delete_image(ns, name)
            except Exception:
                left.append(version["image"])
    save(data, cm)
    return {"ok": True, "detail": "No longer kept" + (f"; {', '.join(left)} stay{'s' if len(left) == 1 else ''} while disks use "
                                                     f"{'it' if len(left) == 1 else 'them'}" if left else "")}


def refresh(force=False):
    """Bring kept images up to date: a new build is downloaded beside the
    old; once it is ready, older builds no disk was made from go."""
    if not _harvester():
        return {"ok": True, "updated": [], "tidied": []}
    data, cm = load()
    have = {f"{i['namespace']}/{i['name']}": i for i in images()}
    updated, tidied, now_ts = [], [], int(time.time())
    for image_id, kept in data["kept"].items():
        entry = BY_ID.get(image_id)
        if not entry or not (kept.get("auto") or force):
            continue
        if not force and now_ts - int(kept.get("checked") or 0) < CHECK_EVERY:
            continue
        url = url_for(entry)
        kept["checked"] = now_ts
        versions = [v for v in kept.get("versions") or [] if v.get("image") in have]
        newest = versions[-1] if versions else None
        if newest and (have[newest["image"]].get("ready")):
            # The newest is ready: the older builds nothing uses can go.
            for old in versions[:-1]:
                if not _in_use(old["image"]):
                    ns, _, name = old["image"].partition("/")
                    try:
                        delete_image(ns, name)
                        tidied.append(old["image"])
                    except Exception:
                        pass
            versions = [v for v in versions if v["image"] not in tidied]
        with _lock:
            _heads.pop(url, None)
        now = upstream(url)
        if url and not now.get("error") and not (newest and _same_build(newest, now)):
            made = hv_download(url, _display(entry, now), not newest)
            versions.append({"image": f"{made['namespace']}/{made['name']}", "etag": now.get("etag", ""),
                             "modified": now.get("modified", ""), "size": now.get("size", 0), "added": now_ts})
            updated.append(entry["name"])
        kept["versions"] = versions
    save(data, cm)
    return {"ok": True, "updated": updated, "tidied": tidied}


def choices():
    """The catalogue as the New VM form offers it: what is available for
    these nodes, and which are kept and ready - without asking publishers."""
    try:
        data, _ = load()
    except Exception:
        data = {"kept": {}}
    harvester, on = _harvester(), arch()
    ready = {f"{i['namespace']}/{i['name']}" for i in (images() if harvester else []) if i.get("ready")}
    out = []
    for entry in CATALOG:
        kept = data["kept"].get(entry["id"]) or {}
        has = bool(entry.get("resolve")) or bool((entry.get("urls") or {}).get(on))
        if not has:
            continue
        out.append({"id": entry["id"], "name": entry["name"], "publisher": entry["publisher"],
                    "user": entry["user"], "min_gb": entry["min_gb"], "kept": bool(kept),
                    "ready": any(v.get("image") in ready for v in kept.get("versions") or []) if harvester else True})
    return out


def source_for(image_id):
    """What a new VM starts from: the newest ready build kept on Harvester,
    else the publisher's address."""
    entry = BY_ID.get(image_id)
    if not entry:
        raise ValueError(f"the store has no image {image_id}")
    if _harvester():
        data, _ = load()
        have = {f"{i['namespace']}/{i['name']}": i for i in images()}
        for version in reversed((data["kept"].get(image_id) or {}).get("versions") or []):
            image = have.get(version.get("image"))
            if image and image.get("ready"):
                return {"image_id": version["image"], "min_gb": entry["min_gb"], "user": entry["user"]}
    url = url_for(entry)
    if not url:
        raise ValueError(f"{entry['name']} has no {arch()} image")
    return {"image_url": url, "min_gb": entry["min_gb"], "user": entry["user"]}
