"""Authenticated, bounded HTTP wrapper for local read-only PodResources.

One request at a time bounds allocation work and threads. A slow client has a
two-second socket timeout; the collector itself has a three-second deadline.
No Kubernetes token, host credentials, checkpoint or arbitrary RPC access.
"""
import json
import os
from pathlib import Path
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import homestead_allocation_auth as AUTH
import allocation

NODE = os.environ.get("NODE_NAME", "")
POD_UID = os.environ.get("POD_UID", "")
KEY_PATH = "/allocation-auth/key"
BOOT_PATH = "/host/boot-id"


def read_key():
    with open(KEY_PATH, encoding="ascii") as handle:
        value = handle.read(65)
    AUTH.key_bytes(value)
    return value


def boot_id():
    with open(BOOT_PATH, encoding="ascii") as handle:
        value = handle.read(64).strip()
    if not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", value):
        raise ValueError("Host boot identity unavailable")
    return value


class Handler(BaseHTTPRequestHandler):
    # Close every connection; no pipelining or unbounded keep-alive clients.
    protocol_version = "HTTP/1.0"
    def log_message(self, *args):
        pass

    def send_json(self, code, value, *, key=None, request=None):
        raw = AUTH.encode(value)
        if len(raw) > AUTH.MAX_BODY:
            code, raw = 503, b'{"complete":false,"reason":"Allocation response exceeds its size limit"}'
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        if key is not None and request is not None:
            self.send_header("X-Homestead-Allocation", AUTH.signature(key, raw, reply_to=request))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path != "/healthz":
            return self.send_json(404, {"error": "Not found"})
        # Process health is deliberately not a claim that allocation is usable.
        return self.send_json(200, {"ok": True})

    def do_POST(self):
        if self.path != "/snapshot":
            return self.send_json(404, {"error": "Not found"})
        try:
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) != 1 or self.headers.get("Transfer-Encoding") or not re.fullmatch(r"[1-9][0-9]{0,3}", lengths[0]):
                raise ValueError()
            length = int(lengths[0])
            if length > 1024:
                raise ValueError()
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError()
            key = read_key()
            if not AUTH.verify(key, raw, self.headers.get("X-Homestead-Allocation")):
                raise ValueError()
            value = json.loads(raw)
            if not AUTH.validate(value, NODE, POD_UID):
                raise ValueError()
            now = time.time()
            self.server.nonces = {nonce: at for nonce, at in self.server.nonces.items() if now - at <= 60}
            if value["nonce"] in self.server.nonces or len(self.server.nonces) >= 256:
                raise ValueError()
            self.server.nonces[value["nonce"]] = now
        except Exception:
            return self.send_json(403, {"error": "Allocation request refused"})
        try:
            before = boot_id()
            result = allocation.snapshot()
            if boot_id() != before:
                raise ValueError()
            result.update(node=NODE, pod_uid=POD_UID, boot_id=before)
        except Exception:
            result = {"schema": 1, "complete": False, "reason": "Host allocation identity is unavailable"}
        return self.send_json(200, result, key=key, request=raw)


class Server(HTTPServer):
    request_queue_size = 4
    def __init__(self, address):
        self.nonces = {}
        super().__init__(address, Handler)

    def get_request(self):
        sock, address = super().get_request()
        sock.settimeout(2)
        return sock, address

    def handle_error(self, request, address):
        # Never print signed request bytes, private responses or socket paths.
        pass


if __name__ == "__main__":
    Server(("0.0.0.0", 9101)).serve_forever()
