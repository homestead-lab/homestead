"""Make an app host a server, in place (#364 step 5).

The host keeps its identity: k3s-agent-uninstall.sh leaves
/etc/rancher/node/password, so the server install registers as the same
Node, with its labels, Longhorn node and replicas. Removing the host and
joining it again would discard its replicas instead.

plan() checks first. start() files a job; a thread cordons and drains the
host, reads the server token from a server host, and launches a detached
systemd unit on the host that uninstalls the agent and installs a server
- it has to be detached, since the uninstall stops Homestead's own helper
pod there. The job's resolver follows the unit's status file, then waits
for the same Node to come back as a Ready etcd member."""
import hashlib
import json
import os
import re
import shlex
import threading
import time
import urllib.error

import homestead_capacity as CAPACITY
import homestead_leader as LEADER
import homestead_node_actions as NODE_ACTIONS
import homestead_maintenance as MAINTENANCE
import homestead_operations as OPS
import homestead_platform as PLATFORM
import homestead_roles as ROLES
import homestead_self as SELF
import homestead_hostrun as HOSTRUN

KIND = "host-promote"
UNIT = "homestead-promote"
DIR = "/var/lib/homestead"
SCRIPT = f"{DIR}/promote.sh"
TOKEN = f"{DIR}/promote.token"
STATUS = f"{DIR}/promote.status"
JOB = f"{DIR}/promote.job"
INSTALLER = f"{DIR}/k3s-install.sh"
LOG = "/var/log/homestead-promote.log"
NODE_PASSWORD = "/etc/rancher/node/password"
TOKEN_FILE = "/var/lib/rancher/k3s/server/node-token"
# The host's own settings a server keeps; anything else it was started with
# is named in the plan, since the reinstall drops it.
NODE_VALUE_FLAGS = ("--node-name", "--node-ip", "--node-external-ip", "--flannel-iface", "--node-label", "--node-taint",
                    "--kubelet-arg")
# A Node named from a random id would come back as another Node.
RENAMING_FLAGS = ("--with-node-id",)
# What every agent is started with, and the server replaces.
EXPECTED_FLAGS = ("--server", "--token", "--token-file", "--agent-token")
TOKEN_SHAPE = re.compile(r"^[A-Za-z0-9:._-]+$")
K3S_DOMAINS = ("kubernetes.io", "k3s.io")


def _args(node):
    try:
        args = json.loads(((node.get("metadata") or {}).get("annotations") or {}).get("k3s.io/node-args") or "[]")
    except ValueError:
        return []
    return [str(a) for a in args] if isinstance(args, list) else []


def node_flags(node):
    """(flags to keep, shell-quoted; names of the flags dropped) from the
    host's own k3s.io/node-args. Without --node-ip there, its InternalIP."""
    args, keep, dropped, i = _args(node), [], [], 0
    while i < len(args):
        flag, value = args[i], None
        i += 1
        if not flag.startswith("--"):
            continue  # "agent", the command itself
        if "=" in flag:
            flag, value = flag.split("=", 1)
        takes = flag in NODE_VALUE_FLAGS or flag in EXPECTED_FLAGS
        if value is None and takes and i < len(args):
            value, i = args[i], i + 1
        if flag in NODE_VALUE_FLAGS:
            keep += [flag, shlex.quote(value or "")]
        elif flag not in EXPECTED_FLAGS and flag not in dropped:
            dropped.append(flag)
    if "--node-ip" not in keep:
        ip = next((a.get("address") for a in (node.get("status") or {}).get("addresses") or []
                   if a.get("type") == "InternalIP"), "")
        if ip:
            keep = ["--node-ip", shlex.quote(ip)] + keep
    return keep, dropped


def own_labels(node):
    """The Node's labels that k3s and Kubernetes do not set themselves."""
    out = {}
    for key, value in ((node.get("metadata") or {}).get("labels") or {}).items():
        domain = key.split("/", 1)[0] if "/" in key else ""
        if any(domain == d or domain.endswith("." + d) for d in K3S_DOMAINS):
            continue
        out[key] = value
    return out


def unprintable(values):
    """Whether any value carries a control character. The script goes to the
    host inside a quoted heredoc: a line break and the heredoc's end word in
    a value read from a Node's annotations would end it early, and what came
    after would run as root. Quoting alone does not stop that."""
    return any(ord(c) < 32 or ord(c) == 127 for v in values for c in str(v))


def promote_script(version, server, args):
    """What the unit runs on the host. The installer is fetched before
    anything is removed (exit 20: nothing changed); the node password is
    checked after the uninstall (exit 10). The token is deleted whatever
    happens. k3s is left as the installer leaves it: stopping a server that
    joined etcd never protects quorum, and with one member before it would
    lose it."""
    if unprintable([version, server, *args]):
        raise ValueError("A setting read from the cluster has a control character in it; nothing was changed")
    return "\n".join([
        "set -eu",
        f"trap 'rm -f {TOKEN}' EXIT",
        f"curl -sfL https://get.k3s.io -o {INSTALLER} || exit 20",
        "/usr/local/bin/k3s-agent-uninstall.sh",
        f"[ -s {NODE_PASSWORD} ] || {{ echo 'The node password went with the agent; not joining as a new node'; exit 10; }}",
        f"INSTALL_K3S_VERSION={shlex.quote(version)} K3S_TOKEN=\"$(cat {TOKEN})\" "
        f"sh {INSTALLER} server --server {shlex.quote(f'https://{server}:6443')}" + "".join(f" {a}" for a in args),
        "",
    ])


def check_token(token):
    """The token as k3s writes it, or ValueError - which never repeats it."""
    if not TOKEN_SHAPE.match(token or ""):
        raise ValueError("The server token read from the cluster is not in the shape k3s writes; nothing was changed")
    return token


def launch_script(op_id, length, body):
    """Run once through hostrun, the token on its stdin (never on a command
    line): write the script, then the token, root-only, and start the unit.
    One launch at a time (flock where the host has it); none if this job
    started the unit already. The token goes again if the unit does not start."""
    if not re.match(r"^[A-Za-z0-9]+$", op_id) or not isinstance(length, int) or length <= 0:
        raise ValueError("bad job id or token length")
    return "\n".join([
        "set -e",
        "umask 077",
        f"mkdir -p {DIR}",
        f"exec 9> {DIR}/promote.lock",
        "if command -v flock >/dev/null; then flock -n 9 || { echo BUSY; exit 1; }; fi",
        f'[ "$(cat {JOB} 2>/dev/null)" = {op_id} ] && {{ echo STARTED; exit 0; }}',
        f"rm -f {STATUS}",
        f"cat > {SCRIPT} <<'HSSCRIPT'", body.rstrip("\n"), "HSSCRIPT",
        f"trap 'rm -f {TOKEN}' EXIT",
        f"head -c {length} > {TOKEN}",
        f'[ "$(wc -c < {TOKEN})" -eq {length} ]',
        f"echo {op_id} > {JOB}",
        f"systemd-run --unit={UNIT} --collect --quiet sh -c 'sh {SCRIPT} > {LOG} 2>&1; echo $? > {STATUS}' "
        f"|| {{ rm -f {JOB}; exit 1; }}",
        "trap - EXIT",
        "echo STARTED",
        "",
    ])


LONGHORN = "/apis/longhorn.io/v1beta2"
# Jobs that restart or change a host: none may run while a host is reinstalled.
HOST_JOBS = ("node-power", "host-os", "os-rollout", "cluster-shutdown", "platform-upgrade")
SERVER_ROLES = ("node-role.kubernetes.io/control-plane", "node-role.kubernetes.io/master", "node-role.kubernetes.io/etcd")
# One host command for the checks only the host can answer.
CHECK = (f"[ -s {NODE_PASSWORD} ] && echo PASSWORD; "
         "[ -x /usr/local/bin/k3s-agent-uninstall.sh ] && echo UNINSTALL; true")


def _own_node():
    """The node this Homestead pod runs on, as server.own_node says."""
    try:
        return kget(f"/api/v1/namespaces/{SELF.NS}/pods/{os.environ.get('HOSTNAME', '')}")["spec"]["nodeName"]
    except Exception:
        return ""


# server.py sits at its line cap, so this module wires itself: Kubernetes
# through the client hostrun is bound with, looked up at each call. bind()
# is for tests.
kget = lambda path, *a, **k: HOSTRUN.kget(path, *a, **k)  # noqa: E731
ksend = lambda *a, **k: HOSTRUN.ksend(*a, **k)  # noqa: E731
hostrun = HOSTRUN
own_node = _own_node


def bind(_kget, _ksend, _hostrun, _own_node):
    global kget, ksend, hostrun, own_node
    kget, ksend, hostrun, own_node = _kget, _ksend, _hostrun, _own_node


def _name(n):
    return (n.get("metadata") or {}).get("name", "")


def _server(n):
    labels = (n.get("metadata") or {}).get("labels") or {}
    return any(k in labels for k in SERVER_ROLES)


def _ready(n):
    return any(c.get("type") == "Ready" and c.get("status") == "True"
               for c in (n.get("status") or {}).get("conditions") or [])


def _version(n):
    return ((n.get("status") or {}).get("nodeInfo") or {}).get("kubeletVersion", "")


def _exists(path):
    try:
        kget(path)
        return True
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return False
        raise


def _replicas_on(node):
    try:
        rows = kget(f"{LONGHORN}/replicas").get("items", [])
    except Exception:
        return None
    return sum(1 for r in rows if (r.get("spec") or {}).get("nodeID") == node)


def _review(node, facts, blockers):
    """The plan as a person approved it: what start() must find again."""
    canon = json.dumps({"node": node, **{k: facts[k] for k in ("uid", "version", "server", "settings", "host_settings", "members")},
                        "blockers": blockers}, sort_keys=True)
    return hashlib.sha256(canon.encode()).hexdigest()


def plan(node):
    """What making node a server would do, and what stops it."""
    nodes = kget("/api/v1/nodes").get("items", [])
    host = next((n for n in nodes if _name(n) == node), None)
    if host is None:
        raise ValueError(f"{node} is not a host in this cluster")
    p = PLATFORM.detect(True) or {}
    q = NODE_ACTIONS.quorum_report()
    members = list(q.get("members") or [])
    blockers, warnings = [], []
    if p.get("harvester") or p.get("distribution") != "k3s":
        blockers.append("Only a k3s app host can be made a server here")
    elif not members:
        blockers.append("This cluster keeps its state outside etcd: a server would add no vote")
    if _server(host):
        blockers.append(f"{node} is a server already")
    elif ROLES.is_tiebreaker(host):
        blockers.append(f"{node} is marked as a tiebreaker; app hosts only")
    if not _ready(host):
        blockers.append(f"{node} is not Ready")
    if own_node() == node:
        blockers.append(f"Homestead runs on {node}: move it to another host first (Host actions › Move all off), then review again")
    shared, known = PLATFORM.shared_server_args(nodes)
    if not known:
        blockers.append("The servers' shared settings could not be read from k3s.io/node-args")
    version = _version(host)
    versions = sorted({_version(n) for n in nodes if _server(n)})
    if versions != [version]:
        blockers.append(f"{node} runs k3s {version or 'unknown'}, the servers {', '.join(versions) or 'unknown'}: "
                        "upgrade so the versions match first")
    down = [m for m in members if m not in (q.get("ready") or [])]
    if down:
        blockers.append(f"etcd member{'s' if len(down) != 1 else ''} {', '.join(down)} not Ready: "
                        "the cluster takes no new member while short of a vote")
    jobs = [o for o in OPS.snapshot() if o.get("status") not in OPS.TERMINAL]
    if any(o.get("kind") == KIND for o in jobs):
        blockers.append("A host is being made a server already")
    for kind in sorted({o.get("kind") for o in jobs if o.get("kind") in HOST_JOBS}):
        blockers.append(f"A {kind} job is running: a restart or update during the reinstall could cost a vote. "
                        "Wait for it to finish")
    forecast = next((h for h in CAPACITY.every_host().get("hosts", []) if h.get("node") == node), None)
    if forecast is None or forecast.get("verdict") in ("waits", "no-host", "unknown"):
        blockers.append((forecast or {}).get("sentence") or f"Could not work out where {node}'s apps would go")
    elif forecast.get("verdict") == "tight":
        warnings.append(forecast.get("sentence", ""))
    if not _exists(f"/api/v1/namespaces/kube-system/secrets/{node}.node-password.k3s"):
        blockers.append(f"kube-system/{node}.node-password.k3s is missing: the host would not register as the same Node")
    if _ready(host):
        out, _ = hostrun.run(node, CHECK, timeout=45, wait=30)
        if "PASSWORD" not in out:
            blockers.append(f"{node} has no node password at {NODE_PASSWORD}: it would register as a new Node")
        if "UNINSTALL" not in out:
            blockers.append(f"{node} has no /usr/local/bin/k3s-agent-uninstall.sh: was k3s installed another way?")
    host_settings, dropped = node_flags(host)
    if unprintable([version, (p.get("control_plane") or [""])[0], *shared, *host_settings]):
        blockers.append("A setting in the nodes' k3s.io/node-args has a control character in it; "
                        "check the servers' and this host's k3s arguments")
    for flag in (f for f in dropped if f in RENAMING_FLAGS):
        blockers.append(f"{node} was started with {flag}: as a server it would register under another name")
    if dropped:
        warnings.append(f"{node} was started with {', '.join(dropped)}, which a server here would not keep")
    after = len(members) + 1
    warnings.append(f"{len(members)} → {after} members: " +
                    ("two members need both: add a third soon" if after == 2 else
                     "losing any one keeps the cluster up" if after % 2 else "an even count adds no failure margin"))
    pods = [x for x in kget("/api/v1/pods").get("items", []) if (x.get("spec") or {}).get("nodeName") == node]
    local = MAINTENANCE.inventory(kget, pods).get("local_storage") or []
    if local:
        warnings.append("Kept on this host, waiting for it: " + ", ".join(str(r.get("pod") or r) for r in local[:6]))
    warnings.append(f"{node} is out of service for several minutes while k3s reinstalls")
    facts = {"uid": (host.get("metadata") or {}).get("uid", ""), "version": version,
             "server": (p.get("control_plane") or [""])[0], "settings": shared, "host_settings": host_settings,
             "members": members, "replicas": _replicas_on(node)}
    return {"node": node, "blockers": blockers, "warnings": warnings, "facts": facts,
            "review_token": _review(node, facts, blockers)}


def _node_param(request):
    node = (request.query.get("node") or [""])[0]
    if not node:
        raise ValueError("which host?")
    return node


RUN_LIMIT = 1200     # seconds the unit may take before the job gives up on it
JOIN_LIMIT = 600     # seconds for the Node to come back as an etcd member
_ACTIVE, _ACTIVE_LOCK = set(), threading.Lock()
# Seams the tests replace.
OPS_START = OPS.start
OPS_RECORD = OPS.record_phase
OPS_READ = OPS.stored
POD = os.environ.get("HOSTNAME", "")
SETTLE = 20          # seconds before the first look at a unit just started
PROBE_EVERY = 30     # seconds between looks at the host while it reinstalls
is_leader = LEADER.is_leader


def _thread(target):
    threading.Thread(target=target, name="host-promote", daemon=True).start()


def read_token(nodes, exclude):
    """The server token, from a Ready server - one that is not a tiebreaker
    first. Held in memory only, by the caller, until it is written."""
    servers = sorted((n for n in nodes if _server(n) and _ready(n) and _name(n) != exclude),
                     key=lambda n: (ROLES.is_tiebreaker(n), _name(n)))
    for n in servers:
        try:
            out, _ = hostrun.run(_name(n), f"cat {TOKEN_FILE}", timeout=45, wait=30)
        except Exception:
            continue
        token = out.strip()
        if token:
            return token
    raise ValueError("No server's token could be read; nothing was changed on the host")


def _longhorn_scheduling(node):
    try:
        return (kget(f"{LONGHORN}/namespaces/longhorn-system/nodes/{node}").get("spec") or {}).get("allowScheduling")
    except Exception:
        return None


def start(node, review_token):
    """File the job for the plan a person reviewed, checked again now."""
    now = plan(node)
    if now["blockers"]:
        raise ValueError(now["blockers"][0])
    if now["review_token"] != review_token:
        raise ValueError("The cluster changed since this was reviewed; review it again")
    host = kget(f"/api/v1/nodes/{node}")
    facts = now["facts"]
    ref = {"node": node, "uid": facts["uid"], "labels": own_labels(host), "longhorn_scheduling": _longhorn_scheduling(node),
           "version": facts["version"], "server": facts["server"], "args": facts["settings"] + facts["host_settings"],
           "members": len(facts["members"]), "since": time.time(), "phase": "draining"}
    item = OPS_START(KIND, f"Make {node} a server", {"kind": "Node", "name": node}, "/nodes", ref,
                     f"Moving apps off {node}", unless=(KIND,))
    # The leader runs it; on another replica its next poll picks it up.
    if is_leader():
        _launch(item["id"], ref)
    return item


def _launch(op_id, ref):
    with _ACTIVE_LOCK:
        if op_id in _ACTIVE:
            return
        _ACTIVE.add(op_id)
    _thread(lambda: _prepare(op_id, ref))


class _Stopped(Exception):
    """The job was cancelled, or another replica carries it on."""


def _drain_text(text):
    """The drain's words, without its reboot job's: nothing is powered here,
    and the host is uncordoned when a promotion stops before the reinstall."""
    for words in ("; power has not been sent", "Power was not sent; ", "Host remains cordoned; "):
        text = text.replace(words, "")
    return text


def _prepare(op_id, ref):
    """Drain, then write and start the unit: the steps before the host is
    out of Homestead's reach. Run by one replica (the job's worker), and
    safe to run again after a restart until the launch is attempted."""
    node, launching = ref["node"], False

    def record(phase, progress, message, **updates):
        # record_phase would turn a job being cancelled back to running.
        job = next((i for i in OPS_READ() if i.get("id") == op_id), None)
        if not job or job.get("status") != "running":
            raise _Stopped()
        return OPS_RECORD(op_id, phase, progress, message, owner=POD or None, **updates)
    try:
        # Claim the job: a replica that carried it before stops at its next write.
        job = next((i for i in OPS_READ() if i.get("id") == op_id), None)
        if not job or job.get("status") != "running":
            raise _Stopped()
        OPS_RECORD(op_id, ref.get("phase", "draining"), 15, f"Preparing {node}", worker=POD)
        if ref.get("phase", "draining") == "draining":
            # Another change on the host refuses now, with its reason, rather
            # than after a drain for nothing. The reinstall takes the lease
            # itself when it starts (#597).
            try:
                hostrun.let_go(node, hostrun.hold(node, f"Make {node} a server"))
            except HOSTRUN.Busy as error:
                record("failed", 100, f"{node} was not changed: {error}"[:400])
                return
            NODE_ACTIONS.set_cordon(node, True)
            NODE_ACTIONS.drain(node, include_system=True, wait=True,
                            progress=lambda phase, pct, msg: record("draining", 20, _drain_text(msg)))
            record("writing", 35, f"Writing the server settings on {node}")
        token = check_token(read_token(kget("/api/v1/nodes").get("items", []), node))
        script = launch_script(op_id, len(token), promote_script(ref["version"], ref["server"], ref["args"]))
        record("launching", 40, f"Starting the reinstall on {node}", running_since=time.time())
        launching = True
        try:
            out, err = hostrun.run(node, script, timeout=90, wait=60, stdin=token.encode(),
                                   change=f"Make {node} a server")
        except HOSTRUN.Busy:
            launching = False    # refused before anything ran there
            raise
        if "STARTED" not in out:
            raise ValueError(f"the reinstall on {node} did not confirm it started: {(err or out)[-200:]}")
        record("running", 45, f"Reinstalling k3s on {node} as a server", running_since=time.time())
    except (_Stopped, OPS.Superseded):
        pass
    except Exception as error:
        try:
            if launching:
                # It may have started: the host's marker and status file decide.
                record("launching", 45, f"Could not confirm the reinstall on {node} started ({str(error)[:160]}); "
                                        "following the host", running_since=time.time())
            else:
                NODE_ACTIONS.set_cordon(node, False)
                record("failed", 100, f"{node} was not changed: {_drain_text(str(error))}"[:400])
        except Exception:
            pass
    finally:
        with _ACTIVE_LOCK:
            _ACTIVE.discard(op_id)


def run_state(node, op_id):
    """{code, last, marked, active} of this job's unit on node; raises when
    the host can't be reached. A status file counts only beside this job's
    marker."""
    out, _ = hostrun.run(node, f"systemctl is-active --quiet {UNIT} && echo ACTIVE; "
                               f"[ \"$(cat {JOB} 2>/dev/null)\" = {op_id} ] && echo MARKED && "
                               f"[ -f {STATUS} ] && echo \"CODE $(cat {STATUS})\"; "
                               f"tail -n 1 {LOG} 2>/dev/null | cut -c1-160", timeout=30, wait=5)
    lines = out.splitlines()
    marked = "MARKED" in lines
    code = next((line[5:].strip() for line in lines if line.startswith("CODE ")), None) if marked else None
    last = next((line for line in reversed(lines) if line not in ("ACTIVE", "MARKED") and not line.startswith("CODE ")), "")
    return {"code": code, "last": last if marked else "", "marked": marked, "active": "ACTIVE" in lines}


def _rejoin():
    try:
        return PLATFORM.join_guide().get("agent", "")
    except Exception:
        return "the join guide's app host command"


def _joined(ref):
    """None while waiting; raises ValueError for a different Node."""
    host = kget(f"/api/v1/nodes/{ref['node']}")
    if (host.get("metadata") or {}).get("uid") != ref["uid"]:
        raise ValueError(f"{ref['node']} came back as a new Node (another uid): its Longhorn replicas may need checking")
    labels = (host.get("metadata") or {}).get("labels") or {}
    if not (_ready(host) and "node-role.kubernetes.io/etcd" in labels and "node-role.kubernetes.io/control-plane" in labels):
        return None
    if ref["node"] not in (NODE_ACTIONS.quorum_report().get("ready") or []):
        return None
    return host


def status(item, now=None):
    """The promotion job's state: (status, progress, message)."""
    ref, now = item["ref"], now or time.time()
    node, phase = ref["node"], ref.get("phase", "draining")
    if phase in ("draining", "writing"):
        if is_leader():
            _launch(item["id"], ref)
        return "running", item.get("progress") or 10, item.get("message") or f"Moving apps off {node}"
    if phase in ("launching", "running"):
        since = ref.get("running_since", now)
        if now - since < SETTLE or now - ref.get("probed_at", 0) < PROBE_EVERY:
            if now - since > RUN_LIMIT:
                return "failed", 100, f"No result from {node} after {RUN_LIMIT // 60} minutes; see {LOG} there"
            return "running", item.get("progress") or 50, item.get("message") or f"Reinstalling k3s on {node}"
        ref["probed_at"] = now
        try:
            state = run_state(node, item["id"])
        except Exception:
            state = None   # the host is out of reach while k3s is gone
        if state is not None and not state["marked"] and not state["active"]:
            # Reachable, and this job's unit never started there.
            NODE_ACTIONS.set_cordon(node, False)
            return "failed", 100, f"{node} was not changed: the reinstall did not start there"
        if state is None or state["code"] is None:
            if now - since > RUN_LIMIT:
                return "failed", 100, f"No result from {node} after {RUN_LIMIT // 60} minutes; see {LOG} there"
            ref["phase"] = "running"
            return "running", 55, f"Reinstalling k3s on {node}" + (f": {state['last']}" if state and state["last"] else "")
        if state["code"] == "20":
            NODE_ACTIONS.set_cordon(node, False)
            return "failed", 100, (f"{node} could not download the k3s installer, so nothing was removed: it is still an app "
                                   f"host, and has been uncordoned. See {LOG} there")
        if state["code"] == "10":
            return "failed", 100, (f"{node}'s node password went with the agent, so it was not joined as a new Node. To bring "
                                   f"it back as an app host, delete the Secret kube-system/{node}.node-password.k3s, then run "
                                   f"there: {_rejoin()}. It comes back as a new Node: check its Longhorn volume copies")
        if state["code"] != "0":
            try:
                joined = _joined(ref)
            except ValueError:
                joined = None
            if joined is None:
                return "failed", 100, (f"The reinstall on {node} stopped with code {state['code']}: {state['last']}. "
                                       f"The agent is gone and k3s is as the installer left it; see {LOG} there. Check "
                                       f"Cluster for {node} among the etcd members; to bring it back as an app host, run "
                                       f"there: {_rejoin()}")
        ref.update(phase="joining", joining_since=now)
        return "running", 75, f"{node} is joining etcd"
    if phase == "joining":
        try:
            host = _joined(ref)
        except ValueError as error:
            return "failed", 100, str(error)
        if host is None:
            if now - ref.get("joining_since", now) > JOIN_LIMIT:
                return "failed", 100, f"{node} has not come back as an etcd member after {JOIN_LIMIT // 60} minutes; see {LOG} there"
            return "running", 85, f"Waiting for {node} to come back as an etcd member"
        NODE_ACTIONS.set_cordon(node, False)
        ref["phase"] = "done"
        labels = (host.get("metadata") or {}).get("labels") or {}
        lost = sorted(k for k, v in (ref.get("labels") or {}).items() if labels.get(k) != v)
        members = len(NODE_ACTIONS.quorum_report().get("members") or [])
        notes = [f"Labels to set again: {', '.join(lost)}"] if lost else []
        if ref.get("longhorn_scheduling") is not None:
            now_scheduling = _longhorn_scheduling(node)
            if now_scheduling is None:
                notes.append(f"Longhorn has no node record for {node} now: check its disks in Longhorn")
            elif now_scheduling != ref["longhorn_scheduling"]:
                notes.append(f"Longhorn scheduling on {node} changed: check it in Longhorn")
        return "succeeded", 100, (f"{node} is a server; the cluster has {members} etcd members"
                                  + "".join(f". {n}" for n in notes))
    return "succeeded", 100, f"{node} is a server"


def cancel_plan(item):
    if (item.get("ref") or {}).get("phase", "draining") == "draining":
        return {"mode": "stop", "can": True, "action": "Stop and let apps back",
                "undo": [f"{item['ref']['node']} is uncordoned"], "keeps": ["Apps already moved stay where they are"],
                "needs": "admin"}
    return {"mode": "forget", "can": False,
            "why_not": "k3s is being reinstalled on the host; stopping now would leave it half done"}


def cancel_run(item, options):
    NODE_ACTIONS.set_cordon(item["ref"]["node"], False)
    return f"Stopped; {item['ref']['node']} is uncordoned"


def _start_route(body):
    node = str(body.get("node") or "")
    if not node:
        raise ValueError("which host?")
    return {"ok": True, "operation": start(node, str(body.get("review_token") or "")),
            "detail": f"Making {node} a server; follow it under Jobs"}

# The job's own, registered when homestead_routes loads this module at start
# (server.py builds the route table before any job is looked at).
OPS.RESOLVERS[KIND] = status
OPS.CANCELLERS[KIND] = (cancel_plan, cancel_run)


ROUTES = {
    ("GET", "/api/node/promote/plan"): ("admin", lambda request: plan(_node_param(request))),
    ("POST", "/api/node/promote"): ("admin", lambda request: _start_route(request.body)),
}
