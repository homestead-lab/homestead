"""
Lifecycle actions: edit a workload, move it between hosts, cordon/drain a node,
reboot or shut a node down, and migrate VMs.

Everything destructive here is guarded. The guards are the point of this module —
the raw API calls are three lines each.

Split by feature (#623): editing a workload lives in homestead_workload_edit and
the node actions in homestead_node_actions. This module keeps the state server.py
binds, moving apps and VMs, and the routes, and re-exports both.
"""
import os
import re
import time
import homestead_cache as CACHE
import homestead_names as NAMES
import homestead_maintenance as MAINTENANCE  # noqa: F401  (tests reach it through here)

# Rebooting a host needs a privileged pod that enters the host namespaces.
# Enabled by default, but still admin-only and guarded by impact review,
# quorum, drain and post-drain checks. An explicit false disables the helper.
NODE_POWER_ENABLED = os.environ.get("ENABLE_NODE_POWER", "true").strip().lower() in ("1", "true", "yes")

# Remembers the replica count a workload should return to when autostart is
# switched back on, because scaling to zero forgets it.
AUTOSTART_REPLICAS = NAMES.key("autostart-replicas")

# injected by server.py so this module stays import-cycle free
kget = ksend = None
SYS_NS = set()
_cache = {}
hardware_features = lambda: []
create_pvc = None
STORAGE_CLASS = "longhorn-r2"


def dns_label(value, label):
    value = (value or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?", value):
        raise ValueError(f"{label} must use lowercase letters, numbers and dashes")
    return value


def bind(_kget, _ksend, _sys_ns, _cache_ref, _hardware_features=None,
         _create_pvc=None, _storage_class=None):
    global kget, ksend, SYS_NS, _cache, hardware_features, create_pvc, STORAGE_CLASS
    kget, ksend, SYS_NS, _cache = _kget, _ksend, _sys_ns, _cache_ref
    if _hardware_features:
        hardware_features = _hardware_features
    if _create_pvc:
        create_pvc = _create_pvc
    if _storage_class:
        STORAGE_CLASS = _storage_class


def _bust(*keys):
    CACHE.drop_where(_cache, lambda k: not keys or k.startswith(keys))


# --------------------------------------------------------------- move
def move_workload(ns, name, node):
    """Pin a Deployment to a node (or unpin) and force it to reschedule.

    RWO volumes mean the pod must fully terminate before it can attach on the
    target, so this is Recreate + a rollout, not a live move.
    """
    dep = kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")
    spec = dep["spec"]["template"]["spec"]
    sel = spec.setdefault("nodeSelector", {})
    if node:
        sel["kubernetes.io/hostname"] = node
    else:
        sel.pop("kubernetes.io/hostname", None)
    if not sel:
        spec.pop("nodeSelector", None)
    dep["spec"].setdefault("strategy", {})["type"] = "Recreate"
    dep["spec"]["template"].setdefault("metadata", {}).setdefault("annotations", {})[
        NAMES.key("movedAt")] = time.strftime("%Y-%m-%dT%H:%M:%SZ")
    ksend("PUT", f"/apis/apps/v1/namespaces/{ns}/deployments/{name}", dep)
    _bust("wl", "ov", "flow")
    return {"ok": True, "moved": name, "to": node or "any node"}


# --------------------------------------------------------------- VM actions
def vm_migrate(ns, name, target=None):
    """Live-migrate a running VM. KubeVirt picks the target unless one is given."""
    guard = globals().get("vm_migration_guard")
    if guard:
        guard(ns, name)
    body = {"apiVersion": "kubevirt.io/v1", "kind": "VirtualMachineInstanceMigration",
            "metadata": {"generateName": f"{name}-mig-", "namespace": ns},
            "spec": {"vmiName": name}}
    if target:
        body["spec"]["addedNodeSelector"] = {"kubernetes.io/hostname": target}
    out = ksend("POST", f"/apis/kubevirt.io/v1/namespaces/{ns}/virtualmachineinstancemigrations", body)
    _bust("flow", "ov")
    return {"ok": True, "migration": out.get("metadata", {}).get("name", ""), "vm": name}


# Its routes and who may use them (homestead_routes.py).
ROUTES = {
    ("POST", "/api/node/cordon"): ("admin", lambda request: set_cordon(request.body["node"], request.body.get("cordon", True))),
    ("GET", "/api/quorum"): ("viewer", lambda request: {**quorum_report(), "power_enabled": NODE_POWER_ENABLED}),
}


# Moved by feature (#623). Re-exported for one release, so other modules,
# server.py and tests that use or replace these names here keep working.
from homestead_workload_edit import (  # noqa: E402,F401
    seed_configs, _prepare_seed_configs, _wait_for_replicas, _renamed_deployment, rename_workload,
    _container_feature_ids, _apply_container_edit, _unique_volume_name, _is_storage_volume,
    _requested_volume_kind, _device_host_paths, _is_device_mount, _pvc_exists, _apply_container_volumes,
    _create_pending_pvcs, _apply_container_hardware, _pod_template, restarts, _apply_priority, _apply_tier,
    prepare_edit, edit_workload,
)
from homestead_node_actions import (  # noqa: E402,F401
    quorum_report, node_action_check, set_cordon, LONGHORN_DRAIN_WAIT, drain, node_power, _send_power,
)
