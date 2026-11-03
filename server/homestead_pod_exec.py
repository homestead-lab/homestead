"""One command in a pod, over the Kubernetes exec websocket.

Every command Homestead runs in a pod - a host script through the host
runner (homestead_hostrun), the file browser's helper, a snapshot copy's
reader - goes through here. The API server speaks v4.channel.k8s.io: each
frame starts with its channel (0 stdin, 1 stdout, 2 stderr, 3 how the run
ended, as a Status object), the client's frames masked.

run() has one deadline for the whole run, not for each wait: a command
that keeps printing is still stopped at it. It raises ExecTimeout, which
carries what the command had printed, rather than handing that back as if
the command had finished. Output is kept up to MAX_OUTPUT per stream and cut
there with CUT. The exit code, read from channel 3, comes back with the
output; a command that fails is not an error here, its caller decides.
"""
import base64
import json
import secrets
import socket
import time
import urllib.parse

from homestead_console import read_frame

API = None              # urllib.parse result for the API server
TOKEN = ""
CTX = None              # ssl context; wrap_socket(raw, server_hostname=)
MAX_OUTPUT = 4 * 1024 * 1024
CUT = b"\n[output cut at 4 MiB by Homestead]\n"
STDIN_PIECE = 16384
CONNECT_SECONDS = 15


def bind(api, token, ssl_context):
    global API, TOKEN, CTX
    API, TOKEN, CTX = api, token, ssl_context


class ExecRefused(ConnectionError):
    """Kubernetes would not open the exec: nothing ran in the pod. Right after
    a host comes back the API server can answer for a Running pod while it
    cannot reach that host's kubelet yet, so this one is worth trying again."""
    retry = True


class ExecUnreached(ExecRefused):
    """The exec request never got to the API server - the connection or its
    TLS handshake timed out first - so the command never started."""


class ExecTimeout(TimeoutError):
    """The command did not finish by its deadline. Homestead stopped waiting;
    stdout and stderr hold what it had printed by then."""

    def __init__(self, message, stdout=b"", stderr="", timeout=None):
        super().__init__(message)
        self.stdout, self.stderr, self.timeout = stdout, stderr, timeout


class Output(tuple):
    """(stdout bytes, stderr text), as before, with the run's exit_code (None
    when the run ended without saying) and cut (output went past MAX_OUTPUT)."""

    def __new__(cls, stdout, stderr, exit_code=None, cut=False):
        self = super().__new__(cls, (stdout, stderr))
        self.stdout, self.stderr, self.exit_code, self.cut = stdout, stderr, exit_code, cut
        return self


def frame(payload, opcode=0x2):
    """One masked frame, as a client sends it; masked all at once, since byte
    by byte is far too slow for a file."""
    key = secrets.token_bytes(4)
    length = len(payload)
    head = bytearray([0x80 | opcode])
    if length < 126:
        head.append(0x80 | length)
    elif length <= 0xFFFF:
        head.append(0x80 | 126)
        head.extend(length.to_bytes(2, "big"))
    else:
        head.append(0x80 | 127)
        head.extend(length.to_bytes(8, "big"))
    if not payload:
        return bytes(head) + key
    repeated = (key * (length // 4 + 1))[:length]
    masked = (int.from_bytes(payload, "big") ^ int.from_bytes(repeated, "big")).to_bytes(length, "big")
    return bytes(head) + key + masked


def _refused(sock, pod, status):
    """The API server's answer to an exec it would not open, for the error:
    its status line and, from the body, its own message - "error dialing
    backend", say, while a host just back has no tunnel to it yet."""
    detail = ""
    try:
        sock.settimeout(2)
        body = sock.recv(2048).decode("utf-8", "replace")
        start = body.find("{")
        if start >= 0:
            try:
                detail = json.loads(body[start:body.rfind("}") + 1]).get("message", "")
            except ValueError:
                detail = ""
        detail = detail or body.strip().splitlines()[-1] if body.strip() else detail
    except OSError:
        pass
    finally:
        sock.close()
    said = f"{status or 'no answer'}" + (f": {detail[:200]}" if detail else "")
    return ExecRefused(f"Kubernetes refused to run a command in {pod} ({said})")


def _late(pod, timeout, stdout=b"", stderr=""):
    return ExecTimeout(f"The command in {pod} did not finish within {timeout:g} seconds, so Homestead stopped "
                       "waiting for it; what it did before then is not known, and it may still be running there",
                       stdout, stderr, timeout)


def connect(namespace, pod, argv, stdin=False, container="files", deadline=None, timeout=None):
    """An exec websocket, open and upgraded: the socket, for the caller to
    stream over. Raises ExecRefused when Kubernetes would not open it, and
    ExecTimeout when the upgrade does not come by deadline (a monotonic
    time; without one, CONNECT_SECONDS for each wait). A timeout before the
    request is sent is ExecUnreached: the command never started."""
    def wait():
        if deadline is None:
            return CONNECT_SECONDS
        left = deadline - time.monotonic()
        if left <= 0:
            raise socket.timeout("deadline")
        return min(CONNECT_SECONDS, left)

    query = [("container", container), ("stdout", "true"), ("stderr", "true")]
    if stdin:
        query.append(("stdin", "true"))
    query.extend(("command", part) for part in argv)
    path = (f"/api/v1/namespaces/{urllib.parse.quote(namespace)}/pods/"
            f"{urllib.parse.quote(pod)}/exec?{urllib.parse.urlencode(query)}")
    raw = sock = None
    sent = False
    try:
        raw = socket.create_connection((API.hostname, API.port or 443), timeout=wait())
        sock = CTX.wrap_socket(raw, server_hostname=API.hostname)
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        sock.settimeout(wait())
        sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {API.netloc}\r\n"
                      f"Authorization: Bearer {TOKEN}\r\nUpgrade: websocket\r\n"
                      "Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\n"
                      f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Protocol: v4.channel.k8s.io\r\n\r\n").encode())
        sent = True
        header = bytearray()
        while not header.endswith(b"\r\n\r\n") and len(header) < 65536:
            sock.settimeout(wait())
            chunk = sock.recv(1)
            if not chunk:
                break
            header.extend(chunk)
    except BaseException as error:
        for opened in (sock, raw):
            if opened is not None:
                opened.close()
        if isinstance(error, socket.timeout):
            if not sent:
                raise ExecUnreached(f"Homestead could not reach {pod} through the API server ({error or 'timed out'}); "
                                    "the command never started") from error
            raise _late(pod, timeout if timeout is not None else CONNECT_SECONDS) from error
        raise
    status = header.split(b"\r\n", 1)[0].decode("latin-1", "replace").strip()
    if " 101 " not in f"{status} ":
        raise _refused(sock, pod, status)
    return sock


def close(sock):
    try:
        sock.sendall(frame(b"", opcode=0x8))
    except Exception:
        pass
    sock.close()


def exit_code(payload):
    """The exit code in a channel-3 Status: 0 for Success, the ExitCode cause
    of a NonZeroExitCode failure, None for anything else."""
    try:
        value = json.loads(payload.decode("utf-8", "replace"))
    except ValueError:
        return None
    if not isinstance(value, dict):
        return None
    if value.get("status") == "Success":
        return 0
    for cause in ((value.get("details") or {}).get("causes") or []):
        if isinstance(cause, dict) and cause.get("reason") == "ExitCode":
            try:
                return int(cause.get("message"))
            except (TypeError, ValueError):
                return None
    return None


class _Kept:
    """One stream, kept up to a cap."""

    def __init__(self, cap):
        self.data, self.cap, self.cut = bytearray(), cap, False

    def add(self, data):
        room = self.cap - len(self.data) if self.cap is not None else len(data)
        if room >= len(data):
            self.data.extend(data)
        elif not self.cut:
            self.data.extend(data[:max(0, room)])
            self.data.extend(CUT)
            self.cut = True


def run(namespace, pod, argv, stdin=b"", timeout=30, container="files", cap=MAX_OUTPUT):
    """Run one command in a pod and read all of its output: Output, or
    ExecTimeout once timeout seconds have passed in all."""
    deadline = time.monotonic() + timeout
    sock = connect(namespace, pod, argv, stdin=bool(stdin), container=container, deadline=deadline, timeout=timeout)
    out, err, code = _Kept(cap), _Kept(cap), None

    def remaining():
        left = deadline - time.monotonic()
        if left <= 0:
            raise socket.timeout("deadline")
        return left

    try:
        try:
            if stdin:
                for index in range(0, len(stdin), STDIN_PIECE):
                    sock.settimeout(remaining())
                    sock.sendall(frame(b"\x00" + stdin[index:index + STDIN_PIECE]))
            while True:
                sock.settimeout(remaining())
                try:
                    _, opcode, payload = read_frame(sock, require_mask=False)
                except socket.timeout:
                    raise
                except (EOFError, OSError, ValueError):
                    break
                if opcode == 0x8:
                    break
                if not payload:
                    continue
                channel, data = payload[0], payload[1:]
                if channel == 1:
                    out.add(data)
                elif channel == 2:
                    err.add(data)
                elif channel == 3:
                    code = exit_code(data)
                    if not stdin:
                        break
        except socket.timeout as error:
            if code is None:
                raise _late(pod, timeout, bytes(out.data), bytes(err.data).decode("utf-8", "replace").strip()) from error
            # It said how it ended; only the close did not come (v4 with stdin).
    finally:
        close(sock)
    return Output(bytes(out.data), bytes(err.data).decode("utf-8", "replace").strip(), code, out.cut or err.cut)
