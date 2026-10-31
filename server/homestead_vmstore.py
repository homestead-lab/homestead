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
homestead-vm-store ConfigMap. A change to it is made on the record as stored
when it is saved (#621): what to download or delete is worked out on the
record read first, done once, and only then recorded, so a 409 from another
writer means recording it again, never downloading or deleting again.
"""
import calendar
import concurrent.futures
import json
import re
import threading
import time
import urllib.error
import urllib.request
import homestead_kube as KUBE
import homestead_routes
import homestead_value_masks as MASKS

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

# Each publisher's own address for its newest cloud image, per variant. Fedora
# and Alpine have no fixed "latest" address, so their newest is looked up.
UBUNTU = "https://cloud-images.ubuntu.com"
DEBIAN = "https://cloud.debian.org/images/cloud"
ROCKY = "https://dl.rockylinux.org/pub/rocky"
ALMA = "https://repo.almalinux.org/almalinux"
CENTOS = "https://cloud.centos.org/centos"
SUSE = "https://download.opensuse.org"
MINIMAL = "Smaller: fewer packages and no manuals, for a server you set up yourself"
DEBIAN_CLOUD = "A kernel slimmed for VMs - the usual choice"
DEBIAN_GENERIC = "The full kernel with every driver - for hardware passed through to the VM"
STANDARD = "The standard cloud image"
SUSE_MINIMAL = "openSUSE's minimal cloud VM"
LVM = "Its disk under LVM, to grow or split later"


def _entry(id_, distro, name, variant, about, user, urls=None, resolve=""):
    return {"id": id_, "distro": distro, "name": name, "variant": variant, "about": about, "user": user,
            "min_gb": 10, "urls": urls or {}, "resolve": resolve}


CATALOG = [
    _entry("ubuntu-24.04", "Ubuntu", "Ubuntu 24.04 LTS", "Server", "The standard server image", "ubuntu",
           {"amd64": f"{UBUNTU}/noble/current/noble-server-cloudimg-amd64.img",
            "arm64": f"{UBUNTU}/noble/current/noble-server-cloudimg-arm64.img"}),
    _entry("ubuntu-24.04-minimal", "Ubuntu", "Ubuntu 24.04 LTS", "Minimal", MINIMAL, "ubuntu",
           {"amd64": f"{UBUNTU}/minimal/releases/noble/release/ubuntu-24.04-minimal-cloudimg-amd64.img",
            "arm64": f"{UBUNTU}/minimal/releases/noble/release/ubuntu-24.04-minimal-cloudimg-arm64.img"}),
    _entry("ubuntu-22.04", "Ubuntu", "Ubuntu 22.04 LTS", "Server", "The standard server image", "ubuntu",
           {"amd64": f"{UBUNTU}/jammy/current/jammy-server-cloudimg-amd64.img",
            "arm64": f"{UBUNTU}/jammy/current/jammy-server-cloudimg-arm64.img"}),
    _entry("ubuntu-22.04-minimal", "Ubuntu", "Ubuntu 22.04 LTS", "Minimal", MINIMAL, "ubuntu",
           {"amd64": f"{UBUNTU}/minimal/releases/jammy/release/ubuntu-22.04-minimal-cloudimg-amd64.img"}),
    _entry("debian-13", "Debian", "Debian 13 (trixie)", "Cloud", DEBIAN_CLOUD, "debian",
           {"amd64": f"{DEBIAN}/trixie/latest/debian-13-genericcloud-amd64.qcow2",
            "arm64": f"{DEBIAN}/trixie/latest/debian-13-genericcloud-arm64.qcow2"}),
    _entry("debian-13-generic", "Debian", "Debian 13 (trixie)", "Generic", DEBIAN_GENERIC, "debian",
           {"amd64": f"{DEBIAN}/trixie/latest/debian-13-generic-amd64.qcow2"}),
    _entry("debian-12", "Debian", "Debian 12 (bookworm)", "Cloud", DEBIAN_CLOUD, "debian",
           {"amd64": f"{DEBIAN}/bookworm/latest/debian-12-genericcloud-amd64.qcow2",
            "arm64": f"{DEBIAN}/bookworm/latest/debian-12-genericcloud-arm64.qcow2"}),
    _entry("debian-12-generic", "Debian", "Debian 12 (bookworm)", "Generic", DEBIAN_GENERIC, "debian",
           {"amd64": f"{DEBIAN}/bookworm/latest/debian-12-generic-amd64.qcow2"}),
    _entry("fedora", "Fedora", "Fedora Cloud", "Base", "The newest Fedora release's cloud image", "fedora",
           resolve="fedora"),
    _entry("rocky-10", "Rocky Linux", "Rocky Linux 10", "Base", STANDARD, "rocky",
           {"amd64": f"{ROCKY}/10/images/x86_64/Rocky-10-GenericCloud-Base.latest.x86_64.qcow2",
            "arm64": f"{ROCKY}/10/images/aarch64/Rocky-10-GenericCloud-Base.latest.aarch64.qcow2"}),
    _entry("rocky-10-lvm", "Rocky Linux", "Rocky Linux 10", "LVM", LVM, "rocky",
           {"amd64": f"{ROCKY}/10/images/x86_64/Rocky-10-GenericCloud-LVM.latest.x86_64.qcow2"}),
    _entry("rocky-9", "Rocky Linux", "Rocky Linux 9", "Base", STANDARD, "rocky",
           {"amd64": f"{ROCKY}/9/images/x86_64/Rocky-9-GenericCloud-Base.latest.x86_64.qcow2",
            "arm64": f"{ROCKY}/9/images/aarch64/Rocky-9-GenericCloud-Base.latest.aarch64.qcow2"}),
    _entry("rocky-9-lvm", "Rocky Linux", "Rocky Linux 9", "LVM", LVM, "rocky",
           {"amd64": f"{ROCKY}/9/images/x86_64/Rocky-9-GenericCloud-LVM.latest.x86_64.qcow2"}),
    _entry("almalinux-10", "AlmaLinux", "AlmaLinux 10", "Base", STANDARD, "almalinux",
           {"amd64": f"{ALMA}/10/cloud/x86_64/images/AlmaLinux-10-GenericCloud-latest.x86_64.qcow2",
            "arm64": f"{ALMA}/10/cloud/aarch64/images/AlmaLinux-10-GenericCloud-latest.aarch64.qcow2"}),
    _entry("almalinux-9", "AlmaLinux", "AlmaLinux 9", "Base", STANDARD, "almalinux",
           {"amd64": f"{ALMA}/9/cloud/x86_64/images/AlmaLinux-9-GenericCloud-latest.x86_64.qcow2",
            "arm64": f"{ALMA}/9/cloud/aarch64/images/AlmaLinux-9-GenericCloud-latest.aarch64.qcow2"}),
    _entry("centos-stream-10", "CentOS Stream", "CentOS Stream 10", "Base", STANDARD, "cloud-user",
           {"amd64": f"{CENTOS}/10-stream/x86_64/images/CentOS-Stream-GenericCloud-10-latest.x86_64.qcow2"}),
    _entry("centos-stream-9", "CentOS Stream", "CentOS Stream 9", "Base", STANDARD, "cloud-user",
           {"amd64": f"{CENTOS}/9-stream/x86_64/images/CentOS-Stream-GenericCloud-9-latest.x86_64.qcow2",
            "arm64": f"{CENTOS}/9-stream/aarch64/images/CentOS-Stream-GenericCloud-9-latest.aarch64.qcow2"}),
    _entry("opensuse-leap-16.0", "openSUSE", "openSUSE Leap 16.0", "Minimal", SUSE_MINIMAL, "opensuse",
           {"amd64": f"{SUSE}/distribution/leap/16.0/appliances/Leap-16.0-Minimal-VM.x86_64-Cloud.qcow2"}),
    _entry("opensuse-leap-15.6", "openSUSE", "openSUSE Leap 15.6", "Minimal", SUSE_MINIMAL, "opensuse",
           {"amd64": f"{SUSE}/distribution/leap/15.6/appliances/openSUSE-Leap-15.6-Minimal-VM.x86_64-Cloud.qcow2",
            "arm64": f"{SUSE}/distribution/leap/15.6/appliances/openSUSE-Leap-15.6-Minimal-VM.aarch64-Cloud.qcow2"}),
    _entry("opensuse-tumbleweed", "openSUSE", "openSUSE Tumbleweed", "Minimal", "Rolling release, minimal cloud VM",
           "opensuse",
           {"amd64": f"{SUSE}/tumbleweed/appliances/openSUSE-Tumbleweed-Minimal-VM.x86_64-Cloud.qcow2",
            "arm64": f"{SUSE}/ports/aarch64/tumbleweed/appliances/openSUSE-Tumbleweed-Minimal-VM.aarch64-Cloud.qcow2"}),
    _entry("arch", "Arch Linux", "Arch Linux", "Cloud", "Rolling release", "arch",
           {"amd64": "https://geo.mirror.pkgbuild.com/images/latest/Arch-Linux-x86_64-cloudimg.qcow2"}),
    _entry("alpine", "Alpine", "Alpine Linux", "Cloud-init",
           "Tiny - musl and OpenRC rather than glibc and systemd", "alpine", resolve="alpine"),
]
BY_ID = {entry["id"]: entry for entry in CATALOG}
FEDORA = "https://fedoraproject.org/releases.json"
_fedora = {"at": 0.0, "value": {}}


def bind(_kget, _ksend, namespace, _platform, _hv_download, _images, _image_disks, _delete_image,
         _fetch_json=None, _head=None, _fetch_text=None):
    global kget, ksend, NS, platform, hv_download, images, image_disks, delete_image, fetch_json, head, fetch_text
    fetch_text = _fetch_text or _get_text
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


ALPINE = "https://dl-cdn.alpinelinux.org/alpine/latest-stable/releases/cloud/"
ALPINE_FILE = re.compile(r'href="(alpine-([0-9.]+)-(x86_64|aarch64)-cloudinit-r([0-9]+)[.]qcow2)"')
_alpine = {"at": 0.0, "value": {}}
fetch_text = None        # url -> text


def _get_text(url):
    request = urllib.request.Request(url, headers={"User-Agent": "Homestead"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return response.read().decode("utf-8", "replace")


def _alpine_urls():
    """The newest Alpine release's cloud-init image, from its listing."""
    if time.time() - _alpine["at"] < CHECK_EVERY and _alpine["value"]:
        return _alpine["value"]
    found = {}
    for file, version, machine, release in ALPINE_FILE.findall(fetch_text(ALPINE)):
        on = "amd64" if machine == "x86_64" else "arm64"
        key = (tuple(int(x) for x in version.split(".")), int(release))
        if on not in found or key > found[on][0]:
            found[on] = (key, ALPINE + file)
    _alpine.update(at=time.time(), value={on: url for on, (_, url) in found.items()})
    return _alpine["value"]


def url_for(entry, on=None):
    """The publisher's address for this entry's newest build, or ""."""
    on = on or arch()
    try:
        if entry.get("resolve") == "fedora":
            return _fedora_urls()["urls"].get(on, "")
        if entry.get("resolve") == "alpine":
            return _alpine_urls().get(on, "")
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
    """The record as the pages show it: one that cannot be read is empty."""
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


UNREADABLE = "the VM store's record (homestead-vm-store) is not one Homestead wrote; nothing was changed"


def _checked(data):
    """The record to change: nothing written over one that is not a record."""
    data = {} if data is None else data
    if not isinstance(data, dict) or not isinstance(data.setdefault("kept", {}), dict) \
            or not all(isinstance(kept, dict) for kept in data["kept"].values()):
        raise ValueError(UNREADABLE)
    return data


def _record():
    """The record to work a change out on, before anything is downloaded or
    deleted for it: one that cannot be read stops the change."""
    try:
        cm = kget(_path())
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return {"kept": {}}
        raise
    try:
        # Exactly what update_json refuses: a missing key is no record yet, "" is not one.
        text = (cm.get("data") or {}).get("store.json")
        return _checked(json.loads("{}" if text is None else text))
    except ValueError:
        raise ValueError(UNREADABLE) from None


def _update(change):
    """change(record) made on the record as stored, and again on a newer one
    if another writer saved meanwhile."""
    def apply(data):
        data = _checked(data)
        change(data)
        return data
    KUBE.update_json(_path(), "store.json", apply, labels={"homestead.io/managed": "true"},
                     read=kget, write=ksend, sort_keys=True)


# ------------------------------------------------------------------ the store
def _harvester():
    return bool((platform(False) or {}).get("harvester"))


def _versions(entry, url, kept, have):
    """The builds of an entry on this cluster: those the store downloaded,
    and any Harvester image made from the same address some other way - by
    Harvester's own dashboard, or before the store - which it takes as its own."""
    known, versions = set(), []
    for version in (kept or {}).get("versions") or []:
        image = have.get(version.get("image", ""))
        if not image:
            continue  # deleted outside the store
        known.add(version["image"])
        versions.append(dict(version, ready=bool(image.get("ready")), failed=bool(image.get("failed")),
                             progress=image.get("progress", 0), display=image.get("display", "")))
    urls = {u for u in (entry.get("urls") or {}).values()} | ({url} if url else set())
    for ref, image in sorted(have.items(), key=lambda kv: kv[1].get("created", 0)):
        if ref not in known and image.get("url") and image.get("url") in urls:
            versions.append({"image": ref, "added": image.get("created", 0), "found": True,
                             "ready": bool(image.get("ready")), "failed": bool(image.get("failed")),
                             "progress": image.get("progress", 0), "display": image.get("display", "")})
    return sorted(versions, key=lambda v: v.get("added", 0))


def _source(image):
    """Where an image came from, by host and port only: its address can
    carry a login or a token."""
    return MASKS.host(image.get("url")) or image.get("source") or "upload"


def view(check=False):
    """The catalogue by distribution, each variant with its download size,
    what this cluster has of it and whether a newer build is out; and the VM
    images here that did not come from the catalogue."""
    data, _ = load()
    harvester, on = _harvester(), arch()
    have = {f"{i['namespace']}/{i['name']}": i for i in (images() if harvester else [])}
    urls = {entry["id"]: url_for(entry, on) for entry in CATALOG}
    # Sizes and newest builds, asked of every publisher at once, at most hourly.
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        heads = dict(zip(urls, pool.map(lambda url: upstream(url) if url else {}, urls.values())))
    rows, claimed = [], set()
    for entry in CATALOG:
        url, kept = urls[entry["id"]], data["kept"].get(entry["id"])
        versions = _versions(entry, url, kept, have) if harvester else []
        claimed.update(v["image"] for v in versions)
        newest = versions[-1] if versions else None
        now = heads.get(entry["id"]) or {}
        rows.append({"id": entry["id"], "distro": entry["distro"], "name": entry["name"], "variant": entry["variant"],
                     "about": entry["about"], "user": entry["user"], "min_gb": entry["min_gb"],
                     "url": url, "available": bool(url), "arch": on, "size": now.get("size", 0),
                     "kept": bool(kept or versions), "auto": bool((kept or {}).get("auto")), "versions": versions,
                     "ready": bool(newest and newest["ready"]) if harvester else True,
                     "update": bool(harvester and newest and now and not now.get("error")
                                    and newest.get("etag") is not None and not _same_build(newest, now)),
                     "checked": (kept or {}).get("checked", 0), "error": now.get("error", "")})
    own = [{"image": ref, "display": image.get("display", ""), "ready": bool(image.get("ready")),
            "failed": bool(image.get("failed")), "progress": image.get("progress", 0),
            "size_gb": image.get("size_gb", 0), "created": image.get("created", 0),
            "from": _source(image)}
           for ref, image in sorted(have.items(), key=lambda kv: kv[1].get("display", "").lower()) if ref not in claimed]
    return {"harvester": harvester, "arch": on, "images": rows, "own": own,
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
    kept = _record()["kept"].get(image_id) or {"versions": []}
    kept.setdefault("versions", [])
    detail, made_version = f"{entry['name']} is kept", None
    have = {f"{i['namespace']}/{i['name']}": i for i in images()} if _harvester() else {}
    found = [v for v in _versions(entry, url, kept, have) if v.get("found")]
    if found:
        kept["versions"] = [{k: v[k] for k in ("image", "added")} for v in found]
        # Downloaded after the publisher's current build came out, the newest
        # one found is that build: no need to fetch it again.
        now = upstream(url)
        try:
            built = calendar.timegm(time.strptime(now.get("modified", ""), "%a, %d %b %Y %H:%M:%S GMT"))
        except (TypeError, ValueError, OverflowError):
            built = None
        if built is not None and kept["versions"][-1]["added"] >= built:
            kept["versions"][-1].update(etag=now.get("etag", ""), modified=now.get("modified", ""), size=now.get("size", 0))
        detail = f"{entry['name']} {entry['variant']} was here already"
    elif _harvester() and not kept["versions"]:
        now = upstream(url)
        made = hv_download(url, _display(entry, now), True)
        made_version = {"image": f"{made['namespace']}/{made['name']}", "etag": now.get("etag", ""),
                        "modified": now.get("modified", ""), "size": now.get("size", 0), "added": int(time.time())}
        detail = f"{entry['name']} is downloading as a Harvester image" + ("" if not made.get("reused") else " (already there)")
    checked = int(time.time())

    def change(data):
        record = data["kept"].setdefault(image_id, {"versions": []})
        record["auto"], record["checked"] = bool(auto), checked
        mine = kept["versions"] if found else [made_version] if made_version else []
        stored = {v.get("image"): v for v in record.get("versions") or []}
        # A build recorded meanwhile keeps what was recorded of it (its etag, say).
        versions = [v for image, v in stored.items() if image not in {m["image"] for m in mine}] \
            + [dict(stored.get(m["image"]) or {}, **m) for m in mine]
        record["versions"] = sorted(versions, key=lambda v: v.get("added", 0))
    _update(change)
    return {"ok": True, "detail": detail + ("; it keeps itself current" if auto else "")}


def set_auto(image_id, auto):
    def change(data):
        if not isinstance(data["kept"].get(image_id), dict):
            raise ValueError("that image is not kept")
        data["kept"][image_id]["auto"] = bool(auto)
    _update(change)
    return {"ok": True}


def _in_use(ref):
    return bool((image_disks() or {}).get(ref))


def forget(image_id):
    """Stop keeping an image: its builds no disk was made from are deleted;
    those a disk came from stay until that disk is gone."""
    kept = _record()["kept"].get(image_id)
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
    _update(lambda data: data["kept"].pop(image_id, None))
    return {"ok": True, "detail": "No longer kept" + (f"; {', '.join(left)} stay{'s' if len(left) == 1 else ''} while disks use "
                                                     f"{'it' if len(left) == 1 else 'them'}" if left else "")}


def refresh(force=False):
    """Bring kept images up to date: a new build is downloaded beside the
    old; once it is ready, older builds no disk was made from go."""
    if not _harvester():
        return {"ok": True, "updated": [], "tidied": []}
    data = _record()
    have = {f"{i['namespace']}/{i['name']}": i for i in images()}
    updated, tidied, now_ts = [], [], int(time.time())
    done = {}   # image id -> (builds gone, build added or None)
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
        added = None
        if url and not now.get("error") and not (newest and _same_build(newest, now)):
            made = hv_download(url, _display(entry, now), not newest)
            added = {"image": f"{made['namespace']}/{made['name']}", "etag": now.get("etag", ""),
                     "modified": now.get("modified", ""), "size": now.get("size", 0), "added": now_ts}
            updated.append(entry["name"])
        kept_now = {v["image"] for v in versions}
        done[image_id] = ({v.get("image") for v in kept.get("versions") or []} - kept_now, added)

    def change(data):
        for image_id, (gone, added) in done.items():
            record = data["kept"].get(image_id)
            if not isinstance(record, dict):
                continue      # no longer kept
            record["checked"] = now_ts
            versions = [v for v in record.get("versions") or [] if v.get("image") not in gone]
            if added and added["image"] not in {v.get("image") for v in versions}:
                versions.append(added)
            record["versions"] = versions
    if done:
        _update(change)
    return {"ok": True, "updated": updated, "tidied": tidied}


def choices():
    """The catalogue as the New VM form offers it: what is available for
    these nodes, and which are kept and ready - without asking publishers."""
    try:
        data, _ = load()
    except Exception:
        data = {"kept": {}}
    harvester, on = _harvester(), arch()
    have = {f"{i['namespace']}/{i['name']}": i for i in (images() if harvester else [])}
    out = []
    for entry in CATALOG:
        kept = data["kept"].get(entry["id"]) or {}
        url = (entry.get("urls") or {}).get(on, "")
        if not (entry.get("resolve") or url):
            continue
        versions = _versions(entry, url, kept, have) if harvester else []
        out.append({"id": entry["id"], "distro": entry["distro"], "name": entry["name"], "variant": entry["variant"],
                    "user": entry["user"], "min_gb": entry["min_gb"], "kept": bool(kept or versions),
                    "ready": any(v["ready"] for v in versions) if harvester else True})
    return out


def source_for(image_id):
    """What a new VM starts from: the newest ready build kept on Harvester,
    else the publisher's address."""
    entry = BY_ID.get(image_id)
    if not entry:
        raise ValueError(f"the store has no image {image_id}")
    url = url_for(entry)
    if _harvester():
        data, _ = load()
        have = {f"{i['namespace']}/{i['name']}": i for i in images()}
        for version in reversed(_versions(entry, url, data["kept"].get(image_id), have)):
            if version["ready"]:
                return {"image_id": version["image"], "min_gb": entry["min_gb"], "user": entry["user"]}
    if not url:
        raise ValueError(f"{entry['name']} has no {arch()} image")
    return {"image_url": url, "min_gb": entry["min_gb"], "user": entry["user"]}


def _fresh(call):
    """Harvester's image list dropped from server.py's cache, then call()."""
    homestead_routes.forget("vmimages")
    return call()


# Its routes and who may use them (homestead_routes.py).
ROUTES = {
    ("POST", "/api/vm/store/auto"): ("admin", lambda request: set_auto(str(request.body.get("id") or ""), bool(request.body.get("auto")))),
    ("GET", "/api/vm/store"): ("viewer", lambda request: MASKS.for_role(
        request.role, view(check=(request.query.get("check") or [""])[0] == "1"))),
    ("POST", "/api/vm/store/keep"): ("admin", lambda request: _fresh(
        lambda: keep(str(request.body.get("id") or ""), request.body.get("auto", True) is not False))),
    ("POST", "/api/vm/store/forget"): ("admin", lambda request: _fresh(lambda: forget(str(request.body.get("id") or "")))),
    ("POST", "/api/vm/store/refresh"): ("admin", lambda request: _fresh(lambda: refresh(force=True))),
}
