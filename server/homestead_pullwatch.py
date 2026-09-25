"""How far an image pull has got, in bytes.

The kubelet says only "Pulling" and later "Pulled"; containerd knows more.
While it fetches an image it lists each layer in progress with the bytes it
has so far (ctr content active), and a layer it has finished it simply holds
(ctr content info). The registry says what the layers are and how big. So
for a pull under way Homestead starts a small pod on that node that prints,
every two seconds, the layers containerd has and those it is still fetching;
their sizes against the registry's make the percentage.

The watcher stops itself once every layer is there, or after half an hour,
and is removed once the pull is over.
"""
import hashlib
import json
import re
import time

import homestead_names as NAMES
import homestead_runtime as RUNTIME

NS = "lab"
TASK = "pull-watch"
LIMIT = 30 * 60
kget = ksend = raw_get = None
layers_of = None     # (image ref, arch) -> [{"digest", "size"}] from its registry
_LAYERS = {}

UNITS = {"b": 1, "kb": 1000, "kib": 1024, "mb": 1000 ** 2, "mib": 1024 ** 2,
         "gb": 1000 ** 3, "gib": 1024 ** 3, "tb": 1000 ** 4, "tib": 1024 ** 4}
ACTIVE = re.compile(r"^\S*?sha256:([0-9a-f]{64})\s+([\d.]+)\s*([kKmMgGtT]?i?B)\s+([\d.]+)\s*([kKmMgGtT]?i?B)")


def bind(_kget, _ksend, _raw_get, _layers_of, namespace="lab"):
    global kget, ksend, raw_get, layers_of, NS
    kget, ksend, raw_get, layers_of, NS = _kget, _ksend, _raw_get, _layers_of, namespace


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
        f"  for d in {wanted}; do",
        f"    if {RUNTIME.CTR} content info $d >/dev/null 2>&1; then echo \"HAVE $d\"; else missing=1; fi",
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


def progress(node, image):
    """{"percent", "done_bytes", "total_bytes"} for a pull on node, starting
    the watcher on the first ask; {} when it cannot be known."""
    if not node or not image:
        return {}
    try:
        arch = ((kget(f"/api/v1/nodes/{node}").get("metadata") or {}).get("labels") or {}).get(
            "kubernetes.io/arch", "amd64")
        layers = _layers(image, arch)
    except Exception:
        return {}
    if not layers:
        return {}
    name = _pod_name(node, image)
    _ASKED[name] = time.time()
    try:
        pod = kget(f"/api/v1/namespaces/{NS}/pods/{name}")
    except Exception:
        pod = None
    if not pod:
        body = RUNTIME.pod(name, NS, node, script([layer["digest"] for layer in layers]), TASK,
                           {NAMES.key("image"): image[:250]}, memory="48Mi", deadline=LIMIT + 60)
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
_ASKED = {}
IDLE = 90


def sweep(now=None):
    """Remove watchers that finished, and those no one has asked about lately."""
    now = now or time.time()
    for pod in NAMES.find(f"/api/v1/namespaces/{NS}/pods", "task", TASK):
        meta = pod.get("metadata") or {}
        name, phase = meta.get("name", ""), (pod.get("status") or {}).get("phase")
        if phase not in ("Succeeded", "Failed") and now - _ASKED.get(name, now) < IDLE:
            continue
        try:
            ksend("DELETE", f"/api/v1/namespaces/{NS}/pods/{name}?gracePeriodSeconds=0")
        except Exception:
            pass
        _ASKED.pop(name, None)
