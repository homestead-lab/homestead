"""Deploy: the App Store catalogue.

The Community Applications feed, read and cached; its text made plain; search,
ranking and the spotlight; and an App Store template turned into a deploy
config. Moved unchanged from homestead_deploy (#623): read and replace these
names here. Names server.py binds on homestead_deploy are read through it
(D.x), so a replacement there still applies.
"""
import html
import json
import os
import re
import time
import urllib.request

import homestead_privileges as PRIV


CA_FEED = os.environ.get(
    "COMMUNITY_CATALOG_URL",
    "https://raw.githubusercontent.com/Squidly271/AppFeed/master/applicationFeed.json",
)


def category_values(value):
    """Flatten inconsistent feed category shapes into stable display strings."""
    found = []

    def visit(item):
        if isinstance(item, (list, tuple)):
            for child in item:
                visit(child)
        elif isinstance(item, dict):
            preferred = [item.get(key) for key in ("name", "label", "category", "Category")]
            usable = [child for child in preferred if child not in (None, "")]
            for child in usable or item.values():
                visit(child)
        elif item is not None:
            for part in re.split(r"[\s,|]+", str(item).strip()):
                if part and part not in found:
                    found.append(part)

    visit(value)
    return found


# Markup in catalogue text: HTML tags, and forum codes such as [b], [/span] and
# [span style='color: red'] - a known tag name, so "[1]" or "[x86]" survive.
CATALOG_MARKUP = re.compile(
    r"<[^>]*>|\[/?(?:b|i|u|s|br|p|hr|img|url|span|color|size|font|center|left|right|quote|code|list|li|\*|h[1-6])"
    r"(?:[= ][^\]]*)?\]", re.I)


def catalog_text(value, limit=None):
    """Turn catalogue HTML fragments and entities into compact readable text."""
    text = str(value or "")
    # Some catalogue fields contain encoded markup (and occasionally encode it
    # twice), so decode before removing tags.
    for _ in range(2):
        decoded = html.unescape(text)
        if decoded == text:
            break
        text = decoded
    text = re.sub(r"(?i)<\s*br\s*/?\s*>|\[\s*br\s*/?\s*\]", "\n", text)
    text = re.sub(r"(?i)</\s*(?:p|div|li|tr|h[1-6])\s*>", "\n", text)
    # HTML, and the forum's [b]/[span style=...] codes that templates use too.
    text = re.sub(CATALOG_MARKUP, "", text)
    text = html.unescape(text).replace("\xa0", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\s*\n+\s*", " · ", text)
    text = re.sub(r"(?:\s*·\s*)+", " · ", text).strip(" ·")
    return text[:limit] if limit is not None else text


def catalog_paragraphs(value, limit=8000):
    """Catalogue text with its paragraphs kept, for reading in full.

    Overviews mix HTML, entities and the forum's [b]/[br] codes; each becomes
    plain text with line breaks where the author put them."""
    text = str(value or "")
    for _ in range(2):
        decoded = html.unescape(text)
        if decoded == text:
            break
        text = decoded
    text = re.sub(r"(?i)\[\s*br\s*/?\s*\]|<\s*br\s*/?\s*>", "\n", text)
    text = re.sub(r"(?i)</\s*(?:p|div|li|tr|h[1-6])\s*>", "\n", text)
    text = re.sub(CATALOG_MARKUP, "", text)
    text = html.unescape(text).replace("\xa0", " ").replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(line.strip() for line in text.split("\n"))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:limit]


def catalog_reason(value):
    """A spotlight's reason, which the feed gives as a dict - sometimes as its text."""
    if isinstance(value, str) and value.strip().startswith("{"):
        try:
            import ast
            value = ast.literal_eval(value)
        except (ValueError, SyntaxError):
            pass
    if isinstance(value, dict):
        value = value.get("en_US") or next(iter(value.values()), "")
    return catalog_text(value)


def catalog_links(value):
    if isinstance(value, str):
        value = [value]
    return [str(x) for x in (value or []) if isinstance(x, str) and x.startswith(("http://", "https://"))][:6]


def appstore_key(app):
    return f"{app.get('name')}|{app.get('repo')}"


# What a list of apps needs; the full record comes one app at a time.
APPSTORE_HEAVY = ("overview", "config", "screenshots", "readme", "comment", "requires")


def appstore_summary(app):
    return {k: v for k, v in app.items() if k not in APPSTORE_HEAVY}


def search_appstore(apps, term):
    """Return catalogue matches with name relevance ahead of description hits."""
    needle = str(term or "").strip().lower()
    if not needle:
        return list(apps)

    def relevance(app):
        name = str(app.get("name") or "").lower()
        description = str(app.get("desc") or "").lower()
        if name == needle:
            return 0
        if name.startswith(needle):
            return 1
        if re.search(rf"(?:^|[^a-z0-9]){re.escape(needle)}", name):
            return 2
        if needle in name:
            return 3
        if needle in description:
            return 4
        return None

    ranked = []
    for position, app in enumerate(apps):
        score = relevance(app)
        if score is not None:
            ranked.append((score, position, app))
    return [app for _, _, app in sorted(ranked, key=lambda row: (row[0], row[1]))]


def appstore_number(value):
    """Coerce optional feed statistics without letting malformed rows break browsing."""
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def unique_appstore_apps(apps):
    """Hide duplicate templates that point at the same named container image."""
    seen, out = set(), []
    for app in apps:
        key = (str(app.get("name") or "").strip().lower(),
               str(app.get("repo") or "").strip().lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(app)
    return out


def rank_appstore(apps, mode="popular"):
    """Rank cached catalogue rows using only statistics supplied by the feed."""
    rows = unique_appstore_apps(apps)
    if mode == "spotlight":
        return sorted((app for app in rows if app.get("spotlight")),
                      key=lambda app: -app["spotlight"]["date"])
    if mode == "recent":
        # Templates added in one feed update share a timestamp; the feed's own
        # order among them is the one Community Applications shows.
        key = lambda app: -appstore_number(app.get("first_seen"))
    elif mode == "trending":
        key = lambda app: (-appstore_number(app.get("top_trending")),
                           -appstore_number(app.get("trending")),
                           -appstore_number(app.get("top_performing")),
                           -appstore_number(app.get("downloads")),
                           str(app.get("name") or "").lower())
    else:
        key = lambda app: (-appstore_number(app.get("top_performing")),
                           -appstore_number(app.get("trending")),
                           -appstore_number(app.get("top_trending")),
                           -appstore_number(app.get("downloads")),
                           str(app.get("name") or "").lower())
    return sorted(rows, key=key)


def appstore_spotlight(apps):
    """Choose the strongest feed-ranked app for the catalogue spotlight."""
    ranked = rank_appstore(apps, "popular")
    return next((app for app in ranked if appstore_number(app.get("top_performing")) > 0),
                next((app for app in ranked if appstore_number(app.get("trending")) > 0),
                     ranked[0] if ranked else None))


def catalog_source():
    """The catalogue feed in use: the one set in Settings, else the default."""
    try:
        custom = D.cached("settings", 15, D.get_app_settings).get("catalog_url") or ""
    except Exception:
        custom = ""
    return custom or CA_FEED


def fetch_appstore():
    source = catalog_source()

    def go():
        req = urllib.request.Request(source, headers={
            "User-Agent": f"Homestead/{D.HOMESTEAD_VERSION} (+https://github.com/homestead-lab/homestead)",
            "Accept": "application/json",
        })
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        apps = data.get("applist", data if isinstance(data, list) else [])
        out = []
        for a in apps:
            repo = a.get("Repository") or ""
            if not repo or not a.get("Name"):
                continue
            # Containers only, as Community Applications shows them: a plugin
            # or language pack is Unraid's own software, and blacklisted,
            # deprecated or hidden templates are not offered there either.
            if (a.get("Plugin") or a.get("PluginURL") or a.get("Language") or a.get("LanguagePack")
                    or a.get("Blacklist") or a.get("Deprecated") or a.get("hideFromCA")
                    or str(repo).lower().endswith(".plg")):
                continue
            spotlight = None
            if appstore_number(a.get("RecommendedDate")):
                stamp = int(appstore_number(a.get("RecommendedDate")))
                spotlight = {"date": stamp, "month": time.strftime("%b %Y", time.gmtime(stamp)),
                             "reason": catalog_reason(a.get("RecommendedReason")),
                             "who": catalog_text(a.get("RecommendedWho") or "")}
            maintainer = a.get("Maintainer") or a.get("Author") or ""
            if isinstance(maintainer, dict):
                maintainer = maintainer.get("Name") or next((v for v in maintainer.values() if isinstance(v, str)), "")
            maintainer = str(maintainer or re.sub(r"'s Repository$", "", str(a.get("Repo") or ""))).strip()
            categories = category_values(a.get("CategoryList") or a.get("Category") or "")
            item = {
                "name": a.get("Name"),
                "repo": repo,
                "icon": a.get("Icon") or "",
                "desc": catalog_text(a.get("Overview") or a.get("Description") or "", 300),
                "cat": categories[0] if categories else "",
                "categories": categories,
                "web": a.get("Project") or a.get("Support") or "",
                "network": a.get("Network") or "bridge",
                "webui": a.get("WebUI") or "",
                "config": a.get("Config") or [],
                "downloads": int(appstore_number(a.get("downloads"))),
                "stars": int(appstore_number(a.get("stars"))),
                "trending": appstore_number(a.get("trending")),
                "top_trending": appstore_number(a.get("topTrending")),
                "top_performing": appstore_number(a.get("topPerforming")),
                "first_seen": int(appstore_number(a.get("FirstSeen"))),
                "last_update": int(appstore_number(a.get("LastUpdate"))),
                "spotlight": spotlight,
                "maintainer": catalog_text(maintainer, 80),
                "official": bool(a.get("Official") or a.get("LTOfficial")),
                "beta": str(a.get("Beta") or "").lower() in ("true", "1", "yes"),
                "privileged": str(a.get("Privileged") or "").lower() == "true",
                "extra_params": catalog_text(a.get("ExtraParams") or "", 600),
                "links": {k: v for k, v in {
                    "project": a.get("Project"), "support": a.get("Support"), "registry": a.get("Registry"),
                    "readme": a.get("ReadMe") or a.get("Readme"), "video": a.get("Video"),
                    "discord": a.get("Discord"), "github": a.get("GitHub"), "web": a.get("WebPage"),
                }.items() if isinstance(v, str) and v.startswith(("http://", "https://"))},
                "overview": catalog_paragraphs(a.get("Overview") or a.get("Description") or ""),
                "screenshots": catalog_links(a.get("Screenshot")),
                "requires": catalog_text(a.get("Requires") or "", 600),
                "comment": catalog_text(a.get("CAComment") or a.get("ModeratorComment") or "", 600),
                "license": catalog_text(a.get("License") or a.get("Licence") or "", 80),
            }
            item["deploy"] = template_to_cfg(item)
            out.append(item)
        return out
    return D.cached(f"appstore:{source}", 21600, go)


def appdata_folders(vols):
    """Gives each path an app keeps in its appdata volume a folder of its own.

    An Unraid template maps each path to its own host folder; here they share
    one volume, as an import lays them out, each mounted from its own folder
    with subPath. A single path takes the whole volume. The volume is sized
    for everything in it."""
    shared = [v for v in vols if v.get("type") == "pvc" and v.get("create")]
    if len(shared) < 2:
        return vols
    taken, total = set(), 0
    access = "ReadWriteMany" if any(v.get("access_mode") == "ReadWriteMany" for v in shared) else "ReadWriteOnce"
    for v in shared:
        base = re.sub(r"[^a-z0-9._-]+", "-", str(v["path"]).rstrip("/").rsplit("/", 1)[-1].lower()).strip("-.") or "data"
        folder, n = base, 2
        while folder in taken:
            folder, n = f"{base}-{n}", n + 1
        taken.add(folder)
        v["sub_path"] = folder
        total += int(v.get("size_gb") or 5)
        v["access_mode"] = access
    for v in shared:
        v["size_gb"] = total
    return vols


def template_to_cfg(app):
    """Turn an Unraid CA template entry into our deploy config."""
    ports, envs, env_meta, vols, devices = [], {}, [], [], []
    cfgs = app.get("config") or []
    if isinstance(cfgs, dict):
        cfgs = [cfgs]
    if not isinstance(cfgs, list):
        cfgs = []
    name = re.sub(r"[^a-z0-9-]", "-", app["name"].lower()).strip("-")[:40]
    for c in cfgs:
        if not isinstance(c, dict):
            continue
        attrs = c.get("@attributes", {}) or {}
        typ = str(attrs.get("Type") or c.get("Type") or "").strip().lower()
        tgt = str(attrs.get("Target") or c.get("Target") or "").strip()
        val = c.get("value")
        if val is None or val == "":
            val = attrs.get("Default") or c.get("Default") or ""
        val = str(val)
        label = catalog_text(attrs.get("Name") or c.get("Name") or tgt)
        description = catalog_text(attrs.get("Description") or c.get("Description") or "")
        required = str(attrs.get("Required") or c.get("Required") or "false").lower() == "true"
        mode = str(attrs.get("Mode") or c.get("Mode") or "")
        read_only = mode.lower() == "ro"
        if typ == "port" and tgt:
            try:
                protocol = mode.upper() if mode.lower() in ("tcp", "udp") else "TCP"
                ports.append({"container": int(tgt), "host": int(val or tgt), "expose": True,
                              "name": f"p{tgt}-{protocol.lower()}", "protocol": protocol,
                              "label": label, "description": description, "required": required})
            except (TypeError, ValueError):
                pass
        elif typ == "variable" and tgt:
            options = [option.strip() for option in val.split("|") if option.strip()] if "|" in val else []
            if options:
                val = options[0]
            masked = str(attrs.get("Mask", "false")).lower() == "true"
            secret_value = masked or bool(re.search(r"(?:PASSWORD|PASS|TOKEN|SECRET|API_KEY|APIKEY)$", tgt, re.I))
            generate = secret_value and (bool(val) or required or (masked and "password" in tgt.lower()))
            if generate:
                # Public catalogue defaults must never become deployed credentials.
                val = ""
            envs[tgt] = val
            env_meta.append({"key": tgt, "label": label, "description": description,
                             "required": required, "masked": secret_value,
                             "generate": generate, "options": options})
        elif typ == "path" and tgt:
            system_bind = tgt in ("/etc/localtime", "/var/run/docker.sock") and val == tgt
            clean_path = tgt.rstrip("/").lower() or "/"
            context = " ".join((clean_path, label, description, val)).lower()
            tokens = set(re.split(r"[^a-z0-9]+", context))
            cache_tokens = {"cache", "caches", "transcode", "transcoding", "temp", "temporary", "tmp"}
            media_tokens = {"media", "movie", "movies", "tv", "music", "photo", "photos",
                            "video", "videos", "recording", "recordings", "download", "downloads"}
            config_path = (clean_path == "/config" or clean_path.endswith("/config") or
                           "appdata" in str(val).lower())
            role = ("system" if system_bind else "cache" if tokens & cache_tokens else
                    "config" if config_path else "media" if tokens & media_tokens else
                    "data" if clean_path == "/data" or clean_path.endswith("/data") else "config")
            volume_type = "host" if system_bind else "emptyDir" if role == "cache" else "pvc"
            create = not system_bind and role not in ("cache", "media")
            size = 50 if role == "data" else 5
            access_mode = ("ReadWriteMany" if {"rwx", "shared", "multinode", "multi-node"} & tokens
                           else "ReadWriteOnce")
            source = (val if system_bind else "" if role in ("cache", "media") else f"{name}-appdata")
            vols.append({"path": tgt, "source": source,
                         "type": volume_type, "create": create, "role": role,
                         "access_mode": access_mode, "size_gb": size,
                         "read_only": read_only, "label": label, "description": description,
                         "required": required, "template_source": val})
        elif typ == "device":
            devices.append({"host_path": val or tgt, "container_path": tgt or val,
                            "label": label, "description": description, "required": required})
    appdata_folders(vols)
    network = str(app.get("network") or "bridge").strip().lower()
    network_mode = "host" if network == "host" else "loadbalancer"
    vip_mode = "auto" if network not in ("bridge", "host", "default", "") else "shared"

    # Host-mode templates frequently omit port rows. Preserve the useful WebUI
    # listener so users can switch to a Service without re-reading the template.
    if not ports:
        match = re.search(r"\[PORT:(\d+)\]", str(app.get("webui") or ""), re.I)
        if match:
            port = int(match.group(1))
            ports.append({"container": port, "host": port, "expose": network != "host",
                          "name": f"p{port}-tcp", "protocol": "TCP",
                          "label": "Web interface", "description": "Inferred from the template WebUI URL",
                          "required": True})
    # What Unraid lets it do to its host: Privileged, and the capabilities and
    # tunnel device its extra parameters ask for (VPN containers need them).
    privileges = PRIV.from_docker(app.get("extra_params", ""), app.get("privileged"),
                                  devices=[d["host_path"] for d in devices])
    devices = [d for d in devices if PRIV.TUN not in (d["host_path"] + d["container_path"])]
    cfg = {"name": name,
            "image": app["repo"], "icon": app.get("icon") or "",
            "ports": ports, "env": envs, "env_meta": env_meta, "volumes": vols,
            **privileges,
            "template_devices": devices, "template_network": network,
            "network_mode": network_mode, "vip_mode": vip_mode,
            "env_bindings": {}}
    return MANIFESTS.analyze_deploy_intent(cfg)


# Imported last, so this module can be imported before or after homestead_deploy:
# both only look these names up when a function runs.
import homestead_deploy as D  # noqa: E402
import homestead_deploy_manifests as MANIFESTS  # noqa: E402
