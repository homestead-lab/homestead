"""Safe, persistent cache for workload icons.

Remote icon URLs are operator input, so fetching them is an SSRF boundary. We
only resolve public HTTP(S) hosts, re-check redirects, cap the response size,
and accept a small set of raster formats and SVG.

An SVG is a document that can carry script, so it is rebuilt before it is
kept: only drawing elements and their presentation attributes survive, with
no script, event handler, foreignObject, animation, embedded image, or link
or url() that leaves the file; a DOCTYPE or entity declaration is refused. It
is served with a sandboxing Content-Security-Policy as well, and pages show
logos through <img>, where an SVG's script never runs anyway. Files are content-addressed beneath
Homestead's Longhorn-backed DATA_DIR so rollouts do not depend on the source URL.
"""
import hashlib
import html.parser
import ipaddress
import json
import base64
import binascii
import os
import re
import socket
import ssl
import http.client
import time
import tempfile
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import homestead_shared as SHARED
import homestead_routes as ROUTER


MAX_ICON_BYTES = 256 * 1024
MIME_EXTENSIONS = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/x-icon": "ico",
    "image/vnd.microsoft.icon": "ico",
    "image/svg+xml": "svg",
}

ICON_NAME = re.compile(r"[0-9a-f]{64}\.(png|jpg|gif|webp|ico|svg)")
SVG_POLICY = "default-src 'none'; style-src 'unsafe-inline'; sandbox"

SVG_NS, XLINK_NS = "http://www.w3.org/2000/svg", "http://www.w3.org/1999/xlink"
SVG_ELEMENTS = {
    "svg", "g", "defs", "title", "desc", "symbol", "use", "style",
    "path", "rect", "circle", "ellipse", "line", "polyline", "polygon",
    "text", "tspan", "textPath",
    "linearGradient", "radialGradient", "stop", "clipPath", "mask", "pattern", "marker",
    "filter", "feBlend", "feColorMatrix", "feComponentTransfer", "feComposite", "feFlood",
    "feGaussianBlur", "feMerge", "feMergeNode", "feMorphology", "feOffset", "feFuncA", "feFuncR",
    "feFuncG", "feFuncB", "feDropShadow",
}
TEXT_ELEMENTS = {"text", "tspan", "textPath", "title", "desc"}
SVG_ATTRIBUTES = {
    "id", "class", "style", "viewBox", "preserveAspectRatio", "width", "height", "x", "y", "x1", "x2", "y1", "y2",
    "cx", "cy", "r", "rx", "ry", "fx", "fy", "d", "points", "transform", "pathLength", "version",
    "fill", "fill-opacity", "fill-rule", "stroke", "stroke-width", "stroke-linecap", "stroke-linejoin",
    "stroke-miterlimit", "stroke-dasharray", "stroke-dashoffset", "stroke-opacity", "opacity", "color",
    "clip-path", "clip-rule", "mask", "filter", "display", "visibility", "overflow", "vector-effect",
    "offset", "stop-color", "stop-opacity", "gradientUnits", "gradientTransform", "spreadMethod",
    "patternUnits", "patternContentUnits", "patternTransform", "clipPathUnits", "maskUnits", "maskContentUnits",
    "markerWidth", "markerHeight", "markerUnits", "refX", "refY", "orient",
    "font-family", "font-size", "font-weight", "font-style", "text-anchor", "dominant-baseline",
    "letter-spacing", "dx", "dy", "rotate", "textLength", "lengthAdjust", "startOffset",
    "filterUnits", "primitiveUnits", "in", "in2", "result", "stdDeviation", "mode", "operator",
    "k1", "k2", "k3", "k4", "values", "type", "tableValues", "slope", "intercept", "amplitude", "exponent",
    "flood-color", "flood-opacity", "radius", "isolation", "mix-blend-mode", "shape-rendering",
    "color-interpolation-filters", "href",
}
_CSS_URL = re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*\)", re.I | re.S)
_CSS_BAD = re.compile(r"@import|expression\s*\(|javascript:|behavior\s*:|-moz-binding", re.I)


def _public_target(url):
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError as exc:
        raise ValueError("logo URL is invalid") from exc
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("logo must use a public http:// or https:// URL")
    if parsed.username or parsed.password:
        raise ValueError("logo URL must not contain credentials")
    try:
        addresses = socket.getaddrinfo(
            parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80),
            type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError("logo host could not be resolved") from exc
    if not addresses:
        raise ValueError("logo host could not be resolved")
    for _, _, _, _, sockaddr in addresses:
        address = sockaddr[0]
        ip = ipaddress.ip_address(address.split("%", 1)[0])
        if not ip.is_global:
            raise ValueError("logo host must resolve only to public addresses")
    return parsed, addresses


def _validate_public_url(url):
    _public_target(url)
    return url


class _PinnedConnection(http.client.HTTPConnection):
    """No second DNS lookup, environmental proxy, or unverified TLS peer."""
    def __init__(self, parsed, addresses, timeout):
        super().__init__(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80), timeout=timeout)
        self.parsed, self.addresses = parsed, addresses

    def connect(self):
        deadline = time.monotonic() + self.timeout
        last = None
        for family, kind, protocol, _, sockaddr in self.addresses:
            raw = socket.socket(family, kind, protocol)
            try:
                raw.settimeout(max(0.01, deadline - time.monotonic()))
                raw.connect(sockaddr)
                if raw.getpeername()[0] != sockaddr[0]:
                    raise OSError("logo peer does not match the approved address")
                self.sock = (ssl.create_default_context().wrap_socket(raw, server_hostname=self.host)
                             if self.parsed.scheme == "https" else raw)
                return
            except OSError as error:
                raw.close()
                last = error
                if time.monotonic() >= deadline:
                    break
        raise OSError("logo host could not be reached") from last


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _sniff_mime(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"\x00\x00\x01\x00"):
        return "image/x-icon"
    if _looks_like_svg(data):
        return "image/svg+xml"
    raise ValueError("logo response is not a supported PNG, JPEG, GIF, WebP, ICO or SVG image")


def _looks_like_svg(data):
    head = data[:2048].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    return head.startswith((b"<?xml", b"<svg", b"<!--")) and b"<svg" in head


def _local(tag):
    return tag.rsplit("}", 1)[-1] if tag.startswith("{") else tag


def _safe_css(text):
    """Style text with no way out of the file: url() only to #fragments."""
    if _CSS_BAD.search(text or ""):
        return ""
    return _CSS_URL.sub(lambda m: m.group(0) if m.group(2).strip().startswith("#") else "none", text or "")


def clean_svg(data):
    """An SVG rebuilt from what is safe to draw. ValueError if it is not one.

    Cleaning a cleaned SVG gives the same bytes, so its content-addressed name
    is the same on every Homestead that keeps it."""
    if len(data) > MAX_ICON_BYTES:
        raise ValueError("logo is too large (maximum 256 KiB)")
    lowered = data.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise ValueError("an SVG logo with a DOCTYPE or entities is not accepted")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise ValueError("the SVG logo could not be read") from exc
    if root.tag not in (f"{{{SVG_NS}}}svg", "svg"):
        raise ValueError("the logo is not an SVG image")

    def rebuild(node):
        if not isinstance(node.tag, str):
            return None                      # a comment or processing instruction
        tag = _local(node.tag)
        if (node.tag.startswith("{") and not node.tag.startswith(f"{{{SVG_NS}}}")) or tag not in SVG_ELEMENTS:
            return None
        out = ET.Element(f"{{{SVG_NS}}}{tag}")
        for name, value in sorted(node.attrib.items()):
            local = _local(name)
            if name.startswith("{") and not name.startswith(f"{{{XLINK_NS}}}"):
                continue
            if local not in SVG_ATTRIBUTES or local.lower().startswith("on"):
                continue
            value = str(value)
            if local == "href":
                if value.startswith("#"):
                    out.set("href", value)
                continue
            if "javascript:" in value.lower().replace(" ", ""):
                continue
            if local == "style" or "url(" in value.lower():
                value = _safe_css(value)
                if not value:
                    continue
            out.set(local, value)
        if tag == "style":
            out.text = _safe_css("".join(node.itertext()))
            return out
        out.text = node.text if tag in TEXT_ELEMENTS else None
        for child in node:
            kept = rebuild(child)
            if kept is not None:
                kept.tail = child.tail if tag in TEXT_ELEMENTS else None
                out.append(kept)
        return out

    ET.register_namespace("", SVG_NS)
    return ET.tostring(rebuild(root), encoding="utf-8", xml_declaration=False, short_empty_elements=True)


IMAGE_ACCEPT = "image/png,image/jpeg,image/gif,image/webp,image/x-icon,image/svg+xml"
MIN_PIXELS = 64            # a site's icon smaller than this looks blurred on a card
MAX_PAGE_BYTES = 512 * 1024
MAX_CANDIDATES = 6


class _Page(Exception):
    """The URL is a web page, not an image: its icons may still be."""
    def __init__(self, html, url):
        super().__init__("a web page")
        self.html, self.url = html, url


def _fetch(url, accept, limit=MAX_ICON_BYTES, deadline=None):
    """GET a public URL, following checked redirects: (bytes, declared type, final URL)."""
    deadline = deadline or time.monotonic() + 15
    for redirect in range(6):
        parsed, addresses = _public_target(url)
        connection = _PinnedConnection(parsed, addresses, max(0.01, deadline - time.monotonic()))
        try:
            connection.request("GET", urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, "")), headers={
                "User-Agent": "Homestead icon cache", "Accept": accept})
            transport = connection.sock
            response = connection.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                location = response.headers.get("Location")
                if not location or redirect == 5:
                    raise ValueError("logo has too many or invalid redirects")
                url = urllib.parse.urljoin(url, location)
                continue
            if response.status != 200:
                raise ValueError("logo server did not return a successful response")
            declared = (response.headers.get_content_type() or "").lower()
            chunks = bytearray()
            while len(chunks) <= limit:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("logo download timed out")
                transport.settimeout(remaining)
                part = response.read1(min(16384, limit + 1 - len(chunks)))
                if not part:
                    break
                chunks.extend(part)
            return bytes(chunks), declared, url
        finally:
            connection.close()
    raise ValueError("logo has too many or invalid redirects")


def _looks_like_html(data, declared):
    head = data[:1024].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    return declared in ("text/html", "application/xhtml+xml") or head.startswith((b"<!doctype html", b"<html"))


def _download(url):
    """An image's bytes and type; _Page when the URL is a site rather than an image."""
    data, declared, final = _fetch(url, IMAGE_ACCEPT + ",text/html;q=0.8", MAX_PAGE_BYTES)
    if _looks_like_html(data, declared):
        raise _Page(data, final)
    if len(data) > MAX_ICON_BYTES:
        raise ValueError("logo is too large (maximum 256 KiB)")
    mime = _sniff_mime(data)
    if declared and declared not in MIME_EXTENSIONS and declared not in (
            "application/octet-stream", "text/xml", "application/xml", "text/plain"):
        raise ValueError("logo server did not return an image")
    return data, mime


class _IconLinks(html.parser.HTMLParser):
    """<link rel="icon"> and friends, and the manifest, from a page's head."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.icons, self.manifest, self.base = [], "", ""

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "base" and a.get("href") and not self.base:
            self.base = a["href"]
        if tag != "link" or not a.get("href"):
            return
        rel = set(a.get("rel", "").lower().split())
        if "manifest" in rel:
            self.manifest = self.manifest or a["href"]
        elif rel & {"icon", "apple-touch-icon", "apple-touch-icon-precomposed"} and "mask-icon" not in rel:
            self.icons.append({"href": a["href"], "sizes": a.get("sizes", ""), "type": a.get("type", "").lower(),
                               "apple": bool(rel & {"apple-touch-icon", "apple-touch-icon-precomposed"})})


def _declared_pixels(sizes, kind="", apple=False):
    """The largest size a page says an icon has; SVG and "any" count as large."""
    if "svg" in kind or "any" in sizes.lower().split():
        return 4096
    best = 0
    for part in sizes.lower().split():
        w, _, h = part.partition("x")
        if w.isdigit() and h.isdigit():
            best = max(best, min(int(w), int(h)))
    return best or (180 if apple else 0)


def _pixels(data, mime):
    """The image's own size, where it is cheap to read; None when unknown."""
    if mime == "image/svg+xml":
        return 4096
    if mime == "image/png" and len(data) >= 24:
        return min(int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big"))
    if mime == "image/gif" and len(data) >= 10:
        return min(int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little"))
    if mime == "image/x-icon" and len(data) >= 6:
        count, best = int.from_bytes(data[4:6], "little"), 0
        for i in range(min(count, 64)):
            entry = data[6 + i * 16: 22 + i * 16]
            if len(entry) < 16:
                break
            best = max(best, min(entry[0] or 256, entry[1] or 256))
        return best
    return None


def _candidates(page, url):
    """A page's icons, best first: SVG, then the largest it declares, then /favicon.ico."""
    links = _IconLinks()
    try:
        links.feed(page[:MAX_PAGE_BYTES].decode("utf-8", "replace"))
    except Exception:
        pass
    base = urllib.parse.urljoin(url, links.base) if links.base else url
    found = [{"url": urllib.parse.urljoin(base, i["href"]), "pixels": _declared_pixels(i["sizes"], i["type"], i["apple"])}
             for i in links.icons]
    if links.manifest:
        found.append({"manifest": urllib.parse.urljoin(base, links.manifest)})
    found.append({"url": urllib.parse.urljoin(url, "/favicon.ico"), "pixels": 0})
    return found


def _from_page(page, url):
    """The best icon a site offers, if it is good enough for a card."""
    deadline = time.monotonic() + 30
    found = _candidates(page, url)
    for row in [r for r in found if "manifest" in r]:
        try:
            raw, _, final = _fetch(row["manifest"], "application/manifest+json,application/json", 64 * 1024, deadline)
            for icon in (json.loads(raw.decode("utf-8", "replace")).get("icons") or [])[:20]:
                if isinstance(icon, dict) and icon.get("src") and "monochrome" not in str(icon.get("purpose", "")):
                    found.append({"url": urllib.parse.urljoin(final, str(icon["src"])),
                                  "pixels": _declared_pixels(str(icon.get("sizes", "")), str(icon.get("type", "")))})
        except (OSError, ValueError, AttributeError):
            pass
    seen, ranked = set(), []
    for row in sorted((r for r in found if "url" in r), key=lambda r: -r["pixels"]):
        if row["url"] not in seen and row["url"].startswith(("https://", "http://")):
            seen.add(row["url"])
            ranked.append(row)
    best, smallest = None, 0
    for row in ranked[:MAX_CANDIDATES]:
        if time.monotonic() > deadline:
            break
        try:
            data, declared, _ = _fetch(row["url"], IMAGE_ACCEPT, MAX_ICON_BYTES, deadline)
            if len(data) > MAX_ICON_BYTES or _looks_like_html(data, declared):
                continue
            mime = _sniff_mime(data)
        except (OSError, ValueError):
            continue
        size = _pixels(data, mime) or row["pixels"]
        if size >= MIN_PIXELS and (best is None or size > best[2]):
            best = (data, mime, size)
            if size >= 180:
                break                     # an SVG or a touch icon: nothing larger is worth asking for
        smallest = max(smallest, size or 0)
    if not best:
        raise ValueError(f"that site has no logo of at least {MIN_PIXELS} px"
                         + (f" (its largest is {smallest} px)" if smallest else "")
                         + "; paste the address of an image, or use Find to pick one from the app store")
    return best[0], best[1]


def persist(source, data_dir):
    """Cache source and return its stable same-origin URL.

    source is an image, or a site whose own logo is taken when it is good
    enough: an SVG, or a picture of at least MIN_PIXELS."""
    source = str(source or "").strip()
    if not source:
        return ""
    if source.startswith("/api/icons/"):
        resolve(source, data_dir)
        return source
    if len(source) > 2048:
        raise ValueError("logo URL is too long")
    try:
        data, mime = _download(source)
    except _Page as page:
        data, mime = _from_page(page.html, page.url)
    return store(data, data_dir, mime)


def store(data, data_dir, mime=""):
    """Cache image bytes already in hand, and return their same-origin URL.

    The name is the bytes' digest, so the same logo fetched by another
    Homestead - the source of a move - lands under the name it had there.
    """
    if len(data) > MAX_ICON_BYTES:
        raise ValueError("logo is too large (maximum 256 KiB)")
    mime = mime or _sniff_mime(data)
    if mime == "image/svg+xml":
        data = clean_svg(data)
    digest = hashlib.sha256(data).hexdigest()
    ext = MIME_EXTENSIONS[mime]
    icon_dir = os.path.join(data_dir, "icons")
    path = os.path.join(icon_dir, f"{digest}.{ext}")
    with SHARED.write_scope(path):
        os.makedirs(icon_dir, mode=0o750, exist_ok=True)
        if not os.path.exists(path):
            fd, temporary = tempfile.mkstemp(prefix=".icon-", dir=icon_dir)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
    return f"/api/icons/{digest}.{ext}"


def exists(reference, data_dir):
    """Whether a same-origin icon reference is in this Homestead's cache."""
    try:
        resolve(reference, data_dir)
        return True
    except FileNotFoundError:
        return False


def resolve(path, data_dir):
    """Return (absolute path, MIME) for an exact cached icon route."""
    name = (path or "").rsplit("/", 1)[-1]
    if not ICON_NAME.fullmatch(name):
        raise FileNotFoundError("invalid icon")
    ext = name.rsplit(".", 1)[1]
    mime = next(k for k, value in MIME_EXTENSIONS.items() if value == ext)
    target = os.path.abspath(os.path.join(data_dir, "icons", name))
    root = os.path.abspath(os.path.join(data_dir, "icons")) + os.sep
    if not target.startswith(root) or not os.path.isfile(target):
        raise FileNotFoundError("icon not found")
    return target, mime


_DATA_URL_CACHE = {}


def data_url(reference, data_dir):
    """Return a bounded in-API representation for a cached icon."""
    if not str(reference or "").startswith("/api/icons/"):
        return reference or ""
    path, mime = resolve(reference, data_dir)
    stat = os.stat(path)
    key = (path, stat.st_mtime_ns, stat.st_size)
    cached = _DATA_URL_CACHE.get(key)
    if cached:
        return cached
    if stat.st_size > MAX_ICON_BYTES:
        raise ValueError("cached logo exceeds the 256 KiB display limit")
    with open(path, "rb") as handle:
        encoded = base64.b64encode(handle.read()).decode("ascii")
    value = f"data:{mime};base64,{encoded}"
    _DATA_URL_CACHE.clear()
    _DATA_URL_CACHE[key] = value
    return value


# ---------------------------------------------------------------- too large
# An App Store logo can be a large picture: past MAX_ICON_BYTES it was refused,
# and the deploy with it. The browser draws it smaller instead - Python's
# standard library cannot - and the smaller copy is kept (logo-fit.js).
MAX_SOURCE_BYTES = 8 * 1024 * 1024
RASTER = ("image/png", "image/jpeg", "image/gif", "image/webp", "image/x-icon")
SMALLER = ("image/png", "image/webp", "image/jpeg")
DATA = None


def bind(data_dir):
    global DATA
    DATA = data_dir


def keep(url, data_dir):
    """The logo kept as it is ({"icon": reference}), or {"too_large": True}
    when it is a picture past the limit that the browser can draw smaller."""
    try:
        return {"icon": persist(url, data_dir)}
    except ValueError as error:
        if "too large" not in str(error) or str(url or "").startswith("/api/icons/"):
            raise
        return {"too_large": True}


def large_source(url):
    """A picture too large to keep, whole, for the browser to draw smaller:
    (bytes, type). Fetched as any logo is, from public addresses only."""
    url = str(url or "").strip()
    if not url.startswith(("http://", "https://")) or len(url) > 2048:
        raise ValueError("give the logo's http(s) address")
    data, _, _ = _fetch(url, IMAGE_ACCEPT, MAX_SOURCE_BYTES)
    if len(data) > MAX_SOURCE_BYTES:
        raise ValueError("logo is larger than 8 MiB, too large to make smaller")
    mime = _sniff_mime(data)
    if mime not in RASTER:
        raise ValueError("only a picture (PNG, JPEG, GIF, WebP or ICO) is made smaller")
    return data, mime


def keep_smaller(encoded, data_dir):
    """The browser's smaller copy, kept: its same-origin reference."""
    try:
        data = base64.b64decode(str(encoded or ""), validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("the smaller logo did not arrive whole") from None
    if not data or _sniff_mime(data) not in SMALLER:
        raise ValueError("the smaller logo must be a PNG, WebP or JPEG")
    return store(data, data_dir)


# Its routes and who may use them (homestead_routes.py). Not under
# /api/icons/, which is served without signing in.
ROUTES = {
    ("POST", "/api/logo-fit/keep"): ("operator", lambda request: keep(request.body.get("url"), DATA)),
    ("GET", "/api/logo-fit/source"): ("operator", lambda request: ROUTER.Raw(
        *large_source((request.query.get("url") or [""])[0]))),
    ("POST", "/api/logo-fit/resized"): ("operator", lambda request: {
        "icon": keep_smaller(request.body.get("data"), DATA)}),
}
