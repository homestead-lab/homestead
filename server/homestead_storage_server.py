"""The storage code that lived in server.py (track 1, part 4): which objects
are Homestead's own SMB and NFS servers, storage-class facts and the share
storage options, Longhorn state by claim, restores and their tidy-up,
restore tests, the storage runtime check and move progress, weekly trim
and the host fixes, the storage loops, and the snapshot-file and share-server
routes server.py's handler answered.

server.py binds what it still owns through bind_routes(): each helper in
ROUTE_NEEDS is looked up in server.py when it is called, or, once moved
(server.MOVED), in the module it moved to, so a test that patches it there
reaches it here too; each name in VALUES is server.py's own value (the same
object, for _cache and _lock)."""
import json
import os
import time
import urllib.request, urllib.parse, urllib.error
import homestead_cancel as CANCEL
import homestead_copy_job as COPY_JOB
import homestead_data_copies as DATA_COPIES
import homestead_disks as DISKS
import homestead_files as FILES
import homestead_fleet as FLEET
import homestead_host_limits as HOST_LIMITS
import homestead_host_os as HOST_OS
import homestead_isos as ISOS
import homestead_kube as KUBE
import homestead_leader as LEADER
import homestead_longhorn as LH
import homestead_longhorn_restore as RESTORE
import homestead_longhorn_access as LH_ACCESS
import homestead_manifests as MANIFESTS
import homestead_names as NAMES
import homestead_nfs as NFS
import homestead_node_parity as NODE_PARITY
import homestead_objectstore as OBJECTS
import homestead_operations as OPS
import homestead_place as PLACE
import homestead_reclass as RECLASS
import homestead_restore_swap as RESTORE_SWAP
import homestead_restore_test as RESTORE_TEST
import homestead_revert as REVERT
import homestead_root_guard as ROOT_GUARD
import homestead_self as SELF
import homestead_share_servers as SHARE_SERVERS
import homestead_shares as SHARES
import homestead_snapshot_delete as SNAPSHOT_DELETE
import homestead_snapshot_files as SNAPSHOT_FILES
import homestead_snapshot_restore as SNAPSHOT_RESTORE
import homestead_storage as STORAGE
import homestead_storage_admission as STORAGE_ADMISSION
import homestead_storage_classes as STORAGE_CLASSES
import homestead_storage_guard as STORAGE_GUARD
import homestead_storage_pending as STORAGE_PENDING
import homestead_storage_runtime as STORAGE_RUNTIME
import homestead_storage_workflow as STORAGE_WORKFLOW
import homestead_weekly_trim as WEEKLY_TRIM
import homestead_workloads as WORKLOADS

ROUTE_NEEDS = ("beat", "cached", "copy_admission", "create_pvc", "get_app_settings",
               "guard_managed_smb", "is_self", "kget", "require_self_data_write",
               "selectable_storage_classes", "self_data_activity", "storage_classes",
               "update_policy_status")
VALUES = ("DATA_DIR", "HOMESTEAD_VERSION", "SMB_NAMESPACE", "_cache", "_lock")


def bind_routes(helpers, **values):
    missing = set(ROUTE_NEEDS) - set(helpers)
    if missing:
        raise TypeError("homestead_storage_server needs " + ", ".join(sorted(missing)))
    if set(values) != set(VALUES):
        raise TypeError("homestead_storage_server is bound with " + ", ".join(sorted(set(values) ^ set(VALUES))))
    globals().update({name: helpers[name] for name in ROUTE_NEEDS})
    globals().update(values)
    # The weekly trim (#591): its state file, and its kind in the job store.
    WEEKLY_TRIM.bind(DATA_DIR)
    WEEKLY_TRIM.register(OPS)


def is_managed_smb(ns, name):
    return (ns, name) in ((SMB_NAMESPACE, NAMES.object_name("smb")), (SMB_NAMESPACE, "samba"))


def is_managed_nfs(ns, name):
    return (ns, name) == (SMB_NAMESPACE, NFS.NAME)


def homestead_part(ns, name):
    """Which part of Homestead a Deployment is - itself, or a helper it runs
    and keeps in step: the SMB and NFS servers, the object store moves use.
    Empty for anything else. The Containers page hides these with the
    platform, and their updates are Homestead's, not an app's."""
    if is_self(ns, name):
        return "self"
    if is_managed_smb(ns, name):
        return "smb"
    if is_managed_nfs(ns, name):
        return "nfs"
    if (ns, name) == (OBJECTS.NS, OBJECTS.NAME):
        return "objectstore"
    return ""


def guard_smb_object(kind, ns, name):
    """Keep the general Kubernetes editor from bypassing Network Shares."""
    if ns != SMB_NAMESPACE:
        return
    kind = str(kind or "").lower()
    if kind in ("deployment", "deployments", "service", "services"):
        guard_managed_smb(ns, name)
    if (kind in ("configmap", "configmaps") and name == SHARES.CONFIGMAP()) or (
            kind in ("secret", "secrets") and name == SHARES.SECRET()):
        raise ValueError("Homestead manages SMB share settings from Network Shares.")


def longhorn_state_by_pvc(namespace):
    """Longhorn's live view of each claim: health, and where it is attached.

    A claim that is Bound tells you nothing about whether a second pod on
    another node can mount it, which is exactly the question a storage picker
    is asking.
    """
    try:
        volumes = kget("/apis/longhorn.io/v1beta2/volumes").get("items", [])
    except Exception:
        return {}
    state = {}
    for volume in volumes:
        status = volume.get("status", {}) or {}
        kubernetes = status.get("kubernetesStatus", {}) or {}
        if kubernetes.get("namespace") != namespace or not kubernetes.get("pvcName"):
            continue
        workloads = sorted({row.get("workloadName") or row.get("podName") or ""
                            for row in kubernetes.get("workloadsStatus") or []
                            if row.get("workloadName") or row.get("podName")})
        state[kubernetes["pvcName"]] = {
            "robustness": status.get("robustness", ""),
            "node": status.get("currentNodeID", ""),
            "migratable": bool((volume.get("spec", {}) or {}).get("migratable")),
            "migrating_to": (volume.get("spec", {}) or {}).get("migrationNodeID", ""),
            "workloads": workloads,
        }
    return state


def share_storage_options():
    """Volumes a new share can be created on, in the namespace Samba runs in."""
    ns = SMB_NAMESPACE
    classes = storage_classes()
    node = ""
    try:
        node = SHARES._samba_node()
    except Exception:
        node = ""
    try:
        samba = bool(SHARES._deployment_state()[2])
    except Exception:
        samba = False
    return {"namespace": ns, "node": node, "pvcs": _pvc_rows(ns), "samba_installed": samba,
            "storage_classes": selectable_storage_classes(classes),
            "shared_storage_classes": shared_storage_classes(classes),
            "storage_class_facts": storage_class_facts(classes)}


def storage_class_facts(rows=None):
    """The handful of class facts worth showing next to a class picker."""
    return {row["name"]: {"replicas": row["replicas"], "engine": row["engine"], "migratable": row["migratable"],
                          "encrypted": row["encrypted"], "expandable": row["expandable"],
                          "reclaim": row["reclaim"], "default": row["default"]}
            for row in (rows if rows is not None else storage_classes()) if class_selectable(row)}


def shared_storage_classes(rows=None):
    """Classes that can actually serve ReadWriteMany to a pod."""
    return [row["name"] for row in (rows if rows is not None else storage_classes())
            if class_selectable(row) and not row["migratable"] and serves_many(row)]


def _restore_then_tidy(item, _resolve=OPS.RESOLVERS["volume-restore"]):
    """Resume snapshot setup after a restart and retain the selected class."""
    ref = item["ref"]
    cfg = ref.get("restore_config")
    if cfg and not ref.get("restore_started"):
        if item.get("status") == "queued":
            cfg["snapshot_wait_started"] = time.time()
        since = cfg.setdefault("snapshot_wait_started", time.time())
        if time.time() - float(since) > 15 * 60:
            raise ValueError("CSI snapshot setup did not become ready; repair snapshot support, then resume this restore")
        existing = KUBE.get_optional(f"/api/v1/namespaces/{ref['namespace']}/persistentvolumeclaims/{ref['name']}", read=LH.kget)
        if existing:
            annotation = (existing.get("metadata", {}).get("annotations") or {}).get("homestead.io/restore-id")
            if annotation != cfg["restore_id"]:
                raise ValueError("The destination PVC was created by another operation")
            ref["restore_started"] = True
        else:
            created = RESTORE.restore_backup(cfg)
            if not created["created"]:
                return "running", 2, created["message"]
            if created.get("snapshot"):
                ref["snapshot"] = created["snapshot"]    # what a cancel removes
            ref["restore_started"] = True
    pvc = KUBE.get_optional(f"/api/v1/namespaces/{ref['namespace']}/persistentvolumeclaims/{ref['name']}", read=LH.kget)
    if pvc:
        if cfg and (pvc.get("metadata", {}).get("annotations") or {}).get("homestead.io/restore-id") != cfg["restore_id"]:
            raise ValueError("The destination PVC was replaced by another operation")
        problem = RESTORE.restore_problem(pvc)
        if problem:
            return "failed", 8, f"CSI restore snapshot failed: {problem}"
    result = _resolve(item)
    if result and result[0] == "succeeded" and cfg and pvc:
        result = RESTORE.finish_restore_resize(pvc, cfg) or result
    if result and result[0] in ("succeeded", "failed"):
        cleanup_restores()
    return result


def storage_runtime_check():
    try:
        return STORAGE_RUNTIME.require(OPS, kget, SELF.NS, SELF.POD, NAMES.BRAND, HOMESTEAD_VERSION, DATA_DIR)
    except STORAGE_WORKFLOW.JOURNAL.Held:
        raise
    except Exception:
        raise STORAGE_WORKFLOW.JOURNAL.Held("Homestead replica compatibility could not be verified; restore cluster and shared-data access before moving storage") from None


def _storage_runtime_loop():
    # Every replica reports, not only the leader. No capability is inferred on
    # behalf of an older binary that does not understand the new journal.
    while True:
        try:
            STORAGE_RUNTIME.report(OPS, kget, SELF.NS, SELF.POD, NAMES.BRAND, HOMESTEAD_VERSION, DATA_DIR, self_data=True)
            beat("storage-runtime", 20)
        except Exception as error:
            beat("storage-runtime", 20, error)
        time.sleep(20)


def storage_move_progress(item):
    if "storage_protocol" in item.get("ref", {}):
        return STORAGE_WORKFLOW.resolve(item, OPS.checkpoint, storage_helper_admission, storage_restart_admission,
                                        runtime_check=storage_runtime_check)
    # Keep pre-upgrade jobs on their existing steps; never infer receipts for
    # mutations made by an older engine. Exempt only this job from its own fence.
    with STORAGE_GUARD.dispatching(item):
        return RECLASS.resolve(item)


def storage_volume_action(volume, action):
    # Snapshot rollback also stops/restarts workloads and writes through the
    # Longhorn REST API, not just ksend. Hold the fence around the entire step.
    with STORAGE_GUARD.volume(OPS, kget, volume):
        return action()


def storage_legacy_cancel(item, options):
    if "storage_protocol" in item.get("ref", {}):
        raise ValueError("Use the storage move recovery review; legacy rollback is not supported for this job")
    with OPS.transaction(), STORAGE_GUARD.dispatching(item):
        return CANCEL.reclass_cancel(item, options)


def _storage_classes():
    return {c["metadata"]["name"]: c.get("parameters") or {}
            for c in kget("/apis/storage.k8s.io/v1/storageclasses").get("items", [])}


def restore_tests_tick():
    """On the leader, hourly: test the app longest untested, when switched on."""
    settings = get_app_settings()
    chosen = RESTORE_TEST.pick(cached("wl", 5, WORKLOADS.get_workloads), OPS.snapshot(),
                               settings.get("restore_tests", {}).get("enabled", False),
                               update_policy_status(settings)["window_open"], time.time(), _storage_classes())
    if chosen:
        RESTORE_TEST.start_test(chosen["ns"], chosen["name"], "monthly")
    return chosen


def data_protection_tick():
    """On the leader, hourly: a restore test when one is due, and the expiry
    of previous volumes (#743). Each runs even when the other failed; the
    first failure is raised for the heartbeat."""
    errors = []
    for tick in (lambda: restore_tests_tick(), lambda: RESTORE_SWAP.expire_tick()):
        try:
            tick()
        except Exception as error:
            errors.append(error)
    if errors:
        raise errors[0]


def _restore_tests_loop():
    while True:
        if LEADER.is_leader():
            try:
                data_protection_tick()
                beat("restore-tests", 3600, leader_only=True)
            except Exception as error:
                beat("restore-tests", 3600, error, leader_only=True)
                print(f"restore-tests: {str(error)[:160]}", flush=True)
        time.sleep(3600)


def _prune_snapshot(volume, name):
    """Remove one old before-update snapshot, as Data Protection would."""
    plan = SNAPSHOT_DELETE.plan(volume, name)
    if not plan["blockers"] and not plan["deleting"] and not plan["removed"]:
        SNAPSHOT_DELETE.start({**plan, "confirmation": name}, OPS)


def weekly_trim():
    """Homestead's weekly trim of every attached Longhorn volume
    (homestead_weekly_trim): started here when it is due."""
    return WEEKLY_TRIM.tick(time.time(), OPS.snapshot())


def _storage_pending_loop():
    """On the leader, a node that has just joined is steered clear of until
    Longhorn is ready on it. Often, and cheap: the gap is a minute or two."""
    time.sleep(20)
    while True:
        if LEADER.is_leader():
            try:
                for node, change in STORAGE_PENDING.tick():
                    print(f"platform: {node}: {change}", flush=True)
                beat("storage-pending", 15, leader_only=True)
            except Exception as error:
                beat("storage-pending", 15, error, leader_only=True)
        time.sleep(15)


def _snapshot_files_loop():
    while True:
        if LEADER.is_leader():
            try:
                with self_data_activity():
                    require_self_data_write()
                    SNAPSHOT_FILES.cleanup()
                beat("snapshot-files", 30, leader_only=True)
            except Exception as error:
                beat("snapshot-files", 30, error, leader_only=True)
        time.sleep(30)


def _files_loop():
    while True:
        if LEADER.is_leader():
            try:
                with self_data_activity():
                    require_self_data_write()
                    FILES.cleanup()
                beat("volume-files", 30, leader_only=True)
            except Exception as error:
                beat("volume-files", 30, error, leader_only=True)
        time.sleep(15)


def _longhorn_copies_loop():
    """On the leader, every minute: Longhorn's default copies follow the
    number of Ready nodes as they join (homestead_node_parity.copies_tick),
    before apps made right after are given one copy. First, on a one-host
    cluster, Homestead's own data volume asks for the one copy that host can
    keep (homestead_data_copies.fit_single_host)."""
    time.sleep(30)
    while True:
        if LEADER.is_leader():
            try:
                note = DATA_COPIES.fit_single_host()
                if note:
                    print(f"platform: {note}", flush=True)
            except Exception as error:
                print(f"platform: Homestead's data copies not fitted: {str(error)[:160]}", flush=True)
            try:
                note = NODE_PARITY.copies_tick()
                if note:
                    print(f"platform: {note}", flush=True)
            except Exception as error:
                print(f"platform: Longhorn copies not checked: {str(error)[:160]}", flush=True)
        time.sleep(60)


def _host_fix_loop():
    """On the leader, what k3s and RKE2 hosts need or undo at each start: inotify
    limits a busy node outgrows (homestead_host_limits.py), what a node that
    joined later needs to match the others (homestead_node_parity.py), the installer's
    auto-deploy files (homestead_manifests.py), and a second default storage
    class; and VMs still holding an ISO read-only. Checked a minute after
    starting, then every ten minutes."""
    time.sleep(60)
    while True:
        if LEADER.is_leader():
            try:
                with self_data_activity():
                    for node, change in HOST_LIMITS.tick().items():
                        print(f"platform: {node}: inotify limits raised ({change})", flush=True)
                    # A node that joined later, set up as the others are.
                    for node, change in NODE_PARITY.tick():
                        print(f"platform: {node + ': ' if node else ''}{change}", flush=True)
                    # Each host's OS - updates, restarts, failed services - every six hours.
                    HOST_OS.tick()
                    # Restored volumes whose class an older release removed.
                    try:
                        repaired = STORAGE_CLASSES.repair_restore_classes()
                        if repaired:
                            print(f"storage: restore classes put back for resizing: {', '.join(repaired)}", flush=True)
                    except Exception as error:
                        print(f"storage: restore classes not checked: {str(error)[:160]}", flush=True)
                    # Linked clusters told this Homestead's address, once it is on its VIP.
                    try:
                        moved = FLEET.follow_address()
                        if moved:
                            print(f"linked clusters: told this Homestead is at {moved}", flush=True)
                    except Exception as error:
                        print(f"linked clusters: address not checked: {str(error)[:160]}", flush=True)
                    # Homestead's way in to Longhorn's API, where network policies
                    # fence Longhorn's manager in (Longhorn 1.13's default). First,
                    # so nothing after it can keep it from running.
                    try:
                        access = LH_ACCESS.ensure(LH.kget, LH.ksend, SELF.NS)
                        if access:
                            print(f"storage: {LH_ACCESS.LOG[access]}", flush=True)
                    except Exception as error:
                        print(f"storage: way in to Longhorn's API not checked: {str(error)[:160]}", flush=True)
                    # A weekly trim of every attached Longhorn volume, unless someone turned it off.
                    trimmed = weekly_trim()
                    if trimmed:
                        print(f"storage: {trimmed}", flush=True)
                    # Longhorn kept from filling a host's root filesystem.
                    for node, change in ROOT_GUARD.tick():
                        print(f"storage: {node}: {change}", flush=True)
                    for node, marked in MANIFESTS.tick().items():
                        print(f"platform: {node}: k3s no longer re-applies "
                              f"{', '.join(marked) or 'no installer files (none left)'} at start", flush=True)
                    for node, disk, tags in DISKS.auto_tag():
                        print(f"storage: {node} {disk} tagged {', '.join(tags)}", flush=True)
                    # VMs made before 2.8.228 hold their ISO read-only and cannot start.
                    for name in ISOS.unlock():
                        print(f"ISO library: {name} no longer holds its ISO read-only", flush=True)
                    fixed = STORAGE_CLASSES.reconcile_default_class()
                    if fixed:
                        print(f"storage: {fixed[0]} kept as the default class; "
                              f"{', '.join(fixed[1])} no longer default", flush=True)
                        with _lock:
                            _cache.pop("classes", None)
                beat("host-fixes", 600, leader_only=True)
            except Exception as error:
                beat("host-fixes", 600, error, leader_only=True)
                print(f"platform: {str(error)[:200]}", flush=True)
        time.sleep(600)


def _samba_loop():
    """One leader keeps SMB's mounts and image aligned with saved settings."""
    while True:
        if LEADER.is_leader():
            try:
                with self_data_activity():
                    state = SHARE_SERVERS.samba_state()
                    if state.get("name") == "samba" and state.get("installed"):
                        SHARE_SERVERS.install_samba()
                    result = SHARES.reconcile_samba(SHARE_SERVERS.SAMBA_IMAGE)
                beat("samba", 60, leader_only=True)
                if result.get("state") == "repaired":
                    print("network shares: restored SMB settings from the share inventory", flush=True)
            except Exception as error:
                beat("samba", 60, error, leader_only=True)
                print(f"network shares: {str(error)[:180]}", flush=True)
            try:
                with self_data_activity():
                    nfs_result = SHARE_SERVERS.reconcile_nfs()
                if nfs_result.get("state") == "updated":
                    print("network shares: restored NFS exports from the share inventory", flush=True)
            except Exception as error:
                print(f"NFS exports: {str(error)[:180]}", flush=True)
            try:
                with self_data_activity():
                    moved = OBJECTS.keep_in_step()
                    OBJECTS.reconcile_target()
                if moved:
                    print(f"object store: reconciled {moved}", flush=True)
            except Exception as error:
                print(f"object store: {str(error)[:180]}", flush=True)
        time.sleep(60)


def _pvc_rows(namespace):
    """Every claim in a namespace, with the Longhorn facts a picker needs."""
    live = longhorn_state_by_pvc(namespace)
    rows = []
    for item in kget(f"/api/v1/namespaces/{namespace}/persistentvolumeclaims").get("items", []):
        spec, status = item.get("spec", {}), item.get("status", {})
        name = item["metadata"]["name"]
        facts = live.get(name, {})
        rows.append({
            "name": name,
            "size": (status.get("capacity", {}) or {}).get("storage") or
                    (spec.get("resources", {}).get("requests", {}) or {}).get("storage", ""),
            "status": status.get("phase", "Unknown"),
            "access_modes": spec.get("accessModes", []) or [],
            "storage_class": spec.get("storageClassName", ""),
            "robustness": facts.get("robustness", ""),
            "node": facts.get("node", ""),
            "migratable": facts.get("migratable", False),
            "workloads": facts.get("workloads", []),
        })
    return sorted(rows, key=lambda row: row["name"])


# Harvester keeps these for VM images and VM state; they are not general
# purpose storage and Harvester's own UI marks them internal.
INTERNAL_STORAGE_CLASSES = {"longhorn-static", "vmstate-persistence"}


def _internal_class(meta):
    annotations = meta.get("annotations", {}) or {}
    return (meta.get("name", "") in INTERNAL_STORAGE_CLASSES or
            str(annotations.get("harvesterhci.io/is-reserved-storageclass", "")).lower() == "true")


def _class_made_for(name, parameters):
    """A class made for one thing, not for choosing: a Harvester image's
    (its disks start as that image - named longhorn-image-* or lh-<uuid>,
    with the image as its backing image) or a restore's (Homestead's
    homestead-restore-*, reading one backup). Neither belongs in a picker."""
    if name.startswith("homestead-restore-") or parameters.get("fromBackup"):
        return "restore"
    if parameters.get("backingImage") or name.startswith("longhorn-image-"):
        return "image"
    if name == NAMES.object_name("isos"):
        # Homestead's ISO copies: one replica, not a class to put data on.
        return "iso"
    return ""


def class_selectable(row):
    return not row.get("internal") and not row.get("made_for")


# Provisioners that can serve one volume to pods on several nodes at once.
# k3s's local-path and most block CSI drivers cannot: a ReadWriteMany claim on
# them stays Pending for good.
SHARED_DRIVERS = {"driver.longhorn.io", "nfs.csi.k8s.io", "efs.csi.aws.com", "file.csi.azure.com", "smb.csi.k8s.io"}


def serves_many(row):
    provisioner = str(row.get("provisioner") or "")
    return provisioner in SHARED_DRIVERS or provisioner.endswith(".cephfs.csi.ceph.com")


# The routes server.py's handler answered for this code.
def _snapshot_files_plan(request):
    q = request.query
    return SNAPSHOT_FILES.review((q.get("volume") or [""])[0], (q.get("snapshot") or [""])[0])


def _snapshot_files_session(request):
    q, p = request.query, request.path
    ns, session = (q.get("namespace") or [""])[0], (q.get("session") or [""])[0]
    if p.endswith("/status"):
        return SNAPSHOT_FILES.status(ns, session)
    if p.endswith("/download"):
        return SNAPSHOT_FILES.download(ns, session, q.get("path") or [""])
    path = (q.get("path") or [""])[0]
    return SNAPSHOT_FILES.read(ns, session, "list", path)


def _snapshot_files_start(request):
    b = request.body
    return storage_volume_action(b.get("volume"), lambda: SNAPSHOT_FILES.start(b))


def _remove_samba(request):
    if request.body.get("confirm") != SHARE_SERVERS.SMB_NAME:
        raise ValueError(f"type {SHARE_SERVERS.SMB_NAME} to remove the SMB server")
    return SHARE_SERVERS.remove_samba()


def _remove_nfs(request):
    if request.body.get("confirm") != NFS.NAME:
        raise ValueError(f"type {NFS.NAME} to remove the NFS server")
    return SHARE_SERVERS.remove_nfs()


ROUTES = {
    ("GET", "/api/snapshot-files/plan"): ("admin", _snapshot_files_plan),
    ("GET", "/api/snapshot-files/status"): ("admin", _snapshot_files_session),
    ("GET", "/api/snapshot-files/list"): ("admin", _snapshot_files_session),
    ("GET", "/api/snapshot-files/download"): ("admin", _snapshot_files_session),
    ("POST", "/api/snapshot-files/start"): ("admin", _snapshot_files_start),
    ("POST", "/api/snapshot-files/restore/plan"): ("admin", lambda request: SNAPSHOT_RESTORE.restore_plan(request.body)),
    ("POST", "/api/snapshot-files/restore"): ("admin", lambda request: SNAPSHOT_RESTORE.restore(request.body)),
    ("POST", "/api/snapshot-files/close"): ("admin", lambda request: SNAPSHOT_FILES.close(request.body.get("namespace"),
                                                                                       request.body.get("session"))),
    ("POST", "/api/self/samba"): ("admin", lambda request: SHARE_SERVERS.set_samba(
        bool(request.body.get("enabled")), str(request.body.get("address") or "").strip())),
    ("POST", "/api/self/nfs"): ("admin", lambda request: SHARE_SERVERS.set_nfs(
        bool(request.body.get("enabled")), str(request.body.get("address") or "").strip())),
    ("POST", "/api/addons/smb/remove"): ("admin", _remove_samba),
    ("POST", "/api/addons/nfs/remove"): ("admin", _remove_nfs),
}


def ensure_claim(ns, name, size_gb, sc=None, access_mode="ReadWriteOnce"):
    """A deploy's new volume - or the one of that name already there, when
    nothing uses it.

    A failed install leaves its volumes behind (deleting a container keeps
    its data), and installing again then stopped at "already exists". One
    that nothing refers to is taken as it is, data and all, and said so; one
    another container, VM or job uses is refused by name.
    """
    try:
        existing = kget(f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{name}")
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        existing = None
    if not existing:
        create_pvc(ns, name, size_gb, sc, access_mode)
        return ""
    try:
        users = STORAGE._references_for(STORAGE.claim_references(), ns, name)
    except Exception:
        users = None
    if users is None:
        raise ValueError(f"a volume named {name} already exists in {ns}, and Homestead could not check what uses it; "
                         "try again, give this app's volume another name, or choose the existing one to share it")
    if users:
        raise ValueError(f"a volume named {name} already exists in {ns} and {', '.join(users)} uses it; "
                         "give this app's volume another name, or choose the existing one to share it")
    return name


def cleanup_restores():
    for cleanup in (STORAGE_CLASSES.cleanup_restore_classes, RESTORE.cleanup_restore_snapshots):
        try:
            cleanup()
        except Exception as error:
            # Cleanup may be retried; it must not change a completed restore
            # into a failed data operation. The cleanup itself fails closed.
            print(f"Restore metadata cleanup deferred: {error}", flush=True)


def storage_restart_admission(item, proposals):
    return STORAGE_ADMISSION.plan(item, proposals, kget, PLACE.get_nodes(), get_app_settings()["thresholds"]["memory"]["critical"])


def storage_helper_admission(item, manifest):
    return copy_admission(manifest)


def register(ops):
    """The storage jobs whose resolvers live here (#622); cancelling a failed
    copy tidies up after it. The Kubernetes client and the copy admission are
    looked up when a job is asked about, as server.py did, so a test that
    patches one is the one used. The reclass canceller replaces the one
    homestead_cancel registers, so this runs after it."""
    ops.RESOLVERS["volume-restore"] = _restore_then_tidy
    ops.RESUMABLE["volume-restore"] = lambda item: not bool(item.get("ref", {}).get("restore_config"))
    ops.RESOLVERS["restructure"] = COPY_JOB.legacy_status
    ops.CLEANUPS.update((COPY_JOB.KIND, "restructure"))
    ops.RESOLVERS[COPY_JOB.KIND] = lambda item: COPY_JOB.resolve(item, KUBE.kget, KUBE.ksend, ops, copy_admission)
    ops.CANCELLERS[COPY_JOB.KIND] = (lambda item: COPY_JOB.recovery_plan(item, KUBE.kget, ops),
                                     lambda item, options: COPY_JOB.recovery_run(item, options, KUBE.kget, KUBE.ksend, ops))
    ops.RESOLVERS["reclass"] = storage_move_progress
    ops.RESUMABLE["reclass"] = RECLASS.resumable
    ops.CANCELLERS["reclass"] = (CANCEL.reclass_plan, storage_legacy_cancel)
    ops.RESOLVERS["snapshot-revert"] = lambda item: storage_volume_action(item["ref"]["volume"], lambda: REVERT.resolve(item))
    ops.CANCELLERS["snapshot-revert"] = (REVERT.cancel_plan,
        lambda item, options: storage_volume_action(item["ref"]["volume"], lambda: REVERT.cancel_run(item, options)))
    ops.RESOLVERS["snapshot-delete"] = lambda item: storage_volume_action(item["ref"]["volume"], lambda: SNAPSHOT_DELETE.resume_resolve(item))
    ops.RESUMABLE["snapshot-delete"] = SNAPSHOT_DELETE.resumable
    # Restore into this app (#743) registers here, so server.py gets no line.
    RESTORE_SWAP.register(ops)
