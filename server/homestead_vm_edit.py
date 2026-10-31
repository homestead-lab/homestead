"""
Editing a virtual machine: what a disk is made from, its disks, network
interfaces and cloud-init, and preparing, re-checking and saving an edit.

Split by feature from homestead_vms (#623), moved unchanged. Names that stay
there, that server.py binds there, or that tests replace there, are read
through it (V.name); homestead_vms re-exports everything here.
"""
import base64
import copy
import json
import re
import urllib.error
import urllib.parse
import homestead_kube as KUBE
import homestead_vm_hardware as HARDWARE
import homestead_hvimage as HVIMAGE
import homestead_vm_network as VMNETWORK
import homestead_value_masks as MASKS
import homestead_storage_resize as RESIZE


# ---- what a disk is made from ---------------------------------------------

def _claim_templates(vm):
    try:
        return json.loads(((vm.get("metadata") or {}).get("annotations") or {}).get(V.CLAIM_TEMPLATES) or "[]")
    except ValueError:
        return []


def _set_claim_templates(vm, items):
    annotations = vm["metadata"].setdefault("annotations", {})
    if items:
        annotations[V.CLAIM_TEMPLATES] = json.dumps(items)
    else:
        annotations.pop(V.CLAIM_TEMPLATES, None)


def _dv_templates(vm):
    return vm["spec"].get("dataVolumeTemplates") or []


def _forget_templates(vm, claim):
    vm["spec"]["dataVolumeTemplates"] = [t for t in _dv_templates(vm) if (t.get("metadata") or {}).get("name") != claim]
    if not vm["spec"]["dataVolumeTemplates"]:
        vm["spec"].pop("dataVolumeTemplates")
    _set_claim_templates(vm, [t for t in _claim_templates(vm) if (t.get("metadata") or {}).get("name") != claim])


def _template_size(vm, claim):
    for t in _dv_templates(vm):
        if (t.get("metadata") or {}).get("name") == claim:
            box = t["spec"].get("storage") or t["spec"].get("pvc") or {}
            return ((box.get("resources") or {}).get("requests") or {}).get("storage", ""), box.get("storageClassName", "")
    for t in _claim_templates(vm):
        if (t.get("metadata") or {}).get("name") == claim:
            spec = t.get("spec") or {}
            return ((spec.get("resources") or {}).get("requests") or {}).get("storage", ""), spec.get("storageClassName", "")
    return "", ""


def _disk_source(vm, ns, claim, claims):
    """Where a disk comes from, and whether it has been made yet. A disk that
    was never made - CDI refused its source, say - can be given another."""
    if not claim:
        return {"made": True, "template": "", "source": {}}
    made = claim in claims
    for t in _dv_templates(vm):
        if (t.get("metadata") or {}).get("name") == claim:
            src = t["spec"].get("source") or {}
            phase = ""
            try:
                dv = V.kget(f"/apis/cdi.kubevirt.io/v1beta1/namespaces/{ns}/datavolumes/{urllib.parse.quote(claim)}")
                phase = (dv.get("status") or {}).get("phase", "")
            except Exception:
                pass
            source = ({"url": src["http"].get("url", "")} if "http" in src else {"blank": True} if "blank" in src
                      else {"other": next(iter(src), "")})
            size, klass = _template_size(vm, claim)
            return {"made": made and phase in ("Succeeded", ""), "template": "datavolume", "phase": phase,
                    "source": source, "template_size": size, "template_class": klass}
    for t in _claim_templates(vm):
        if (t.get("metadata") or {}).get("name") == claim:
            image = ((t.get("metadata") or {}).get("annotations") or {}).get("harvesterhci.io/imageId", "")
            size, klass = _template_size(vm, claim)
            return {"made": made, "template": "claim", "source": {"image": image} if image else {"blank": True},
                    "template_size": size, "template_class": klass}
    return {"made": made, "template": "", "source": {}}


def _size(value):
    value = str(value or "").strip()
    if re.fullmatch(r"\d+", value):
        value += "Gi"
    if not re.fullmatch(r"\d+(Mi|Gi|Ti)", value):
        raise ValueError("a disk size is like 20Gi")
    return value


def _bytes(quantity):
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(Ki|Mi|Gi|Ti|K|M|G|T)?", str(quantity or "").strip())
    if not match:
        return 0
    scale = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40, "K": 10**3, "M": 10**6, "G": 10**9, "T": 10**12}
    return float(match.group(1)) * scale.get(match.group(2) or "", 1)


def _image(ref):
    ns, _, name = str(ref).rpartition("/")
    for item in V.images():
        if item["name"] == name and (not ns or item.get("namespace") == ns):
            if not item.get("storage_class"):
                raise ValueError(f"image {item['display']} is not ready yet")
            return item
    raise ValueError(f"image {ref} was not found")


def _disk_volume(vm, ns, claim, size, klass, source, to_create, effects):
    """A disk named claim, made the way this cluster makes disks; returns the
    VM volume that points at it. A plain claim is queued in to_create."""
    p = V.platform() or {}
    harvester, cdi = bool(p.get("harvester")), p.get("cdi", True)
    url, image = str(source.get("url") or "").strip(), str(source.get("image") or "").strip()
    if url and MASKS.has_stripped(url):
        # A copy shown below the reveal role, saved back, would download from the mask.
        raise ValueError("this disk address is hidden in your copy; type the full address in again")
    if url:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError(f"{url} is not a download address; give the full http(s):// URL"
                             + (", or pick the Harvester image" if harvester else ""))
        if not cdi and not harvester:
            raise ValueError("CDI is not installed, so a disk cannot be downloaded; install it from kubevirt.io")
    if image and not harvester:
        raise ValueError("images from the image list are Harvester's; use an image URL on this cluster")
    _forget_templates(vm, claim)
    if harvester:
        template = {"metadata": {"name": claim, "annotations": {}},
                    "spec": {"accessModes": ["ReadWriteMany"], "volumeMode": "Block",
                             "resources": {"requests": {"storage": size}}}}
        if url:
            # Resolve only at commit, after the entire form has validated.
            # The unresolved template is internal and must never be written.
            effects.append({"kind": "image-download", "namespace": ns, "claim": claim,
                            "url": url, "storage_class": klass})
        elif image:
            item = _image(image)
            template["metadata"]["annotations"]["harvesterhci.io/imageId"] = f"{item['namespace']}/{item['name']}"
            template["spec"]["storageClassName"] = item["storage_class"]
            if item.get("size_gb") and _bytes(size) < item["size_gb"] * 2**30:
                raise ValueError(f"the disk must be at least as big as the image ({item['size_gb']} GiB)")
        elif klass:
            template["spec"]["storageClassName"] = klass
        _set_claim_templates(vm, _claim_templates(vm) + [template])
        return {"persistentVolumeClaim": {"claimName": claim}}
    if cdi:
        storage = {"resources": {"requests": {"storage": size}}}
        if klass:
            storage["storageClassName"] = klass
        if harvester:
            storage.update(accessModes=["ReadWriteMany"], volumeMode="Block")
        vm["spec"]["dataVolumeTemplates"] = _dv_templates(vm) + [{
            "metadata": {"name": claim},
            "spec": {"source": {"http": {"url": url}} if url else {"blank": {}}, "storage": storage}}]
        return {"dataVolume": {"name": claim}}
    claim_obj = {"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": {"name": claim, "namespace": ns},
                 "spec": {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": size}}}}
    if klass:
        claim_obj["spec"]["storageClassName"] = klass
    to_create.append(claim_obj)
    return {"persistentVolumeClaim": {"claimName": claim}}


def _volume_claim(volume):
    return (volume.get("persistentVolumeClaim") or {}).get("claimName") or (volume.get("dataVolume") or {}).get("name") or ""


def _edit_disks(vm, ns, edits, adds, claims, to_create, resize, dropped, effects):
    tspec = vm["spec"]["template"]["spec"]
    devices = tspec["domain"].setdefault("devices", {})
    disks, volumes = devices.get("disks") or [], tspec.get("volumes") or []
    changed = False
    for e in edits:
        d = next((x for x in disks if x.get("name") == e.get("name")), None)
        if not d:
            raise ValueError(f"there is no disk {e.get('name')}")
        v = next((x for x in volumes if x.get("name") == d["name"]), {})
        claim = _volume_claim(v)
        if e.get("remove"):
            disks.remove(d)
            if v in volumes:
                volumes.remove(v)
            if claim:
                _forget_templates(vm, claim)
                dropped.append(claim)
            changed = True
            continue
        kind = "cdrom" if "cdrom" in d else "disk" if "disk" in d else ""
        if "boot" in e:
            boot = int(e["boot"]) if str(e["boot"] if e["boot"] is not None else "").strip() else None
            if boot is not None and not 1 <= boot <= 64:
                raise ValueError("boot order is a number from 1")
            if d.get("bootOrder") != boot:
                if boot is None:
                    d.pop("bootOrder", None)
                else:
                    d["bootOrder"] = boot
                changed = True
        if e.get("bus") and kind:
            if e["bus"] not in V.BUSES or (kind == "cdrom" and e["bus"] == "virtio"):
                raise ValueError(f"{e['bus']} is not a bus for a {'CD-ROM' if kind == 'cdrom' else 'disk'}")
            if (d.get(kind) or {}).get("bus") != e["bus"]:
                d[kind] = dict(d.get(kind) or {}, bus=e["bus"])
                changed = True
        made = claim in claims
        if e.get("source") is not None and claim and not made:
            size, klass = _template_size(vm, claim)
            size = _size(e.get("size") or size or "20Gi")
            volume = _disk_volume(vm, ns, claim, size, e.get("storage_class") or klass, e["source"] or {}, to_create, effects)
            v.clear()
            v.update({"name": d["name"], **volume})
            # A DataVolume CDI took but could not fill is made again from the new source.
            path = f"/apis/cdi.kubevirt.io/v1beta1/namespaces/{ns}/datavolumes/{urllib.parse.quote(claim)}"
            current = KUBE.get_optional(path, read=V.kget)
            effects.append({"kind": "replace-datavolume", "path": path,
                            "current": current, "claim": claim})
            changed = True
        elif e.get("size") and claim:
            size = _size(e["size"])
            pvc = claims.get(claim)
            if pvc:
                current = max((((pvc.get("status") or {}).get("capacity") or {}).get("storage", ""),
                              (((pvc.get("spec") or {}).get("resources") or {}).get("requests") or {}).get("storage", "")), key=_bytes)
                if _bytes(size) < _bytes(current):
                    raise ValueError(f"{claim} is {current}; a disk can grow but not shrink")
                if _bytes(size) > _bytes(current):
                    RESIZE.require(pvc, V.kget)
                    resize.append((claim, size))
            else:
                for t in _dv_templates(vm):
                    if t["metadata"].get("name") == claim:
                        box = t["spec"].get("storage") or t["spec"].setdefault("pvc", {})
                        box.setdefault("resources", {}).setdefault("requests", {})["storage"] = size
                        changed = True
                items = _claim_templates(vm)
                for t in items:
                    if t["metadata"].get("name") == claim:
                        t["spec"].setdefault("resources", {}).setdefault("requests", {})["storage"] = size
                        changed = True
                _set_claim_templates(vm, items)
    taken = {d.get("name") for d in disks} | {v.get("name") for v in volumes}
    claim_names = set(claims) | {_volume_claim(v) for v in volumes}
    for a in adds:
        cdrom = a.get("kind") == "cd-rom"
        index = 0
        while f"{'cdrom' if cdrom else 'disk'}-{index}" in taken:
            index += 1
        disk_name = f"{'cdrom' if cdrom else 'disk'}-{index}"
        claim = f"{vm['metadata']['name']}-{disk_name}"
        while claim in claim_names:
            claim += "-x"
        source = {"url": a["url"]} if a.get("url") else {"image": a["image"]} if a.get("image") else {}
        if cdrom and not source and not a.get("iso"):
            raise ValueError("a CD-ROM needs an image to hold")
        bus = a.get("bus") or ("sata" if cdrom else "virtio")
        if bus not in V.BUSES or (cdrom and bus == "virtio"):
            raise ValueError(f"{bus} is not a bus for a {'CD-ROM' if cdrom else 'disk'}")
        if a.get("iso"):
            # An ISO from the library: its one shared copy. A CD-ROM is read-only
            # to the guest; the claim itself is not marked read-only, because
            # KubeVirt sets the owner of a filesystem volume's folder before
            # starting and fails on a read-only mount.
            if not cdrom:
                raise ValueError("an ISO goes in a CD-ROM drive")
            if not callable(V.iso_ready):
                raise ValueError("ISO images are not offered on this cluster")
            V.iso_ready(ns, str(a["iso"]))
            volume = {"persistentVolumeClaim": {"claimName": str(a["iso"])}}
        else:
            volume = _disk_volume(vm, ns, claim, _size(a.get("size") or "20Gi"), a.get("storage_class") or "", source, to_create, effects)
        device = {"name": disk_name, ("cdrom" if cdrom else "disk"): {"bus": bus}}
        if a.get("boot"):
            device["bootOrder"] = int(a["boot"])
            if not 1 <= device["bootOrder"] <= 64:
                raise ValueError("boot order is a number from 1 to 64")
        disks.append(device)
        volumes.append({"name": disk_name, **volume})
        taken.add(disk_name)
        claim_names.add(claim)
        changed = True
    devices["disks"], tspec["volumes"] = disks, volumes
    return changed


def _set_network(iface, net, network):
    for binding in ("masquerade", "bridge", "binding"):
        iface.pop(binding, None)
    net.pop("pod", None)
    net.pop("multus", None)
    if network == "pod":
        net["pod"] = {}
        iface["masquerade"] = {}
    else:
        if not re.fullmatch(r"[a-z0-9-]+/[a-z0-9.-]+", network or ""):
            raise ValueError("a network is 'pod' or namespace/name of a network attachment")
        kind = _network_kind(network)
        net["multus"] = {"networkName": network}
        if kind == "macvtap":
            iface["binding"] = {"name": "macvtap"}
        else:
            iface["bridge"] = {}


def _network_kind(network):
    # macvlan passes only frames for the address it made itself; a VM bridged
    # onto it has its own, so it never hears DHCP or anything else. macvtap
    # is joined through KubeVirt's binding for it; anything else is bridged.
    ns, name = network.split("/", 1)
    found = KUBE.get_optional(f"/apis/k8s.cni.cncf.io/v1/namespaces/{ns}/network-attachment-definitions/{urllib.parse.quote(name)}", read=V.kget)
    config = ((found or {}).get("spec") or {}).get("config") or ""
    if '"macvlan"' in config:
        raise ValueError(f"{network} is a macvlan network, which carries containers only: "
                         "a VM needs a macvtap network or one on a host bridge")
    return "macvtap" if '"macvtap"' in config else "bridge"


def _edit_nics(tspec, edits, adds):
    devices = tspec["domain"].setdefault("devices", {})
    ifaces, nets = devices.get("interfaces") or [], tspec.get("networks") or []
    changed = False
    for e in edits:
        iface = next((x for x in ifaces if x.get("name") == e.get("name")), None)
        if not iface:
            raise ValueError(f"there is no network interface {e.get('name')}")
        net = next((x for x in nets if x.get("name") == iface["name"]), None)
        if net is None:
            net = {"name": iface["name"]}
            nets.append(net)
        if e.get("remove"):
            ifaces.remove(iface)
            nets.remove(net)
            changed = True
            continue
        if e.get("model") and e["model"] != iface.get("model", "virtio"):
            if e["model"] not in V.MODELS:
                raise ValueError(f"the model is one of {', '.join(V.MODELS)}")
            iface["model"] = e["model"]
            changed = True
        if "mac" in e:
            mac = str(e.get("mac") or "").strip().lower()
            if mac and not re.fullmatch(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", mac):
                raise ValueError(f"{mac} is not a MAC address")
            if mac != iface.get("macAddress", ""):
                if mac:
                    iface["macAddress"] = mac
                else:
                    iface.pop("macAddress", None)
                changed = True
        current = "pod" if "pod" in net else (net.get("multus") or {}).get("networkName", "")
        if e.get("network") and e["network"] != current:
            _set_network(iface, net, e["network"])
            changed = True
    for a in adds:
        index = 0
        names = {i.get("name") for i in ifaces}
        while f"nic-{index}" in names:
            index += 1
        iface, net = {"name": f"nic-{index}", "model": a.get("model") or "virtio"}, {"name": f"nic-{index}"}
        if iface["model"] not in V.MODELS:
            raise ValueError(f"the model is one of {', '.join(V.MODELS)}")
        _set_network(iface, net, a.get("network") or "pod")
        ifaces.append(iface)
        nets.append(net)
        changed = True
    if sum(1 for n in nets if "pod" in n) > 1:
        raise ValueError("a VM can be on the pod network once")
    devices["interfaces"], tspec["networks"] = ifaces, nets
    # Empty lists alone let KubeVirt recreate its default NIC on the next boot.
    changed |= devices.get("autoattachPodInterface") is not False
    devices["autoattachPodInterface"] = False
    return changed


# ---- cloud-init -------------------------------------------------------------

def _cloud_volume(tspec):
    for v in tspec.get("volumes") or []:
        for key in ("cloudInitNoCloud", "cloudInitConfigDrive"):
            if key in v:
                return v, key
    return None, ""


def _secret_values(ns, name):
    data = (V.kget(f"/api/v1/namespaces/{ns}/secrets/{urllib.parse.quote(name)}").get("data") or {})
    return {k: base64.b64decode(v).decode("utf-8", "replace") for k, v in data.items()}


def _read_cloud_init(vm, ns):
    volume, key = _cloud_volume(vm["spec"]["template"].get("spec") or {})
    if not volume:
        return {"user_data": "", "network_data": "", "source": ""}
    c = volume[key]
    user, network, source = c.get("userData", ""), c.get("networkData", ""), "inline"
    try:
        if (c.get("secretRef") or c.get("userDataSecretRef") or {}).get("name"):
            values = _secret_values(ns, (c.get("secretRef") or c.get("userDataSecretRef"))["name"])
            user, source = values.get("userdata", values.get("userData", "")), "secret"
        if (c.get("networkDataSecretRef") or {}).get("name"):
            values = _secret_values(ns, c["networkDataSecretRef"]["name"])
            network = values.get("networkdata", values.get("networkData", ""))
    except Exception:
        source = "unreadable"
    return {"user_data": user, "network_data": network, "source": source}


def _identity(obj):
    meta = (obj or {}).get("metadata") or {}
    if not meta.get("uid") or not meta.get("resourceVersion"):
        raise ValueError("Resource identity/version is unavailable; refresh before changing the VM")
    return {key: meta[key] for key in ("uid", "resourceVersion")}


def _queue_secret(ns, name, key, text, effects):
    path = f"/api/v1/namespaces/{ns}/secrets/{urllib.parse.quote(name)}"
    effect = next((row for row in effects if row.get("kind") == "secret" and row.get("path") == path), None)
    if effect is None:
        current = V.kget(path)
        effect = {"kind": "secret", "path": path, "identity": V._identity(current), "data": {}}
        effects.append(effect)
    effect["data"][key] = base64.b64encode(text.encode()).decode()


def _edit_cloud_init(vm, ns, cfg, effects):
    tspec = vm["spec"]["template"]["spec"]
    user, network = str(cfg.get("user_data") or ""), str(cfg.get("network_data") or "")
    volume, key = _cloud_volume(tspec)
    current = V._read_cloud_init(vm, ns)
    if current["source"] == "unreadable":
        raise ValueError("Cloud-init could not be read; nothing can be changed until its Secret is available")
    if user == current["user_data"] and network == current["network_data"]:
        return False
    if not volume:
        tspec["domain"].setdefault("devices", {}).setdefault("disks", []).append(
            {"name": "cloudinitdisk", "disk": {"bus": "virtio"}})
        tspec.setdefault("volumes", []).append(
            {"name": "cloudinitdisk", "cloudInitNoCloud": dict({"userData": user}, **({"networkData": network} if network else {}))})
        return True
    c = volume[key]
    user_ref = (c.get("secretRef") or c.get("userDataSecretRef") or {}).get("name")
    if user_ref:
        # Harvester keeps cloud-init in a secret of the VM's; that is what changes.
        if user != current["user_data"]:
            _queue_secret(ns, user_ref, "userdata", user, effects)
    else:
        c["userData"] = user
    net_ref = (c.get("networkDataSecretRef") or {}).get("name")
    if net_ref:
        if network != current["network_data"]:
            _queue_secret(ns, net_ref, "networkdata", network, effects)
    elif network:
        c["networkData"] = network
    else:
        c.pop("networkData", None)
    return True



def prepare_edit(ns, name, cfg, current=None):
    """Validate and prepare all VM changes without writing dependent resources."""
    V._name(ns, "namespace"), V._name(name, "VM name")
    current = copy.deepcopy(current if current is not None else V._get(ns, name))
    identity = V._identity(current)
    if current["metadata"].get("deletionTimestamp"):
        raise ValueError("The VM is deleting; wait before editing")
    vm = copy.deepcopy(current)
    spec = vm["spec"]
    if spec.get("instancetype") and any(key in cfg for key in ("cores", "memory")):
        raise ValueError("CPU and memory are controlled by this VM's instance type. Leave those fields unchanged; change the profile through a separately reviewed configuration workflow.")
    tspec = spec["template"]["spec"]
    dom = tspec["domain"]
    changed_hardware = False
    to_create, resize, dropped, effects = [], [], [], []
    claims = V._claims(ns, strict=True) if cfg.get("disks") or cfg.get("add_disks") else {}
    if "node" in cfg:
        selector = dict(tspec.get("nodeSelector") or {})
        node = str(cfg.get("node") or "")
        if node != selector.get(V.HOST, ""):
            if node:
                selector[V.HOST] = V._name(node, "node name")
            else:
                selector.pop(V.HOST, None)
            if selector:
                tspec["nodeSelector"] = selector
            else:
                tspec.pop("nodeSelector", None)
            changed_hardware = True
    if cfg.get("disks") or cfg.get("add_disks"):
        changed_hardware |= _edit_disks(vm, ns, cfg.get("disks") or [], cfg.get("add_disks") or [],
                                        claims, to_create, resize, dropped, effects)
    network_isolated = VMNETWORK.isolation_requested(cfg, VMNETWORK.isolated(vm))
    changed_hardware |= VMNETWORK.apply_isolation(vm, cfg)
    if not network_isolated and ("nics" in cfg or cfg.get("add_nics")):
        changed_hardware |= _edit_nics(tspec, cfg.get("nics") or [], cfg.get("add_nics") or [])
    if cfg.get("cloud_init") is not None:
        import homestead_host_access as HOSTACCESS
        if HOSTACCESS.role() not in (None, "admin"):
            raise PermissionError("only an admin can edit cloud-init")
        changed_hardware |= _edit_cloud_init(vm, ns, cfg["cloud_init"], effects)
    if cfg.get("host_devices"):
        # PCI and USB devices of a host, and a GPU's ROM (homestead_passthrough).
        import homestead_passthrough as PASSTHROUGH
        replaced = set(cfg["host_devices"].get("roms") or {}) | set(cfg["host_devices"].get("remove") or [])
        changed_hardware |= PASSTHROUGH.edit_vm(vm, ns, cfg["host_devices"], effects, PASSTHROUGH.current_roms(vm, ns, replaced))
    if cfg.get("hardware"):
        if "cores" in cfg and any(k in (cfg["hardware"].get("cpu") or {}) for k in ("sockets", "cores", "threads")):
            raise ValueError("set the CPU count or its topology, not both")
        changed_hardware |= HARDWARE.apply(vm, cfg["hardware"], locked_cpu=bool(spec.get("instancetype")))
    elif cfg.get("host_devices"):
        HARDWARE.validate_boot_output(vm)
    if "cores" in cfg:
        cores = int(cfg["cores"])
        if not 1 <= cores <= 128:
            raise ValueError("between 1 and 128 cores")
        cpu = dom.setdefault("cpu", {})
        per = int(cpu.get("sockets") or 1) * int(cpu.get("threads") or 1)
        if cores % per:
            raise ValueError(f"this VM has {cpu.get('sockets', 1)} socket(s) of {cpu.get('threads', 1)} thread(s); cores must be a multiple of {per}")
        if cpu.get("cores") != cores // per:
            cpu["cores"] = cores // per
            changed_hardware = True
        limits = (dom.get("resources") or {}).get("limits")
        if limits and "cpu" in limits:
            limits["cpu"] = str(cores)
    if "memory" in cfg:
        memory = str(cfg["memory"]).strip()
        if not re.fullmatch(r"\d+(\.\d+)?(Mi|Gi)", memory) or _bytes(memory) <= 0:
            raise ValueError("memory is like 4Gi or 512Mi")
        if V._memory(dom) != memory:
            changed_hardware = True
        dom.setdefault("memory", {})["guest"] = memory
        resources = dom.setdefault("resources", {})
        if (resources.get("limits") or {}).get("memory") is not None:
            resources["limits"]["memory"] = memory
        elif (resources.get("requests") or {}).get("memory") is not None or not resources:
            resources.setdefault("requests", {})["memory"] = memory
    if cfg.get("run_strategy"):
        if cfg["run_strategy"] not in V.RUN_STRATEGIES:
            raise ValueError(f"the run strategy is one of {', '.join(V.RUN_STRATEGIES)}")
        spec["runStrategy"] = cfg["run_strategy"]
        spec.pop("running", None)
    if "description" in cfg:
        annotations = vm["metadata"].setdefault("annotations", {})
        text = " ".join(str(cfg.get("description") or "").split())[:300]
        if text:
            annotations[V.DESCRIPTION] = text
        else:
            annotations.pop(V.DESCRIPTION, None)
    vm["metadata"].pop("managedFields", None)
    for effect in effects:
        if effect["kind"] == "configmap":
            current_map = KUBE.get_optional(effect["path"], read=V.kget)
            if current_map:
                labels = (current_map.get("metadata") or {}).get("labels") or {}
                if labels.get("homestead.io/managed") != "true" or labels.get("app") != name:
                    raise ValueError("The vBIOS ConfigMap belongs to another resource; inspect it before editing this VM")
            effect["identity"] = V._identity(current_map) if current_map else None
        if effect["kind"] == "replace-datavolume" and effect["current"] is not None:
            effect["identity"] = V._identity(effect.pop("current"))
    for claim, _ in resize:
        V._identity(claims[claim])
    return {"namespace": ns, "name": name, "current": current, "identity": identity,
            "vm": vm, "effects": effects, "to_create": to_create, "resize": resize,
            "claims": claims, "dropped": dropped, "changed_hardware": changed_hardware,
            "restart": bool(cfg.get("restart"))}


def _recheck_edit(prepared):
    ns, name = prepared["namespace"], prepared["name"]
    if V._identity(V._get(ns, name)) != prepared["identity"]:
        raise ValueError("The VM changed; review its edit again")
    for effect in prepared["effects"]:
        if effect["kind"] in ("image-download", "kubevirt-gates"):
            continue
        current = KUBE.get_optional(effect["path"], read=V.kget)
        if (None if current is None else V._identity(current)) != effect.get("identity"):
            raise ValueError("A VM dependency changed; review its edit again")
        if effect["kind"] == "replace-datavolume":
            path = f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{urllib.parse.quote(effect['claim'])}"
            if KUBE.get_optional(path, read=V.kget) is not None:
                raise ValueError("The disk now has a PVC; its source cannot be replaced")
    for claim, _ in prepared["resize"]:
        current = V.kget(f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{urllib.parse.quote(claim)}")
        if V._identity(current) != V._identity(prepared["claims"][claim]):
            raise ValueError("A disk changed; review its resize again")
        RESIZE.require(current, V.kget)
    old_names = {_volume_claim(v) for v in prepared["current"]["spec"]["template"]["spec"].get("volumes") or []}
    new_names = {_volume_claim(v) for v in prepared["vm"]["spec"]["template"]["spec"].get("volumes") or []} - old_names - {""}
    for claim in new_names:
        for path in (f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{urllib.parse.quote(claim)}",
                     f"/apis/cdi.kubevirt.io/v1beta1/namespaces/{ns}/datavolumes/{urllib.parse.quote(claim)}"):
            found = KUBE.get_optional(path, read=V.kget)
            # An ISO from the library is one shared copy that exists before
            # any VM holds it; attaching it is the point, not an adoption.
            if found is not None and ((found.get("metadata") or {}).get("labels") or {}).get("homestead.io/iso") == "true":
                break
            if found is not None:
                raise ValueError(f"New disk {claim} already exists; it cannot be adopted by this edit")


def commit_edit(prepared, before_save=None, send=None):
    """Apply a prepared edit, preserving Kubernetes optimistic concurrency.

    This is not a multi-object transaction: downloads or earlier dependency
    writes may remain if a later phase fails. Never replay an uncertain commit.
    before_save can re-admit after image downloads, before dependent writes.
    send is a request-local write/journal adapter; the returned vm_identity
    comes from the actual VM PUT response, not the pre-edit observation.
    """
    prepared = copy.deepcopy(prepared)
    send = send or V.ksend  # request-local; image and dependency writes share it
    ns, name, vm = prepared["namespace"], prepared["name"], prepared["vm"]
    to_create, resize, dropped = prepared["to_create"], prepared["resize"], prepared["dropped"]
    changed_hardware = prepared["changed_hardware"]
    V._recheck_edit(prepared)
    if any(effect["kind"] == "replace-datavolume" and effect.get("identity") for effect in prepared["effects"]):
        raise ValueError("An existing DataVolume cannot be replaced safely in a VM edit. Add a disk with a new name/source, then detach the old disk; its data is retained.")
    for effect in prepared["effects"]:
        if effect["kind"] == "image-download":
            result = HVIMAGE.download(V.kget, send, effect["namespace"], effect["url"], effect["storage_class"])
            templates = _claim_templates(vm)
            target = next(row for row in templates if row["metadata"]["name"] == effect["claim"])
            target["metadata"].setdefault("annotations", {})["harvesterhci.io/imageId"] = f"{result['namespace']}/{result['name']}"
            target["spec"]["storageClassName"] = result["storage_class"]
            _set_claim_templates(vm, templates)
    V._recheck_edit(prepared)
    if before_save:
        before_save(prepared)
    for effect in prepared["effects"]:
        if effect["kind"] == "secret":
            send("PATCH", effect["path"], {"metadata": effect["identity"], "data": effect["data"]},
                  ctype="application/merge-patch+json")
        elif effect["kind"] in ("configmap", "kubevirt-gates"):
            import homestead_passthrough as PASSTHROUGH
            if effect["kind"] == "configmap":
                PASSTHROUGH.write_configmap(effect, send)
            else:
                PASSTHROUGH.ensure_gates(*effect["gates"], send=send)
    for claim in to_create:
        try:
            send("POST", f"/api/v1/namespaces/{ns}/persistentvolumeclaims", claim)
        except urllib.error.HTTPError as error:
            raise ValueError(f"the disk {claim['metadata']['name']} could not be made: {V._refusal(error)}")
    try:
        saved = send("PUT", f"{V.API}/namespaces/{ns}/virtualmachines/{name}", vm)
    except urllib.error.HTTPError as error:
        raise ValueError(f"the VM was not saved: {V._refusal(error)}")
    grown = []
    for claim, size in resize:
        try:
            send("PATCH", f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{urllib.parse.quote(claim)}",
                  {"metadata": V._identity(prepared["claims"][claim]),
                   "spec": {"resources": {"requests": {"storage": size}}}}, ctype="application/merge-patch+json")
            grown.append(f"{claim} to {size}")
        except urllib.error.HTTPError as error:
            raise ValueError(f"the VM was saved, but {claim} could not grow: {V._refusal(error)}")
    restarted = False
    if changed_hardware and prepared["restart"]:
        try:
            send("PUT", f"{V.SUB}/namespaces/{ns}/virtualmachines/{name}/restart", {})
            restarted = True
        except urllib.error.HTTPError:
            pass
    detail = f"{name} saved"
    if grown:
        detail += "; growing " + ", ".join(grown)
    if dropped:
        detail += f"; {', '.join(dropped)} detached and kept"
    if changed_hardware:
        detail += "; restarting now to use the changes" if restarted else "; template updated; KubeVirt may apply supported changes live, otherwise review a restart"
    identity = (saved.get("metadata") or {}) if isinstance(saved, dict) else {}
    return {"ok": True, "detail": detail, "restart_needed": changed_hardware and not restarted,
            "vm_identity": {key: identity.get(key, "") for key in ("namespace", "name", "uid", "resourceVersion")}}


def edit(ns, name, cfg):
    return V.commit_edit(prepare_edit(ns, name, cfg))


# homestead_vms holds the bound state and re-exports this module, so it is imported last.
import homestead_vms as V  # noqa: E402
