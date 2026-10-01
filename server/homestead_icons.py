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
import ssl
import http.client
import time
import tempfile
import urllib.parse
import urllib.request
import homestead_shared as SHARED


MAX_ICON_BYTES = 256 * 1024
MIME_EXTENSIONS = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/x-icon": "ico",
    "image/vnd.microsoft.icon": "ico",
}


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
    raise ValueError("logo response is not a supported PNG, JPEG, GIF, WebP, or ICO image")


def _download(url):
    deadline = time.monotonic() + 15
    for redirect in range(6):
        parsed, addresses = _public_target(url)
        connection = _PinnedConnection(parsed, addresses, max(0.01, deadline - time.monotonic()))
        try:
            connection.request("GET", urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, "")), headers={
                "User-Agent": "Homestead icon cache", "Accept": "image/png,image/jpeg,image/gif,image/webp,image/x-icon"})
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
            while len(chunks) <= MAX_ICON_BYTES:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("logo download timed out")
                transport.settimeout(remaining)
                part = response.read1(min(16384, MAX_ICON_BYTES + 1 - len(chunks)))
                if not part:
                    break
                chunks.extend(part)
            data = bytes(chunks)
            break
        finally:
            connection.close()
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
