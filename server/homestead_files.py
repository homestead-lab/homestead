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
import binascii
import datetime
import hashlib
import io
import json
import os
import posixpath
import re
import secrets
import socket
import stat as STAT
import tarfile
import threading
import time
import urllib.parse
import zipfile

from homestead_console import encode_frame, read_frame
import homestead_names as NAMES
import homestead_routes as ROUTER

kget = ksend = None
API = None
TOKEN = ""
CTX = None
SYSTEM_NAMESPACES = set()

POD_PREFIX = "homestead-files-"
MOUNT = "/data"
IMAGE = os.environ.get("FILES_IMAGE", "alpine:3.24")
# A helper pod is cheap but it holds a ReadWriteOnce claim, keeping its
# workload from starting. So it goes a minute after it was last used: each
# operation, a person working in the browser (keepalive) and a download or
# copy still running keep it; anything else - a tab closed, a laptop shut,
# someone gone for coffee - lets it go. The next operation starts it again.
# None outlives its deadline.
SESSION_SECONDS = int(os.environ.get("FILES_SESSION_SECONDS", str(4 * 3600)))
IDLE_SECONDS = int(os.environ.get("FILES_IDLE_SECONDS", "60"))
TOUCH_EVERY = 15                   # at most one "still in use" write per helper this often
_touched = {}
SEEN = NAMES.key("files-seen")
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
    if pvc.startswith("homestead-snapshot-files-"):
        raise PermissionError("Snapshot copies are read-only; use their snapshot browser")
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
                     "labels": NAMES.labels("files", app=pvc),
                     "annotations": {SEEN: str(int(time.time()))}},
        "spec": {"restartPolicy": "Never", "activeDeadlineSeconds": SESSION_SECONDS,
                 "terminationGracePeriodSeconds": 0, "automountServiceAccountToken": False,
                 "securityContext": {"seccompProfile": {"type": "RuntimeDefault"}},
                 "containers": [{"name": "files", "image": IMAGE,
                                 # Root, to read and write files whoever owns them, and nothing more.
                                 "securityContext": {"runAsUser": 0, "allowPrivilegeEscalation": False,
                                                     "capabilities": {"drop": ["ALL"],
                                                                      "add": ["CHOWN", "DAC_OVERRIDE", "FOWNER", "FSETID"]}},
                                 "command": ["sh", "-c", f"trap 'exit 0' TERM; sleep {SESSION_SECONDS} & wait"],
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


def close_session(namespace, pvc, wait=20, sleep=time.sleep):
    """Stop the helper and wait for it to go, so the volume is free for its
    workload again; says whether it has (a slow node can take longer)."""
    namespace, pvc = _name(namespace, "namespace"), _name(pvc, "volume name")
    if pvc.startswith("homestead-snapshot-files-"):
        raise PermissionError("Close snapshot copies through their snapshot browser")
    pod = pod_name(pvc)
    if _pod_status(namespace, pod) is None:
        return {"ok": True, "stopped": True}
    ksend("DELETE", f"/api/v1/namespaces/{namespace}/pods/{pod}",
          {"apiVersion": "v1", "kind": "DeleteOptions", "gracePeriodSeconds": 0})
    for _ in range(int(wait)):
        if _pod_status(namespace, pod) is None:
            return {"ok": True, "stopped": True}
        sleep(1)
    return {"ok": True, "stopped": False}


def _touch(namespace, pod, force=False):
    """Mark the helper as in use now, at most every TOUCH_EVERY seconds."""
    now = time.time()
    if not force and now - _touched.get((namespace, pod), 0) < TOUCH_EVERY:
        return True
    try:
        ksend("PATCH", f"/api/v1/namespaces/{namespace}/pods/{pod}",
              {"metadata": {"annotations": {SEEN: str(int(now))}}},
              ctype="application/merge-patch+json")
    except Exception:
        return False
    _touched[(namespace, pod)] = now
    return True


def keepalive(namespace, pvc):
    """Someone is working in the browser: the helper is kept a minute more.
    Says whether it is still running, so the browser can say it stopped."""
    namespace, pvc = _name(namespace, "namespace"), _name(pvc, "volume name")
    pod = pod_name(pvc)
    running = (((_pod_status(namespace, pod) or {}).get("status") or {}).get("phase") == "Running")
    return {"ok": True, "running": running and _touch(namespace, pod, force=True), "idle_seconds": IDLE_SECONDS}


def cleanup(now=None):
    """Remove expired helpers, including pods left behind after their deadline.

    The kubelet stops their containers; it does not delete bare Pod objects.
    Read only our labelled helpers and fence deletion to the observed identity,
    so a browser opening a replacement cannot lose its new pod to this sweep.
    """
    now = time.time() if now is None else now
    selector = urllib.parse.quote(NAMES.key("task") + "=files")
    pods = kget(f"/api/v1/pods?labelSelector={selector}").get("items", [])
    removed = []
    for pod in pods:
        meta, spec = pod.get("metadata", {}), pod.get("spec", {})
        namespace, name = meta.get("namespace"), meta.get("name", "")
        if (namespace in SYSTEM_NAMESPACES or not namespace or not meta.get("uid")
                or meta.get("deletionTimestamp") or not name.startswith(POD_PREFIX)
                or NAMES.label_of(meta, "task") != "files"):
            continue
        claim = next((v.get("persistentVolumeClaim", {}).get("claimName")
                      for v in spec.get("volumes", []) if v.get("name") == "data"), None)
        if not claim or name != pod_name(claim):
            continue
        terminal = pod.get("status", {}).get("phase") in ("Succeeded", "Failed")
        try:
            created = datetime.datetime.fromisoformat(meta["creationTimestamp"].replace("Z", "+00:00")).timestamp()
            deadline = int(spec.get("activeDeadlineSeconds") or SESSION_SECONDS)
        except (KeyError, TypeError, ValueError):
            continue
        # No browser has said it is open for a while: closed without saying.
        try:
            seen = int((meta.get("annotations") or {}).get(SEEN) or 0)
        except ValueError:
            seen = 0
        idle = bool(seen) and now - seen >= IDLE_SECONDS
        if not terminal and not idle and now - created < max(1, deadline):
            continue
        preconditions = {"uid": meta["uid"]}
        if meta.get("resourceVersion"):
            preconditions["resourceVersion"] = meta["resourceVersion"]
        ksend("DELETE", f"/api/v1/namespaces/{namespace}/pods/{name}", {
            "apiVersion": "v1", "kind": "DeleteOptions", "gracePeriodSeconds": 0,
            "preconditions": preconditions})
        removed.append(name)
    return removed


# --------------------------------------------------------------------- exec
class ExecRefused(ConnectionError):
    """Kubernetes would not open the exec: nothing ran in the pod. Right after
    a host comes back the API server can answer for a Running pod while it
    cannot reach that host's kubelet yet, so this one is worth trying again."""
    retry = True


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


def _exec(namespace, pod, argv, stdin=b"", timeout=30, container="files"):
    """Run one command in the helper pod and read all of its output."""
    query = [("container", container), ("stdout", "true"), ("stderr", "true")]
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
    status = header.split(b"\r\n", 1)[0].decode("latin-1", "replace").strip()
    if " 101 " not in f"{status} ":
        raise _refused(sock, pod, status)
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
    if len(out) > MAX_EDIT_BYTES:
        raise ValueError("the file changed size; open it again")
    return {"path": relative, "size": len(out), "content": out.decode("utf-8", "replace"),
            "revision": hashlib.sha256(out).hexdigest()}


def write_file(namespace, pvc, path, content, revision=None):
    """Replace a file, keeping one backup and verifying what arrived."""
    relative = safe_path(path)
    if not relative:
        raise ValueError("choose a file to save")
    if not re.fullmatch(r"[0-9a-f]{64}", str(revision or "")):
        raise ValueError("open the file again before saving; its revision is required")
    payload = str(content if content is not None else "").encode("utf-8")
    if len(payload) > MAX_EDIT_BYTES:
        raise ValueError(f"the file is larger than {MAX_EDIT_BYTES // 1024} KB")
    session = open_session(namespace, pvc)
    namespace, pod = session["namespace"], session["pod"]
    target = _full(path)
    lock = target + ".homestead-lock"
    temporary = target + ".homestead-" + secrets.token_hex(16)
    backup = temporary + ".bak"
    t, tmp, lk, bak = map(_quote, (target, temporary, lock, backup))
    acquired, err = _sh(namespace, pod,
        f"umask 077; mkdir {lk} && printf locked")
    if acquired != b"locked":
        raise ValueError("another save holds this file; try again or inspect its .homestead-lock directory")
    digest = hashlib.sha256(payload).hexdigest()
    try:
        _, err = _sh(namespace, pod, f"umask 077; set -C; head -c {len(payload)} > {tmp}", stdin=payload or b"", timeout=60)
        if err:
            raise ValueError(err[:200])
        # Marker only follows a complete, verified, durable replacement.
        script = (f"set -e; [ -f {t} ] && [ ! -L {t} ]; "
            f"[ \"$(sha256sum {t} | cut -d ' ' -f1)\" = {_quote(revision)} ] || "
            "{ echo 'file changed; reopen it before saving' >&2; exit 1; }; "
            f"[ \"$(sha256sum {tmp} | cut -d ' ' -f1)\" = {_quote(digest)} ]; "
            f"cp -p {t} {bak}; chmod \"$(stat -c %a {t})\" {tmp}; chown \"$(stat -c %u:%g {t})\" {tmp}; "
            f"sync -f {tmp}; sync -f {bak}; "
            f"mv -fT {bak} {_quote(target + '.homestead-bak')}; mv -fT {tmp} {t}; "
            f"sync -f {_quote(posixpath.dirname(target))}; printf saved")
        answer, err = _sh(namespace, pod, script, timeout=60)
        if answer != b"saved":
            raise ValueError(err[:200] or "save failed; the original or its backup is retained")
    finally:
        _sh(namespace, pod, f"rm -f {tmp} {bak}; rmdir {lk}")
    return {"ok": True, "path": relative, "bytes": len(payload),
            "revision": digest,
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


# ------------------------------------------------------------- file manager
# A two-pane manager (filemanager.js): what each entry is and who may use it,
# and the operations a file manager has. Each runs as one command in the
# claim's helper; nothing here can address a path outside its volume.
LIST_FORMAT = "%F|%s|%a|%u|%g|%U|%G|%Y|%n"
TYPES = {"directory": "dir", "regular file": "file", "regular empty file": "file", "symbolic link": "link"}
MAX_CHUNK = 4 * 1024 * 1024        # one upload request, before base64
MAX_NAME = 255


def _session(namespace, pvc):
    session = open_session(namespace, pvc)
    _touch(session["namespace"], session["pod"])
    return session["namespace"], session["pod"]


def _run(namespace, pvc, script, stdin=b"", timeout=60):
    """Run script in the claim's helper; its stderr, if it failed, as the error."""
    ns, pod = _session(namespace, pvc)
    out, err = _sh(ns, pod, script + "; printf '\n%s' \"rc=$?\"", stdin=stdin, timeout=timeout)
    text, _, status = out.rpartition(b"\nrc=")
    if status.strip() != b"0":
        raise ValueError((err or "the helper could not do that")[:300])
    return text


def _entry_name(name):
    name = str(name or "").strip()
    if not name or name in (".", "..") or "/" in name or "\\" in name or "\x00" in name or len(name) > MAX_NAME:
        raise ValueError("give a name without slashes")
    return name


def _paths(paths):
    """Paths inside the volume, never the volume itself."""
    out = [safe_path(p) for p in (paths if isinstance(paths, list) else [paths])]
    if not out or any(not p for p in out):
        raise ValueError("choose what to change; the volume itself cannot be")
    return out


def list_entries(namespace, pvc, path=""):
    """One folder, with what a file manager shows: size, permissions, owner,
    modified, and whether it is a link."""
    target = _full(path)
    script = (f"cd {_quote(target)} 2>/dev/null || {{ echo 'no such folder' >&2; exit 3; }}; "
              f"for e in * .[!.]* ..?*; do [ -e \"$e\" ] || [ -L \"$e\" ] || continue; "
              f"stat -c {_quote(LIST_FORMAT)} \"$e\"; done")
    out = _run(namespace, pvc, script)
    rows = []
    for line in out.decode("utf-8", "replace").splitlines():
        parts = line.split("|", 8)
        if len(parts) != 9:
            continue
        kind, size, mode, uid, gid, user, group, mtime, name = parts
        rows.append({"name": name, "kind": TYPES.get(kind, "other"), "size": int(size) if size.isdigit() else 0,
                     "mode": mode, "uid": int(uid) if uid.isdigit() else None, "gid": int(gid) if gid.isdigit() else None,
                     "user": user if user != "UNKNOWN" else uid, "group": group if group != "UNKNOWN" else gid,
                     "modified": int(mtime) if mtime.isdigit() else 0,
                     "editable": TYPES.get(kind) == "file" and size.isdigit() and int(size) <= MAX_EDIT_BYTES})
    rows.sort(key=lambda row: (row["kind"] != "dir", row["name"].lower()))
    return {"path": safe_path(path), "entries": rows[:MAX_LIST], "truncated": len(rows) > MAX_LIST,
            "volume": pvc, "namespace": namespace}


def make_folder(namespace, pvc, path, name):
    target = posixpath.join(_full(path), _entry_name(name))
    _run(namespace, pvc, f"mkdir -- {_quote(target)}")
    return {"ok": True, "path": safe_path(posixpath.join(safe_path(path), name))}


def rename(namespace, pvc, path, name):
    source = _paths(path)[0]
    target = posixpath.join(posixpath.dirname(_full(source)), _entry_name(name))
    _run(namespace, pvc, f"[ ! -e {_quote(target)} ] || {{ echo 'that name is taken' >&2; exit 1; }}; "
                         f"mv -- {_quote(_full(source))} {_quote(target)}")
    return {"ok": True}


def delete(namespace, pvc, paths):
    targets = " ".join(_quote(_full(p)) for p in _paths(paths))
    _run(namespace, pvc, f"rm -rf -- {targets}", timeout=600)
    return {"ok": True, "deleted": len(_paths(paths))}


def set_mode(namespace, pvc, paths, mode, recursive=False):
    mode = str(mode or "").strip()
    if not re.fullmatch(r"[0-7]{3,4}", mode):
        raise ValueError("permissions are three or four octal digits, like 755")
    targets = " ".join(_quote(_full(p)) for p in _paths(paths))
    _run(namespace, pvc, f"chmod {'-R ' if recursive else ''}{mode} -- {targets}", timeout=600)
    return {"ok": True}


def set_owner(namespace, pvc, paths, uid, gid=None, recursive=False):
    def ident(value, label):
        value = str(value if value is not None else "").strip()
        if not value.isdigit() or int(value) > 4294967294:
            raise ValueError(f"{label} is a number")
        return value
    owner = ident(uid, "the user") + ("" if gid in (None, "") else ":" + ident(gid, "the group"))
    targets = " ".join(_quote(_full(p)) for p in _paths(paths))
    _run(namespace, pvc, f"chown {'-R ' if recursive else ''}{owner} -- {targets}", timeout=600)
    return {"ok": True}


def _inside(source, folder):
    return folder == source or folder.startswith(source + "/")


def copy_within(namespace, pvc, paths, folder, move=False):
    """Copy or move entries to a folder of the same volume."""
    sources, folder = _paths(paths), safe_path(folder)
    if any(_inside(source, folder) for source in sources):
        raise ValueError("a folder cannot go inside itself")
    targets = " ".join(_quote(_full(p)) for p in sources)
    verb = "mv" if move else "cp -a"
    _run(namespace, pvc, f"[ -d {_quote(_full(folder))} ] || {{ echo 'no such folder' >&2; exit 1; }}; "
                         f"for s in {targets}; do [ ! -e {_quote(_full(folder))}/\"$(basename \"$s\")\" ] || "
                         f"{{ echo \"$(basename \"$s\") is already there\" >&2; exit 1; }}; done; "
                         f"{verb} -- {targets} {_quote(_full(folder))}/", timeout=3600)
    return {"ok": True}


# ---- streaming: downloads, zips and transfers between volumes
def _mask(payload, key):
    """A websocket mask over a whole frame at once; byte by byte is far too slow."""
    if not payload:
        return b""
    repeated = (key * (len(payload) // 4 + 1))[:len(payload)]
    return (int.from_bytes(payload, "big") ^ int.from_bytes(repeated, "big")).to_bytes(len(payload), "big")


def _frame(payload):
    key = secrets.token_bytes(4)
    length = len(payload)
    head = bytearray([0x82])
    if length < 126:
        head.append(0x80 | length)
    elif length <= 0xFFFF:
        head.append(0x80 | 126)
        head.extend(length.to_bytes(2, "big"))
    else:
        head.append(0x80 | 127)
        head.extend(length.to_bytes(8, "big"))
    return bytes(head) + key + _mask(payload, key)


def _open_exec(namespace, pod, argv, stdin=False, container="files"):
    """An exec websocket, open: the socket, for streaming in either direction."""
    query = [("container", container), ("stdout", "true"), ("stderr", "true")]
    if stdin:
        query.append(("stdin", "true"))
    query.extend(("command", part) for part in argv)
    path = (f"/api/v1/namespaces/{urllib.parse.quote(namespace)}/pods/"
            f"{urllib.parse.quote(pod)}/exec?{urllib.parse.urlencode(query)}")
    raw = socket.create_connection((API.hostname, API.port or 443), timeout=15)
    sock = CTX.wrap_socket(raw, server_hostname=API.hostname)
    key = base64.b64encode(secrets.token_bytes(16)).decode()
    sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {API.netloc}\r\n"
                  f"Authorization: Bearer {TOKEN}\r\nUpgrade: websocket\r\n"
                  "Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\n"
                  f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Protocol: v4.channel.k8s.io\r\n\r\n").encode())
    header = bytearray()
    while not header.endswith(b"\r\n\r\n") and len(header) < 65536:
        chunk = sock.recv(1)
        if not chunk:
            break
        header.extend(chunk)
    status = header.split(b"\r\n", 1)[0].decode("latin-1", "replace").strip()
    if " 101 " not in f"{status} ":
        raise _refused(sock, pod, status)
    sock.settimeout(300)
    return sock


def _close_exec(sock):
    try:
        sock.sendall(encode_frame(b"", opcode=0x8, masked=True))
    except Exception:
        pass
    sock.close()


def _stdout(sock, errors):
    """Yield what the command writes, as it writes it; stderr is gathered.
    Raises when the command ends unsuccessfully."""
    while True:
        try:
            _, opcode, payload = read_frame(sock, require_mask=False, max_size=MAX_CHUNK * 4)
        except (EOFError, OSError):
            return
        if opcode == 0x8:
            return
        if not payload:
            continue
        channel, data = payload[0], payload[1:]
        if channel == 1:
            yield data
        elif channel == 2:
            errors.extend(data)
        elif channel == 3:
            status = data.decode("utf-8", "replace")
            if '"Success"' not in status:
                raise ValueError((errors.decode("utf-8", "replace") or status)[:300])
            return


def _tar_argv(paths):
    """tar of entries, each named by its own name, from their folder."""
    folders = {posixpath.dirname(_full(p)) for p in paths}
    if len(folders) != 1:
        raise ValueError("choose entries from one folder")
    names = " ".join(_quote(posixpath.basename(_full(p))) for p in paths)
    return ["sh", "-c", f"cd {_quote(folders.pop())} && tar -cf - -- {names}"]


def download(namespace, pvc, paths):
    """A file as it is, or a folder or several entries as one zip: Stream."""
    sources = _paths(paths)
    ns, pod = _session(namespace, pvc)
    if len(sources) == 1:
        kind = _sh(ns, pod, f"stat -c %F {_quote(_full(sources[0]))}")[0].decode().strip()
        if kind in ("regular file", "regular empty file"):
            size = int(_sh(ns, pod, f"stat -c %s {_quote(_full(sources[0]))}")[0].decode().strip() or 0)
            sock = _open_exec(ns, pod, ["cat", "--", _full(sources[0])])

            def body():
                errors = bytearray()
                try:
                    for chunk in _stdout(sock, errors):
                        _touch(ns, pod)
                        yield chunk
                finally:
                    _close_exec(sock)
            return ROUTER.Stream(body(), "application/octet-stream",
                                 posixpath.basename(sources[0]), size)
    base = posixpath.basename(sources[0]) if len(sources) == 1 else (posixpath.basename(posixpath.dirname(_full(sources[0]))) or pvc)
    sock = _open_exec(ns, pod, _tar_argv(sources))
    return ROUTER.Stream(_zip_of_tar(sock, lambda: _touch(ns, pod)), "application/zip", f"{base}.zip", None)


class _Pipe(io.RawIOBase):
    """A tar stream, read as a file: what the helper writes, as it arrives."""
    def __init__(self, chunks):
        self.chunks, self.buffer = chunks, b""

    def readable(self):
        return True

    def readinto(self, target):
        while not self.buffer:
            try:
                self.buffer = next(self.chunks)
            except StopIteration:
                return 0
        count = min(len(target), len(self.buffer))
        target[:count] = self.buffer[:count]
        self.buffer = self.buffer[count:]
        return count


class _Out(io.RawIOBase):
    """Where the zip is written: collected, then yielded as it grows."""
    def __init__(self):
        self.parts, self.position = [], 0

    def writable(self):
        return True

    def write(self, data):
        self.parts.append(bytes(data))
        self.position += len(data)
        return len(data)

    def tell(self):
        return self.position

    def take(self):
        data, self.parts = b"".join(self.parts), []
        return data


def _zip_of_tar(sock, touch=lambda: None):
    """The helper's tar, rewritten as a zip as it streams: nothing is held
    whole, so a large folder downloads like a small one."""
    errors = bytearray()
    out = _Out()
    try:
        archive = tarfile.open(fileobj=io.BufferedReader(_Pipe(_stdout(sock, errors)), 1024 * 1024), mode="r|")
        with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as zipped:
            for member in archive:
                name = member.name.lstrip("./")
                if not name:
                    continue
                info = zipfile.ZipInfo(name + ("/" if member.isdir() else ""),
                                       time.localtime(max(member.mtime, 315532800))[:6])
                info.external_attr = ((member.mode & 0o7777) | (STAT.S_IFDIR if member.isdir() else STAT.S_IFREG)) << 16
                if member.isdir():
                    zipped.writestr(info, b"")
                elif member.isreg():
                    info.compress_type = zipfile.ZIP_DEFLATED
                    source = archive.extractfile(member)
                    with zipped.open(info, "w", force_zip64=member.size > 0x7FFFFFFF) as target:
                        while True:
                            block = source.read(1024 * 1024)
                            if not block:
                                break
                            target.write(block)
                            touch()
                            chunk = out.take()
                            if chunk:
                                yield chunk
                chunk = out.take()
                if chunk:
                    yield chunk
        chunk = out.take()
        if chunk:
            yield chunk
    finally:
        _close_exec(sock)


def upload(namespace, pvc, path, name, data, offset=0, final=False, token=""):
    """One piece of a file, in order; the last puts it in place. Pieces land
    in a hidden file next to it, so a cut-off upload never replaces anything."""
    folder, name = safe_path(path), _entry_name(name)
    try:
        payload = base64.b64decode(str(data or ""), validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("a piece of the upload did not arrive whole") from None
    if len(payload) > MAX_CHUNK:
        raise ValueError("an upload piece is larger than 4 MiB")
    offset = int(offset or 0)
    token = token or secrets.token_hex(8)
    if not re.fullmatch(r"[0-9a-f]{16}", token):
        raise ValueError("unknown upload")
    target = posixpath.join(_full(folder), name)
    part = posixpath.join(_full(folder), f".{name}.homestead-upload-{token}")
    t, p = _quote(target), _quote(part)
    start = f"[ -d {_quote(_full(folder))} ] || {{ echo 'no such folder' >&2; exit 1; }}; " + (
        f"umask 022; : > {p}; " if offset == 0 else
        f"[ \"$(stat -c %s {p} 2>/dev/null)\" = {offset} ] || {{ echo 'the upload lost its place; start it again' >&2; exit 1; }}; ")
    finish = f"; mv -f -- {p} {t}" if final else ""
    _run(namespace, pvc, f"{start}head -c {len(payload)} >> {p}{finish}", stdin=payload, timeout=300)
    return {"ok": True, "token": token, "received": offset + len(payload), "done": bool(final)}


# Copies and moves between two volumes run in the background: the helper of
# one sends a tar, the other's unpacks it. Progress is what has passed.
_JOBS, _JOBS_LOCK = {}, threading.Lock()


def transfer(source, target, paths, move=False):
    """Copy or move entries from one volume's folder to another's. source and
    target are {namespace, pvc, path}. Within one volume it is immediate."""
    src_ns, src_pvc = _name(source.get("namespace"), "namespace"), _name(source.get("pvc"), "volume name")
    dst_ns, dst_pvc = _name(target.get("namespace"), "namespace"), _name(target.get("pvc"), "volume name")
    sources, folder = _paths(paths), safe_path(target.get("path"))
    if (src_ns, src_pvc) == (dst_ns, dst_pvc):
        copy_within(src_ns, src_pvc, sources, folder, move)
        return {"ok": True, "done": True}
    src_ns, src_pod = _session(src_ns, src_pvc)
    dst_ns, dst_pod = _session(dst_ns, dst_pvc)
    names = [posixpath.basename(_full(p)) for p in sources]
    clash, _ = _sh(dst_ns, dst_pod, f"cd {_quote(_full(folder))} || exit 0; for n in "
                   + " ".join(_quote(n) for n in names) + "; do [ -e \"$n\" ] && echo \"$n\"; done; true")
    if not _sh(dst_ns, dst_pod, f"[ -d {_quote(_full(folder))} ] && echo yes")[0].strip():
        raise ValueError("no such folder on the other volume")
    if clash.strip():
        raise ValueError(f"{clash.decode('utf-8', 'replace').split()[0]} is already there")
    job = {"id": secrets.token_hex(8), "status": "running", "bytes": 0, "error": "", "move": bool(move),
           "from": f"{src_pvc}", "to": f"{dst_pvc}", "names": names, "started": time.time()}
    with _JOBS_LOCK:
        _JOBS[job["id"]] = job
    threading.Thread(target=_transfer, args=(job, src_ns, src_pod, sources, dst_ns, dst_pod, folder),
                     name="files-transfer", daemon=True).start()
    return {"ok": True, "job": dict(job)}


def _transfer(job, src_ns, src_pod, sources, dst_ns, dst_pod, folder):
    reader = writer = None
    try:
        reader = _open_exec(src_ns, src_pod, _tar_argv(sources))
        writer = _open_exec(dst_ns, dst_pod, ["sh", "-c", f"cd {_quote(_full(folder))} && tar -xpf -"], stdin=True)
        errors = bytearray()
        for chunk in _stdout(reader, errors):
            for start in range(0, len(chunk), 1024 * 1024):
                writer.sendall(_frame(b"\x00" + chunk[start:start + 1024 * 1024]))
            job["bytes"] += len(chunk)
            _touch(src_ns, src_pod)
            _touch(dst_ns, dst_pod)
        # End of input: the receiving tar finishes, and says how it went.
        writer.sendall(_frame(b"\xff\x00"))
        received = bytearray()
        for _ in _stdout(writer, received):
            pass
        if job["move"]:
            targets = " ".join(_quote(_full(p)) for p in sources)
            out, err = _sh(src_ns, src_pod, f"rm -rf -- {targets} && printf removed", timeout=600)
            if out != b"removed":
                raise ValueError(f"copied, but the originals could not be removed: {err[:200]}")
        job["status"] = "done"
    except Exception as error:
        job["status"], job["error"] = "failed", str(error)[:300]
    finally:
        for sock in (reader, writer):
            if sock:
                _close_exec(sock)
        job["finished"] = time.time()


def transfer_status(job_id):
    with _JOBS_LOCK:
        for key in [k for k, v in _JOBS.items() if v.get("finished") and time.time() - v["finished"] > 3600]:
            del _JOBS[key]
        job = _JOBS.get(str(job_id or ""))
    if not job:
        raise ValueError("that copy is no longer known here")
    return dict(job)


# Its routes and who may use them (homestead_routes.py): a volume's files
# are an admin's to see and change, as they always have been.
def _q(request, key, default=""):
    return (request.query.get(key) or [default])[0]


def _b(request, key="namespace"):
    return request.body.get(key)


ROUTES = {
    ("GET", "/api/files/entries"): ("admin", lambda request: list_entries(
        _q(request, "namespace", "lab"), _q(request, "pvc"), _q(request, "path"))),
    ("GET", "/api/files/download"): ("admin", lambda request: download(
        _q(request, "namespace", "lab"), _q(request, "pvc"), request.query.get("path") or [""])),
    ("POST", "/api/files/keepalive"): ("admin", lambda request: keepalive(_b(request), _b(request, "pvc"))),
    ("POST", "/api/files/folder"): ("admin", lambda request: make_folder(
        _b(request), _b(request, "pvc"), _b(request, "path"), _b(request, "name"))),
    ("POST", "/api/files/rename"): ("admin", lambda request: rename(
        _b(request), _b(request, "pvc"), _b(request, "path"), _b(request, "name"))),
    ("POST", "/api/files/delete"): ("admin", lambda request: delete(
        _b(request), _b(request, "pvc"), _b(request, "paths"))),
    ("POST", "/api/files/mode"): ("admin", lambda request: set_mode(
        _b(request), _b(request, "pvc"), _b(request, "paths"), _b(request, "mode"), bool(_b(request, "recursive")))),
    ("POST", "/api/files/owner"): ("admin", lambda request: set_owner(
        _b(request), _b(request, "pvc"), _b(request, "paths"), _b(request, "uid"), _b(request, "gid"),
        bool(_b(request, "recursive")))),
    ("POST", "/api/files/upload"): ("admin", lambda request: upload(
        _b(request), _b(request, "pvc"), _b(request, "path"), _b(request, "name"), _b(request, "data"),
        _b(request, "offset") or 0, bool(_b(request, "final")), _b(request, "token") or "")),
    ("POST", "/api/files/transfer"): ("admin", lambda request: transfer(
        request.body.get("from") or {}, request.body.get("to") or {}, _b(request, "paths"), bool(_b(request, "move")))),
    ("GET", "/api/files/transfer"): ("admin", lambda request: transfer_status(_q(request, "id"))),
    ("GET", "/api/files/list"): ("admin", lambda request: list_files(
        _q(request, "namespace", "lab"), _q(request, "pvc"), _q(request, "path"))),
    ("GET", "/api/files/read"): ("admin", lambda request: read_file(
        _q(request, "namespace", "lab"), _q(request, "pvc"), _q(request, "path"))),
    ("POST", "/api/files/write"): ("admin", lambda request: _checked_write(request.body)),
    ("POST", "/api/files/close"): ("admin", lambda request: close_session(_b(request) or "lab", _b(request, "pvc"))),
}


def _checked_write(body):
    """A save: what is plainly wrong with the format is said first, unless
    the person chose to save it anyway."""
    warning = check_syntax(body.get("path"), body.get("content"))
    if warning and not body.get("ignore_syntax"):
        raise ValueError(warning)
    return write_file(body.get("namespace") or "lab", body.get("pvc"), body.get("path"),
                      body.get("content"), body.get("revision"))
