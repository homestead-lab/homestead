"""Explicit HTTP authorization declarations. New routes must declare a policy:
here, or beside their handler in a feature module's ROUTES (homestead_routes.py)."""
import homestead_routes as MODULE_ROUTES

# Routes whose workload (body ns and name) server.py checks an operator may
# act on before the route runs - for a request, and for an AI agent's tool
# calling the route (homestead_agent_tools).
TARGET_CHECKED = ("/api/move", "/api/move/preview", "/api/image-updates/apply", "/api/image-updates/rollback",
                  "/api/image-updates/preview", "/api/image-updates/notes")

POLICY = {
    ('GET', '/api/console'): 'operator',
    ('GET', '/api/node/shell'): 'admin',
    ('GET', '/api/vm/console'): 'operator',
}

POLICY.update({
    ('GET', '/api/addons/kube-vip'): "admin",
    ('GET', '/api/addons/kubevirt'): "admin",
    ('GET', '/api/addons/kubevirt/emulation'): "admin",
    ('GET', '/api/addons/longhorn'): "admin",
    ('GET', '/api/addons/multus'): "admin",
    ('GET', '/api/addons/multus/repair'): "admin",
    ('GET', '/api/addons/nfs/remove'): "admin",
    ('GET', '/api/addons/smb/remove'): "admin",
    ('POST', '/api/auth/history'): "admin",
    ('GET', '/api/auth/keys/revoke'): "admin",
    ('GET', '/api/auth/role'): "admin",
    ('GET', '/api/auth/users/delete'): "admin",
    ('POST', '/api/cluster/cleanup'): "admin",
    ('GET', '/api/cluster/cleanup/run'): "admin",
    ('GET', '/api/cluster/components/upgrade'): "admin",
    ('POST', '/api/cluster/removal'): "admin",
    ('GET', '/api/cluster/remove-node'): "admin",
    ('GET', '/api/cluster/upgrades/start'): "admin",
    ('GET', '/api/config/backup'): "admin",
    ('GET', '/api/config/inspect'): "admin",
    ('POST', '/api/config/parts'): "admin",
    ('GET', '/api/config/restore'): "admin",
    ('GET', '/api/fleet/accept'): "admin",
    ('GET', '/api/fleet/address'): "admin",
    ('GET', '/api/fleet/join'): "admin",
    ('GET', '/api/fleet/leave'): "admin",
    ('GET', '/api/fleet/link-legacy'): "admin",
    ('GET', '/api/fleet/remove'): "admin",
    ('GET', '/api/fleet/sync'): "admin",
    ('GET', '/api/images/cleanup'): "admin",
    ('GET', '/api/images/forget-rollback'): "admin",
    ('GET', '/api/images/scan'): "admin",
    ('GET', '/api/images/vm/delete'): "admin",
    ('GET', '/api/import'): "admin",
    ('GET', '/api/import/preview'): "admin",
    ('GET', '/api/imports/cleanup-plan'): "admin",
    ('GET', '/api/imports/delete'): "admin",
    ('GET', '/api/lh/backup/delete'): "admin",
    ('GET', '/api/lh/group/delete'): "admin",
    ('GET', '/api/lh/job/delete'): "admin",
    ('GET', '/api/lh/restore'): "admin",
    ('GET', '/api/lh/snapshot/delete'): "admin",
    ('GET', '/api/lh/snapshot/revert'): "admin",
    ('GET', '/api/lh/target'): "admin",
    ('GET', '/api/move/clusters/add'): "admin",
    ('GET', '/api/move/clusters/check'): "admin",
    ('GET', '/api/move/clusters/readiness'): "admin",
    ('GET', '/api/move/clusters/remove'): "admin",
    ('GET', '/api/move/clusters/storage'): "admin",
    ('GET', '/api/move/clusters/target'): "admin",
    ('GET', '/api/move/clusters/transfers'): "admin",
    ('POST', '/api/move/definition'): "admin",
    ('GET', '/api/move/moves/abandon'): "admin",
    ('GET', '/api/move/moves/dismiss'): "admin",
    ('GET', '/api/move/moves/finish'): "admin",
    ('GET', '/api/move/moves/retry'): "admin",
    ('GET', '/api/move/remote'): "admin",
    ('GET', '/api/move/source'): "admin",
    ('POST', '/api/move/source-status'): "admin",
    ('GET', '/api/move/start'): "admin",
    ('POST', '/api/move/target'): "admin",
    ('GET', '/api/namespaces/create'): "admin",
    ('GET', '/api/namespaces/delete'): "admin",
    ('GET', '/api/network/service/delete'): "admin",
    ('GET', '/api/network/vips/add'): "admin",
    ('GET', '/api/network/vips/default'): "admin",
    ('GET', '/api/network/vips/label'): "admin",
    ('GET', '/api/network/vips/remove'): "admin",
    ('GET', '/api/network/vm-networks'): "admin",
    ('GET', '/api/node/cordon'): "admin",
    ('GET', '/api/node/drain'): "admin",
    ('GET', '/api/node/hardware'): "admin",
    ('GET', '/api/node/power'): "admin",
    ('POST', '/api/node/probe/allocation/check'): "admin",
    ('GET', '/api/node/probe/install'): "admin",
    ('GET', '/api/node/probe/remove'): "admin",
    ('GET', '/api/node/smart/test'): "admin",
    ('GET', '/api/objectstore/deploy'): "admin",
    ('GET', '/api/objectstore/longhorn'): "admin",
    ('GET', '/api/objectstore/remove'): "admin",
    ('POST', '/api/onboard/guide'): "admin",
    ('GET', '/api/operations/power-recovery/preview'): "admin",
    ('GET', '/api/operations/power-recovery/resolve'): "admin",
    ('GET', '/api/operations/resume'): "admin",
    ('GET', '/api/operations/storage-recovery/act'): "admin",
    ('GET', '/api/operations/storage-recovery/preview'): "admin",
    ('GET', '/api/operations/vm-recovery/preview'): "admin",
    ('GET', '/api/operations/vm-recovery/resolve'): "admin",
    ('GET', '/api/platform/baseline/install'): "admin",
    ('GET', '/api/self/nfs'): "admin",
    ('GET', '/api/self/permissions'): "admin",
    ('GET', '/api/self/samba'): "admin",
    ('GET', '/api/setup/https-check'): "admin",
    ('GET', '/api/setup/opened'): "admin",
    ('GET', '/api/shares/delete'): "admin",
    ('GET', '/api/shares/edit'): "admin",
    ('GET', '/api/shares/nfs'): "admin",
    ('POST', '/api/shares/options'): "admin",
    ('GET', '/api/shares/repair'): "admin",
    ('GET', '/api/shares/users/delete'): "admin",
    ('GET', '/api/sources/browse'): "admin",
    ('GET', '/api/sources/containers'): "admin",
    ('GET', '/api/sources/delete'): "admin",
    ('GET', '/api/sources/inspect'): "admin",
    ('GET', '/api/sources/measure'): "admin",
    ('GET', '/api/sources/scan'): "admin",
    ('GET', '/api/sources/trust'): "admin",
    ('GET', '/api/sources/vms'): "admin",
    ('GET', '/api/sources/vms/shutdown'): "admin",
    ('GET', '/api/storage/classes/cleanup'): "admin",
    ('GET', '/api/storage/classes/default'): "admin",
    ('GET', '/api/storage/classes/delete'): "admin",
    ('GET', '/api/vm-disks/import'): "admin",
    ('POST', '/api/vm/isos/browse'): "admin",
    ('GET', '/api/vm/isos/delete'): "admin",
    ('GET', '/api/vm/isos/folders'): "admin",
    ('GET', '/api/vm/isos/keep'): "admin",
    ('GET', '/api/vm/store/auto'): "admin",
    ('GET', '/api/vm/store/forget'): "admin",
    ('GET', '/api/vm/store/keep'): "admin",
    ('GET', '/api/vm/store/refresh'): "admin",
    ('GET', '/api/vms/import-unraid'): "admin",
    ('GET', '/api/vms/import-unraid/room'): "admin",
    ('GET', '/api/volumes/chown'): "admin",
    ('GET', '/api/volumes/delete'): "admin",
    ('GET', '/api/volumes/old-copies/remove'): "admin",
    ('GET', '/api/volumes/reclass/start'): "admin",
})

def role(path, method):
    """The role a request needs: the policy's or the module route's, and when
    a module's prefix route covers a path the policy names, the stricter."""
    declared, route = _declared(path, method), MODULE_ROUTES.find(method, path)
    if route is None or declared is None:
        return declared if route is None else route.role
    return max(declared, route.role, key=MODULE_ROUTES.ROLES.index)


# The paths _declared() answers by prefix or pattern, and server.py by its own
# prefix branch: no module prefix route may cover them. A PR that moves one into
# a module lifts exactly its entry (#764: workload DELETE in part 6, the
# handoff in part 7). /api/v1/ is homestead_api_v1's alone (homestead_routes.table).
CODE_PREFIXES = ("/api/icons/",)


def _declared(path, method):
    return POLICY.get((method, path))
