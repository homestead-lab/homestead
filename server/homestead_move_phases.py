"""The phases of a move, and the tick that drives them.

Each phase is a question with a yes-or-not-yet answer (joining, quiescing,
backing up, releasing the source, syncing, restoring, creating, starting).
tick_all asks every running move's current question once; the leader calls it
every few seconds (homestead_deploy_server). Finishing a move tidies what it
created and gives the backup target back.

Split out of homestead_move_engine (#623).
"""
import hashlib
import uuid
import json
import time
import urllib.error
import urllib.parse
import homestead_shared as SHARED
import homestead_icons as ICONS
import homestead_kube as KUBE
import homestead_names as NAMES
import homestead_storage_classes as STORAGE_CLASSES
import homestead_passthrough as PASSTHROUGH
import homestead_vm_transfer_devices as VM_DEVICES
import homestead_transfer_cleanup as CLEANUP
import homestead_platform as PLATFORM
import homestead_move_state as STATE
import homestead_move_target as TARGET
import homestead_move_plan as PLAN
import homestead_move_actions as ACT


def _joining(move):
    if STATE._moving(move):
        support = STATE.LH.ensure_restore_support()
        if not support["ready"]:
            since = move.setdefault("flags", {}).setdefault("snapshot_wait_started", time.time())
            if time.time() - since > STATE.START_GRACE:
                raise ValueError("CSI snapshot support did not become ready; repair the snapshot controller, then retry. The source has not been stopped")
            return STATE._note(move, 1, support["message"])
    there = STATE.CLIENT.remote(move["cluster"], "/api/move/target")
    if TARGET._borrow_target(move, there):
        return STATE._advance(move, "quiescing", 4, f"Reading backups from {move['cluster']}'s storage")


def _quiescing(move):
    flags = move.setdefault("flags", {})
    if not flags.get("quiesced"):
        definition = None
        if move["kind"] == "vm":
            # Revalidate inventory immediately before interrupting the source.
            definition = STATE._definition(move)
            if move.get("device_signature") and VM_DEVICES.signature(definition) != move["device_signature"]:
                raise ValueError("The source passthrough devices or vBIOS changed; cancel and review this transfer again")
            VM_DEVICES.prepare(definition, move["namespace"], move.get("host_devices"))
        if not flags.get("quiesce_requested"):
            if move.get("transfer_mode") == "copy":
                definition = definition or STATE._definition(move)
                flags["quiesce_version"] = definition.get("source_version", "")
            flags["quiesce_requested"] = True
            STATE._store(move)  # A lost reply must still offer source recovery.
        stopped = STATE._source_action(move, "quiesce", **({"live": True} if move.get("live") else {}))
        if stopped.get("origin"):
            move["origin"] = stopped["origin"]
        flags["quiesced"] = True
    status = STATE._source_status(move)
    if move.get("live") and not (status.get("origin") or {}).get("_copy_live"):
        # A source that ignored the request to keep running stopped it instead.
        raise ValueError(f"{move['cluster']} did not hold {move['name']} for a live copy; update Homestead there, then retry")
    if status.get("running") and not move.get("live"):
        return STATE._note(move, 6, f"Waiting for {move['name']} to stop on {move['cluster']}")
    if move["kind"] == "volume":
        return STATE._advance(move, "backing-up", 10, f"{move['name']} is held on {move['cluster']}")
    if (move.get("transfer_mode") == "copy" or move.get("host_devices")) and not move.get("definition"):
        definition = STATE._definition(move)
        if move["kind"] == "vm" and move.get("device_signature") and VM_DEVICES.signature(definition) != move["device_signature"]:
            raise ValueError("The source passthrough devices or vBIOS changed; cancel and review this transfer again")
        keys = ("claim", "volume", "size_gb", "access_mode", "volume_mode")
        if [tuple(c.get(k) for k in keys) for c in definition["claims"]] != [tuple(c.get(k) for k in keys) for c in move["claims"]]:
            raise ValueError("The source disks changed; cancel this copy and review it again")
        move["definition"] = definition
    if move.get("live"):
        return STATE._advance(move, "backing-up", 10, f"{move['name']} keeps running on {move['cluster']}; snapshotting its disks")
    return STATE._advance(move, "backing-up", 10, f"{move['name']} stopped on {move['cluster']}")


def _backing_up(move):
    flags = move.setdefault("flags", {})
    # A new move may follow a dismissed/cleared attempt while the source still
    # remembers its backups. Repair failed references on the first pass too.
    moving = STATE._moving(move)
    if not moving:
        return STATE._advance(move, "releasing-source" if move.get("transfer_mode") == "copy" else "restoring",
                        50, "No volume data to back up")
    names = [c["claim"] for c in moving]
    extra = {} if len(moving) == len(move["claims"]) else {"claims": names}
    if move.get("cleanup_protocol") == 1:
        extra["cleanup_id"] = move["id"]
    made = STATE._source_action(move, "backup", retry_failed=bool(flags.get("retry_backups")) or not flags.get("backed_up"),
                          **extra)
    by_claim = {row["claim"]: row["backup"] for row in made.get("backups", [])}
    for claim in moving:
        claim["backup"] = by_claim.get(claim["claim"], claim.get("backup", ""))
    flags["backed_up"] = True
    flags.pop("retry_backups", None)
    rows = [r for r in STATE._source_status(move).get("backups", []) if r.get("claim") in names]
    for row in rows:
        state = str(row.get("state", "")).lower()
        if state in ("error", "failed") or row.get("error"):
            raise ValueError(f"backup of {row['claim']} failed on {move['cluster']}: "
                             f"{row.get('error') or state}. Retry the move to take a fresh snapshot; completed backups are kept")
        image = row.get("image") or {}
        if str(image.get("state", "")).lower() in ("error", "failed") or image.get("error"):
            raise ValueError(f"backup of the {row.get('backing_image')} image failed: "
                             f"{image.get('error') or image.get('state')}")
    if not rows:
        return STATE._note(move, 10, "Waiting for backups to start")
    done = [r for r in rows if str(r.get("state", "")).lower() == "completed"
            and (not r.get("backing_image")
                 or str((r.get("image") or {}).get("state", "")).lower() in ("completed", "ready"))]
    average = sum(int(r.get("progress") or 0) for r in rows) / max(1, len(rows))
    if len(done) == len(moving):
        return STATE._advance(move, "releasing-source" if move.get("transfer_mode") == "copy" else "syncing",
                        50, "Every selected volume is backed up")
    return STATE._note(move, 10 + 40 * average / 100,
                 f"Backing up {len(rows)} volume{'' if len(rows) == 1 else 's'} on "
                 f"{move['cluster']}: {int(average)}%")


def _releasing_source(move):
    flags = move.setdefault("flags", {})
    if not flags.get("source_released"):
        STATE._source_action(move, "release")
        flags["source_released"] = True
    return STATE._advance(move, "syncing", 50, "Source running state restored; restoring the stopped copy here")


def _request_sync(move):
    flags = move.setdefault("flags", {})
    if time.time() - float(flags.get("synced_at", 0)) < 60:
        return
    flags["synced_at"] = time.time()
    try:
        STATE.ksend("PATCH", f"{STATE.LH_API}/namespaces/{STATE.LHNS}/backuptargets/default",
              {"spec": {"syncRequestedAt": STATE._now()}}, ctype="application/merge-patch+json")
    except Exception:
        pass                # Longhorn will look on its own interval regardless


def _syncing(move):
    _request_sync(move)
    here = {row["name"]: row for row in STATE.LH.backups()}
    waiting = [c["claim"] for c in STATE._moving(move) if not here.get(c["backup"], {}).get("restorable")]
    # An image a disk is built on has to be readable here too, or the disk
    # cannot be restored however visible its own backup is.
    for image in {c["backing_image"] for c in STATE._moving(move) if c.get("backing_image")}:
        if KUBE.get_optional(f"{STATE.LH_API}/namespaces/{STATE.LHNS}/backingimages/{image}", read=STATE.kget):
            continue
        found = KUBE.get_optional(f"{STATE.LH_API}/namespaces/{STATE.LHNS}/backupbackingimages/{image}", read=STATE.kget)
        if not ((found or {}).get("status", {}) or {}).get("url"):
            waiting.append(f"the {image} image")
    if waiting:
        return STATE._note(move, 52, f"Waiting for this cluster's Longhorn to see "
                               f"{len(waiting)} backup{'' if len(waiting) == 1 else 's'}")
    return STATE._advance(move, "restoring", 55, "Backups visible here")


def _ensure_namespace(namespace):
    if KUBE.get_optional(f"/api/v1/namespaces/{namespace}", read=STATE.kget):
        return
    STATE.ksend("POST", "/api/v1/namespaces", {"apiVersion": "v1", "kind": "Namespace",
                                         "metadata": {"name": namespace}})


def _image_ready(name):
    image = KUBE.get_optional(f"{STATE.LH_API}/namespaces/{STATE.LHNS}/backingimages/{name}", read=STATE.kget)
    if not image:
        return False
    files = ((image.get("status", {}) or {}).get("diskFileStatusMap") or {}).values()
    return any(str(f.get("state", "")).lower() == "ready" for f in files)


def _image_class():
    """A Longhorn storage class for a restored image to take its settings from.

    The class new volumes go on (homestead_storage_classes.default_class)
    when it is Longhorn's; otherwise a Longhorn class the cluster marks as
    default, one of Longhorn's usual names, or any usable Longhorn class.
    Never one Harvester keeps for itself or one made for an image or a
    restore (those carry a backingImage or fromBackup of their own).
    """
    try:
        classes = STATE.kget("/apis/storage.k8s.io/v1/storageclasses").get("items", [])
    except Exception as error:
        # Not "no Longhorn class": a ValueError would end the move, and a read
        # that failed is asked again on the next pass.
        raise RuntimeError(f"Homestead could not read the storage classes, so it did not choose one "
                           f"for the restored image: {error}") from error
    rows = [STORAGE_CLASSES.class_row(c) for c in classes]
    longhorn = [row for row in rows if row["provisioner"] == STORAGE_CLASSES.LONGHORN_PROVISIONER
                and STORAGE_CLASSES.class_selectable(row)]
    names = [row["name"] for row in longhorn]
    marked = [row["name"] for row in longhorn if row["default"]]
    for choice in [STORAGE_CLASSES.default_class(rows)] + marked + list(STORAGE_CLASSES.FALLBACK_CLASSES) + names:
        if choice and choice in names:
            return choice
    raise ValueError("this cluster has no Longhorn storage class to restore the image with")


def _ensure_image(move, name, storage_class=""):
    """Restore a Harvester image from its backup before the disks built on it."""
    if KUBE.get_optional(f"{STATE.LH_API}/namespaces/{STATE.LHNS}/backingimages/{name}", read=STATE.kget):
        return
    backup = KUBE.get_optional(f"{STATE.LH_API}/namespaces/{STATE.LHNS}/backupbackingimages/{name}", read=STATE.kget)
    url = ((backup or {}).get("status", {}) or {}).get("url", "")
    if not url:
        raise STATE.CLIENT.Unreachable(f"the {name} image backup is not visible here yet")
    # Harvester's admission webhook sets an image's copies and disks from a
    # storage class: the one named here, or else the cluster's default - and
    # refuses the image outright when there is no default.
    STATE.ksend("POST", f"{STATE.LH_API}/namespaces/{STATE.LHNS}/backingimages", {
        "apiVersion": "longhorn.io/v1beta2", "kind": "BackingImage",
        "metadata": {"name": name, "namespace": STATE.LHNS,
                     "annotations": {NAMES.key(STATE.MOVE_ID): move["id"],
                                     STATE.HARVESTER_IMAGE_CLASS: storage_class or move.get("storage_class") or _image_class()}},
        "spec": {"sourceType": "restore",
                 "sourceParameters": {"backup-url": url, "concurrent-limit": "2"}}})


def _restore_progress(namespace, claim):
    """(finished, percent) for one claim being restored from a backup."""
    pvc = KUBE.get_optional(f"/api/v1/namespaces/{namespace}/persistentvolumeclaims/{claim}", read=STATE.kget)
    if pvc:
        problem = STATE.LH.restore_problem({**pvc, "metadata": {**pvc.get("metadata", {}), "namespace": namespace}})
        if problem:
            raise ValueError(f"Restoring {claim} failed: {problem}")
    if not pvc or (pvc.get("status", {}) or {}).get("phase") != "Bound":
        return False, 0
    volume_name = (pvc.get("spec", {}) or {}).get("volumeName", "")
    volume = KUBE.get_optional(f"{STATE.LH_API}/namespaces/{STATE.LHNS}/volumes/{volume_name}", read=STATE.kget) if volume_name else None
    if not volume:
        return False, 0
    status = volume.get("status", {}) or {}
    if status.get("restoreInitiated") and not status.get("restoreRequired") and status.get("state") in ("detached", "attached"):
        return True, 100
    percents = []
    try:
        engines = STATE.kget(f"{STATE.LH_API}/namespaces/{STATE.LHNS}/engines").get("items", [])
    except Exception:
        engines = []
    for engine in engines:
        if (engine.get("spec", {}) or {}).get("volumeName") != volume_name:
            continue
        for row in ((engine.get("status", {}) or {}).get("restoreStatus") or {}).values():
            if row.get("error"):
                raise ValueError(f"restoring {claim} failed: {row['error']}")
            percents.append(int(row.get("progress") or 0))
    return False, (sum(percents) / len(percents)) if percents else 0


def _restoring(move):
    namespace = move["namespace"]
    _ensure_namespace(namespace)
    for claim in move["claims"]:
        if claim.get("created"):
            continue
        existing = KUBE.get_optional(f"/api/v1/namespaces/{namespace}/persistentvolumeclaims/{claim['claim']}", read=STATE.kget)
        if claim.get("action") == "skip":
            if not existing:
                raise ValueError(f"volume {claim['claim']} was to be skipped, using one already in {namespace} "
                                 "here, but there is none; create it, then retry")
            claim.update(created=True, restored=True)
            continue
        if claim.get("action") == "blank" and not existing:
            _blank_claim(move, claim, namespace)
            claim.update(created=True, restored=True)
            continue
        if existing:
            if not STATE._ours(existing, move["id"]):
                raise ValueError(f"volume {claim['claim']} already exists in {namespace} "
                                 "and was not made by this move")
            claim["created"] = True
            continue
        if claim.get("backing_image") and not _image_ready(claim["backing_image"]):
            _ensure_image(move, claim["backing_image"], claim.get("target_class") or move.get("storage_class") or "")
            return STATE._note(move, 56, f"Restoring the {claim['backing_image']} image first")
        result = STATE.LH.restore_backup({
            "backup": claim["backup"], "namespace": namespace, "name": claim["claim"],
            "size_gb": claim.get("size_gb"), "access_mode": claim.get("access_mode"),
            "storage_class": claim.get("target_class") or move.get("storage_class") or "",
            "volume_mode": claim.get("volume_mode"),
            "migratable": claim.get("migratable"), "backing_image": claim.get("backing_image"),
            "annotations": {NAMES.key(STATE.MOVE_ID): move["id"],
                            NAMES.key(STATE.MOVED_FROM): f"{move['cluster']}/{move['name']}"}})
        if result and result.get("created") is False:
            return STATE._note(move, 56, result["message"])
        claim["created"] = True
    states = []
    for claim in move["claims"]:
        if claim.get("action", "move") != "move":
            claim["restored"] = True
            continue
        finished, percent = _restore_progress(namespace, claim["claim"])
        claim["restored"] = finished
        states.append(100 if finished else percent)
    if all(c["restored"] for c in move["claims"]):
        TARGET._release_target(move)
        if move["kind"] == "volume":
            return _finish(move, "succeeded", f"{move['name']} is here; the original stays on "
                                              f"{move['cluster']} until you remove it there")
        return STATE._advance(move, "creating", 86, "Every volume is restored here")
    average = sum(states) / max(1, len(states))
    return STATE._note(move, 56 + 29 * average / 100,
                 f"Restoring {len(states)} volume{'' if len(states) == 1 else 's'} here: {int(average)}%")


def _blank_claim(move, claim, namespace):
    """An empty volume of the claim's shape and chosen size, on its chosen class (or the
    cluster's default), stamped as this move's so putting it back removes it.
    Not waited on: a class that binds on first use binds when the app starts."""
    body = {"apiVersion": "v1", "kind": "PersistentVolumeClaim",
            "metadata": {"name": claim["claim"], "namespace": namespace,
                         "labels": {NAMES.key("managed"): "true"},
                         "annotations": {NAMES.key(STATE.MOVE_ID): move["id"],
                                         NAMES.key(STATE.MOVED_FROM): f"{move['cluster']}/{move['name']}"}},
            "spec": {"accessModes": [claim.get("access_mode") or "ReadWriteOnce"],
                     "volumeMode": claim.get("volume_mode") or "Filesystem",
                     "resources": {"requests": {"storage": f"{int(claim.get('target_size_gb') or claim.get('size_gb') or 1)}Gi"}}}}
    klass = claim.get("target_class") or move.get("storage_class") or ""
    if klass:
        body["spec"]["storageClassName"] = klass
    STATE.ksend("POST", f"/api/v1/namespaces/{namespace}/persistentvolumeclaims", body)


def _post_ours(path, collection, body, move):
    """Create something, treating 'it exists and this move made it' as done."""
    existing = KUBE.get_optional(path, read=STATE.kget)
    if existing:
        if STATE._ours(existing, move["id"]):
            return
        raise ValueError(f"{path.rsplit('/', 1)[-1]} already exists here and was not made "
                         "by this move")
    STATE.ksend("POST", collection, body)


def _stamp(meta, move, namespace):
    meta["namespace"] = namespace
    annotations = meta.setdefault("annotations", {})
    annotations[NAMES.key(STATE.MOVE_ID)] = move["id"]
    annotations[NAMES.key(STATE.MOVED_FROM)] = f"{move['cluster']}/{move['name']}"
    return meta


def carry_icon(cluster, annotations):
    """Make a workload's logo work here, where its cache reference is unknown.

    The logo annotation names the source Homestead's icon cache, which a move
    does not bring. The bytes are fetched from the source (they keep their
    name, being named by their digest), or failing that from the original
    URL. A logo is never a reason for a move to fail: without one the card
    shows the usual placeholder.
    """
    reference = NAMES.read(annotations, "icon") or ""
    if not reference.startswith("/api/icons/") or ICONS.exists(reference, STATE.DATA_DIR):
        return reference
    try:
        found = ICONS.store(STATE.CLIENT.icon_bytes(cluster, reference), STATE.DATA_DIR)
    except Exception:
        source = NAMES.read(annotations, "icon-source") or ""
        try:
            found = ICONS.persist(source, STATE.DATA_DIR) if source.startswith(("http://", "https://")) else ""
        except Exception:
            found = ""
    if found:
        annotations[NAMES.key("icon")] = found
    return found


def _creating(move):
    namespace, name = move["namespace"], move["name"]
    definition = STATE._definition(move)
    if move["kind"] == "vm":
        definition["object"], effects, _, _ = VM_DEVICES.prepare(definition, namespace, move.get("host_devices"))
        for effect in effects:
            if effect["kind"] == "configmap" and effect["body"]:
                body = effect["body"]
                _stamp(body["metadata"], move, namespace)
                _post_ours(effect["path"], effect["path"].rsplit("/", 1)[0], body, move)
            elif effect["kind"] == "kubevirt-gates":
                PASSTHROUGH.ensure_gates(*effect["gates"], send=STATE.ksend)
    for secret in definition.get("secrets", []):
        _stamp(secret["metadata"], move, namespace)
        _post_ours(f"/api/v1/namespaces/{namespace}/secrets/{secret['metadata']['name']}",
                   f"/api/v1/namespaces/{namespace}/secrets", secret, move)
    chosen = move.setdefault("flags", {}).get("vip")
    pod_spec = ((((definition.get("object") or {}).get("spec") or {}).get("template") or {}).get("spec") or {})
    for service in definition.get("services", []):
        service_name = service["metadata"]["name"]
        path = f"/api/v1/namespaces/{namespace}/services/{service_name}"
        existing = KUBE.get_optional(path, read=STATE.kget)
        if existing and STATE._ours(existing, move["id"]):
            continue
        # On the host network an app answers on the node itself. Here k3s's
        # ServiceLB would publish its Service by holding the same ports on
        # every node - and the app could then never be placed. It needs none.
        if (pod_spec.get("hostNetwork") and (service.get("spec") or {}).get("type") == "LoadBalancer"
                and STATE.NETWORK.servicelb_present()):
            move.setdefault("flags", {})["skipped_services"] = sorted(
                set(move["flags"].get("skipped_services") or []) | {service_name})
            continue
        planned = STATE._plan_service(service, definition, namespace, name,
                                move.get("address_mode") or "shared", move.get("address", ""),
                                chosen)
        meta = _stamp(service["metadata"], move, namespace)
        # The class the source cluster's load balancer wanted means nothing
        # here: this cluster's own is set below when the Service gets a VIP.
        (service.get("spec") or {}).pop("loadBalancerClass", None)
        if planned.get("vip"):
            service.setdefault("spec", {}).update(PLATFORM.vip_spec(planned["vip"]))
            meta["annotations"].update(PLATFORM.vip_annotations(planned["vip"]))
            meta["annotations"][NAMES.key("vip-mode")] = planned["vip_mode"]
            chosen = chosen or planned["vip"]
            move["flags"]["vip"] = chosen
        _post_ours(path, f"/api/v1/namespaces/{namespace}/services", service, move)
    body = definition["object"]
    if move.get("transfer_mode") == "copy" and move["kind"] == "vm":
        domain = body["spec"]["template"]["spec"].setdefault("domain", {})
        domain.setdefault("firmware", {})["uuid"] = str(uuid.uuid5(uuid.NAMESPACE_URL, move["id"] + ":firmware"))
        for index, interface in enumerate(domain.get("devices", {}).get("interfaces", [])):
            raw = hashlib.sha256(f"{move['id']}:interface:{index}".encode()).digest()
            interface["macAddress"] = "02:" + ":".join(f"{byte:02x}" for byte in raw[:5])
    _stamp(body["metadata"], move, namespace)
    carry_icon(move["cluster"], body["metadata"]["annotations"])
    _post_ours(STATE._object_path(move["kind"], namespace, name),
               STATE._collection_path(move["kind"], namespace), body, move)
    move["origin"] = definition.get("origin") or move.get("origin") or {}
    move["flags"]["created_at"] = time.time()
    where = f" on {chosen}" if chosen else ""
    return STATE._advance(move, "starting", 90, f"{name} created here{where}")


def _starting(move):
    namespace, name, origin = move["namespace"], move["name"], move.get("origin") or {}
    path = STATE._object_path(move["kind"], namespace, name)
    if move.get("transfer_mode") == "copy":
        return _finish(move, "succeeded", f"{name} copied here, stopped. The original on {move['cluster']} keeps its previous running state")
    if move["kind"] == "vm":
        stopped = (origin.get("runStrategy") == "Halted"
                   or ("running" in origin and not origin.get("running")))
    else:
        stopped = int(origin.get("replicas", 1) or 0) == 0
    if stopped:
        return _finish(move, "succeeded", f"{name} is here, stopped as it was on {move['cluster']}")
    flags = move.setdefault("flags", {})
    if not flags.get("started"):
        # Never two copies at once: start here only while the source is still
        # stopped for this move, by a fresh read. A read that fails stops the start.
        there = STATE._source_status(move)
        # The fence Cancel uses too: the same workload, held by no other transfer.
        replaced = bool(move.get("source_uid")) and there.get("source_uid") != move["source_uid"]
        if (there.get("running") or not there.get("stopped_for_move") or replaced
                or there.get("transfer_owner") not in ("", None, move["id"])):
            raise ValueError(f"{name} is not stopped for this move on {move['cluster']}, so it is not started "
                             f"here. Cancel this move, or stop it there and retry")
    if not flags.get("start_requested"):
        # Stored before the request and kept for good, through retries too: a
        # start whose reply was lost may still have happened, and a give-up must
        # then not start the source as well (_start_source_again).
        flags["start_requested"] = time.time()
        STATE._store(move)
    if not flags.get("started"):
        # started times START_GRACE; a retry clears it to send the start again.
        STATE.ksend("PATCH", path, {"spec": dict(origin)}, ctype="application/merge-patch+json")
        flags["started"] = time.time()
    if move["kind"] == "vm":
        vmi = KUBE.get_optional(f"/apis/kubevirt.io/v1/namespaces/{namespace}/virtualmachineinstances/{name}", read=STATE.kget)
        ready = (vmi or {}).get("status", {}).get("phase") == "Running"
        waiting = "Waiting for the VM to boot"
    else:
        found = KUBE.get_optional(path, read=STATE.kget) or {}
        want = int(origin.get("replicas", 1) or 1)
        have = int((found.get("status", {}) or {}).get("readyReplicas", 0) or 0)
        ready = have >= want
        waiting = f"Waiting for {name} to become ready: {have} of {want}"
    if ready:
        return _finish(move, "succeeded", f"{name} is running here; still stopped on "
                                          f"{move['cluster']} until you remove it there")
    if time.time() - float(flags["started"]) > STATE.START_GRACE:
        # The data arrived; the application not starting is a different
        # problem, and one to be looked at rather than waited on for ever.
        raise ValueError(f"{name} was created and started here but is not ready after "
                         f"{STATE.START_GRACE // 60} minutes; check its logs. It is still stopped "
                         f"on {move['cluster']}")
    return STATE._note(move, 95, waiting)


HANDLERS = {"joining": _joining, "quiescing": _quiescing, "backing-up": _backing_up,
            "releasing-source": _releasing_source,
            "syncing": _syncing, "restoring": _restoring, "creating": _creating,
            "starting": _starting}


def _finish(move, status, message):
    move.update(status=status, message=message, finished_at=STATE._now())
    if status == "succeeded":
        move.update(phase="done", progress=100)
        move.pop("definition", None)
        move.setdefault("flags", {})["resource_cleanup_pending"] = True
    # Persist completion before deleting backups. A lost cleanup response must
    # not restart restoration against artifacts which have already been removed.
    STATE._store(move)
    _cleanup_resources(move)
    TARGET._cleanup_target(move)
    if ENGINE.after_finish:
        try:
            ENGINE.after_finish()
        except Exception:
            pass


def _cleanup_resources(move):
    if move.get("status") not in ("succeeded", "cancelled"):
        return
    flags = move.setdefault("flags", {})
    flags["resource_cleanup_pending"] = True
    # Only this function's own fields go onto the stored move: "Remove
    # original" may have written source_removed since the caller read it.
    STATE._update(move, flags={"resource_cleanup_pending": True})
    try:
        cancelled = move["status"] == "cancelled"
        if cancelled:
            _remove_created(move)
        destination = CLEANUP.destination(STATE.kget, STATE.ksend, move, cancelled)
        pending = list(destination["pending"])
        notes = list(destination["retained"])
        if flags.get("destination_deletion_pending"):
            pending.append("destination object deletion")
        if any(c.get("cleanup_retain") for c in move.get("claims") or []):
            notes.append("Retain storage policy keeps the removed destination disks; remove them separately if no longer needed")
        if pending:
            pass  # The source backup remains available until restore users are gone.
        elif move.get("cleanup_protocol") == 1 and (flags.get("quiesce_requested") or flags.get("quiesced")):
            result = STATE.CLIENT.remote(move["cluster"], "/api/move/source", {
                "action": "cleanup", "kind": move["kind"], "name": move["name"],
                "namespace": move.get("source_namespace", ""),
                "transfer_id": move["id"], "expected_uid": move["source_uid"]})
            if not isinstance(result.get("complete"), bool):
                raise ValueError("Source cleanup was not confirmed")
            if not result["complete"]:
                pending.append("source transfer backups or snapshots")
            notes += result.get("retained") or []
        elif move.get("cleanup_protocol") != 1 and flags.get("backed_up"):
            notes.append("Older source transfer backups retained; their ownership cannot be verified")
        flags["resource_cleanup_pending"] = bool(pending)
        move["cleanup_message"] = ("Temporary resource cleanup pending; Homestead retries automatically"
                                   if pending else "; ".join(str(note) for note in notes))
    except Exception:
        flags["resource_cleanup_pending"] = True
        move["cleanup_message"] = "Temporary resource cleanup pending; check both clusters. Homestead retries automatically"
    STATE._update(move, flags={"resource_cleanup_pending": flags["resource_cleanup_pending"]},
            cleanup_message=move["cleanup_message"])


def _service_addresses(service):
    """The addresses a Service asks for or holds, as the network page reads them."""
    meta, spec = service.get("metadata") or {}, service.get("spec") or {}
    annotations = meta.get("annotations") or {}
    asked = ",".join(str(annotations.get(key) or "") for key in
                     (STATE.VIP_ANNOTATION, "metallb.universe.tf/loadBalancerIPs", "metallb.io/loadBalancerIPs"))
    held = [row.get("ip") for row in ((service.get("status") or {}).get("loadBalancer") or {}).get("ingress") or []]
    return {a.strip() for a in [*asked.split(","), *held, spec.get("loadBalancerIP"), *(spec.get("externalIPs") or [])]
            if a and str(a).strip()}


def _address_claimed_here(move):
    """An address that a LoadBalancer Service this move made here and the
    source's cluster would both announce, by fresh reads of both (a reply to
    the Service's creation may have been lost). A read that fails on either
    side counts as a clash; with no such Service the source is not asked."""
    try:
        ours = [item for item in CLEANUP.items(STATE.kget, f"/api/v1/namespaces/{move['namespace']}/services")
                if STATE._ours(item, move["id"]) and (item.get("spec") or {}).get("type") == "LoadBalancer"]
        if not ours:
            return ""
        waiting = sorted(item["metadata"].get("name", "") for item in ours if not _service_addresses(item))
        if waiting:
            # An address not given yet may still be the source's, once it is.
            return ", ".join(f"Service {name} has no address yet" for name in waiting)
        here = set().union(*(_service_addresses(item) for item in ours))
        rows = (STATE.CLIENT.remote(move["cluster"], "/api/network") or {}).get("services")
        if not isinstance(rows, list):
            raise ValueError("no Service list")
        there = {str(ip) for row in rows for ip in (row.get("external_ips") or [])}
    except Exception:
        return "the addresses could not be compared"
    return ", ".join(sorted(here & there))


def _kubernetes_reason(error):
    """What the API server (or a webhook behind it) said, from its Status body."""
    try:
        body = json.loads(error.read().decode("utf-8", "replace") or "{}")
        return str(body.get("message") or "").strip()
    except Exception:
        return ""


def _tick(move):
    handler = HANDLERS.get(move.get("phase"))
    if not handler:
        return _finish(move, "failed", f"unknown phase {move.get('phase')}")
    try:
        if move.get("phase") in ("quiescing", "backing-up", "releasing-source", "syncing", "restoring"):
            if not TARGET._borrow_target(move):
                return
        handler(move)
        move["failures"] = 0
    except TARGET.JournalUnreadable as error:
        # Only the backup-storage journal: the move waits, saying why, and
        # nothing is borrowed or put back until it can be read.
        move["failures"] = int(move.get("failures", 0)) + 1
        move["message"] = f"Waiting: {str(error)[:300]}"
    except STATE.STORE_ERRORS:
        # The move store, not the move: stop this pass with nothing written
        # (a ValueError below would end the move as failed).
        raise
    except STATE.CLIENT.Unreachable as error:
        _wait_or_give_up(move, str(error))
    except (ValueError, PermissionError) as error:
        _finish(move, "failed", str(error)[:400])
    except urllib.error.HTTPError as error:
        reason = _kubernetes_reason(error)
        # Kubernetes saying no - an invalid object, a webhook refusing it - does
        # not change by asking again, so it stops the move with what was said.
        # A conflict, a rate limit or the API server struggling may clear.
        if error.code in (400, 403, 404, 422):
            return _finish(move, "failed", f"Kubernetes refused it (HTTP {error.code}): {reason}"[:400]) \
                if reason else _finish(move, "failed", f"Kubernetes refused it (HTTP {error.code})")
        _wait_or_give_up(move, f"Kubernetes returned HTTP {error.code}{f': {reason}' if reason else ''}"[:400])
    except Exception as error:
        _wait_or_give_up(move, str(error)[:200])
    move["updated_at"] = STATE._now()


def _wait_or_give_up(move, said):
    """Every error that may clear counts: MAX_TRANSIENT of them in a row and the
    move fails with the last one, rather than waiting for ever while its source
    stays stopped on the other cluster."""
    move["failures"] = int(move.get("failures", 0)) + 1
    if move["failures"] <= STATE.MAX_TRANSIENT:
        move["message"] = f"Waiting: {said}"
        return
    _finish(move, "failed", f"Gave up after {move['failures']} attempts: {said}{_start_source_again(move)}"[:600])


def _start_source_again(move):
    """Put the source back at the replicas it had - but only before anything
    may have started the app here. Fails closed: once a start was asked for
    (its reply may have been lost) or the move reached "starting", the source
    stays stopped, as two copies, one on each cluster, would both serve and
    both write. A move whose source runs again is not resumed from its backups."""
    flags = move.setdefault("flags", {})
    if not STATE._source_held(move):
        return ""
    name, cluster = move["name"], move["cluster"]
    if flags.get("start_requested") or flags.get("started") or move.get("phase") in ("starting", "done"):
        return (f". {name} may already be running here, so it stays stopped on {cluster}; check it here, "
                f"then Cancel puts it back there")
    clash = _address_claimed_here(move)
    if clash:
        return (f". {name} stays stopped on {cluster}: its address may be announced by both clusters ({clash}). "
                f"Cancel removes it here, then puts {name} back there")
    # Stored before the request, as quiesce_requested is: a release whose reply
    # is lost may have started the source, and Retry must then refuse.
    flags["release_requested"] = time.time()
    STATE._store(move)
    try:
        STATE._source_action(move, "release")
    except Exception as error:
        return (f". {name} may still be stopped on {cluster}: starting it again there got no clear answer "
                f"({str(error)[:160]}). Cancel puts it back there")
    flags["source_released"] = flags["released_on_give_up"] = True
    return f". {name} runs again on {cluster}; Cancel tidies what this move made here"


def tick_all():
    """One pass over every running move."""
    with SHARED.write_scope(STATE._path()):
        _tick_all()


def _tick_all():
    with STATE._lock:
        rows = STATE._read()
    try:
        journal = TARGET._target_journal()
    except TARGET.JournalUnreadable:
        journal = None    # each move that needs it waits and says why; no orphan is released on a guess
    running = [m["id"] for m in rows if m.get("status") == "running"
               or (m.get("flags") or {}).get("target_cleanup_pending")
               or (m.get("flags") or {}).get("resource_cleanup_pending")
               or (m.get("status") in ("failed", "cancelled") and (m.get("flags") or {}).get("cancelling"))
               or (m.get("status") in ("succeeded", "cancelled") and m.get("transfer_target"))
               or m["id"] == (journal or {}).get("owner")]
    if journal and not any(m["id"] == journal["owner"] for m in rows):
        TARGET._release_target({"id": journal["owner"], "status": "cancelled"})
    for move_id in running:
        with STATE._serialized(move_id):
            move = STATE._find(move_id)
            if not move:
                continue
            if move.get("status") == "running":
                if not move.get("op"):
                    PLAN._add_operation(move)
                _tick(move)
            elif move.get("status") in ("failed", "cancelled") and (move.get("flags") or {}).get("cancelling"):
                try:
                    ACT._abandon(move_id)
                except Exception:
                    pass
                move = STATE._find(move_id)
            else:
                _cleanup_resources(move)
                TARGET._cleanup_target(move)
                move["updated_at"] = STATE._now()
            with STATE._lock:
                current = STATE._find(move_id)
                if current and current.get("rev", 0) == move.get("rev", 0):
                    STATE._store(move)


def _remove_created(move):
    namespace, removed, move_id = move["namespace"], [], move["id"]
    pending = False
    # A volume is its own claim, removed with the claims below.
    obj = None if move["kind"] == "volume" else CLEANUP.optional(STATE.kget, STATE._object_path(move["kind"], namespace, move["name"]))
    if obj and STATE._ours(obj, move_id):
        pending |= not CLEANUP.delete(STATE.kget, STATE.ksend, STATE._object_path(move["kind"], namespace, move["name"]), obj)
        removed.append(move["name"])
    for kind in ("services", "secrets", "configmaps"):
        items = CLEANUP.items(STATE.kget, f"/api/v1/namespaces/{namespace}/{kind}")
        for item in items:
            if STATE._ours(item, move_id):
                pending |= not CLEANUP.delete(STATE.kget, STATE.ksend, f"/api/v1/namespaces/{namespace}/{kind}/{item['metadata']['name']}", item)
                removed.append(item["metadata"]["name"])
    for claim in move["claims"]:
        pvc = CLEANUP.optional(STATE.kget, f"/api/v1/namespaces/{namespace}/persistentvolumeclaims/{claim['claim']}")
        if pvc and STATE._ours(pvc, move_id):
            volume = (pvc.get("spec") or {}).get("volumeName")
            if volume:
                pv = CLEANUP.optional(STATE.kget, f"/api/v1/persistentvolumes/{volume}")
                if pv and (pv.get("spec") or {}).get("persistentVolumeReclaimPolicy") == "Retain":
                    claim["cleanup_retain"] = True
                claim["cleanup_volume"] = ((pv or {}).get("spec", {}).get("csi") or {}).get("volumeHandle") or volume
                STATE._store(move)  # Preserve reclamation evidence before deleting the PVC.
            pending |= not CLEANUP.delete(STATE.kget, STATE.ksend, f"/api/v1/namespaces/{namespace}/persistentvolumeclaims/{claim['claim']}", pvc)
            removed.append(claim["claim"])
    move.setdefault("flags", {})["destination_deletion_pending"] = pending
    return removed


# Last, so that importing this module first works: homestead_move_engine
# imports this one. server.py sets after_finish there.
import homestead_move_engine as ENGINE  # noqa: E402
