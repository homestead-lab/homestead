"""How far an image pull has got, in bytes.

The kubelet says only "Pulling" and later "Pulled"; containerd knows more.
While it fetches an image it lists each layer in progress with the bytes it
has so far (ctr content active: the layer, its bytes, how long it has been
going), and a layer it has finished it simply holds (ctr content ls). The
registry says what the layers are and how big. ctr content info, which this
asked first, is gone from containerd 2, as k3s now ships it. So
for a pull under way Homestead starts a small pod on that node that prints,
every two seconds, the layers containerd has and those it is still fetching;
their sizes against the registry's make the percentage.

The watcher stops itself once every layer is there, or after half an hour,
and is removed once the pull is over.
"""
import hashlib
import json
import os
import re
import time

import homestead_names as NAMES
import homestead_runtime as RUNTIME
import homestead_shared as SHARED

NS = "lab"
DATA_DIR = "/data"
TASK = "pull-watch"
LIMIT = 30 * 60
kget = ksend = raw_get = None
layers_of = None     # (image ref, arch) -> [{"digest", "size"}] from its registry
_LAYERS = {}

UNITS = {"b": 1, "kb": 1000, "kib": 1024, "mb": 1000 ** 2, "mib": 1024 ** 2,
         "gb": 1000 ** 3, "gib": 1024 ** 3, "tb": 1000 ** 4, "tib": 1024 ** 4}
# "layer-sha256:<digest>  74.45MB  34 seconds": the bytes so far, then
# how long - one size, not two.
ACTIVE = re.compile(r"^\S*?sha256:([0-9a-f]{64})\s+([\d.]+)\s*([kKmMgGtT]?i?B)\b")


# What bind() puts back for an argument it is not given: the module's own default,
# not the last bind's (a test's fake must not outlive the test).
_UNBOUND = {"DATA_DIR": DATA_DIR}


def bind(_kget, _ksend, _raw_get, _layers_of, namespace="lab", data_dir=None):
    global kget, ksend, raw_get, layers_of, NS, DATA_DIR
    kget, ksend, raw_get, layers_of, NS = _kget, _ksend, _raw_get, _layers_of, namespace
    DATA_DIR = data_dir if data_dir else _UNBOUND["DATA_DIR"]


def _bytes(number, unit):
    return int(float(number) * UNITS.get(unit.lower(), 1))


def _pod_name(node, image):
    return "homestead-pull-watch-" + hashlib.sha256(f"{node}|{image}".encode()).hexdigest()[:14]


def _layers(image, arch):
    key = (image, arch)
    if key not in _LAYERS:
        _LAYERS[key] = layers_of(image, arch) or []
    return _LAYERS[key]


def script(digests):
    """Print the layers containerd has, and those it is fetching, until all
    are there or the time is up."""
    wanted = " ".join(digests)
    return "\n".join([
        f"end=$(( $(date +%s) + {LIMIT} ))",
        "while [ $(date +%s) -lt $end ]; do",
        "  echo \"== $(date +%s)\"; missing=0",
        f"  have=$({RUNTIME.CTR} content ls -q 2>/dev/null)",
        f"  for d in {wanted}; do",
        "    case \"$have\" in *\"$d\"*) echo \"HAVE $d\" ;; *) missing=1 ;; esac",
        "  done",
        f"  {RUNTIME.CTR} content active 2>/dev/null | tail -n +2",
        "  [ $missing = 0 ] && { echo '== done'; exit 0; }",
        "  sleep 2",
        "done"])


def parse(log, layers):
    """(bytes so far, bytes in all) from the watcher's last complete report."""
    sizes = {layer["digest"].split(":", 1)[-1]: int(layer.get("size") or 0) for layer in layers}
    total = sum(sizes.values())
    blocks = [b for b in (log or "").split("== ") if b.strip()]
    if not blocks:
        return 0, total
    finished = blocks[-1].startswith("done")
    # The last block may still be being written; the one before is whole.
    block = blocks[-2] if not finished and len(blocks) > 1 else blocks[-1]
    if finished:
        return total, total
    have, partial = set(), {}
    for line in block.splitlines():
        line = line.strip()
        if line.startswith("HAVE sha256:"):
            have.add(line.split(":", 1)[1])
            continue
        match = ACTIVE.match(line)
        if match:
            partial[match.group(1)] = _bytes(match.group(2), match.group(3))
    done = sum(size for digest, size in sizes.items() if digest in have)
    done += sum(min(size, sizes.get(digest, size)) for digest, size in partial.items()
                if digest in sizes and digest not in have)
    return done, total


def disposable(pod, namespace=None):
    """Only our bounded, read-only progress observer can waive the owner guard."""
    containers = (pod.get("spec") or {}).get("containers") or []
    command = (containers[0].get("command") or []) if len(containers) == 1 else []
    if len(command) != 3 or not isinstance(command[2], str):
        return False
    match = re.search(r"^  for d in (sha256:[0-9a-f]{64}(?: sha256:[0-9a-f]{64})*); do$", command[2], re.M)
    return bool(match) and RUNTIME.readonly_observer(pod, namespace or NS, TASK,
        r"homestead-pull-watch-[0-9a-f]{14}", ["sh", "-c", script(match[1].split())], LIMIT + 60)


def progress(node, image):
    """{"percent", "done_bytes", "total_bytes"} for a pull on node, starting
    the watcher on the first ask; {} when it cannot be known."""
    if not node or not image:
        return {}
    try:
        host = kget(f"/api/v1/nodes/{node}")
        # nodeName and the broad toleration bypass scheduling: do not recreate
        # observers after maintenance cordons the host and evicts its pods.
        if (host.get("spec") or {}).get("unschedulable"):
            return {}
        arch = ((host.get("metadata") or {}).get("labels") or {}).get("kubernetes.io/arch", "amd64")
        layers = _layers(image, arch)
    except Exception:
        return {}
    if not layers:
        return {}
    name = _pod_name(node, image)
    _asked(name)
    try:
        pod = kget(f"/api/v1/namespaces/{NS}/pods/{name}")
    except Exception:
        pod = None
    if not pod:
        body = RUNTIME.pod(name, NS, node, script([layer["digest"] for layer in layers]), TASK,
                           {NAMES.key("image"): image[:250]}, memory="64Mi", deadline=LIMIT + 60)
        body["spec"]["automountServiceAccountToken"] = False
        try:
            ksend("POST", f"/api/v1/namespaces/{NS}/pods", body)
        except Exception:
            return {}
        return {"percent": 0, "done_bytes": 0, "total_bytes": sum(int(l.get("size") or 0) for l in layers)}
    try:
        log = raw_get(f"/api/v1/namespaces/{NS}/pods/{name}/log?tailLines=120") or ""
    except Exception:
        log = ""
    done, total = parse(log, layers)
    return {"percent": int(done * 100 / total) if total else 0, "done_bytes": done, "total_bytes": total}


# When each watcher was last asked about: one nobody has asked after for a
# while belongs to a pull that is over, or a rollout nobody is following.
# Kept on the data volume too (ASKED_FILE), at most every SHARE_EVERY, since
# the jobs' progress is read through either copy of Homestead: one copy alone
# saw a watcher the other was using as idle, and removed it.
_ASKED = {}
_SHARED_AT = {}
IDLE = 90
SHARE_EVERY = 20
ASKED_FILE = "pull-watch-asked.json"


def _asked_path():
    return os.path.join(DATA_DIR, ASKED_FILE)


def _shared_asks():
    try:
        with open(_asked_path(), encoding="utf-8") as handle:
            found = json.load(handle)
        return {str(k): float(v) for k, v in found.items()} if isinstance(found, dict) else {}
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def _asked(name, now=None):
    now = time.time() if now is None else now
    _ASKED[name] = now
    if now - _SHARED_AT.get(name, 0) < SHARE_EVERY:
        return
    _SHARED_AT[name] = now
    asks = {k: v for k, v in _shared_asks().items() if now - v < LIMIT + 120}
    asks[name] = now
    try:
        SHARED.write_json(_asked_path(), asks)
    except (OSError, TypeError):
        pass


def sweep(now=None):
    """Remove watchers that finished, and those no one has asked about lately."""
    now = time.time() if now is None else now
    shared = _shared_asks()
    for pod in NAMES.find(f"/api/v1/namespaces/{NS}/pods", "task", TASK):
        if not disposable(pod):
            continue
        meta = pod.get("metadata") or {}
        name, phase = meta.get("name", ""), (pod.get("status") or {}).get("phase")
        last = max(_ASKED.setdefault(name, now), shared.get(name, 0))
        if phase not in ("Succeeded", "Failed") and now - last < IDLE:
            continue
        try:
            ksend("DELETE", f"/api/v1/namespaces/{NS}/pods/{name}?gracePeriodSeconds=0",
                  {"apiVersion": "v1", "kind": "DeleteOptions", "preconditions": {"uid": meta["uid"]}})
        except Exception:
            continue
        _ASKED.pop(name, None)
