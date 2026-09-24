"""A VM's screen and serial port, in the browser.

KubeVirt serves both as WebSocket subresources of a running VM instance:
`vnc` carries the RFB protocol a VNC client speaks, `console` the bytes of the
first serial port. Homestead stands in the middle, as it does for container
consoles, so the browser never holds the service-account token:

* screen - frames pass through untouched; noVNC in the page speaks RFB;
* serial - bytes become the same small JSON messages the container console
  uses, so one terminal view serves both.

Operator only, and each session's start and end are written to the console
audit log, like container consoles. Keystrokes and output are not recorded.
"""
import json
import secrets
import socket
import threading
import urllib.parse

import homestead_console as WS

KINDS = {"vnc": "vnc", "serial": "console"}
MAX_FRAME = 16 * 1024 * 1024


def validate(namespace, name, kind, system_namespaces):
    for label, value in (("namespace", namespace), ("VM", name)):
        if not value or not WS.IDENT.fullmatch(value):
            raise ValueError(f"invalid {label}")
    if namespace in system_namespaces:
        raise PermissionError("consoles of system namespaces are disabled")
    if kind not in KINDS:
        raise ValueError("the console is vnc or serial")


def subresource_path(namespace, name, kind):
    return (f"/apis/subresources.kubevirt.io/v1/namespaces/{urllib.parse.quote(namespace)}"
            f"/virtualmachineinstances/{urllib.parse.quote(name)}/{KINDS[kind]}")


def read_message(stream, require_mask):
    """One whole WebSocket message: continuation frames joined, control frames
    returned on their own."""
    final, opcode, payload = WS.read_frame(stream, require_mask=require_mask, max_size=MAX_FRAME)
    if opcode >= 8:
        return opcode, payload
    data = bytearray(payload)
    while not final:
        last, more, part = WS.read_frame(stream, require_mask=require_mask, max_size=MAX_FRAME)
        if more >= 8:
            # A ping may come between the pieces of a message; it is not one
            # of them. (Answering it is left to the next read of the stream.)
            continue
        final = last
        data.extend(part)
        if len(data) > MAX_FRAME:
            raise ValueError("websocket message is too large")
    return opcode, bytes(data)


def serial_input(payload):
    value = json.loads(payload.decode("utf-8"))
    if value.get("type") == "input":
        data = str(value.get("data", "")).encode("utf-8")
        if len(data) > 65536:
            raise ValueError("console input is too large")
        return data
    return None


class VmConsole:
    def __init__(self, proxy, system_namespaces, kget):
        self.proxy = proxy
        self.system_namespaces = system_namespaces
        self.kget = kget

    def handle(self, handler, user, query):
        get = lambda key, default="": (query.get(key) or [default])[0]
        namespace, name, kind = get("ns"), get("vm"), get("kind", "vnc")
        validate(namespace, name, kind, self.system_namespaces)
        vmi = self.kget(f"/apis/kubevirt.io/v1/namespaces/{namespace}/virtualmachineinstances/{name}")
        if (vmi.get("status") or {}).get("phase") != "Running":
            raise ValueError(f"{name} is not running, so it has no screen to show")

        if (handler.headers.get("Upgrade") or "").lower() != "websocket":
            return handler._send(426, {"error": "websocket upgrade required"})
        key = handler.headers.get("Sec-WebSocket-Key") or ""
        if not key:
            return handler._send(400, {"error": "missing websocket key"})
        origin = handler.headers.get("Origin") or ""
        host = handler.headers.get("X-Forwarded-Host") or handler.headers.get("Host") or ""
        if not origin or urllib.parse.urlparse(origin).netloc.lower() != host.lower():
            return handler._send(403, {"error": "console websocket origin rejected"})
        offered = [p.strip() for p in (handler.headers.get("Sec-WebSocket-Protocol") or "").split(",") if p.strip()]

        upstream = self.proxy.connect_upstream(subresource_path(namespace, name, kind), protocol="plain.kubevirt.io")
        handler.send_response(101, "Switching Protocols")
        handler.send_header("Upgrade", "websocket")
        handler.send_header("Connection", "Upgrade")
        handler.send_header("Sec-WebSocket-Accept", WS.accept_value(key))
        if "binary" in offered:
            # noVNC asks for this; a browser refuses a protocol it did not offer.
            handler.send_header("Sec-WebSocket-Protocol", "binary")
        handler.end_headers()
        handler.close_connection = True

        session = secrets.token_hex(8)
        base = {"session": session, "user": user, "namespace": namespace, "vm": name, "console": kind}
        WS.audit(self.proxy.data_dir, {**base, "event": "start"})
        stopped = threading.Event()
        browser_lock, upstream_lock = threading.Lock(), threading.Lock()
        reason = "closed"
        serial = kind == "serial"

        def to_browser(data, opcode):
            with browser_lock:
                handler.connection.sendall(WS.encode_frame(data, opcode=opcode))

        def to_upstream(data, opcode=2):
            with upstream_lock:
                upstream.sendall(WS.encode_frame(data, opcode=opcode, masked=True))

        def browser_reader():
            nonlocal reason
            try:
                while not stopped.is_set():
                    opcode, payload = read_message(handler.rfile, require_mask=True)
                    if opcode == 8:
                        reason = "browser closed"
                        break
                    if opcode == 9:
                        to_browser(payload, 10)
                        continue
                    if opcode not in (1, 2):
                        continue
                    if serial:
                        try:
                            data = serial_input(payload)
                        except (ValueError, json.JSONDecodeError):
                            continue
                        if data:
                            to_upstream(data)
                    else:
                        to_upstream(payload, opcode)
            except (EOFError, OSError, ValueError):
                reason = "browser disconnected"
            finally:
                stopped.set()
                try:
                    upstream.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

        threading.Thread(target=browser_reader, daemon=True).start()
        try:
            if serial:
                to_browser(json.dumps({"type": "connected", "session": session}), 1)
            while not stopped.is_set():
                opcode, payload = read_message(upstream, require_mask=False)
                if opcode == 8:
                    reason = "the VM console closed"
                    break
                if opcode == 9:
                    to_upstream(payload, 10)
                    continue
                if opcode not in (1, 2):
                    continue
                if serial:
                    to_browser(json.dumps({"type": "output", "stream": "stdout",
                                           "data": payload.decode("utf-8", "replace")}), 1)
                else:
                    to_browser(payload, 2)
        except (EOFError, OSError, ValueError) as error:
            reason = str(error)[:160] or "upstream disconnected"
        finally:
            stopped.set()
            try:
                upstream.close()
            except OSError:
                pass
            try:
                to_browser(b"", 8)
            except OSError:
                pass
            WS.audit(self.proxy.data_dir, {**base, "event": "stop", "reason": reason})
