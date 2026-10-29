"""Shared-history storage exclusions; no cluster writes or name-based adoption.

Claim identities include the namespace. Longhorn handles are a different key
from PV names: they are not interchangeable, even when provisioners make them
look alike. The creating workflow must capture both from its reviewed binding.
"""

KINDS = {"reclass", "snapshot-delete", "snapshot-revert", "volume-delete", "volume-restore", "disk-v2-convert", "disk-allocation", "longhorn-runtime", "longhorn-v2-cleanup"}
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
    elif kind == "restore-swap":
        # Restore into an app (#743): both claims and the app's volume, while it runs.
        claim(ref.get("old_claim")); claim(ref.get("new_claim")); handle(ref.get("old_volume"))
    elif kind in ("volume-delete", "volume-restore"):
        if not ref.get("released"):   # a Released volume's claim name is another volume's now (#932)
            claim(ref.get("name") or resource.get("name"))
        pv(ref.get("pv")); handle(ref.get("volume"))
    return result


def unresolved(item):
    """A timeout or stopped tracking is not proof that a storage write ended."""
    if item.get("kind") == "restore-swap":
        return item.get("status") not in TERMINAL    # it puts things back itself when it ends
    if item.get("kind") not in KINDS:
        return False
    ref = item.get("ref", {})
    if item.get('kind') == 'longhorn-v2-cleanup':
        return ref.get('phase') != 'complete'
    if item.get("kind") == "longhorn-runtime":
        return ref.get("phase") != "retired"
    if item.get("kind") in ("disk-v2-convert", "disk-allocation"):
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
    gated = ("reclass", "disk-v2-convert", "restore-swap")
    return [item for item in items if (kind in gated or item.get("kind") in gated)
            and unresolved(item) and wanted & targets(item.get("kind"), item.get("ref", {}), item.get("resource"))]


def _holds_v2(kind, ref):
    """Jobs a V2 disk conversion must not overlap: a Longhorn upgrade (a
    platform-upgrade of the longhorn component) restarts the instance
    managers it depends on; a shut-down or OS rollout stops its host."""
    return kind in ("cluster-shutdown", "os-rollout") or kind == "platform-upgrade" and (ref or {}).get("component") == "longhorn"


def require_clear(items, kind, ref, resource=None):
    for item in items:
        if item.get('kind') != 'longhorn-v2-cleanup' or not unresolved(item): continue
        row = next((r for r in item['ref'].get('nodes', []) if r['node'] == ref.get('node')), None)
        restart = kind == 'node-power' and ref.get('action') == 'reboot' and row and row.get('phase') == 'restart-required'
        if kind == 'longhorn-v2-prepare' or _holds_v2(kind, ref) or row and kind in ('node-power', 'host-os', 'disk-v2-convert', 'disk-allocation', 'longhorn-runtime') and not restart:
            raise ValueError('Finish V2 resource cleanup before changing this host or upgrading Longhorn')
    runtime = [i for i in items if i.get('kind') == 'longhorn-runtime' and unresolved(i)]
    for item in runtime:
        saved = item.get('ref') or {}
        same_host = saved.get('node') == ref.get('node')
        # A reboot is part of the approved workflow, but still goes through
        # the ordinary workload/quorum/storage maintenance review.
        reboot = (kind == 'node-power' and ref.get('action') == 'reboot'
                  and saved.get('phase') in ('staged', 'retire-staged'))
        if _holds_v2(kind, ref) or (same_host and kind in ('disk-v2-convert', 'disk-allocation', 'node-power', 'host-os',
                'host-promote', 'longhorn-v2-prepare', 'disk-retire', 'longhorn-runtime') and not reboot):
            raise ValueError('Finish the saved Longhorn runtime maintenance before changing this host or upgrading Longhorn.')
    if (_holds_v2(kind, ref) and any(i.get("kind") in ("disk-v2-convert", "disk-allocation") and unresolved(i) for i in items)
            or kind in ("disk-v2-convert", "disk-allocation") and any(_holds_v2(i.get("kind"), i.get("ref")) and i.get("status") not in TERMINAL for i in items)):
        raise ValueError("Finish disk preparation before upgrading V2 instance managers or shutting down hosts.")
    host_kinds = {"disk-v2-convert", "disk-allocation", "node-power", "host-os", "host-promote", "longhorn-v2-prepare", "disk-retire"}
    if kind in host_kinds and any(i.get("ref", {}).get("node") == ref.get("node")
            and i.get("kind") in host_kinds and (kind in ("disk-v2-convert", "disk-allocation") or i.get("kind") in ("disk-v2-convert", "disk-allocation"))
            and (unresolved(i) if i.get("kind") in ("disk-v2-convert", "disk-allocation") else i.get("status") not in TERMINAL) for i in items):
        raise ValueError("This host has a disk preparation or maintenance job. Finish or review it before changing the host.")
    if conflicts(items, kind, ref, resource):
        raise ValueError("This volume has another storage job or an unresolved outcome. Finish or review that job before starting a new operation.")
