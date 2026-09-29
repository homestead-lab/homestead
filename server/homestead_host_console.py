"""Admin-managed host consoles, installed from this container's bundled release."""
import base64
import hashlib
import json
from pathlib import Path
import re
import shlex
import time
import urllib.parse

import homestead_shared as SHARED

kget = hostrun = ops = None
VERSION = ""
DATA_DIR = "/data"
PAYLOAD = Path(__file__).resolve().parent / "host-console"
if not PAYLOAD.is_dir():
    PAYLOAD = Path(__file__).resolve().parents[1] / "scripts"
DROPIN = "/etc/systemd/system/getty@tty1.service.d/50-homestead-console.conf"
DEST = "/usr/local/lib/homestead"
_lock = SHARED.SharedLock("host-consoles", directory=lambda: DATA_DIR, strict=True)


def bind(read, runner, operations, version, data_dir):
    global kget, hostrun, ops, VERSION, DATA_DIR
    kget, hostrun, ops, VERSION, DATA_DIR = read, runner, operations, version, data_dir


def _path():
    return str(Path(DATA_DIR) / "host-consoles.json")


def _read():
    try:
        state = json.loads(Path(_path()).read_text())
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def inventory():
    saved = _read()
    expected = hashlib.sha256((PAYLOAD / "host-console.py").read_bytes()).hexdigest()
    nodes = []
    for node in kget("/api/v1/nodes").get("items", []):
        meta = node["metadata"]
        current = saved.get(meta["name"], {})
        if current.get("uid") != meta.get("uid"):
            current = {}
        elif current:
            current = dict(current, current=current.get("digest") == expected)
            if current.get("enabled") and not current.get("native"):
                current["detail"] = f"Installed {current['version']}" + (
                    "; matches this release" if current["current"] else "; update available")
        ready = any(c.get("type") == "Ready" and c.get("status") == "True"
                    for c in (node.get("status") or {}).get("conditions", []))
        nodes.append({**current, "name": meta["name"], "ready": ready})
    return {"version": VERSION, "nodes": nodes}


def start(node, action):
    if not node:
        raise ValueError("which host?")
    if action not in ("inspect", "enable", "disable"):
        raise ValueError("console action must be inspect, enable or disable")
    found = kget("/api/v1/nodes/" + urllib.parse.quote(str(node), safe=""))
    if not any(c.get("type") == "Ready" and c.get("status") == "True"
               for c in (found.get("status") or {}).get("conditions", [])):
        raise ValueError("the host must be Ready")
    return ops.start("host-console", f"Host console: {action} on {node}",
                     {"kind": "Node", "name": node}, "/settings?tab=cluster",
                     {"node": node, "uid": found["metadata"]["uid"], "action": action,
                      "version": VERSION}, "Queued; the current login session stays open")


def script(action):
    lines = ["set -eu", f"DEST={DEST}", f"DROPIN={DROPIN}",
             "native=no; [ ! -f /etc/harvester-release ] || native=yes"]
    if action != "inspect":
        lines += ["[ \"$native\" = no ] || { echo 'Harvester keeps its native console'; exit 1; }"]
        if action == "enable":
            if not re.fullmatch(r"\d+\.\d+\.\d+", VERSION):
                raise ValueError("console installation requires a released Homestead version")
            files = {name: (PAYLOAD / name).read_bytes() for name in ("host-console.py", "install-console.sh")}
            lines += ["TASK_DIR=$(mktemp -d)", "trap 'rm -rf -- \"$TASK_DIR\"' EXIT"]
            for name, body in files.items():
                encoded = base64.b64encode(body).decode("ascii")
                lines += [f"printf '%s' '{encoded}' | base64 -d > \"$TASK_DIR/{name}\""]
            lines += ['sh "$TASK_DIR/install-console.sh" enable "$TASK_DIR/host-console.py"',
                      f"printf '%s\\n' {shlex.quote(VERSION)} > \"$DEST/host-console.version\""]
        else:
            lines += ['rm -f -- "$DROPIN"', "systemctl daemon-reload"]
    lines += ["enabled=no", '[ ! -f "$DROPIN" ] || enabled=yes',
              "version=-; digest=-", '[ ! -f "$DEST/host-console.version" ] || version=$(head -c 64 "$DEST/host-console.version")',
              '[ ! -f "$DEST/host-console.py" ] || digest=$(sha256sum "$DEST/host-console.py" | cut -d " " -f 1)',
              'printf "HSCONSOLE enabled=%s version=%s digest=%s native=%s\\n" "$enabled" "$version" "$digest" "$native"']
    return "\n".join(lines)


def status(item):
    ref = item["ref"]
    if ref.get("done"):
        return "succeeded", 100, ref["detail"]
    if ref["version"] != VERSION:
        return "failed", 0, "Homestead changed version while this console job was queued; run it again from Settings"
    # Host-run uses one helper per node. Serialize console jobs across replicas.
    with _lock:
        node = kget("/api/v1/nodes/" + urllib.parse.quote(ref["node"], safe=""))
        if node["metadata"].get("uid") != ref["uid"]:
            return "failed", 0, "The original host was replaced; refresh Settings"
        try:
            out, err = hostrun.run(ref["node"], script(ref["action"]), timeout=240)
            report = next((s for s in out.splitlines() if s.startswith("HSCONSOLE ")), "")
            if not report:
                raise ValueError((err or out or "Host did not return console status")[-300:])
            fields = dict(pair.split("=", 1) for pair in report.split()[1:])
            native, enabled = fields["native"] == "yes", fields["enabled"] == "yes"
            version = fields.get("version", "")
            version = version if re.fullmatch(r"\d+\.\d+\.\d+", version) else "unknown"
            expected = hashlib.sha256((PAYLOAD / "host-console.py").read_bytes()).hexdigest()
            current = fields.get("digest") == expected
            detail = ("Native Harvester console" if native else "Disabled" if not enabled else
                      f"Installed {version}" + ("; matches this release" if current else "; update available"))
            if ref["action"] == "enable":
                if not enabled or not current:
                    raise ValueError("Installed console did not match the bundled release")
                detail += "; appears after console logout or reboot"
            saved = {"uid": ref["uid"], "enabled": enabled, "native": native,
                     "version": version, "digest": fields.get("digest", ""), "current": current,
                     "checked_at": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
                     "detail": detail}
            state = _read()
            state[ref["node"]] = saved
            SHARED.write_json(_path(), state, durable=True)
            ref.update(done=True, detail=detail)
            return "succeeded", 100, detail
        except Exception as error:
            return "failed", 0, str(error)[:400]
