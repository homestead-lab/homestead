"""Shared-history storage exclusions; no cluster writes or name-based adoption.

Claim identities include the namespace. Longhorn handles are a different key
from PV names: they are not interchangeable, even when provisioners make them
look alike. The creating workflow must capture both from its reviewed binding.
"""

KINDS = {"reclass", "snapshot-delete", "snapshot-revert", "volume-delete", "volume-restore", "disk-v2-convert"}
TERMINAL = {"succeeded", "failed", "cancelled"}


def targets(kind, ref, resource=None):
    resource = resource or {}
    ns = ref.get("namespace") or resource.get("namespace")
    result = set()
    def claim(name):
        if ns and name:
            result.add(("claim", ns, name))
    def pv(name):
        if name:
            result.add(("pv", name))
    def handle(name):
        if name:
            result.add(("longhorn", name))
    if kind == "disk-v2-convert":
        for row in ref.get("volumes", []):
            handle(row.get("volume"))
    elif kind == "reclass":
        claim(ref.get("claim") or resource.get("name")); claim(ref.get("temp"))
        for name in (ref.get("old_pv"), ref.get("new_pv")):
            pv(name)
        for row in ref.get("copy_claims", {}).values():
            pv(row.get("pv"))
            if row.get("csi_driver") == "driver.longhorn.io":
                handle(row.get("csi_handle"))
    elif kind in ("snapshot-delete", "snapshot-revert"):
        claim(ref.get("claim")); handle(ref.get("volume"))
    elif kind in ("volume-delete", "volume-restore"):
        claim(ref.get("name") or resource.get("name"))
        pv(ref.get("pv")); handle(ref.get("volume"))
    return result


def unresolved(item):
    """A timeout or stopped tracking is not proof that a storage write ended."""
    if item.get("kind") not in KINDS:
        return False
    ref = item.get("ref", {})
    if item.get("kind") == "disk-v2-convert":
        return ref.get("phase") not in ("complete", "cancelled")
    if ref.get("retain_resources") or item.get("status") not in TERMINAL:
        return True
    if item.get("status") == "succeeded":
        return False
    if item.get("kind") == "reclass" and ref.get("phase") in ("done", "rolled-back"):
        return False
    # Older cleanup records explicitly acknowledge completed cleanup. Merely
    # hiding a failed operation has never supplied that evidence.
    return not item.get("cleaned") and bool(targets(item.get("kind"), ref, item.get("resource")))


def conflicts(items, kind, ref, resource=None):
    wanted = targets(kind, ref, resource)
    return [item for item in items if (kind in ("reclass", "disk-v2-convert") or item.get("kind") in ("reclass", "disk-v2-convert"))
            and unresolved(item) and wanted & targets(item.get("kind"), item.get("ref", {}), item.get("resource"))]


def require_clear(items, kind, ref, resource=None):
    if (kind in ("longhorn-v2-upgrade", "cluster-shutdown", "os-rollout") and any(i.get("kind") == "disk-v2-convert" and unresolved(i) for i in items)
            or kind == "disk-v2-convert" and any(i.get("kind") in ("longhorn-v2-upgrade", "cluster-shutdown", "os-rollout") and i.get("status") not in TERMINAL for i in items)):
        raise ValueError("Finish disk preparation before upgrading V2 instance managers or shutting down hosts.")
    host_kinds = {"disk-v2-convert", "node-power", "host-os", "longhorn-v2-prepare", "disk-retire"}
    if kind in host_kinds and any(i.get("ref", {}).get("node") == ref.get("node")
            and i.get("kind") in host_kinds and (kind == "disk-v2-convert" or i.get("kind") == "disk-v2-convert")
            and (unresolved(i) if i.get("kind") == "disk-v2-convert" else i.get("status") not in TERMINAL) for i in items):
        raise ValueError("This host has a disk preparation or maintenance job. Finish or review it before changing the host.")
    if conflicts(items, kind, ref, resource):
        raise ValueError("This volume has another storage job or an unresolved outcome. Finish or review that job before starting a new operation.")
