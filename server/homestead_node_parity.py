"""A node that joins a k3s or RKE2 cluster later, made to match the others.

The DaemonSets - the node probe, Longhorn, Multus, macvtap, KubeVirt - reach
a new node by themselves. Three things set up when the cluster was one
machine do not:

- the host: Longhorn's iSCSI devices are plain /dev/sd* disks, and Ubuntu's
  multipathd claims every one it sees, after which a volume's mount fails as
  "already mounted or mount point busy". Longhorn's own advice is to keep
  multipathd off sd devices in /etc/multipath.conf; the installer does that
  on each machine it sets up, and the leader does it once on any node that
  joined without it - unless the host itself boots from a multipath device,
  and one with any mounted filesystem on a multipath device is left alone. iscsid, installed but stopped, is started too;
- kube-vip: installed on one machine, it was told that machine's network
  interface. A node whose interface is named differently cannot announce a
  VIP there, so once the nodes' probes disagree the name is taken out and
  kube-vip finds each node's own from its default route;
- the journal: capped at 1 GB, as the installer caps it, unless a cap is
  set already - journald's own default is up to 4 GB of the system disk;
- Longhorn's copies: a one-machine cluster keeps one copy of each volume,
  since it can hold no more. Once more nodes are Ready, the default for new
  volumes rises with them, up to three - 1, then 2, then 3 as nodes join -
  only while it is still the installer's one copy or the count Homestead
  itself set, so a count someone chose is kept, and it is never lowered. A
  change in Ready nodes is noticed within a minute (copies_tick), so apps
  made just after the nodes join get the copies too. Existing volumes keep
  their count; the Volumes page raises those - except Homestead's own data
  volume, made by the installer on the first machine with its one copy: it
  rises with the default, on the same terms, or the host holding it could
  never be drained for a restart.

Each node's host is done once, and recorded in /data, like the inotify
limits (homestead_host_limits); a host done by an older release, with fewer
steps, is done again (HOST_STEPS), which each step allows.
"""
import calendar
import json
import os
import re
import time
import urllib.error

import homestead_addons as addons
import homestead_shared as SHARED

kget = ksend = hostrun = platform = probes = None
DATA_DIR = "/data"
CONTROLLER_NS = "kube-system"
HELMCHARTS = f"/apis/helm.cattle.io/v1/namespaces/{CONTROLLER_NS}/helmcharts"
LONGHORN_SETTING = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system/settings/default-replica-count"
VIP_INTERFACE = re.compile(r'(?m)^  vip_interface: "?([A-Za-z0-9_.:-]{1,15})"?\n')
# Exactly what the installer (bootstrap-k3s.sh) and Add-ons write for one
# machine: nothing else in the values, so nothing anyone chose is changed.
INSTALLER_COPIES = re.compile(r"\s*persistence:\s*\n\s+defaultClassReplicaCount:\s*(\d+)\s*\n"
                              r"\s*defaultSettings:\s*\n\s+defaultReplicaCount:\s*(\d+)\s*")

# The host steps this release takes; a host done with fewer is done again.
HOST_STEPS = 2
HOST_SCRIPT = r"""R=$(findmnt -n -o SOURCE / 2>/dev/null)
if grep -qs '^SystemMaxUse=' /etc/systemd/journald.conf /etc/systemd/journald.conf.d/*.conf; then echo "JOURNAL kept"
elif [ -d /etc/systemd ]; then
  mkdir -p /etc/systemd/journald.conf.d
  printf '[Journal]\nSystemMaxUse=1G\n' > /etc/systemd/journald.conf.d/90-homestead.conf
  systemctl restart systemd-journald 2>/dev/null || true
  echo "JOURNAL capped"
fi
[ "$LONGHORN" = 1 ] || { echo END; exit 0; }
if systemctl is-active --quiet multipathd 2>/dev/null; then
  if for m in $(findmnt -rn -o SOURCE 2>/dev/null | grep '^/dev/'); do lsblk -s -n -o TYPE "$m" 2>/dev/null; done | grep -q mpath; then echo "MULTIPATH root"
  elif grep -qs 'devnode "\^sd\[a-z0-9\]+"' /etc/multipath.conf; then echo "MULTIPATH kept"
  else
    [ -f /etc/multipath.conf ] && cp /etc/multipath.conf /etc/multipath.conf.homestead-backup
    grep -qs '^blacklist *{' /etc/multipath.conf || printf 'blacklist {\n}\n' >> /etc/multipath.conf
    sed -i '/^blacklist *{/a\    devnode "^sd[a-z0-9]+"' /etc/multipath.conf
    systemctl restart multipathd 2>/dev/null || true
    multipath -F >/dev/null 2>&1 || true
    echo "MULTIPATH set"
  fi
else echo "MULTIPATH none"; fi
if command -v iscsiadm >/dev/null 2>&1; then
  if systemctl is-active --quiet iscsid 2>/dev/null || systemctl is-active --quiet iscsid.socket 2>/dev/null; then echo "ISCSI kept"
  else systemctl enable --now iscsid >/dev/null 2>&1 && echo "ISCSI started" || echo "ISCSI failed"; fi
else echo "ISCSI missing"; fi
echo END
"""


OWN = ("", "")      # Homestead's namespace and Deployment, whose data volume follows the nodes
LH_VOLUMES = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system/volumes"


def bind(_kget, _ksend, _hostrun, _platform, _probes, data_dir="/data", own=("", "")):
    global kget, ksend, hostrun, platform, probes, DATA_DIR, OWN
    kget, ksend, hostrun, platform, probes, DATA_DIR = _kget, _ksend, _hostrun, _platform, _probes, data_dir
    OWN = tuple(own)


def _path():
    return os.path.join(DATA_DIR, "node-parity.json")


def _load():
    try:
        with open(_path(), encoding="utf-8") as handle:
            state = json.load(handle)
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def _get(path):
    try:
        return kget(path)
    except Exception:
        return None


def _ready_nodes():
    return [n["metadata"]["name"] for n in (kget("/api/v1/nodes") or {}).get("items", [])
            if any(c.get("type") == "Ready" and c.get("status") == "True"
                   for c in (n.get("status") or {}).get("conditions") or [])]


def hosts(ready, state, longhorn=True):
    """Each Ready node's host, once: {node: [what changed]}."""
    done = state.setdefault("hosts", {})
    changed = {}
    for node in ready:
        if (done.get(node) or {}).get("steps", 1) >= HOST_STEPS:
            continue
        try:
            out, err = hostrun.run(node, f"LONGHORN={1 if longhorn else 0}\n" + HOST_SCRIPT, timeout=90)
        except Exception as error:
            out, err = "", str(error)
        words = dict(line.split(" ", 1) for line in out.splitlines() if " " in line)
        if "END" not in out.split():
            # Tried again next time; the other nodes are not held up by it.
            changed[node] = [f"could not check its host: {(err or out)[:200]}"]
            continue
        done[node] = {"at": int(time.time()), "steps": HOST_STEPS, "multipath": words.get("MULTIPATH", ""),
                      "iscsi": words.get("ISCSI", ""), "journal": words.get("JOURNAL", "")}
        notes = ["journal capped at 1 GB"] if words.get("JOURNAL") == "capped" else []
        if words.get("MULTIPATH") == "set":
            notes.append("multipathd kept off Longhorn's devices")
        if words.get("ISCSI") == "started":
            notes.append("iscsid started")
        if words.get("ISCSI") in ("missing", "failed"):
            notes.append("open-iscsi is " + ("not installed" if words["ISCSI"] == "missing" else "installed but would not start")
                         + ": Longhorn cannot attach volumes there")
        if notes:
            changed[node] = notes
    return changed


def kube_vip():
    """The interface pin taken out once the nodes' own interfaces differ.
    Returns the interface it was, or ""."""
    chart = _get(f"{HELMCHARTS}/kube-vip")
    if not chart:
        return ""
    values = str((chart.get("spec") or {}).get("valuesContent") or "")
    match = VIP_INTERFACE.search(values)
    if not match:
        return ""
    try:
        found = {str(data.get("default_interface") or "") for data in (probes() or {}).values()}
    except Exception:
        return ""
    found.discard("")
    # Only on what the probes say: no probe, no evidence either way.
    if not found or found == {match.group(1)}:
        return ""
    ksend("PATCH", f"{HELMCHARTS}/kube-vip", {"spec": {"valuesContent": VIP_INTERFACE.sub("", values, count=1)}},
          ctype="application/merge-patch+json")
    return match.group(1)


ENV_LINE = re.compile(r'^  ([A-Za-z_]+): "?([^"\n]*)"?$')


def _ours(values):
    """The env and extras of kube-vip values Homestead wrote, or None for
    values someone else wrote - which are left as they are."""
    env, rest = {}, values
    if not values.startswith("env:\n"):
        return None
    lines = values.split("\n")[1:]
    index = 0
    while index < len(lines) and lines[index].startswith("  "):
        match = ENV_LINE.match(lines[index])
        if not match:
            return None
        env[match.group(1)] = match.group(2)
        index += 1
    rest = "\n".join(lines[index:]).strip("\n")
    if rest and rest + "\n" != addons.KUBE_VIP_SECURITY:
        return None
    return env, bool(rest)


def _local_traffic_vips():
    """Services whose VIP must follow their own pod (externalTrafficPolicy
    Local - NFS shares): they need kube-vip's per-Service election. None
    when the Services cannot be read."""
    try:
        services = kget("/api/v1/services").get("items", [])
    except Exception:
        return None
    return [f"{s['metadata'].get('namespace')}/{s['metadata'].get('name')}" for s in services
            if (s.get("spec") or {}).get("type") == "LoadBalancer"
            and (s.get("spec") or {}).get("externalTrafficPolicy") == "Local"]


def kube_vip_settings():
    """Homestead's kube-vip brought to the settings that hold across reboots:
    one leader for every VIP, the interface named where every node agrees,
    and NET_ADMIN and NET_RAW. Only values Homestead wrote are changed, and
    per-Service election stays where a Local-traffic Service needs it.
    Returns what changed, or ""."""
    chart = _get(f"{HELMCHARTS}/kube-vip")
    if not chart:
        return ""
    values = str((chart.get("spec") or {}).get("valuesContent") or "")
    parsed = _ours(values)
    if parsed is None:
        return ""
    env, secured = parsed
    local = _local_traffic_vips()
    if local is None:
        return ""
    election = "service" if local else "global"
    interface = env.get("vip_interface", "")
    if not interface:
        # Pinned only when every Ready node's probe names the same interface:
        # a node that has not said may have another, and kube-vip leading
        # from there could not put the VIPs on it.
        try:
            reported = probes() or {}
            ready = _ready_nodes()
        except Exception:
            reported, ready = {}, []
        found = {str((reported.get(node) or {}).get("default_interface") or "") for node in ready}
        interface = found.pop() if ready and len(found) == 1 and "" not in found else ""
    wanted = addons.kube_vip_values(interface, env.get("lb_class_only") == "true", election)
    if wanted == values:
        return ""
    ksend("PATCH", f"{HELMCHARTS}/kube-vip", {"spec": {"valuesContent": wanted}},
          ctype="application/merge-patch+json")
    notes = []
    if election == "global" and env.get("svc_election") != "false":
        notes.append("one leader now holds every VIP (global election), so VIPs come back after a reboot")
    if interface and not env.get("vip_interface"):
        notes.append(f"it announces on {interface}")
    if not secured:
        notes.append("it has NET_ADMIN and NET_RAW")
    if election == "service" and env.get("svc_election") != "true":
        notes.append(f"per-Service election kept for {', '.join(local[:3])}")
    return "; ".join(notes) or "its settings were brought up to date"


WORKLOADS = {"Deployment": "/apis/apps/v1/namespaces/{ns}/deployments",
             "StatefulSet": "/apis/apps/v1/namespaces/{ns}/statefulsets",
             "DaemonSet": "/apis/apps/v1/namespaces/{ns}/daemonsets",
             "VirtualMachine": "/apis/kubevirt.io/v1/namespaces/{ns}/virtualmachines"}
STALE_AFTER = 600
OBJECTSTORE = "homestead-objectstore"


def _namespace_workloads(ns):
    """Every workload in a namespace and the pod labels each makes, plus the
    pods: None when any of it could not be read - nothing is removed on a
    guess."""
    found = {}
    for kind, path in WORKLOADS.items():
        try:
            items = kget(path.format(ns=ns)).get("items", [])
        except urllib.error.HTTPError as error:
            if kind == "VirtualMachine" and error.code == 404:
                items = []          # no KubeVirt: no VMs
            else:
                return None
        except Exception:
            return None
        found[kind] = {i["metadata"]["name"]: ((((i.get("spec") or {}).get("template") or {}).get("metadata") or {})
                                               .get("labels") or {}) for i in items}
    try:
        pods = kget(f"/api/v1/namespaces/{ns}/pods").get("items", [])
    except Exception:
        return None
    found["Pod"] = {p["metadata"]["name"]: (p["metadata"].get("labels") or {}) for p in pods}
    return found


def stale_services(now=None):
    """Remove LoadBalancer Services Homestead made whose workload no longer
    exists. One left behind still asks kube-vip for its VIP, and a VIP two
    Services ask for can stay <pending> after a reboot. A Service stays while
    anything - a workload stopped at zero included - could still use it.
    Returns the names removed."""
    now = now or time.time()
    try:
        services = kget("/api/v1/services").get("items", [])
    except Exception:
        return []
    removed, cache = [], {}
    for service in services:
        meta, spec = service.get("metadata") or {}, service.get("spec") or {}
        if spec.get("type") != "LoadBalancer" or (meta.get("labels") or {}).get("homestead.io/managed") != "true":
            continue
        try:
            born = calendar.timegm(time.strptime(meta.get("creationTimestamp", ""), "%Y-%m-%dT%H:%M:%SZ"))
        except (ValueError, OverflowError):
            continue
        if now - born < STALE_AFTER:
            continue
        ns, name = meta.get("namespace", ""), meta.get("name", "")
        if ns not in cache:
            cache[ns] = _namespace_workloads(ns)
        found = cache[ns]
        if found is None:
            continue
        annotations = meta.get("annotations") or {}
        kind, workload = annotations.get("homestead.io/workload-kind"), annotations.get("homestead.io/workload")
        if not (kind and workload) and (name == OBJECTSTORE or name.startswith(OBJECTSTORE + "-vip")):
            kind, workload = "Deployment", OBJECTSTORE      # the backup store's own, which names no workload
        # Only a Service that says whose it is, and whose that is gone: one
        # that does not say - an older release's, one made by hand - might
        # belong to something this cannot see, and is left alone.
        if not (kind and workload) or kind not in WORKLOADS or workload in found.get(kind, {}):
            continue
        selector = spec.get("selector") or {}
        if selector and any(all(labels.get(k) == v for k, v in selector.items()) for labels in found["Pod"].values()):
            continue            # its pods are still there
        try:
            ksend("DELETE", f"/api/v1/namespaces/{ns}/services/{name}")
            removed.append(f"{ns}/{name}")
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
    return removed


def longhorn_copies(ready):
    """The default copies raised with the nodes, while it is the installer's
    one or the count Homestead set before. Returns (from, to), or None."""
    want = max(1, min(3, len(ready)))
    chart = _get(f"{HELMCHARTS}/longhorn")
    if not chart or want < 2:
        return None
    values = str((chart.get("spec") or {}).get("valuesContent") or "")
    match = INSTALLER_COPIES.fullmatch(values)
    ours = {"1", str(_load().get("longhorn_copies") or "1")}
    if not match or match.group(1) != match.group(2) or match.group(1) not in ours:
        return None
    have = int(match.group(1))
    if want <= have:
        return None
    ksend("PATCH", f"{HELMCHARTS}/longhorn",
          {"spec": {"valuesContent": "\n".join(["persistence:", f"  defaultClassReplicaCount: {want}",
                                                "defaultSettings:", f"  defaultReplicaCount: {want}", ""])}},
          ctype="application/merge-patch+json")
    # The chart's defaults reach a running Longhorn only at its install; the
    # setting is what it reads for each new volume now.
    setting = _get(LONGHORN_SETTING)
    if setting and str(setting.get("value") or "") in ours:
        ksend("PATCH", LONGHORN_SETTING, {"value": str(want)}, ctype="application/merge-patch+json")
    state = _load()
    state["longhorn_copies"] = want
    SHARED.write_json(_path(), state, indent=1, sort_keys=True)
    return (have, want)


def own_copies(ready):
    """Homestead's own data volume raised with the nodes, while it keeps the
    installer's one copy or the count Homestead set before. Returns the
    claims raised, as (claim, to)."""
    want = max(1, min(3, len(ready)))
    ns, name = OWN
    if want < 2 or not ns or not name:
        return []
    deployment = _get(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}") or {}
    claims = [v["persistentVolumeClaim"]["claimName"] for v in
              (((deployment.get("spec") or {}).get("template") or {}).get("spec") or {}).get("volumes") or []
              if (v.get("persistentVolumeClaim") or {}).get("claimName")]
    state = _load()
    ours = {1, int(state.get("own_copies") or 1)}
    raised = []
    for claim in claims:
        pv = ((_get(f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{claim}") or {}).get("spec") or {}).get("volumeName")
        volume = _get(f"{LH_VOLUMES}/{pv}") if pv else None
        have = int(((volume or {}).get("spec") or {}).get("numberOfReplicas") or 0)
        if not volume or have not in ours or have >= want:
            continue
        ksend("PATCH", f"{LH_VOLUMES}/{pv}", {"spec": {"numberOfReplicas": want}}, ctype="application/merge-patch+json")
        raised.append((claim, want))
    if raised:
        state = _load()
        state["own_copies"] = want
        SHARED.write_json(_path(), state, indent=1, sort_keys=True)
    return raised


_seen_ready = None


def copies_tick():
    """Every minute: once the number of Ready nodes changes, the default
    copies follow. Returns a line for the log, or None."""
    global _seen_ready
    p = platform() or {}
    if p.get("harvester") or p.get("distribution") not in ("k3s", "rke2") or not p.get("longhorn"):
        return None
    ready = _ready_nodes()
    if len(ready) == _seen_ready:
        return None
    raised = longhorn_copies(ready)
    own = own_copies(ready)
    _seen_ready = len(ready)
    notes = [n for n in [_copies_note(raised, ready)] + [f"Homestead's data volume {claim} keeps {to} copies now"
                                                          for claim, to in own] if n]
    return "; ".join(notes) or None


def _copies_note(raised, ready):
    if not raised:
        return None
    return (f"new Longhorn volumes keep {raised[1]} copies, now there are {len(ready)} nodes "
            "(existing volumes keep theirs; raise them on the Volumes page)")


def tick():
    """Returns [(node or "", what changed)] for the log."""
    p = platform(True) or {}
    if p.get("harvester") or p.get("distribution") not in ("k3s", "rke2"):
        return []
    state = _load()
    ready = _ready_nodes()
    out = []
    try:
        for node, notes in hosts(ready, state, bool(p.get("longhorn"))).items():
            out.extend((node, note) for note in notes)
    finally:
        SHARED.write_json(_path(), state, indent=1, sort_keys=True)
    was = kube_vip()
    if was:
        out.append(("", f"kube-vip no longer pinned to {was}: the nodes' interfaces differ, so each announces on its own"))
    else:
        settled = kube_vip_settings()
        if settled:
            out.append(("", f"kube-vip: {settled}"))
    for name in stale_services():
        out.append(("", f"removed the LoadBalancer Service {name}: its workload is gone, and it still asked for its VIP"))
    if p.get("longhorn"):
        note = _copies_note(longhorn_copies(ready), ready)
        if note:
            out.append(("", note))
    return out
