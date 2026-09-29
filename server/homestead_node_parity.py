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
  which is then left alone. iscsid, installed but stopped, is started too;
- kube-vip: installed on one machine, it was told that machine's network
  interface. A node whose interface is named differently cannot announce a
  VIP there, so once the nodes' probes disagree the name is taken out and
  kube-vip finds each node's own from its default route;
- the journal: capped at 1 GB, as the installer caps it, unless a cap is
  set already - journald's own default is up to 4 GB of the system disk;
- Longhorn's copies: a one-machine cluster keeps one copy of each volume,
  since it can hold no more. Once more nodes are Ready, the default for new
  volumes rises with them, up to three - only while it is still the
  installer's own one copy, so a count someone chose is kept. Existing
  volumes keep their count; the Volumes page raises those.

Each node's host is done once, and recorded in /data, like the inotify
limits (homestead_host_limits); a host done by an older release, with fewer
steps, is done again (HOST_STEPS), which each step allows.
"""
import json
import os
import re
import time

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
  if [ -n "$R" ] && lsblk -s -n -o TYPE "$R" 2>/dev/null | grep -q mpath; then echo "MULTIPATH root"
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


def bind(_kget, _ksend, _hostrun, _platform, _probes, data_dir="/data"):
    global kget, ksend, hostrun, platform, probes, DATA_DIR
    kget, ksend, hostrun, platform, probes, DATA_DIR = _kget, _ksend, _hostrun, _platform, _probes, data_dir


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


def longhorn_copies(ready):
    """The default copies raised with the nodes, while it is the installer's
    one. Returns (from, to), or None."""
    want = max(1, min(3, len(ready)))
    chart = _get(f"{HELMCHARTS}/longhorn")
    if not chart or want < 2:
        return None
    values = str((chart.get("spec") or {}).get("valuesContent") or "")
    match = INSTALLER_COPIES.fullmatch(values)
    if not match or match.group(1) != "1" or match.group(2) != "1":
        return None
    ksend("PATCH", f"{HELMCHARTS}/longhorn",
          {"spec": {"valuesContent": "\n".join(["persistence:", f"  defaultClassReplicaCount: {want}",
                                                "defaultSettings:", f"  defaultReplicaCount: {want}", ""])}},
          ctype="application/merge-patch+json")
    # The chart's defaults reach a running Longhorn only at its install; the
    # setting is what it reads for each new volume now.
    setting = _get(LONGHORN_SETTING)
    if setting and str(setting.get("value") or "") == "1":
        ksend("PATCH", LONGHORN_SETTING, {"value": str(want)}, ctype="application/merge-patch+json")
    return (1, want)


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
    if p.get("longhorn"):
        raised = longhorn_copies(ready)
        if raised:
            out.append(("", f"new Longhorn volumes keep {raised[1]} copies, now there are {len(ready)} nodes "
                            "(existing volumes keep theirs; raise them on the Volumes page)"))
    return out
