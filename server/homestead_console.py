"""Interactive Kubernetes exec WebSocket proxy for Homestead.

The browser never receives the service-account token.  Homestead terminates the
browser WebSocket, opens a second v4.channel.k8s.io WebSocket to the apiserver,
and translates small JSON messages to Kubernetes channel frames.
"""
import base64
import hashlib
import json
import os
import secrets
import socket
import struct
import threading
import time
import urllib.parse


GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
IDENT = __import__("re").compile(r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$")
SHELLS = frozenset({"/bin/sh", "/bin/bash", "/bin/ash"})
MAX_FRAME = 1024 * 1024


def accept_value(key):
    return base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()


def encode_frame(payload, opcode=2, masked=False):
    if isinstance(payload, str):
        payload = payload.encode()
    head = bytearray([0x80 | opcode])
    length = len(payload)
    maskbit = 0x80 if masked else 0
    if length < 126:
        head.append(maskbit | length)
    elif length <= 0xFFFF:
        head.append(maskbit | 126)
        head.extend(struct.pack("!H", length))
    else:
        head.append(maskbit | 127)
        head.extend(struct.pack("!Q", length))
    if masked:
        key = secrets.token_bytes(4)
        head.extend(key)
        payload = bytes(value ^ key[i % 4] for i, value in enumerate(payload))
    return bytes(head) + payload


def _read_exact(stream, length):
    chunks = bytearray()
    while len(chunks) < length:
        part = stream.read(length - len(chunks)) if hasattr(stream, "read") else stream.recv(length - len(chunks))
        if not part:
            raise EOFError("websocket closed")
        chunks.extend(part)
    return bytes(chunks)


def read_frame(stream, require_mask=None, max_size=None):
    first, second = _read_exact(stream, 2)
    final, opcode = bool(first & 0x80), first & 0x0F
    masked, length = bool(second & 0x80), second & 0x7F
    if require_mask is not None and masked != require_mask:
        raise ValueError("invalid websocket masking")
    if length == 126:
        length = struct.unpack("!H", _read_exact(stream, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", _read_exact(stream, 8))[0]
    if length > (max_size or MAX_FRAME):
        raise ValueError("websocket frame is too large")
    mask = _read_exact(stream, 4) if masked else b""
    payload = _read_exact(stream, length)
    if masked:
        payload = bytes(value ^ mask[i % 4] for i, value in enumerate(payload))
    return final, opcode, payload


def validate_target(namespace, pod, container, shell, system_namespaces, allowed_namespaces=None):
    for label, value in (("namespace", namespace), ("pod", pod), ("container", container)):
        if not value or not IDENT.fullmatch(value):
            raise ValueError(f"invalid {label}")
    if namespace in system_namespaces:
        raise PermissionError("console access to system namespaces is disabled")
    if allowed_namespaces is not None and namespace not in allowed_namespaces:
        raise PermissionError("console access is not enabled for this namespace")
    if shell not in SHELLS:
        raise ValueError("unsupported shell")


def exec_path(namespace, pod, container, shell):
    query = urllib.parse.urlencode([
        ("container", container), ("command", shell), ("stdin", "true"),
        ("stdout", "true"), ("stderr", "true"), ("tty", "true"),
    ])
    return f"/api/v1/namespaces/{urllib.parse.quote(namespace)}/pods/{urllib.parse.quote(pod)}/exec?{query}"


def browser_message(payload):
    value = json.loads(payload.decode("utf-8"))
    kind = value.get("type")
    if kind == "input":
        data = str(value.get("data", "")).encode("utf-8")
        if len(data) > 65536:
            raise ValueError("console input is too large")
        return b"\x00" + data
    if kind == "resize":
        size = {"Width": max(20, min(500, int(value.get("cols", 80)))),
                "Height": max(5, min(200, int(value.get("rows", 24))))}
        return b"\x04" + json.dumps(size, separators=(",", ":")).encode()
    if kind == "ping":
        return None
    raise ValueError("unknown console message")


def kubernetes_message(payload):
    if not payload:
        return None
    channel, data = payload[0], payload[1:].decode("utf-8", "replace")
    if channel == 1:
        return {"type": "output", "stream": "stdout", "data": data}
    if channel == 2:
        return {"type": "output", "stream": "stderr", "data": data}
    if channel == 3:
        return {"type": "error", "data": data}
    return None


def audit(data_dir, event):
    record = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **event}
    path = os.path.join(data_dir, "console-audit.jsonl")
    os.makedirs(data_dir, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, separators=(",", ":")) + "\n")


class ConsoleProxy:
    def __init__(self, api, token, ssl_context, data_dir, system_namespaces, allowed_namespaces, kget):
        self.api = urllib.parse.urlparse(api)
        self.token = token
        self.ssl_context = ssl_context
        self.data_dir = data_dir
        self.system_namespaces = system_namespaces
        self.allowed_namespaces = frozenset(allowed_namespaces)
        self.kget = kget

    def validate_pod(self, namespace, pod, container):
        obj = self.kget(f"/api/v1/namespaces/{namespace}/pods/{pod}")
        names = {x.get("name") for x in (obj.get("spec", {}).get("containers", []) or [])}
        if container not in names:
            raise ValueError("container does not exist in this pod")
        if obj.get("status", {}).get("phase") != "Running":
            raise ValueError("the pod is not running")

    def connect_upstream(self, path, protocol="v4.channel.k8s.io"):
        port = self.api.port or 443
        raw = socket.create_connection((self.api.hostname, port), timeout=15)
        sock = self.ssl_context.wrap_socket(raw, server_hostname=self.api.hostname)
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        request = (f"GET {path} HTTP/1.1\r\nHost: {self.api.netloc}\r\n"
                   f"Authorization: Bearer {self.token}\r\nUpgrade: websocket\r\n"
                   "Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\n"
                   f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Protocol: {protocol}\r\n\r\n")
        sock.sendall(request.encode())
        response = bytearray()
        while not response.endswith(b"\r\n\r\n") and len(response) < 65536:
            response.extend(sock.recv(1))
        status = response.split(b"\r\n", 1)[0]
        if b" 101 " not in status:
            sock.close()
            raise ConnectionError(status.decode("utf-8", "replace"))
        sock.settimeout(None)
        return sock

    def handle(self, handler, user, query):
        get = lambda key, default="": (query.get(key) or [default])[0]
        namespace, pod, container = get("ns"), get("pod"), get("container")
        shell = get("shell", "/bin/sh")
        validate_target(namespace, pod, container, shell, self.system_namespaces, self.allowed_namespaces)
        self.validate_pod(namespace, pod, container)

        if (handler.headers.get("Upgrade") or "").lower() != "websocket":
            return handler._send(426, {"error": "websocket upgrade required"})
        key = handler.headers.get("Sec-WebSocket-Key") or ""
        if not key:
            return handler._send(400, {"error": "missing websocket key"})
        origin = handler.headers.get("Origin") or ""
        host = handler.headers.get("X-Forwarded-Host") or handler.headers.get("Host") or ""
        if not origin or urllib.parse.urlparse(origin).netloc.lower() != host.lower():
            return handler._send(403, {"error": "console websocket origin rejected"})

        upstream = self.connect_upstream(exec_path(namespace, pod, container, shell))
        handler.send_response(101, "Switching Protocols")
        handler.send_header("Upgrade", "websocket")
        handler.send_header("Connection", "Upgrade")
        handler.send_header("Sec-WebSocket-Accept", accept_value(key))
        handler.end_headers()
        handler.close_connection = True

        session = secrets.token_hex(8)
        base = {"session": session, "user": user, "namespace": namespace,
                "pod": pod, "container": container, "shell": shell}
        audit(self.data_dir, {**base, "event": "start"})
        stopped = threading.Event()
        browser_lock = threading.Lock()
        upstream_lock = threading.Lock()
        reason = "closed"

        def send_browser(value, opcode=1):
            data = value if isinstance(value, (bytes, bytearray)) else json.dumps(value, separators=(",", ":"))
            with browser_lock:
                handler.connection.sendall(encode_frame(data, opcode=opcode))

        def browser_reader():
            nonlocal reason
            try:
                while not stopped.is_set():
                    _, opcode, payload = read_frame(handler.rfile, require_mask=True)
                    if opcode == 8:
                        reason = "browser closed"
                        break
                    if opcode == 9:
                        send_browser(payload, opcode=10)
                        continue
                    if opcode not in (1, 2):
                        continue
                    try:
                        channel = browser_message(payload)
                        if channel is not None:
                            with upstream_lock:
                                upstream.sendall(encode_frame(channel, opcode=2, masked=True))
                        else:
                            send_browser({"type": "pong"})
                    except (ValueError, json.JSONDecodeError) as error:
                        send_browser({"type": "error", "data": str(error)})
            except (EOFError, OSError, ValueError):
                reason = "browser disconnected"
            finally:
                stopped.set()
                try:
                    upstream.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

        thread = threading.Thread(target=browser_reader, daemon=True)
        thread.start()
        try:
            send_browser({"type": "connected", "session": session, "shell": shell})
            while not stopped.is_set():
                _, opcode, payload = read_frame(upstream, require_mask=False)
                if opcode == 8:
                    reason = "exec completed"
                    break
                if opcode == 9:
                    with upstream_lock:
                        upstream.sendall(encode_frame(payload, opcode=10, masked=True))
                    continue
                if opcode not in (1, 2):
                    continue
                value = kubernetes_message(payload)
                if value:
                    send_browser(value)
        except (EOFError, OSError, ValueError) as error:
            reason = str(error)[:160] or "upstream disconnected"
            try:
                send_browser({"type": "disconnected", "reason": reason})
            except OSError:
                pass
        finally:
            stopped.set()
            try:
                upstream.close()
            except OSError:
                pass
            try:
                send_browser(b"", opcode=8)
            except OSError:
                pass
            audit(self.data_dir, {**base, "event": "stop", "reason": reason})
