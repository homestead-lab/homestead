"""Browse and edit the files on a volume, without a shell on a node.

A PersistentVolumeClaim's contents are only reachable from a pod that mounts
it, so this runs a short-lived helper pod and talks to it with the same
exec WebSocket the console uses.  Every operation is a one-shot command whose
output is read back in full, rather than an interactive session.

Writes are deliberately two steps: the content lands in a temporary file, its
length is checked against what was sent, and only then does it replace the
original - a half-delivered stream can never truncate a config file.
"""
import base64
import json
import os
import posixpath
import re
import secrets
import socket
import time
import urllib.parse

from harvui_console import encode_frame, read_frame

kget = ksend = None
API = None
TOKEN = ""
CTX = None
SYSTEM_NAMESPACES = set()

POD_PREFIX = "homestead-files-"
MOUNT = "/data"
IMAGE = os.environ.get("FILES_IMAGE", "alpine:3.20")
# A helper pod is cheap but it holds a ReadWriteOnce claim, so it gives itself
# a deadline rather than relying on anyone remembering to close the browser.
SESSION_SECONDS = int(os.environ.get("FILES_SESSION_SECONDS", "1800"))
MAX_EDIT_BYTES = 1024 * 1024
MAX_LIST = 500


def bind(_kget, _ksend, api, token, ssl_context, system_namespaces):
    global kget, ksend, API, TOKEN, CTX, SYSTEM_NAMESPACES
    kget, ksend, API, TOKEN, CTX = _kget, _ksend, api, token, ssl_context
    SYSTEM_NAMESPACES = set(system_namespaces or ())


def _name(value, label):
    value = str(value or "").strip()
    if not re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?", value):
        raise ValueError(f"{label} must use lowercase letters, numbers and dashes")
    return value


def safe_path(path):
    """A path inside the volume, with no way to address anything outside it."""
    raw = str(path or "").strip().replace("\\", "/")
    # Refused before normalising, not quietly resolved: a path with .. in it is
    # never something the browser sent, so it is answered rather than rewritten.
    if any(part == ".." for part in raw.split("/")):
        raise ValueError("path cannot climb out of the volume")
    cleaned = posixpath.normpath("/" + raw)
    if cleaned in ("/", "//"):
        return ""
    if len(cleaned) > 1024:
        raise ValueError("path is too long")
    return cleaned.lstrip("/")


def _full(path):
    relative = safe_path(path)
    return MOUNT + ("/" + relative if relative else "")


def _quote(value):
    return "'" + str(value).replace("'", "'\\''") + "'"


# ------------------------------------------------------------------ session
def pod_name(pvc):
    return (POD_PREFIX + pvc)[:63].rstrip("-")


def _pod_status(namespace, pod):
    try:
        return kget(f"/api/v1/namespaces/{namespace}/pods/{pod}")
    except Exception:
        return None


def open_session(namespace, pvc):
    """Start (or reuse) the helper pod that mounts this claim."""
    namespace, pvc = _name(namespace, "namespace"), _name(pvc, "volume name")
    if namespace in SYSTEM_NAMESPACES:
        raise PermissionError("Homestead does not browse volumes in system namespaces")
    pod = pod_name(pvc)
    existing = _pod_status(namespace, pod)
    phase = ((existing or {}).get("status", {}) or {}).get("phase", "")
    if existing and phase in ("Pending", "Running"):
        return _wait_ready(namespace, pod)
    if existing:
        try:
            ksend("DELETE", f"/api/v1/namespaces/{namespace}/pods/{pod}?gracePeriodSeconds=0")
        except Exception:
            pass
        time.sleep(1)
    body = {
        "apiVersion": "v1", "kind": "Pod",
        "metadata": {"name": pod, "namespace": namespace,
                     "labels": {"harvui.io/task": "files", "harvui.io/app": pvc}},
        "spec": {"restartPolicy": "Never", "activeDeadlineSeconds": SESSION_SECONDS,
                 "terminationGracePeriodSeconds": 0,
                 "containers": [{"name": "files", "image": IMAGE,
                                 "command": ["sh", "-c", f"sleep {SESSION_SECONDS}"],
                                 "resources": {"requests": {"cpu": "10m", "memory": "32Mi"}},
                                 "volumeMounts": [{"name": "data", "mountPath": MOUNT}]}],
                 "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": pvc}}]},
    }
    ksend("POST", f"/api/v1/namespaces/{namespace}/pods", body)
    return _wait_ready(namespace, pod)


def _wait_ready(namespace, pod, timeout=60):
    deadline = time.time() + timeout
    blocker = ""
    while time.time() < deadline:
        current = _pod_status(namespace, pod) or {}
        status = current.get("status", {}) or {}
        if status.get("phase") == "Running":
            return {"ok": True, "pod": pod, "namespace": namespace}
        for container in status.get("containerStatuses", []) or []:
            waiting = (container.get("state", {}) or {}).get("waiting") or {}
            if waiting.get("reason") in ("ErrImagePull", "ImagePullBackOff", "CreateContainerError"):
                raise ValueError(f"the file browser could not start: {waiting.get('reason')}")
        for condition in status.get("conditions", []) or []:
            if condition.get("type") == "PodScheduled" and condition.get("status") == "False":
                blocker = condition.get("message", "") or condition.get("reason", "")
        time.sleep(1.5)
    if blocker:
        # The usual cause: a ReadWriteOnce claim still attached to its workload.
        raise ValueError(f"the file browser could not start: {blocker[:200]}")
    raise ValueError("the file browser did not start in time; stop the workload using this "
                     "volume and try again")


def close_session(namespace, pvc):
    namespace, pvc = _name(namespace, "namespace"), _name(pvc, "volume name")
    try:
        ksend("DELETE", f"/api/v1/namespaces/{namespace}/pods/{pod_name(pvc)}"
                        "?gracePeriodSeconds=0")
    except Exception:
        pass
    return {"ok": True}


# --------------------------------------------------------------------- exec
def _exec(namespace, pod, argv, stdin=b"", timeout=30):
    """Run one command in the helper pod and read all of its output."""
    query = [("container", "files"), ("stdout", "true"), ("stderr", "true")]
    if stdin:
        query.append(("stdin", "true"))
    query.extend(("command", part) for part in argv)
    path = (f"/api/v1/namespaces/{urllib.parse.quote(namespace)}/pods/"
            f"{urllib.parse.quote(pod)}/exec?{urllib.parse.urlencode(query)}")
    port = API.port or 443
    raw = socket.create_connection((API.hostname, port), timeout=15)
    sock = CTX.wrap_socket(raw, server_hostname=API.hostname)
    key = base64.b64encode(secrets.token_bytes(16)).decode()
    request = (f"GET {path} HTTP/1.1\r\nHost: {API.netloc}\r\n"
               f"Authorization: Bearer {TOKEN}\r\nUpgrade: websocket\r\n"
               "Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Protocol: v4.channel.k8s.io\r\n\r\n")
    sock.sendall(request.encode())
    header = bytearray()
    while not header.endswith(b"\r\n\r\n") and len(header) < 65536:
        chunk = sock.recv(1)
        if not chunk:
            break
        header.extend(chunk)
    if b" 101 " not in header.split(b"\r\n", 1)[0]:
        sock.close()
        raise ConnectionError("the file browser connection was refused by Kubernetes")
    out, err = bytearray(), bytearray()
    try:
        if stdin:
            for index in range(0, len(stdin), 16384):
                sock.sendall(encode_frame(b"\x00" + stdin[index:index + 16384], masked=True))
        sock.settimeout(timeout)
        while True:
            try:
                _, opcode, payload = read_frame(sock, require_mask=False)
            except (EOFError, OSError, ValueError):
                break
            if opcode == 0x8:
                break
            if not payload:
                continue
            channel, data = payload[0], payload[1:]
            if channel == 1:
                out.extend(data)
            elif channel == 2:
                err.extend(data)
            elif channel == 3 and not stdin:
                break
    finally:
        try:
            sock.sendall(encode_frame(b"", opcode=0x8, masked=True))
        except Exception:
            pass
        sock.close()
    return bytes(out), bytes(err).decode("utf-8", "replace").strip()


def _sh(namespace, pod, script, stdin=b"", timeout=30):
    return _exec(namespace, pod, ["sh", "-c", script], stdin=stdin, timeout=timeout)


# -------------------------------------------------------------------- files
def list_files(namespace, pvc, path=""):
    """One directory of the volume, directories first."""
    session = open_session(namespace, pvc)
    target = _full(path)
    script = (f"cd {_quote(target)} 2>/dev/null || exit 3; "
              "for entry in * .[!.]*; do [ -e \"$entry\" ] || continue; "
              "if [ -d \"$entry\" ]; then printf 'd|0|%s\\n' \"$entry\"; "
              "else printf 'f|%s|%s\\n' \"$(stat -c %s \"$entry\" 2>/dev/null || echo 0)\" \"$entry\"; fi; done")
    out, err = _sh(session["namespace"], session["pod"], script)
    if err and not out:
        raise ValueError(err[:200])
    rows = []
    for line in out.decode("utf-8", "replace").splitlines():
        kind, _, rest = line.partition("|")
        size, _, name = rest.partition("|")
        if not name or kind not in ("d", "f"):
            continue
        rows.append({"name": name, "kind": "dir" if kind == "d" else "file",
                     "size": int(size) if size.isdigit() else 0,
                     "editable": kind == "f" and int(size or 0) <= MAX_EDIT_BYTES})
    rows.sort(key=lambda row: (row["kind"] != "dir", row["name"].lower()))
    truncated = len(rows) > MAX_LIST
    return {"path": safe_path(path), "entries": rows[:MAX_LIST], "truncated": truncated,
            "pod": session["pod"]}


def read_file(namespace, pvc, path):
    relative = safe_path(path)
    if not relative:
        raise ValueError("choose a file to open")
    session = open_session(namespace, pvc)
    target = _full(path)
    size_out, _ = _sh(session["namespace"], session["pod"],
                      f"stat -c %s {_quote(target)} 2>/dev/null || echo -1")
    try:
        size = int(size_out.decode().strip() or -1)
    except ValueError:
        size = -1
    if size < 0:
        raise ValueError(f"{relative} does not exist")
    if size > MAX_EDIT_BYTES:
        raise ValueError(f"{relative} is {round(size / 1024)} KB; only files up to "
                         f"{MAX_EDIT_BYTES // 1024} KB can be edited here")
    out, err = _sh(session["namespace"], session["pod"], f"cat {_quote(target)}")
    if err and not out:
        raise ValueError(err[:200])
    if b"\x00" in out:
        raise ValueError(f"{relative} looks like a binary file, so it is not editable here")
    return {"path": relative, "size": size, "content": out.decode("utf-8", "replace")}


def write_file(namespace, pvc, path, content):
    """Replace a file, keeping one backup and verifying what arrived."""
    relative = safe_path(path)
    if not relative:
        raise ValueError("choose a file to save")
    payload = str(content if content is not None else "").encode("utf-8")
    if len(payload) > MAX_EDIT_BYTES:
        raise ValueError(f"the file is larger than {MAX_EDIT_BYTES // 1024} KB")
    session = open_session(namespace, pvc)
    namespace, pod = session["namespace"], session["pod"]
    target = _full(path)
    temporary = target + ".homestead-tmp"
    _sh(namespace, pod, f"cat > {_quote(temporary)}", stdin=payload or b"", timeout=60)
    # Only a complete write replaces the original.
    check, _ = _sh(namespace, pod, f"wc -c < {_quote(temporary)} 2>/dev/null || echo -1")
    try:
        written = int(check.decode().strip() or -1)
    except ValueError:
        written = -1
    if written != len(payload):
        _sh(namespace, pod, f"rm -f {_quote(temporary)}")
        raise ValueError(f"only {max(0, written)} of {len(payload)} bytes arrived; "
                         "the file was left unchanged")
    _, err = _sh(namespace, pod,
                 f"[ -f {_quote(target)} ] && cp -p {_quote(target)} {_quote(target + '.homestead-bak')}; "
                 f"cat {_quote(temporary)} > {_quote(target)} && rm -f {_quote(temporary)}")
    if err:
        raise ValueError(err[:200])
    return {"ok": True, "path": relative, "bytes": len(payload),
            "message": f"Saved {relative} ({len(payload)} bytes); previous contents kept as "
                       f"{posixpath.basename(relative)}.homestead-bak"}


def check_syntax(path, content):
    """Say what is obviously wrong before it is written, for the formats we know."""
    name = str(path or "").lower()
    text = str(content or "")
    if name.endswith(".json"):
        try:
            json.loads(text or "null")
        except json.JSONDecodeError as error:
            return f"JSON is invalid: {error.msg} on line {error.lineno}"
    if name.endswith((".yml", ".yaml")):
        for number, line in enumerate(text.splitlines(), start=1):
            indent = line[:len(line) - len(line.lstrip())]
            if "\t" in indent:
                return f"YAML cannot be indented with tabs (line {number})"
    return ""
