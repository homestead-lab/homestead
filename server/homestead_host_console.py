"""Host consoles, an add-on for the whole cluster, installed from this
container's bundled release.

One setting says whether hosts have the console. tick() brings every Ready
host to it, a couple at a time: installed where it is missing - a host that
joined later too - updated where it differs from the console this Homestead
carries (so updating Homestead rolls the console out), and removed where the
setting is off. Harvester hosts keep their native console. A host that fails
is tried again after an hour, not every pass."""
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
platform = lambda: {}
CLUSTER = "_cluster"            # the add-on's setting, beside each host's record
PER_TICK = 2
RETRY_AFTER = 3600
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


def _expected():
    return hashlib.sha256((PAYLOAD / "host-console.py").read_bytes()).hexdigest()


def wanted(saved=None):
    """Whether hosts should have the console: the setting, else what the
    installer did (it turns the console on by default), else unknown."""
    saved = _read() if saved is None else saved
    setting = saved.get(CLUSTER) or {}
    if "enabled" in setting:
        return bool(setting["enabled"])
    rows = [v for k, v in saved.items() if not k.startswith("_") and isinstance(v, dict) and not v.get("native")]
    if any(r.get("enabled") for r in rows):
        return True
    return False if rows else None


def set_cluster(enabled, by=""):
    """Turn the add-on on or off for every host; tick() does the rest."""
    with _lock:
        state = _read()
        state[CLUSTER] = {"enabled": bool(enabled), "by": str(by or ""),
                          "at": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())}
        SHARED.write_json(_path(), state, durable=True)
    return {"ok": True, "enabled": bool(enabled),
            "detail": ("Host consoles are being installed on every host" if enabled
                       else "Host consoles are being removed from every host")
                      + ", a couple at a time within ten minutes each; login sessions stay open"}


def inventory():
    saved = _read()
    expected = _expected()
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
    want = wanted(saved)
    hosts = [n for n in nodes if not n.get("native")]
    installed = [n for n in hosts if n.get("enabled")]
    current = [n for n in installed if n.get("current")]
    settled = (want is True and len(current) == len(hosts)) or (want is False and not installed)
    return {"version": VERSION, "nodes": nodes, "enabled": want,
            "setting": (saved.get(CLUSTER) or {}), "hosts": len(hosts),
            "installed": len(installed), "current": len(current), "settled": bool(hosts) and settled,
            "harvester": bool((platform() or {}).get("harvester"))}


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
    # A step that fails prints nothing of its own under set -e; the trap says
    # which step it was and how it ended, so the failure is never silent.
    lines = ["set -eu", "STEP=start",
             "trap 'rc=$?; [ \"$rc\" = 0 ] || echo \"HSCONSOLE-FAILED step=$STEP exit=$rc\"' EXIT",
             f"DEST={DEST}", f"DROPIN={DROPIN}",
             "native=no; [ ! -f /etc/harvester-release ] || native=yes"]
    if action != "inspect":
        lines += ["[ \"$native\" = no ] || { echo 'Harvester keeps its native console'; exit 1; }"]
        if action == "enable":
            if not re.fullmatch(r"\d+\.\d+\.\d+", VERSION):
                raise ValueError("console installation requires a released Homestead version")
            files = {name: (PAYLOAD / name).read_bytes() for name in ("host-console.py", "install-console.sh")}
            lines += ["STEP=unpack", "TASK_DIR=$(mktemp -d)",
                      "trap 'rc=$?; rm -rf -- \"$TASK_DIR\"; [ \"$rc\" = 0 ] || echo \"HSCONSOLE-FAILED step=$STEP exit=$rc\"' EXIT"]
            for name, body in files.items():
                encoded = base64.b64encode(body).decode("ascii")
                lines += [f"printf '%s' '{encoded}' | base64 -d > \"$TASK_DIR/{name}\""]
            lines += ["STEP=install", 'sh "$TASK_DIR/install-console.sh" enable "$TASK_DIR/host-console.py" 2>&1',
                      "STEP=record", f"printf '%s\\n' {shlex.quote(VERSION)} > \"$DEST/host-console.version\""]
        else:
            # Nobody signed in on the screen: the normal login comes back at once.
            lines += ["STEP=remove", 'rm -f -- "$DROPIN"', "systemctl daemon-reload",
                      "who 2>/dev/null | awk '$2 == \"tty1\" { f = 1 } END { exit !f }' || systemctl restart getty@tty1.service || true"]
    lines += ["STEP=report", "enabled=no", '[ ! -f "$DROPIN" ] || enabled=yes',
              "version=-; digest=-", '[ ! -f "$DEST/host-console.version" ] || version=$(head -c 64 "$DEST/host-console.version")',
              '[ ! -f "$DEST/host-console.py" ] || digest=$(sha256sum "$DEST/host-console.py" | cut -d " " -f 1)',
              'printf "HSCONSOLE enabled=%s version=%s digest=%s native=%s\\n" "$enabled" "$version" "$digest" "$native"']
    return "\n".join(lines)


def _apply(node, uid, action):
    """Run one console action on one host and record what it is now. Called
    holding _lock: host-run uses one helper per node."""
    try:
        out, err = hostrun.run(node, script(action), timeout=240)
        report = next((s for s in out.splitlines() if s.startswith("HSCONSOLE ")), "")
        if not report:
            failed = next((s for s in out.splitlines() if s.startswith("HSCONSOLE-FAILED ")), "")
            said = "\n".join(s for s in out.splitlines() if s and not s.startswith("HSCONSOLE"))
            raise ValueError((failed[len("HSCONSOLE-FAILED "):] + ": " if failed else "")
                             + ((err or said)[-280:] or "the host gave no output"))
        fields = dict(pair.split("=", 1) for pair in report.split()[1:])
        native, enabled = fields["native"] == "yes", fields["enabled"] == "yes"
        version = fields.get("version", "")
        version = version if re.fullmatch(r"\d+\.\d+\.\d+", version) else "unknown"
        current = fields.get("digest") == _expected()
        detail = ("Native Harvester console" if native else "Disabled" if not enabled else
                  f"Installed {version}" + ("; matches this release" if current else "; update available"))
        if action == "enable":
            if not enabled or not current:
                raise ValueError("Installed console did not match the bundled release")
            detail += "; on its screen now, or after a logout if someone is signed in there"
        saved = {"uid": uid, "enabled": enabled, "native": native,
                 "version": version, "digest": fields.get("digest", ""), "current": current,
                 "checked_at": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
                 "detail": detail}
    except Exception as error:
        state = _read()
        saved = dict(state.get(node) if (state.get(node) or {}).get("uid") == uid else {"uid": uid},
                     failed_at=time.time(), detail=f"{action} failed: {str(error)[:300]}")
        state[node] = saved
        SHARED.write_json(_path(), state, durable=True)
        raise
    state = _read()
    state[node] = saved
    SHARED.write_json(_path(), state, durable=True)
    return saved


def status(item):
    ref = item["ref"]
    if ref.get("done"):
        return "succeeded", 100, ref["detail"]
    if ref["version"] != VERSION:
        return "failed", 0, "Homestead changed version while this console job was queued; run it again from Add-ons"
    # Host-run uses one helper per node. Serialize console jobs across replicas.
    with _lock:
        node = kget("/api/v1/nodes/" + urllib.parse.quote(ref["node"], safe=""))
        if node["metadata"].get("uid") != ref["uid"]:
            return "failed", 0, "The original host was replaced; refresh Add-ons"
        try:
            saved = _apply(ref["node"], ref["uid"], ref["action"])
        except Exception as error:
            return "failed", 0, str(error)[:400]
        ref.update(done=True, detail=saved["detail"])
        return "succeeded", 100, saved["detail"]


def _due(record, uid, want, expected, now):
    """What a host needs, or "" - from its record and the setting."""
    same = record.get("uid") == uid
    if same and now - float(record.get("failed_at") or 0) < RETRY_AFTER:
        return ""               # failed within the hour: not every pass
    if not same or not record.get("checked_at"):
        # Never looked at (or a new host under the same name).
        return "enable" if want else "inspect"
    if record.get("native"):
        return ""
    if want and (not record.get("enabled") or record.get("digest") != expected):
        return "enable"
    if want is False and record.get("enabled"):
        return "disable"
    return ""


def tick(now=None):
    """Bring a couple of hosts to the add-on's setting. Returns
    [(host, what was done)] for the log."""
    now = now or time.time()
    p = platform() or {}
    if p.get("harvester") or p.get("distribution") not in ("k3s", "rke2"):
        return []
    if not re.fullmatch(r"\d+\.\d+\.\d+", VERSION):
        return []               # a development build carries no release to install
    try:
        nodes = kget("/api/v1/nodes").get("items", [])
    except Exception:
        return []
    expected, done = _expected(), []
    with _lock:
        saved = _read()
        want = wanted(saved)
        for node in nodes:
            if len(done) >= PER_TICK:
                break
            meta = node["metadata"]
            ready = any(c.get("type") == "Ready" and c.get("status") == "True"
                        for c in (node.get("status") or {}).get("conditions", []))
            if not ready:
                continue
            action = _due(saved.get(meta["name"]) or {}, meta.get("uid"), want, expected, now)
            if not action:
                continue
            try:
                record = _apply(meta["name"], meta.get("uid"), action)
                done.append((meta["name"], {"enable": "host console installed or updated",
                                            "disable": "host console removed"}.get(action, "host console checked")
                             + f" ({record['detail']})"))
            except Exception as error:
                done.append((meta["name"], f"host console {action} failed, tried again in an hour: {str(error)[:200]}"))
            saved = _read()
            want = wanted(saved)
    return done


# Its routes and who may use them (homestead_routes.py).
ROUTES = {
    ("GET", "/api/host-console"): ("admin", lambda request: inventory()),
}
