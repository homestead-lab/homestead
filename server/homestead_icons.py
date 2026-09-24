"""Safe, persistent cache for workload icons.

Remote icon URLs are operator input, so fetching them is an SSRF boundary. We
only resolve public HTTP(S) hosts, re-check redirects, cap the response size,
and accept a small set of raster formats. Files are content-addressed beneath
Homestead's Longhorn-backed DATA_DIR so rollouts do not depend on the source URL.
"""
import hashlib
import ipaddress
import base64
import os
import re
import socket
import tempfile
import urllib.parse
import urllib.request


MAX_ICON_BYTES = 256 * 1024
MIME_EXTENSIONS = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/x-icon": "ico",
    "image/vnd.microsoft.icon": "ico",
}


def _validate_public_url(url):
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError as exc:
        raise ValueError("logo URL is invalid") from exc
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("logo must use a public http:// or https:// URL")
    if parsed.username or parsed.password:
        raise ValueError("logo URL must not contain credentials")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(
            parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80),
            type=socket.SOCK_STREAM)}
    except OSError as exc:
        raise ValueError("logo host could not be resolved") from exc
    if not addresses:
        raise ValueError("logo host could not be resolved")
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
        if not ip.is_global:
            raise ValueError("logo host must resolve only to public addresses")
    return url


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
    raise ValueError("logo response is not a supported PNG, JPEG, GIF, WebP, or ICO image")


def _download(url):
    _validate_public_url(url)
    opener = urllib.request.build_opener(_SafeRedirect())
    req = urllib.request.Request(url, headers={
        "User-Agent": "Homestead icon cache",
        "Accept": "image/png,image/jpeg,image/gif,image/webp,image/x-icon",
    })
    with opener.open(req, timeout=15) as response:
        _validate_public_url(response.geturl())
        declared = (response.headers.get_content_type() or "").lower()
        data = response.read(MAX_ICON_BYTES + 1)
    if len(data) > MAX_ICON_BYTES:
        raise ValueError("logo is too large (maximum 256 KiB)")
    mime = _sniff_mime(data)
    if declared and declared not in MIME_EXTENSIONS and declared != "application/octet-stream":
        raise ValueError("logo server did not return an image")
    return data, mime


def persist(source, data_dir):
    """Cache source and return its stable same-origin URL."""
    source = str(source or "").strip()
    if not source:
        return ""
    if source.startswith("/api/icons/"):
        resolve(source, data_dir)
        return source
    if len(source) > 2048:
        raise ValueError("logo URL is too long")
    data, mime = _download(source)
    return store(data, data_dir, mime)


def store(data, data_dir, mime=""):
    """Cache image bytes already in hand, and return their same-origin URL.

    The name is the bytes' digest, so the same logo fetched by another
    Homestead - the source of a move - lands under the name it had there.
    """
    if len(data) > MAX_ICON_BYTES:
        raise ValueError("logo is too large (maximum 256 KiB)")
    mime = mime or _sniff_mime(data)
    digest = hashlib.sha256(data).hexdigest()
    ext = MIME_EXTENSIONS[mime]
    icon_dir = os.path.join(data_dir, "icons")
    os.makedirs(icon_dir, mode=0o750, exist_ok=True)
    path = os.path.join(icon_dir, f"{digest}.{ext}")
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
    if not re.fullmatch(r"[0-9a-f]{64}\.(png|jpg|gif|webp|ico)", name):
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
