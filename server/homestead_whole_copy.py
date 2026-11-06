"""Is this Longhorn replica a whole copy of its volume? One answer (#1126).

Host power, rebalance, moving copies between disks, disk retire, the V2
conversion and the V2 upgrade each ask "does a whole copy survive elsewhere".
They used to answer by seven rules, and one of them counted a copy that was
still being rebuilt. A copy is whole when:

- it is not being deleted, has not failed (no failedAt, not in error), and
  Longhorn has had it whole (healthyAt set; a rebuild clears it until done);
- its host is Ready, when the caller says which hosts are (host_ready);
- and either a running engine of its volume has it as RW in replicaModeMap
  (missing from the map, WO or ERR is not whole), or the volume itself says
  it is detached, the copy is stopped, and the caller counts such copies
  (detached_ok): Longhorn keeps a detached volume's healthy copies stopped,
  not lost. A volume that says attached with no running engine has no whole
  copy anywhere: Longhorn is not serving it.

The differences that are on purpose are the two arguments: detached_ok is
False where an operation needs the copy running (the V2 conversion), and
host readiness is asked where the caller reads the hosts.

Objects only: nothing here reads the cluster.
"""


def ready(obj):
    """A Kubernetes or Longhorn node that says Ready and is not going away."""
    obj = obj or {}
    return not (obj.get("metadata") or {}).get("deletionTimestamp") and any(
        c.get("type") == "Ready" and c.get("status") == "True" for c in (obj.get("status") or {}).get("conditions") or [])


def _running(engines):
    if engines is None:
        return []
    if isinstance(engines, dict):
        engines = [engines]
    return [e for e in engines if (e.get("status") or {}).get("currentState") == "running"
            and not (e.get("metadata") or {}).get("deletionTimestamp")]


def whole_copy(replica, engine, *, detached_ok, volume_state=None, host_ready=None):
    """engine: the volume's engine, a list of its engines (two while it
    migrates), or None. volume_state: the Volume's status.state; a stopped
    copy counts only when it is "detached". host_ready: a function of the
    host's name, or None where the caller checks hosts itself."""
    meta, spec, status = replica.get("metadata") or {}, replica.get("spec") or {}, replica.get("status") or {}
    if meta.get("deletionTimestamp") or spec.get("failedAt") or status.get("currentState") == "error":
        return False
    if not spec.get("healthyAt"):
        return False
    if host_ready is not None and not host_ready(spec.get("nodeID")):
        return False
    running = _running(engine)
    if running:
        name = meta.get("name")
        return status.get("currentState") == "running" and any(
            ((e.get("status") or {}).get("replicaModeMap") or {}).get(name) == "RW" for e in running)
    return bool(detached_ok) and volume_state == "detached" and status.get("currentState") == "stopped"


def judge(engines, *, detached_ok, volumes=(), nodes=None, lh_nodes=None):
    """whole_copy for many replicas over one read of the engines, the volumes
    (Volume objects; one not given is not detached) and, where given, the
    hosts (name -> Kubernetes node, name -> Longhorn node). A host missing
    from a map that is given is not Ready."""
    states = {(v.get("metadata") or {}).get("name"): (v.get("status") or {}).get("state") for v in volumes or ()}
    by_volume = {}
    for engine in engines or []:
        by_volume.setdefault((engine.get("spec") or {}).get("volumeName"), []).append(engine)
    host_ready = None
    if nodes is not None or lh_nodes is not None:
        def host_ready(host):
            return ((nodes is None or ready(nodes.get(host))) and (lh_nodes is None or ready(lh_nodes.get(host))))
    def whole(replica):
        name = (replica.get("spec") or {}).get("volumeName")
        return whole_copy(replica, by_volume.get(name, []), detached_ok=detached_ok,
                          volume_state=states.get(name), host_ready=host_ready)
    return whole


def rebuilding(engines, volume=None):
    """The volumes with a copy being rebuilt (WO in an engine's map); with
    volume, whether that one has."""
    # An engine with no volumeName came from a read of that one volume's engines (a label selector).
    found = {((e.get("spec") or {}).get("volumeName") or volume) for e in engines or []
             if "WO" in ((e.get("status") or {}).get("replicaModeMap") or {}).values()}
    return volume in found if volume is not None else found
