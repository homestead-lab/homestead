"""Real compressed SSH into an isolated HTTP upload, inside source-copy.Dockerfile."""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import subprocess
import threading

import homestead_imports as imports
import homestead_unraid_vms as vms


def exercise(src, env, root):
    disk = root / "VM disk with spaces.img"
    # Mostly empty space, with nonzero bytes in the beginning and end.
    with disk.open("wb") as handle:
        handle.write(os.urandom(4096))
        handle.seek(4 * 1024 * 1024)
        handle.write(os.urandom(4096))
    expected = hashlib.sha256(disk.read_bytes()).hexdigest()
    Path("/usr/local/bin/virsh").write_text("#!/bin/sh\nprintf 'shut off\\n'\n")
    Path("/usr/local/bin/virsh").chmod(0o755)

    class Upload(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        code, digest = 201, ""

        def log_message(self, *args):
            pass

        def do_POST(self):
            assert self.headers.get("Authorization") == "Bearer fixture-token"
            content = bytearray()
            assert self.headers.get("Transfer-Encoding") == "chunked"
            while True:
                size = int(self.rfile.readline().split(b";", 1)[0].strip(), 16)
                if size == 0:
                    self.rfile.readline()
                    break
                content.extend(self.rfile.read(size))
                assert self.rfile.read(2) == b"\r\n"
            Upload.digest = hashlib.sha256(content).hexdigest()
            body = b"" if Upload.code == 201 else b"No space left on device\n"
            self.send_response(Upload.code)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Upload)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    original = vms.IMP
    vms.IMP = imports
    try:
        script = vms.copy_script(src, "Fixture VM", str(disk), disk.stat().st_size)
        # Fixture image already has these packages; do not contact a registry.
        script = "\n".join(line for line in script.splitlines() if not line.startswith("apk add "))
        # HTTP is loopback-only in the network-isolated fixture. Production keeps TLS verification.
        script = script.replace("--cacert /ca/ca.crt ", "")
        local = {**env, "TOKEN": "fixture-token", "UPLOAD_URL": f"http://127.0.0.1:{server.server_port}/upload"}
        for code in (201, 413):
            Upload.code = code
            result = subprocess.run(["sh", "-c", script], env=local, capture_output=True, text=True, timeout=30)
            assert Upload.digest == expected, "compressed transfer changed the disk bytes"
            if code == 201:
                assert result.returncode == 0 and "HSVM-DONE" in result.stdout
                assert vms.progress(result.stderr, disk.stat().st_size) == 100, "elapsed/byte meter output must reach 100%"
            else:
                assert result.returncode != 0 and "HSVM-DONE" not in result.stdout
                assert "HSVM-FAILED" in result.stdout
                error = vms.failure_reason(result.stdout + result.stderr + "\n0\n")
                assert "413" in error or "No space left" in error, error
        print("PASS: compressed VM stream keeps exact bytes; rejected upload cannot succeed or report a bare percentage", flush=True)
    finally:
        vms.IMP = original
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
