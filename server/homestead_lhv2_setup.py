"""Reviewed V2 host preparation; Kubernetes Jobs survive a Homestead restart.

Host tasks never restart Kubernetes or touch disks. Reboots use the existing
maintenance review, and enabling is a separate action after fresh evidence.
"""
import hashlib
import json
import re
import urllib.error

import homestead_lhcapacity as CAP
import homestead_hostrun as HOST
import homestead_pod_resources as R

LH = CAP.LH
LABEL = "homestead.io/longhorn-v2-setup"
KIND = "longhorn-v2-prepare"
kget = ksend = status = ops = None
NS = "lab"


def bind(read, send, observe, operations, namespace):
    global kget, ksend, status, ops, NS
    kget, ksend, status, ops, NS = read, send, observe, operations, namespace


def _get(path):
    try:
        return kget(path)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


def _items(path):
    result = kget(path)
    if not isinstance(result.get("items"), list) or result.get("metadata", {}).get("continue"):
        raise ValueError("V2 setup inventory is incomplete; check again")
    return result["items"]


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _value(obj, fallback=""):
    return (obj or {}).get("value") or (obj or {}).get("default") or fallback


def memory_mib():
    enabled = _get(LH + "/settings/data-engine-hugepage-enabled")
    if enabled:
        value = json.loads(_value(enabled, '{}')).get("v2", "true")
        if value not in ("true", "false"):
            raise ValueError("Longhorn's hugepage configuration is unrecognized")
        if value == "false":
            return 0
    setting = _get(LH + "/settings/data-engine-memory-size")
    value = (json.loads(_value(setting, '{}')).get("v2", "2048") if setting else
             _value(_get(LH + "/settings/v2-data-engine-hugepage-limit"), "2048"))
    if not re.fullmatch(r"[0-9]+", str(value)) or not 2 <= int(value) <= 1048576 or int(value) % 2:
        raise ValueError("Longhorn's V2 memory size must be an even number of MiB")
    return int(value)


def cpu_mask():
    """V2's polling cores - (cores, CPU isolation on, the setting's value) -
    or None where Longhorn has no such setting. With isolation on, SPDK keeps
    those cores to itself and refuses to start when they are all the host
    has: Longhorn 1.13's default of two (0x3) never starts on a 2-CPU host."""
    setting = _get(LH + "/settings/data-engine-cpu-mask")
    if not setting:
        return None
    raw = _value(setting, "")
    try:
        mask = json.loads(raw).get("v2", "") if raw.strip().startswith("{") else raw.strip()
    except ValueError:
        raise ValueError("Longhorn's V2 CPU mask is unrecognized") from None
    if not re.fullmatch(r"0x[0-9a-fA-F]+", str(mask)) or int(mask, 16) == 0:
        raise ValueError("Longhorn's V2 CPU mask is unrecognized")
    isolation = _get(LH + "/settings/data-engine-cpu-isolation-enabled")
    try:
        isolated = bool(isolation) and json.loads(_value(isolation, "{}")).get("v2", "false") == "true"
    except ValueError:
        isolated = False
    return bin(int(mask, 16)).count("1"), isolated, raw


def v2_cpu_percent():
    """The share of a host's CPU Longhorn reserves for V2's instance manager."""
    setting = _get(LH + "/settings/guaranteed-instance-manager-cpu")
    try:
        value = json.loads(_value(setting, "{}")).get("v2", "12") if setting else "12"
        return max(0.0, min(40.0, float(value)))
    except (TypeError, ValueError):
        return 12.0


def _cpus(node):
    try:
        return int(R.quantity(((node.get("status") or {}).get("capacity") or {}).get("cpu"), "cpu") // 1000)
    except (TypeError, ValueError):
        return 0


def _job_state(job):
    state = job.get("status", {})
    if any(c.get("type") == "Failed" and c.get("status") == "True" for c in state.get("conditions", [])):
        return "failed"
    if state.get("succeeded"):
        return "succeeded"
    return "running"


def plan():
    base = status()
    setting = kget(LH + "/settings/v2-data-engine")
    enabled = _value(setting) == "true"
    harvester = _get(CAP.HARVESTER_V2)
    distribution = base.get("distribution", "")
    if distribution == "harvester" and harvester is None:
        raise ValueError("Harvester's V2 setting could not be verified")
    managed = harvester is not None
    required = memory_mib()
    kube = {n["metadata"]["name"]: n for n in _items("/api/v1/nodes")}
    lh_nodes = _items(LH + "/nodes")
    probes = {n["name"]: n for n in base.get("nodes", [])}
    jobs = _items(f"/apis/batch/v1/namespaces/{NS}/jobs?labelSelector={LABEL}%3Dtrue")
    pods = _items("/api/v1/pods")
    managers = _items(LH + "/instancemanagers")
    saved = {i.get("ref", {}).get("name"): i.get("ref", {}) for i in ops._read() if i.get("kind") == KIND}
    cpu_percent = v2_cpu_percent()
    rows = []
    for lh_node in sorted(lh_nodes, key=lambda n: n["metadata"]["name"]):
        name = lh_node["metadata"]["name"]
        node = kube.get(name)
        if not node or not node["metadata"].get("uid"):
            raise ValueError(f"Kubernetes identity for {name} is unavailable")
        node_status = node.get("status", {})
        info = node_status.get("nodeInfo", {})
        boot = info.get("bootID", "")
        capacity = R.quantity(node_status.get("capacity", {}).get("hugepages-2Mi"), "storage") // 1048576
        allocatable = R.quantity(node_status.get("allocatable", {}).get("hugepages-2Mi"), "storage") // 1048576
        used = sum(R.pod_request(p.get("spec", {}), "hugepages-2Mi") for p in pods if p.get("spec", {}).get("nodeName") == name
                   and p.get("status", {}).get("phase") not in ("Failed", "Succeeded"))
        target_pages = (required * 1048576 + used + 2097151) // 2097152 if required else 0
        identity = {"node": name, "node_uid": node["metadata"]["uid"], "boot_id": boot,
                    "required_mib": required, "target_pages": target_pages, "distribution": distribution, "version": 1}
        matching = [j for j in jobs if j.get("metadata", {}).get("annotations", {}).get("homestead.io/node-uid") == identity["node_uid"]]
        matching.sort(key=lambda j: j["metadata"].get("creationTimestamp", ""), reverse=True)
        job = matching[0] if matching else None
        annotations = job.get("metadata", {}).get("annotations", {}) if job else {}
        current_job = job if annotations.get("homestead.io/required-mib") == str(required) and int(annotations.get("homestead.io/target-pages", '-1')) >= target_pages else None
        proof = saved.get(job["metadata"]["name"], {}) if job else {}
        configured = bool(current_job and proof.get("node_uid") == identity["node_uid"]
                          and _matches(proof, current_job) and _job_state(current_job) == "succeeded")
        just_verified = configured and annotations.get("homestead.io/boot-id") == boot
        checks = probes.get(name, {}).get("checks", {})
        cpu = checks.get("cpu") is True or just_verified
        modules = checks.get("modules") is True or just_verified
        ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in node_status.get("conditions", []))
        engine_ready = any(m.get("spec", {}).get("nodeID") == name and m.get("spec", {}).get("dataEngine") == "v2"
                           and m.get("status", {}).get("currentState") == "running" for m in managers)
        memory_ready = required == 0 or (capacity >= required and allocatable * 1048576 - (0 if enabled else used) >= required * 1048576)
        supported = distribution in ("k3s", "rke2") and info.get("operatingSystem") == "linux" and bool(re.fullmatch(r"[a-fA-F0-9-]{32,36}", boot))
        # V2's instance manager reserves a share of the host's CPU; on a small
        # host whose control plane has taken the rest it is never scheduled.
        cpu_alloc = R.quantity(node_status.get("allocatable", {}).get("cpu"), "cpu")
        cpu_used = sum(R.pod_request(p.get("spec", {}), "cpu") for p in pods if p.get("spec", {}).get("nodeName") == name
                       and p.get("status", {}).get("phase") not in ("Failed", "Succeeded"))
        cpu_need = int(cpu_alloc * cpu_percent / 100 + 0.999)
        problems = ([] if ready else ["Host is not Ready"]) + ([] if cpu else ["CPU support needs verification"]) + ([] if modules else ["Kernel modules need preparation"])
        if cpu_alloc and not engine_ready and cpu_alloc - cpu_used < cpu_need:
            problems.append(f"V2's instance manager reserves {cpu_need}m of CPU and Kubernetes has {max(0, cpu_alloc - cpu_used)}m "
                            f"left on this host: free some, give the host more CPUs, or lower guaranteed-instance-manager-cpu")
        if not memory_ready:
            problems.append(f"Kubernetes reports {capacity} MiB capacity / {allocatable} MiB allocatable; V2 needs {required} MiB plus other hugepage requests")
        if not (managed or configured or engine_ready):
            problems.append("Run host preparation to verify nvme-cli and persistent configuration")
        if not managed and not supported and not engine_ready:
            problems.append("Automatic preparation supports Linux k3s/rke2 hosts with a verified boot ID")
        active = bool(job and _job_state(job) == "running")
        rows.append({**identity, "review_token": _hash(identity), "capacity_mib": capacity, "allocatable_mib": allocatable,
                     "ready": ready, "cpu": cpu, "modules": modules, "memory_ready": memory_ready,
                     "configured": configured, "engine_ready": engine_ready, "block_disks": probes.get(name, {}).get("block_disks", 0),
                     "can_prepare": not managed and not enabled and supported and ready and checks.get('cpu') is not False and not active,
                     "needs_reboot": configured and not memory_ready,
                     "problems": problems, "job": {"name": job["metadata"]["name"], "state": _job_state(job)} if job else None})
    blockers = [] if base.get("longhorn_ok") else ["Verify Longhorn 1.8 or newer before continuing"]
    # SPDK's polling cores must leave each host at least one of its own.
    mask, cpu_fix = cpu_mask(), None
    counts = {row["node"]: _cpus(kube[row["node"]]) for row in rows}
    if mask and mask[1] and counts and all(counts.values()):
        fewest = min(counts.values())
        if mask[0] >= fewest:
            if fewest < 2:
                blockers.extend(f"{name}: Longhorn V2 needs at least 2 CPUs - one to poll, one kept for the host - and it has {n}"
                                for name, n in counts.items() if n < 2)
            else:
                cores = fewest - 1
                value = f"0x{(1 << cores) - 1:x}"
                cpu_fix = {"from": mask[2], "to": json.dumps({"v2": value}) if mask[2].strip().startswith("{") else value,
                           "cores": cores, "was": mask[0], "host_cpus": fewest}
    if not rows:
        blockers.append("No Longhorn hosts were found")
    if not managed:
        blockers.extend(f"{n['node']}: {p}" for n in rows for p in n["problems"])
    out = {"namespace": NS, "enabled": enabled, "harvester": managed, "harvester_requested": managed and _value(harvester) == "true",
           "distribution": distribution, "required_mib": required, "nodes": rows, "blockers": blockers,
           "can_enable": not enabled and not blockers and not (managed and _value(harvester) == "true"), "engine_ready": bool(rows) and enabled and all(n["ready"] and n["engine_ready"] for n in rows),
           "cpu_mask_fix": None if managed else cpu_fix}
    out["review_token"] = _hash(out)
    return out


# Fixed host code, with only validated integers and a boot UUID substituted.
# Never lower an existing pool, edit disks, restart services, or reboot here.
SCRIPT = r'''set -eu
fail() { echo "V2 setup: $*" >&2; exit 1; }
[ "$(cat /proc/sys/kernel/random/boot_id)" = '__BOOT__' ] || fail 'Host restarted or changed; review again'
case "$(uname -m)" in x86_64) grep -qw sse4_2 /proc/cpuinfo || fail 'CPU lacks SSE4.2';; aarch64) :;; *) fail 'Unsupported CPU architecture';; esac
[ -d /run/systemd/system ] || fail 'Use this OS vendor preparation procedure; systemd is unavailable'
. /etc/os-release
case "$ID" in ubuntu|debian|rhel|rocky|almalinux|fedora|centos|opensuse-leap|sles) :;; *) fail 'This host OS needs manual V2 preparation';; esac
echo 'V2 setup: installing nvme-cli if missing'
if ! command -v nvme >/dev/null 2>&1; then
  if command -v apt-get >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l
    apt-get update && apt-get install -y --no-install-recommends nvme-cli
  elif command -v dnf >/dev/null 2>&1; then dnf install -y nvme-cli
  elif command -v zypper >/dev/null 2>&1; then zypper --non-interactive install nvme-cli
  else fail 'Install nvme-cli with this OS package manager'; fi
fi
nvme version
echo 'V2 setup: loading kernel modules'
for module in vfio_pci uio_pci_generic nvme_tcp; do
  if ! modprobe "$module"; then
    if [ "$ID" = ubuntu ] && command -v apt-get >/dev/null 2>&1; then
      export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l
      apt-get update && apt-get install -y --no-install-recommends "linux-modules-extra-$(uname -r)"
      modprobe "$module" || fail "Kernel module $module is unavailable"
    else fail "Install kernel module $module for the running kernel, then retry"; fi
  fi
done
mkdir -p /etc/modules-load.d
[ ! -L /etc/modules-load.d/99-homestead-longhorn-v2.conf ] || fail 'Managed modules file is a symlink'
printf 'vfio_pci\nuio_pci_generic\nnvme_tcp\n' > /etc/modules-load.d/99-homestead-longhorn-v2.conf
required=__PAGES__
if [ "$required" -gt 0 ]; then
  [ "$(awk '/^Hugepagesize:/ {print $2}' /proc/meminfo)" = 2048 ] || fail 'Default hugepage size is not 2 MiB; configure the 2 MiB pool manually'
  pool=/sys/kernel/mm/hugepages/hugepages-2048kB
  current=$(cat "$pool/nr_hugepages")
  free=$(cat "$pool/free_hugepages")
  reserved=$(cat "$pool/resv_hugepages")
  minimum=$((__V2PAGES__ + current - free + reserved))
  [ "$required" -ge "$minimum" ] || required=$minimum
  [ "$required" -ge "$current" ] || required=$current
  config=/etc/sysctl.d/99-homestead-longhorn-v2.conf
  [ ! -L "$config" ] || fail 'Managed hugepage file is a symlink'
  if [ -f "$config" ]; then
    prior=$(awk -F= '/^vm.nr_hugepages=[0-9]+$/ {print $2}' "$config" | tail -n 1)
    case "$prior" in ''|*[!0-9]*) fail 'Managed hugepage file needs manual review';; esac
    [ "$required" -ge "$prior" ] || required=$prior
  fi
  extra=$(((required - current) * 2048))
  available=$(awk '/^MemAvailable:/ {print $2}' /proc/meminfo)
  [ "$extra" -eq 0 ] || [ "$available" -ge $((extra + 1048576)) ] || fail 'Insufficient available RAM; leave at least 1 GiB free after reserving hugepages'
  mkdir -p /etc/sysctl.d
  config=/etc/sysctl.d/99-homestead-longhorn-v2.conf
  [ ! -L "$config" ] || fail 'Managed hugepage file is a symlink'
  printf 'vm.nr_hugepages=%s\n' "$required" > "$config"
  echo "V2 setup: reserving $required hugepages of 2 MiB; existing reservations are preserved"
  sysctl -w "vm.nr_hugepages=$required" || fail 'Could not reserve hugepages; inspect the saved configuration before rebooting'
  actual=$(cat "$pool/nr_hugepages")
  if [ "$actual" -lt "$required" ]; then echo 'V2 setup: allocation is partial; a reviewed host reboot is required'; fi
fi
echo 'V2 setup: host configuration saved. Recheck Kubernetes capacity before enabling; a reviewed reboot may be needed.'
'''


def _body(ref):
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,252}", ref["node"]) or not re.fullmatch(r"[a-fA-F0-9-]{32,36}", ref["boot_id"]):
        raise ValueError("Invalid V2 host identity")
    if type(ref["target_pages"]) is not int or not 0 <= ref["target_pages"] <= 1048576 or type(ref["required_mib"]) is not int:
        raise ValueError("Invalid V2 memory requirement")
    script = SCRIPT.replace('__BOOT__', ref['boot_id']).replace('__PAGES__', str(ref['target_pages'])).replace('__V2PAGES__', str(ref['required_mib'] // 2))
    return {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": ref["name"], "namespace": NS,
        "labels": {LABEL: "true"}, "annotations": {"homestead.io/node-uid": ref["node_uid"], "homestead.io/approval": ref["approval"],
            "homestead.io/required-mib": str(ref['required_mib']), "homestead.io/target-pages": str(ref['target_pages']), "homestead.io/boot-id": ref['boot_id']}},
        "spec": {"backoffLimit": 0, "activeDeadlineSeconds": 900, "ttlSecondsAfterFinished": 604800,
            "template": {"metadata": {"labels": {LABEL: "true"}}, "spec": {"nodeName": ref["node"], "hostPID": True,
                "hostNetwork": True, "hostIPC": True, "automountServiceAccountToken": False, "restartPolicy": "Never",
                "tolerations": [{"operator": "Exists"}], "containers": [{"name": "prepare", "image": HOST.IMAGE,
                    "command": HOST.HOST + [script], "securityContext": {"privileged": True},
                    "resources": {"requests": {"cpu": "10m", "memory": "64Mi"}, "limits": {"memory": HOST.MEMORY_LIMIT}}}]}}}}


def _matches(ref, job):
    if not ref:
        return False
    body = _body(ref)
    actual = job.get('spec', {}).get('template', {}).get('spec', {})
    expected = body['spec']['template']['spec']
    return (job.get('metadata', {}).get('annotations') == body['metadata']['annotations']
            and job.get('metadata', {}).get('labels', {}).get(LABEL) == 'true'
            and all(actual.get(k) == expected[k] for k in ('nodeName', 'hostPID', 'hostNetwork', 'hostIPC', 'automountServiceAccountToken'))
            and len(actual.get('containers', [])) == 1
            and all(actual['containers'][0].get(k) == expected['containers'][0][k]
                    for k in ('name', 'image', 'command', 'securityContext')))


def _ensure(ref, create=False):
    node = kget('/api/v1/nodes/' + ref['node'])
    if node['metadata']['uid'] != ref['node_uid']:
        raise ValueError("Host identity changed; review V2 setup again")
    path = f"/apis/batch/v1/namespaces/{NS}/jobs/{ref['name']}"
    job = _get(path)
    body = _body(ref)
    if not job:
        if not create:
            raise ValueError('The host setup Job is missing. Check saved tasks before reviewing another attempt.')
        if node.get('status', {}).get('nodeInfo', {}).get('bootID') != ref['boot_id']:
            raise ValueError('Host restarted; review V2 setup again')
        try:
            return ksend('POST', path.rsplit('/', 1)[0], body)
        except urllib.error.HTTPError as error:
            if error.code != 409:
                raise
            job = kget(path)
    if not _matches(ref, job):
        raise ValueError('Existing V2 setup job has different settings')
    return job


def prepare(body):
    if body.get('confirm') is not True or not re.fullmatch(r'[a-f0-9]{24}', str(body.get('request_id', ''))):
        raise ValueError('Review and confirm the host preparation tasks first')
    with ops._lock:
        current = plan()
        row = next((n for n in current['nodes'] if n['node'] == body.get('node')), None)
        if not row or row['review_token'] != body.get('review_token'):
            raise ValueError('Host setup requirements changed; review again')
        name = 'homestead-v2-' + body['request_id']
        saved = ops._read()
        existing = next((i for i in saved if i.get('kind') == KIND and i.get('ref', {}).get('name') == name), None)
        if existing:
            if existing['ref'].get('approval') != row['review_token']:
                raise ValueError('This request ID belongs to another review')
            return {'operation': ops._public(existing)}
        if any(i.get('kind') in (KIND, 'node-power', 'host-os') and i.get('status') not in ops.TERMINAL and i.get('ref', {}).get('node') == row['node'] for i in saved):
            raise ValueError('A host setup or maintenance task is already active; inspect Jobs first')
        if current['harvester'] or not row['can_prepare']:
            raise ValueError('Host preparation is unavailable or already running; check its saved task')
        ref = {k: row[k] for k in ('node', 'node_uid', 'boot_id', 'required_mib', 'target_pages', 'distribution', 'version')}
        ref.update(name=name, namespace=NS, approval=row['review_token'])
        op = ops.start(KIND, 'Prepare Longhorn V2 on ' + row['node'], {'kind': 'Node', 'name': row['node']},
                       '/settings?tab=hardware', ref, 'Queued host prerequisite setup; no reboot or disk changes')
        _ensure(ref, create=True)
        return {'operation': op}


def progress(item):
    try:
        job = _ensure(item['ref'])
    except ValueError as error:
        return 'failed', item.get('progress', 0), str(error)
    state = _job_state(job)
    if state == 'failed':
        return 'failed', 100, 'Host preparation failed; inspect its log before retrying. Partial configuration is retained.'
    if state == 'succeeded':
        return 'succeeded', 100, 'Host configured. Recheck Kubernetes hugepage capacity; review a reboot if needed.'
    return 'running', 25 if job.get('status', {}).get('active') else 5, 'Preparing packages, modules and hugepages; open the task log for live steps'


def enable(body):
    current = plan()
    if body.get('confirm') is not True or body.get('review_token') != current['review_token']:
        raise ValueError('V2 setup changed; check and confirm the enable step again')
    if not current['can_enable']:
        raise ValueError('; '.join(current['blockers']) or 'V2 is already enabled')
    fix = current.get('cpu_mask_fix')
    if fix:
        # Fewer polling cores, so each host keeps one: SPDK refuses otherwise.
        setting = kget(LH + '/settings/data-engine-cpu-mask')
        if _value(setting, '') != fix['from']:
            raise ValueError("Longhorn's V2 CPU mask changed; check and confirm the enable step again")
        setting['value'] = fix['to']
        ksend('PUT', LH + '/settings/data-engine-cpu-mask', setting)
    CAP.save({'v2': True}, allow_v2_enable=True)
    return plan()


# Its routes and who may use them (homestead_routes.py).
ROUTES = {
    ("GET", "/api/longhorn/v2/plan"): ("admin", lambda request: plan()),
    ("POST", "/api/longhorn/v2/prepare"): ("admin", lambda request: prepare(request.body)),
}
