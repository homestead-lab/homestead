"""Reviewing a move before it starts, and starting it: what the source sends,
which storage each claim lands on, what is missing here, and the Activity tray
operation that follows the move.

Split out of homestead_move_engine (#623).
"""
import json
import secrets
import urllib.error
import urllib.parse
import homestead_kube as KUBE
import homestead_passthrough as PASSTHROUGH
import homestead_vm_transfer_devices as VM_DEVICES
import homestead_move_state as STATE
import homestead_storage_classes as STORAGE_CLASSES


ACTIONS = ("move", "blank", "skip")


def _all_classes():
    items = (STATE.kget("/apis/storage.k8s.io/v1/storageclasses") or {}).get("items", [])
    return sorted(c["metadata"]["name"] for c in items
                  if not any((c.get("parameters") or {}).get(k) for k in ("fromBackup", "backingImage")))


def _choices(claims, volumes, kind):
    """Each claim's action and storage class: move (backed up and restored,
    the default), blank (an empty volume of the same shape) or skip (one of
    that name already here is used)."""
    volumes = volumes or {}
    out = {}
    for claim in claims:
        pick = volumes.get(claim["claim"]) or {}
        action = str(pick.get("action") or "move")
        if action not in ACTIONS:
            raise ValueError(f"volume {claim['claim']}: choose move, blank or skip")
        if kind == "volume" and action != "move":
            raise ValueError("a volume moved on its own is moved")
        size = int(claim.get("size_gb") or 1)
        if action == "blank" and pick.get("size_gb") not in (None, ""):
            try:
                size = int(pick["size_gb"])
            except (TypeError, ValueError):
                raise ValueError(f"volume {claim['claim']}: its size is a whole number of GB")
            if not 1 <= size <= 16384:
                raise ValueError(f"volume {claim['claim']}: a blank volume is between 1 and 16384 GiB")
        out[claim["claim"]] = {"action": action, "storage_class": str(pick.get("storage_class") or "").strip(),
                               "size_gb": size}
    return out


def storage_choices():
    items = STATE.kget("/apis/storage.k8s.io/v1/storageclasses").get("items", [])
    return sorted(c["metadata"]["name"] for c in items
                  if c.get("provisioner") == "driver.longhorn.io"
                  and not any((c.get("parameters") or {}).get(k) for k in ("fromBackup", "backingImage")))


def _persistent_state(vm_spec):
    """What of the VM's firmware or TPM lives in KubeVirt's state volume, in words; "" if nothing."""
    domain = vm_spec.get("domain") or {}
    efi = (((domain.get("firmware") or {}).get("bootloader") or {}).get("efi") or {}).get("persistent") is True
    tpm = (((domain.get("devices") or {}).get("tpm") or {}).get("persistent")) is True
    return " and ".join(part for part, kept in (("UEFI settings", efi), ("TPM", tpm)) if kept)


def _fresh(state):
    return " and ".join(part for part, kept in (("blank UEFI settings", "UEFI" in state),
                                                ("a new, empty TPM", "TPM" in state)) if kept)


def _state_storage_problem():
    """Why KubeVirt here could not make the VM's state volume, or "" when it can.

    It uses its vmStateStorageClass, or else the one default storage class
    (KubeVirt's own default-virt class first)."""
    try:
        kubevirts = (STATE.kget("/apis/kubevirt.io/v1/kubevirts") or {}).get("items") or []
        config = ((kubevirts[0].get("spec") or {}).get("configuration") or {}) if len(kubevirts) == 1 else {}
        explicit = config.get("vmStateStorageClass")
        if explicit:
            if not KUBE.get_optional(f"/apis/storage.k8s.io/v1/storageclasses/{urllib.parse.quote(explicit, safe='')}",
                                     read=STATE.kget):
                return (f"KubeVirt here keeps VM firmware and TPM state on storage class {explicit}, which does not "
                        "exist; the VM cannot start until it does")
            return ""
        classes = (STATE.kget("/apis/storage.k8s.io/v1/storageclasses") or {}).get("items") or []
    except Exception:
        return "Could not check where KubeVirt here would keep the VM's firmware and TPM state"
    marked = lambda key: [c for c in classes if ((c.get("metadata") or {}).get("annotations") or {}).get(key) == "true"]
    choices = marked("storageclass.kubevirt.io/is-default-virt-class") or marked("storageclass.kubernetes.io/is-default-class")
    if len(choices) == 1:
        return ""
    return ("KubeVirt here has no single default storage class for VM firmware and TPM state, so the VM cannot "
            "start: mark one storage class as the default, or set KubeVirt's vmStateStorageClass")


def plan(cluster, kind, name, namespace=None, address_mode="shared", address="", storage_class="", volumes=None,
         transfer_mode="move", source_namespace="", host_devices=None, stop_source=False):
    """Everything that would stop a move, or surprise someone, before it starts.

    Asks both clusters, and reports blockers and warnings separately: a blocker
    means the move would fail, a warning means it would work in a way someone
    should know about first.
    """
    namespace = namespace or STATE.NS
    blockers, warnings, fixes = [], [], []
    live = can_live = False
    device_rows, device_resources, device_hosts = [], [], []
    if kind not in STATE.KINDS:
        raise ValueError("kind must be container, vm or volume")
    if transfer_mode not in ("move", "copy") or transfer_mode == "copy" and kind == "volume":
        raise ValueError("Choose move or copy; copy supports containers and VMs")
    if address_mode not in ("shared", "automatic", "manual"):
        raise ValueError("address must be shared, automatic or manual")
    versions = STATE.CLIENT.check_cluster(cluster)
    if versions.get("compatible") is False:
        return {"ok": False, "transfer_mode": transfer_mode, "blockers": [versions["message"]], "warnings": [], "claims": [],
                "versions": versions}
    if versions.get("state") == "differs":
        warnings.append(versions["message"])
    if transfer_mode == "copy" and not {"copy-source-lease", "copy-source-fence"}.issubset(versions.get("capabilities", [])):
        return {"ok": False, "transfer_mode": "copy", "blockers": [
            "Update Homestead on the source cluster before copying workloads; this version cannot protect and release a copy's source"],
            "warnings": [], "claims": [], "versions": versions}
    try:
        definition = STATE._definition({"cluster": cluster, "kind": kind, "name": name, "source_namespace": source_namespace})
        there = STATE.CLIENT.remote(cluster, "/api/move/target")
    except STATE.CLIENT.Unreachable as error:
        return {"ok": False, "transfer_mode": transfer_mode, "blockers": [str(error)], "warnings": [], "claims": []}
    except ValueError as error:
        # No backup storage over there is the usual first hurdle, and this
        # side can clear it: say so, so the page can offer to.
        fixes = [{"kind": "source-storage", "cluster": cluster}] if "backup target" in str(error) else []
        return {"ok": False, "transfer_mode": transfer_mode, "blockers": [str(error)], "warnings": [], "claims": [], "fixes": fixes}

    here = STATE.LH.backup_target()
    if source_namespace and definition.get("namespace") != source_namespace:
        blockers.append("Update Homestead on the source cluster; it did not return the selected source namespace")
    if transfer_mode == "copy" and (definition.get("held") or definition.get("transfer_owner")):
        blockers.append("The source is held by another transfer; finish or put back that transfer first. "
                        f"If no cluster lists it any more, release it on {cluster} in Settings, Linked clusters, under Held here for transfers")
    if transfer_mode == "copy" and (not definition.get("source_uid") or not definition.get("source_version")):
        blockers.append("Could not verify the source workload identity; refresh before copying")
    if kind == "vm":
        device_rows = PASSTHROUGH.vm_devices(definition["object"])
        try:
            if device_rows:
                device_resources = PASSTHROUGH.resources()["resources"]
            _, effects, _, device_hosts = VM_DEVICES.prepare(definition, namespace, host_devices)
            for effect in effects:
                if effect["kind"] == "configmap" and effect["body"] and KUBE.get_optional(effect["path"], read=STATE.kget):
                    blockers.append("The destination vBIOS ConfigMap already exists; choose another namespace")
            if device_rows:
                warnings.append("Review GPU compatibility before starting the destination VM. Its devices use the selected destination hardware; the source hostname selector is removed and other placement rules are retained")
        except ValueError as error:
            blockers.append(str(error))
        # A halted copy still needs the VM API; discovering this after backup
        # would needlessly stop the source on a destination without KubeVirt.
        if not KUBE.get_optional("/apis/kubevirt.io/v1", read=STATE.kget):
            blockers.append("Install KubeVirt on the destination before transferring a VM")
        can_live = transfer_mode == "copy" and "copy-live-vm" in versions.get("capabilities", [])
        live = can_live and not stop_source
        if transfer_mode == "copy" and not can_live and definition.get("vm_running") is not False:
            warnings.append(f"{cluster} pauses {name} while its disks are backed up; update Homestead there to copy it while it runs")
        elif transfer_mode == "copy" and not live and definition.get("vm_running"):
            warnings.append(f"{name} stops while its disks are backed up, then starts again on {cluster}")
        if transfer_mode == "copy":
            warnings.append("The VM copy gets new MAC addresses and a firmware UUID. Review guest static IP and network settings before starting it")
            vm = (definition.get("object") or {}).get("spec") or {}
            if vm.get("instancetype") or vm.get("preference"):
                blockers.append("This VM uses an external instance type or preference; expand those settings into the VM before copying")
            if (definition.get("origin") or {}).get("runStrategy") == "Once":
                blockers.append("A VM with the Once run strategy cannot safely resume after copying. Change its run strategy before copying")
            template = vm.get("template") or {}
            vm_spec = template.get("spec") or {}
            for volume in vm_spec.get("volumes") or []:
                portable = ("persistentVolumeClaim", "cloudInitNoCloud", "cloudInitConfigDrive", "containerDisk",
                            "emptyDisk", "downwardMetrics")
                if not any(key in volume for key in portable):
                    blockers.append(f"Disk {volume.get('name', '?')} has an external source that cannot be copied; use a Longhorn PVC")
            if vm_spec.get("accessCredentials"):
                blockers.append("VM access credentials use additional Secrets; copy those dependencies separately before using this transfer")
        vm_spec = ((((definition.get("object") or {}).get("spec") or {}).get("template") or {}).get("spec")) or {}
        state = _persistent_state(vm_spec)
        if state:
            warnings.append(f"This VM keeps its {state} in a small volume of its own, outside its disks, and that is "
                            f"not brought across: here it starts with {_fresh(state)}. Anything sealed to the TPM, "
                            "such as BitLocker or Windows Hello, then asks for its recovery key")
            problem = _state_storage_problem()
            origin = definition.get("origin") or {}
            starts = transfer_mode == "move" and (origin.get("runStrategy", "Halted") != "Halted" or origin.get("running"))
            if problem:
                # A copy arrives stopped, so there is time to fix it; a move starts the VM here.
                (blockers if starts else warnings).append(problem)
        if live and definition.get("vm_running"):
            warnings.append(f"{name} keeps running while it is copied. " + (
                "Its guest agent freezes its filesystems while the disks are snapshotted, so they hold one instant"
                if definition.get("guest_agent") else
                "Its guest agent is not connected, so the copy's disks are as a power cut would leave them: "
                "the guest repairs its filesystems on first boot, and a database inside may need recovery"))
        for secret in definition.get("secrets", []):
            if KUBE.get_optional(f"/api/v1/namespaces/{namespace}/secrets/{secret['metadata']['name']}", read=STATE.kget):
                blockers.append(f"Secret {secret['metadata']['name']} already exists in {namespace}; choose another destination namespace")
    joined = bool(STATE._same_target(here, there))
    if not joined:
        if not there.get("reachable_off_cluster"):
            blockers.append(f"backup storage on {cluster} is only reachable inside that cluster; "
                            "give its object store a LAN address first")
            fixes.append({"kind": "source-address", "cluster": cluster})
        if here.get("configured"):
            warnings.append(f"this cluster's Longhorn backup target changes from {here.get('url')} "
                            f"to {there.get('url')} for this transfer, then returns to its previous setting; "
                            "backups already written to the old one stay there")

    if not KUBE.get_optional(f"/api/v1/namespaces/{namespace}", read=STATE.kget):
        warnings.append(f"namespace {namespace} does not exist here and will be created")
    # A volume is its own claim, checked with the claims below.
    if kind != "volume" and KUBE.get_optional(STATE._object_path(kind, namespace, name), read=STATE.kget):
        blockers.append(f"{name} already exists in {namespace} on this cluster")
    choices = _choices(definition.get("claims", []), volumes, kind)
    chosen_class = storage_class or STORAGE_CLASSES.claim_class()

    def check_class(klass, claim, longhorn):
        base = KUBE.get_optional(f"/apis/storage.k8s.io/v1/storageclasses/{urllib.parse.quote(klass, safe='')}", read=STATE.kget)
        where = f"volume {claim}: " if claim else ""
        if not base:
            blockers.append(f"{where}storage class {klass} does not exist here")
        elif longhorn and base.get("provisioner") != "driver.longhorn.io":
            blockers.append(f"{where}storage class {klass} is not Longhorn; a moved volume is restored "
                            "from a Longhorn backup, so it needs a Longhorn class (or make it blank)")
        elif any((base.get("parameters") or {}).get(k) for k in ("fromBackup", "backingImage")):
            blockers.append(f"{where}choose a regular storage class, not an existing restore or image class")

    moving = [c for c in definition.get("claims", []) if choices[c["claim"]]["action"] == "move"]
    if moving:
        support = STATE.LH.restore_support()
        if not support["ready"]:
            (warnings if support.get("can_install") or support.get("waiting") else blockers).append(support["message"])
    if any(not choices[c["claim"]]["storage_class"] for c in moving) or not definition.get("claims"):
        check_class(chosen_class, "", True)
    for claim in definition.get("claims", []):
        pick = choices[claim["claim"]]
        exists = KUBE.get_optional(f"/api/v1/namespaces/{namespace}/persistentvolumeclaims/{claim['claim']}", read=STATE.kget)
        if pick["action"] == "skip":
            if not exists:
                blockers.append(f"volume {claim['claim']} is skipped, but no volume of that name is in "
                                f"{namespace} here; create it first, or make it blank")
            continue
        if exists:
            blockers.append(f"volume {claim['claim']} already exists in {namespace} here")
        if pick["storage_class"] and pick["storage_class"] != chosen_class:
            check_class(pick["storage_class"], claim["claim"], pick["action"] == "move")
        if pick["action"] == "move":
            klass = pick["storage_class"] or chosen_class
            base = KUBE.get_optional(f"/apis/storage.k8s.io/v1/storageclasses/{urllib.parse.quote(klass, safe='')}", read=STATE.kget)
            if base:
                try:
                    STATE.LH.validate_restore_class(base, claim.get("volume_mode") or "Filesystem", migration=True)
                except ValueError as error:
                    blockers.append(f"volume {claim['claim']}: {error}")
        if pick["action"] == "move" and claim.get("backing_image") and not KUBE.get_optional(f"{STATE.LH_API}/namespaces/{STATE.LHNS}/backingimages/{claim['backing_image']}", read=STATE.kget):
            warnings.append(f"disk {claim['claim']} is built on the {claim['backing_image']} image, "
                            "which will be restored from its backup first")
        if pick["action"] == "blank":
            warnings.append(f"volume {claim['claim']} starts empty here; its data stays on {cluster}")

    selector = dict(definition.get("node_selector") or {})
    if device_rows:
        selector.pop("kubernetes.io/hostname", None)
    if selector:
        try:
            nodes = STATE.kget("/api/v1/nodes").get("items", [])
        except Exception:
            nodes = []
        fits = [n for n in nodes if all(((n.get("metadata", {}) or {}).get("labels", {}) or {})
                                        .get(k) == v for k, v in selector.items())]
        if not fits:
            warnings.append("no node here carries " + ", ".join(f"{k}={v}" for k, v in selector.items())
                            + ", so it will wait unscheduled until one does")
    for secret in definition.get("pull_secrets", []) or []:
        if not KUBE.get_optional(f"/api/v1/namespaces/{namespace}/secrets/{secret}", read=STATE.kget):
            warnings.append(f"image pull secret {secret} does not exist here, so a private image "
                            "may not pull")
    for network in definition.get("networks", []) or []:
        net_ns, _, net_name = network.rpartition("/")
        if not KUBE.get_optional(f"/apis/k8s.cni.cncf.io/v1/namespaces/{net_ns or namespace}/"
                    f"network-attachment-definitions/{net_name}", read=STATE.kget):
            warnings.append(f"network {network} does not exist here; the VM will not start "
                            "until it does")

    addresses = []
    chosen = None
    for service in definition.get("services", []):
        try:
            planned = STATE._plan_service(service, definition, namespace, name, address_mode,
                                    address, chosen)
        except (ValueError, PermissionError) as error:
            blockers.append(f"service {service['metadata']['name']}: {error}")
            continue
        warnings.extend(planned.get("warnings") or [])
        if planned.get("vip"):
            chosen = chosen or planned["vip"]
            addresses.append(f"{service['metadata']['name']} on {planned['vip']}")

    origin = definition.get("origin") or {}
    will_run = (False if kind == "volume" else origin.get("replicas", 0) > 0 if kind == "container"
                else origin.get("runStrategy", "Halted") != "Halted" or origin.get("running"))
    return {
        "ok": not blockers, "blockers": blockers, "fixes": fixes,
        "warnings": list(dict.fromkeys(warnings)),
        "cluster": cluster, "kind": kind, "name": name, "namespace": namespace,
        "source_namespace": definition.get("namespace", ""),
        "joined": joined, "will_run": bool(will_run) and transfer_mode != "copy", "addresses": addresses,
        "transfer_mode": transfer_mode, "live": live, "can_live": kind == "vm" and can_live,
        "host_devices": device_rows, "device_resources": device_resources, "device_hosts": device_hosts,
        "versions": versions,
        "storage_class": chosen_class, "storage_classes": storage_choices(),
        "claims": [{**{k: c.get(k) for k in ("claim", "size_gb", "access_mode", "volume_mode",
                                             "backing_image", "storage_class")},
                    "source_class": c.get("storage_class"), "action": choices[c["claim"]]["action"],
                    "storage_class": choices[c["claim"]]["storage_class"] or chosen_class,
                    "target_size_gb": choices[c["claim"]]["size_gb"]}
                   for c in definition.get("claims", [])],
        "all_storage_classes": _all_classes(),
        "total_gb": sum(int(c.get("size_gb") or 0) for c in moving),
    }


def start(cluster, kind, name, namespace=None, address_mode="shared", address="", storage_class="", volumes=None,
          transfer_mode="move", source_namespace="", host_devices=None, stop_source=False):
    namespace = namespace or STATE.NS
    checked = plan(cluster, kind, name, namespace, address_mode, address, storage_class, volumes, transfer_mode, source_namespace, host_devices,
                   stop_source=stop_source)
    if not checked["ok"]:
        raise ValueError(checked["blockers"][0])
    _refuse_second(cluster, kind, name)
    definition = STATE._definition({"cluster": cluster, "kind": kind, "name": name, "source_namespace": source_namespace})
    if kind == "vm":
        VM_DEVICES.prepare(definition, namespace, host_devices)
    choices = _choices(definition.get("claims", []), volumes, kind)
    move = {
        "id": secrets.token_hex(6), "cluster": cluster, "kind": kind, "name": name,
        "source_namespace": definition.get("namespace", ""), "namespace": namespace,
        "transfer_mode": transfer_mode, "live": bool(checked.get("live")),
        "source_uid": definition.get("source_uid", ""),
        "cleanup_protocol": 1 if definition.get("cleanup_protocol") == 1 and definition.get("source_uid") else 0,
        "host_devices": json.loads(json.dumps(host_devices or {})),
        "device_signature": VM_DEVICES.signature(definition) if kind == "vm" else "",
        "address_mode": address_mode, "address": address,
        "storage_class": checked["storage_class"],
        "status": "running", "phase": "joining", "progress": 1,
        "message": "Queued", "claims": [dict(c, backup="", created=False, restored=False,
                                             action=choices[c["claim"]]["action"],
                                             target_class=choices[c["claim"]]["storage_class"],
                                             target_size_gb=choices[c["claim"]]["size_gb"])
                                        for c in definition.get("claims", [])],
        "origin": definition.get("origin") or {}, "flags": {},
        "previous_target": "", "failures": 0, "source_removed": False,
        "created_at": STATE._now(), "updated_at": STATE._now(), "finished_at": "", "op": "",
    }
    # The step lock keeps the engine off this move until its Activity item is
    # recorded, so no copy without one is written back over it.
    with STATE._serialized(move["id"]):
        # Checked again where it is recorded: a second Start of the same
        # workload may have passed the check above while this one asked the source.
        with STATE._lock:
            _refuse_second(cluster, kind, name)
            STATE._store(move)
        _add_operation(move)
        if not move["op"]:
            move["message"] = "Queued; Activity was busy, so this move appears there shortly"
        with STATE.recording(f"The move of {name} is recorded and will run", "its Activity item",
                             "Activity may list it twice." if move["op"] else ""):
            STATE._store(move)
    return STATE._public(move)


def _add_operation(move):
    """Give a recorded move its Activity item. Outside the moves lock: the
    Activity tray takes its own lock and then reads moves (op_state). The move
    is already recorded and will run, so a busy tray must not make Start
    report an error: the engine tries again on its next pass."""
    try:
        move["op"] = _operation(move)
    except Exception:
        move["op"] = ""


def _refuse_second(cluster, kind, name):
    with STATE._lock:
        if any(m.get("status") == "running" and (m["cluster"], m["kind"], m["name"]) == (cluster, kind, name)
               for m in STATE._read()):
            raise ValueError(f"{name} is already being moved")


def _operation(move):
    verb = "Copy" if move.get("transfer_mode") == "copy" else "Move"
    item = STATE.OPS.start("move", f"{verb} {move['name']} from {move['cluster']}",
                     {"kind": {"vm": "VirtualMachine", "volume": "PersistentVolumeClaim"}.get(move["kind"], "Deployment"),
                      "name": move["name"], "namespace": move["namespace"]},
                     "/import", {"move": move["id"]}, message="Queued")
    return item["id"]


def op_state(item):
    """How a move reads in the Activity tray."""
    move = STATE._find((item.get("ref") or {}).get("move", ""))
    if not move:
        return "failed", item.get("progress", 0), "This move's record is gone"
    return move["status"], move.get("progress", 0), move.get("message", "")
