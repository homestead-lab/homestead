"""Logos for apps that are already running, picked from the app store.

The app store's catalogue names a logo for each of its apps. An app that was
deployed by hand, imported, or installed before logos were kept has none, so
this finds one: first the catalogue entry whose image is the app's own image
(lscr.io/linuxserver/sonarr and linuxserver/sonarr are the same repository),
then entries whose name matches. A chosen logo is saved like any other, through
homestead_icons, so it survives the catalogue going away.
"""
import re

LIMIT = 24
_REF = re.compile(r"^[a-z0-9][a-z0-9._/:@+-]*$")

# Each operating system Homestead has a logo for (web/assets/os-<key>.svg),
# with what its guest agent, Harvester's label or an image name call it.
# More particular names come before the families they belong to.
OS_LOGOS = [
    ("homeassistant", r"home\s*assistant|haos"), ("truenas", r"truenas|freenas"), ("unraid", r"unraid"),
    ("proxmox", r"proxmox|\bpve\b"), ("opnsense", r"opnsense"), ("pfsense", r"pfsense"), ("talos", r"talos"),
    ("k3s", r"\bk3s\b"), ("popos", r"pop[!_ ]?_?os"), ("linuxmint", r"\bmint\b"), ("kalilinux", r"\bkali\b"),
    ("elementary", r"elementary"), ("zorin", r"zorin"), ("ubuntu", r"ubuntu"), ("debian", r"debian"),
    ("rockylinux", r"\brocky"), ("almalinux", r"\balma"), ("centos", r"centos"),
    ("redhat", r"red\s*hat|\brhel\b"), ("opensuse", r"opensuse|\bleap\b|tumbleweed"), ("suse", r"\bsuse|\bsles\b"),
    ("archlinux", r"\barch\b|archlinux"), ("manjaro", r"manjaro"), ("alpinelinux", r"alpine"), ("nixos", r"\bnixos\b"),
    ("gentoo", r"gentoo"), ("freebsd", r"freebsd"), ("openbsd", r"openbsd"), ("android", r"android"),
    ("windows", r"windows|mswindows|\bwin(10|11|2k|20\d\d)\b"), ("linux", r"\blinux\b"),
]
OS_KEYS = [key for key, _ in OS_LOGOS]
_OS = [(key, re.compile(pattern, re.I)) for key, pattern in OS_LOGOS]


def os_logo(*names):
    """The bundled logo for whatever these names say the OS is, or ""."""
    text = " ".join(str(n or "") for n in names)
    return next((key for key, pattern in _OS if pattern.search(text)), "")


def image_repo(ref):
    """An image's repository without its registry, tag or digest, lower case.

    ghcr.io/hotio/sonarr:release -> hotio/sonarr; nginx -> nginx;
    docker.io/library/nginx@sha256:... -> nginx."""
    ref = str(ref or "").strip().lower()
    if not ref or not _REF.match(ref):
        return ""
    ref = ref.split("@", 1)[0]
    parts = ref.split("/")
    if len(parts) > 1 and ("." in parts[0] or ":" in parts[0] or parts[0] == "localhost"):
        parts = parts[1:]
    last = parts[-1].split(":", 1)[0]
    parts = parts[:-1] + [last]
    if parts[0] == "library" and len(parts) == 2:
        parts = parts[1:]
    return "/".join(p for p in parts if p)


def _usable(app):
    icon = str(app.get("icon") or "")
    return icon.startswith(("https://", "http://")) and app.get("name")


def _tile(app, match):
    return {"name": str(app.get("name")), "icon": str(app["icon"]), "repo": str(app.get("repo") or ""),
            "key": f"{app.get('name')}|{app.get('repo')}", "match": match}


def image_matches(apps, images):
    """Catalogue apps whose image is one of these images, most downloaded first."""
    wanted = {image_repo(i) for i in images or []} - {""}
    if not wanted:
        return []
    found = [a for a in apps if _usable(a) and image_repo(a.get("repo")) in wanted]
    return sorted(found, key=lambda a: -int(a.get("downloads") or 0))


def chart_tile(chart):
    """The logo a Helm chart names for itself, as a tile; None without one.
    chart: {name, icon} from the app's Helm release."""
    icon = str((chart or {}).get("icon") or "")
    if not icon.startswith(("https://", "http://")):
        return None
    return {"name": str(chart.get("name") or "chart"), "icon": icon, "repo": "", "key": f"chart|{icon}", "match": "chart"}


def suggest(apps, images, term, search, limit=LIMIT, chart=None):
    """Tiles for the picker: its Helm chart's logo, image matches, then what
    search(apps, term) finds.

    search is the app store's own ranking, so the picker finds what the store
    would. One tile per logo: the same picture under two names is one choice."""
    tiles, seen = [], set()
    own = chart_tile(chart)
    if own:
        tiles.append(own)
        seen.add(own["icon"])
    for match, found in (("image", image_matches(apps, images)),
                         ("name", search(apps, term) if str(term or "").strip() else [])):
        for app in found:
            if not _usable(app) or app["icon"] in seen:
                continue
            seen.add(app["icon"])
            tiles.append(_tile(app, match))
            if len(tiles) >= limit:
                return tiles
    return tiles


def best_guess(apps, images, name, chart=None):
    """One suggestion for an app with no logo, or None.

    The logo its own Helm chart names, or an image match, is near certain. A
    catalogue app with exactly the app's name is a fair guess, offered but not
    chosen for you."""
    own = chart_tile(chart)
    if own:
        return own
    found = image_matches(apps, images)
    if found:
        return _tile(found[0], "image")
    plain = str(name or "").strip().lower()
    if plain:
        named = [a for a in apps if _usable(a) and str(a["name"]).strip().lower() == plain]
        if named:
            return _tile(sorted(named, key=lambda a: -int(a.get("downloads") or 0))[0], "name")
    return None


def missing(workloads, apps, charts=None):
    """Apps that can have a logo and have none, each with its best guess.
    charts: {(namespace, release): {name, icon}} for apps Helm installed."""
    charts = charts or {}
    out = []
    for w in workloads or []:
        if w.get("icon") or w.get("has_logo") or w.get("logo_skipped") or w.get("platform") or w.get("self") or w.get("homestead") \
                or w.get("managed_smb") or w.get("managed_nfs") or w.get("site"):
            continue
        out.append({"ns": w.get("ns"), "name": w.get("name"), "images": list(w.get("images") or []),
                    "suggestion": best_guess(apps, w.get("images"), w.get("name"),
                                             charts.get((w.get("ns"), w.get("helm_release"))))})
    order = {"chart": 0, "image": 0, "name": 1, None: 2}
    return sorted(out, key=lambda r: (order[(r["suggestion"] or {}).get("match")], r["ns"] or "", r["name"] or ""))
