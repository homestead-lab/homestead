"""
VM disks, the VM image lists and creating a VM: importing a disk into a
DataVolume, the disks and images Homestead shows, and preparing and committing
a new VM.

Split by feature from homestead_imports (#623), moved unchanged. Names that
homestead_imports owns or that server.py binds there are read through it (I.name),
so a replacement there still applies; read and replace the names defined here, here.
"""
import base64
import copy
import json
import re
import time
import urllib.error
import urllib.parse
import homestead_kube as KUBE
import homestead_names as NAMES
import homestead_hvimage as HVIMAGE
import homestead_pod_resources as RESOURCES
import homestead_operations as OPS
import homestead_passthrough as PASSTHROUGH
import homestead_vm_network as VMNETWORK


# --------------------------------------------------------------- VMs
VM_DISK_LABEL = "homestead.io/vm-disk-import"


def _required_name(value, label="name"):
    value = str(value or "").strip()
    if not I.SAFE.match(value):
        raise ValueError(f"{label} must be lowercase letters, numbers and dashes")
    return value


def _get_or_none(path):
    """The object, or None when it is not there (404); kept by name because
    the Unraid VM import reads it through homestead_imports."""
    return KUBE.get_optional(path, read=I.kget)


def vm_disk_import_plan(namespace, name):
    """Preflight an import without returning or persisting its source URL."""
    namespace = _required_name(namespace or I.NS, "namespace")
    name = _required_name(name, "disk name")
    dv = _get_or_none(
        f"/apis/cdi.kubevirt.io/v1beta1/namespaces/{namespace}/datavolumes/{name}")
    pvc = _get_or_none(f"/api/v1/namespaces/{namespace}/persistentvolumeclaims/{name}")
    conflicts = []
    if dv:
        conflicts.append({"kind": "DataVolume", "name": name})
    if pvc:
        conflicts.append({"kind": "PersistentVolumeClaim", "name": name})
    return {"namespace": namespace, "name": name, "ready": not conflicts,
            "conflicts": conflicts,
            "message": ("Ready to create a new CDI DataVolume and PVC" if not conflicts else
                        f"{namespace}/{name} already exists; choose a new disk name")}


def _http_disk_source(cfg):
    source_url = str(cfg.get("source_url") or "").strip()
    try:
        parsed = urllib.parse.urlsplit(source_url)
    except ValueError as error:
        raise ValueError("source URL is invalid") from error
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("source URL must use HTTP or HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("put HTTP credentials in a Kubernetes Secret, not in the URL")
    source = {"url": source_url}
    checksum = str(cfg.get("checksum") or "").strip().lower()
    if checksum:
        match = re.fullmatch(r"(sha256|sha512):([0-9a-f]+)", checksum)
        if not match or len(match.group(2)) != {"sha256": 64, "sha512": 128}[match.group(1)]:
            raise ValueError("checksum must be sha256:<64 hex characters> or sha512:<128 hex characters>")
        source["checksum"] = checksum
    secret = str(cfg.get("secret_ref") or "").strip()
    cert = str(cfg.get("cert_config_map") or "").strip()
    if secret:
        source["secretRef"] = _required_name(secret, "secret reference")
    if cert:
        source["certConfigMap"] = _required_name(cert, "CA ConfigMap")
    return source


def import_vm_disk(cfg, ops=OPS):
    """Import a qemu-supported disk image into a new PVC through CDI."""
    namespace = _required_name(cfg.get("namespace") or I.NS, "namespace")
    name = _required_name(cfg.get("name"), "disk name")
    plan = vm_disk_import_plan(namespace, name)
    if not plan["ready"]:
        raise ValueError(plan["message"])
    try:
        size_gb = int(cfg.get("size_gb", 20))
    except (TypeError, ValueError) as error:
        raise ValueError("capacity must be a whole number of GiB") from error
    if not 1 <= size_gb <= 16384:
        raise ValueError("capacity must be between 1 and 16384 GiB")
    access_mode = str(cfg.get("access_mode") or "ReadWriteOnce")
    if access_mode not in ("ReadWriteOnce", "ReadWriteMany"):
        raise ValueError("access mode must be ReadWriteOnce or ReadWriteMany")
    storage_class = _required_name(cfg.get("storage_class") or "longhorn-r2", "storage class")
    source = _http_disk_source(cfg)
    body = {
        "apiVersion": "cdi.kubevirt.io/v1beta1", "kind": "DataVolume",
        "metadata": {
            "name": name, "namespace": namespace,
            "labels": {NAMES.key("managed"): "true", VM_DISK_LABEL: "true"},
            "annotations": {"homestead.io/import-source": "http",
                            "homestead.io/disk-format": "auto-detected"},
        },
        "spec": {
            "source": {"http": source}, "contentType": "kubevirt",
            "storage": {
                "storageClassName": storage_class,
                "accessModes": [access_mode], "volumeMode": "Filesystem",
                "resources": {"requests": {"storage": f"{size_gb}Gi"}},
            },
        },
    }
    import homestead_cdi_cleanup as CDI_CLEANUP
    operation = CDI_CLEANUP.dispatch(I.kget, I.ksend, ops, "vm-disk-import", f"Import VM disk {name}",
        {"kind": "DataVolume", "name": name, "namespace": namespace}, "/import",
        {"namespace": namespace, "name": name}, [body], "import")
    I._bust("vms", "vol")
    return {"ok": True, "namespace": namespace, "name": name, "pvc": name,
            "size_gb": size_gb, "operation": operation,
            "message": f"CDI import into {namespace}/{name} started"}


def _disk_consumers():
    consumers = {}
    try:
        vms = I.kget("/apis/kubevirt.io/v1/virtualmachines").get("items", [])
    except Exception:
        return consumers
    for vm in vms:
        meta = vm.get("metadata", {}) or {}
        ns, vm_name = meta.get("namespace", ""), meta.get("name", "")
        for volume in (((vm.get("spec", {}) or {}).get("template", {}) or {})
                       .get("spec", {}).get("volumes", []) or []):
            disk_name = (volume.get("dataVolume") or {}).get("name")
            if disk_name:
                consumers.setdefault((ns, disk_name), []).append(vm_name)
    return consumers


def list_vm_disks():
    """Return managed imports with status only; source URLs remain cluster-private."""
    try:
        items = I.kget("/apis/cdi.kubevirt.io/v1beta1/datavolumes").get("items", [])
    except Exception:
        return []
    consumers = _disk_consumers()
    out = []
    for item in items:
        meta = item.get("metadata", {}) or {}
        if (meta.get("labels", {}) or {}).get(VM_DISK_LABEL) != "true":
            continue
        spec, status = item.get("spec", {}) or {}, item.get("status", {}) or {}
        ns, name = meta.get("namespace", ""), meta.get("name", "")
        progress = str(status.get("progress", "") or "")
        amount = float(progress.rstrip("%") or 0) if re.fullmatch(r"\d+(?:\.\d+)?%?", progress) else 0
        request = ((spec.get("storage", {}) or {}).get("resources", {}) or {}).get("requests", {}) or {}
        messages = [c.get("message") or c.get("reason") for c in status.get("conditions", []) or []
                    if c.get("status") == "False" and (c.get("message") or c.get("reason"))]
        used_by = consumers.get((ns, name), [])
        out.append({"namespace": ns, "name": name, "pvc": status.get("claimName") or name,
                    "phase": status.get("phase") or "Pending", "progress": round(amount, 1),
                    "capacity": request.get("storage", ""),
                    "storage_class": (spec.get("storage", {}) or {}).get("storageClassName", ""),
                    "access_modes": (spec.get("storage", {}) or {}).get("accessModes", []),
                    "message": str(messages[0])[:300] if messages else "",
                    "in_use": bool(used_by), "used_by": used_by})
    return sorted(out, key=lambda row: (row["namespace"], row["name"]))


def _epoch(stamp):
    try:
        return int(__import__("calendar").timegm(time.strptime(str(stamp), "%Y-%m-%dT%H:%M:%SZ")))
    except (TypeError, ValueError):
        return 0


def list_vm_images():
    """Harvester's images. Each is a Longhorn backing image with a storage
    class of its own; a disk made on that class starts as a copy of it."""
    try:
        rows = []
        for i in I.kget("/apis/harvesterhci.io/v1beta1/virtualmachineimages").get("items", []):
            status = i.get("status", {}) or {}
            imported = next((c for c in status.get("conditions") or [] if c.get("type") == "Imported"), {})
            rows.append({"name": i["metadata"]["name"], "namespace": i["metadata"].get("namespace", ""),
                         "display": i["spec"].get("displayName", i["metadata"]["name"]),
                         "size_gb": round(int(status.get("size", 0) or 0) / 1024**3, 1),
                         "storage_class": status.get("storageClassName", ""),
                         "progress": status.get("progress", 0),
                         "ready": imported.get("status") == "True",
                         "failed": imported.get("status") == "False" and imported.get("reason") == "ImportFailed",
                         "message": " ".join(str(imported.get("message") or "").split())[:240],
                         # Where it came from and when, so the image store
                         # knows one of its own when it finds it.
                         "url": str(i["spec"].get("url") or ""), "source": i["spec"].get("sourceType", ""),
                         "created": _epoch(i["metadata"].get("creationTimestamp"))})
        return rows
    except Exception:
        return []


LH_API = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system"


def _items_or_empty(path):
    try:
        return I.kget(path).get("items", [])
    except Exception:
        return []


def vm_image_cache():
    """The VM images this cluster keeps, the way the image cache lists
    container ones: how big each is, which nodes hold a copy, and which VMs'
    disks were made from it.

    On Harvester an image is downloaded once and kept as a Longhorn backing
    image; every disk made from it starts as a copy, and each node its disks
    run on holds a copy of the image. Elsewhere CDI downloads a VM's disk for
    that VM alone - there is no cache to show, and this says so.

    The address an image came from is shown by its host only: a download
    link can carry a token."""
    images = _items_or_empty("/apis/harvesterhci.io/v1beta1/virtualmachineimages")
    if not images:
        try:
            I.kget("/apis/harvesterhci.io/v1beta1/virtualmachineimages")
            harvester = True
        except Exception:
            harvester = False
        return {"harvester": harvester, "images": [],
                "note": "" if harvester else
                "On this cluster CDI downloads each VM's disk for that VM alone, so there is no VM image cache: "
                "a disk imported under Import can be attached to one VM instead."}
    classes = {c["metadata"]["name"]: c for c in _items_or_empty("/apis/storage.k8s.io/v1/storageclasses")}
    backing = {b["metadata"]["name"]: b for b in _items_or_empty(f"{LH_API}/backingimages")}
    disk_node = {}
    for node in _items_or_empty(f"{LH_API}/nodes"):
        for disk in ((node.get("status") or {}).get("diskStatus") or {}).values():
            if disk.get("diskUUID"):
                disk_node[disk["diskUUID"]] = node["metadata"]["name"]
    claims = _items_or_empty("/api/v1/persistentvolumeclaims")
    mounted = {}
    for vm in _items_or_empty("/apis/kubevirt.io/v1/virtualmachines"):
        meta = vm.get("metadata") or {}
        for volume in (((vm.get("spec") or {}).get("template") or {}).get("spec") or {}).get("volumes") or []:
            claim = (volume.get("persistentVolumeClaim") or {}).get("claimName") or (volume.get("dataVolume") or {}).get("name")
            if claim:
                mounted.setdefault((meta.get("namespace", ""), claim), []).append(meta.get("name", ""))
    rows = []
    for image in images:
        meta, spec, status = image.get("metadata") or {}, image.get("spec") or {}, image.get("status") or {}
        ns, name = meta.get("namespace", ""), meta.get("name", "")
        ref, klass = f"{ns}/{name}", status.get("storageClassName", "")
        imported = next((c for c in status.get("conditions") or [] if c.get("type") == "Imported"), {})
        state = ("ready" if imported.get("status") == "True"
                 else "failed" if imported.get("status") == "False" and imported.get("reason") == "ImportFailed"
                 else "downloading")
        lh = backing.get(((classes.get(klass) or {}).get("parameters") or {}).get("backingImage", "")) or {}
        files = (lh.get("status") or {}).get("diskFileStatusMap") or {}
        nodes = sorted({disk_node.get(uuid, "") for uuid, row in files.items()
                        if (row or {}).get("state") == "ready"} - {""})
        disks, used_by = [], set()
        for claim in claims:
            cmeta, cspec = claim.get("metadata") or {}, claim.get("spec") or {}
            if ((cmeta.get("annotations") or {}).get("harvesterhci.io/imageId") == ref
                    or (klass and cspec.get("storageClassName") == klass)):
                disks.append(f"{cmeta.get('namespace')}/{cmeta.get('name')}")
                used_by.update(f"{cmeta.get('namespace')}/{vm}" for vm in mounted.get((cmeta.get("namespace"), cmeta.get("name")), []))
        size = int(status.get("size", 0) or 0)
        rows.append({"name": name, "namespace": ns, "display": spec.get("displayName") or name,
                     "source": urllib.parse.urlparse(str(spec.get("url") or "")).netloc
                               or str(spec.get("sourceType") or ""),
                     "size_mb": round(size / 1024 ** 2, 1),
                     "virtual_size_gb": round(int(status.get("virtualSize", 0) or 0) / 1024 ** 3, 1),
                     "state": state, "progress": int(status.get("progress", 0) or 0),
                     "message": " ".join(str(imported.get("message") or "").split())[:240],
                     "storage_class": klass, "nodes": nodes, "copies": len(nodes),
                     "disks": sorted(disks), "used_by": sorted(used_by),
                     "deleting": bool(meta.get("deletionTimestamp"))})
    rows.sort(key=lambda r: -r["size_mb"])
    return {"harvester": True, "images": rows, "note": ""}


def delete_vm_image(namespace, name, confirm=""):
    """Delete a VM image no disk was made from. A disk made from one keeps
    reading its blocks from it, so one still in use is refused by name."""
    namespace, name = _required_name(namespace, "namespace"), _required_name(name, "image")
    row = next((r for r in vm_image_cache()["images"] if (r["namespace"], r["name"]) == (namespace, name)), None)
    if not row:
        raise ValueError(f"there is no VM image {namespace}/{name}")
    if row["disks"]:
        raise PermissionError(f"{row['display']} is what {', '.join(row['disks'][:3])}"
                              f"{' and more' if len(row['disks']) > 3 else ''} started from; delete "
                              f"{'that disk' if len(row['disks']) == 1 else 'those disks'} first")
    if str(confirm or "").strip() != row["display"]:
        raise ValueError(f"type {row['display']} to confirm")
    I.ksend("DELETE", f"/apis/harvesterhci.io/v1beta1/namespaces/{namespace}/virtualmachineimages/{name}")
    I._bust("imgcache", "vms")
    return {"ok": True, "detail": f"{row['display']} is being deleted, with its copies on "
                                  f"{len(row['nodes'])} node{'s' if len(row['nodes']) != 1 else ''}"}


def _harvester_image(ref):
    """namespace/name of a Harvester image -> the image, or a clear refusal."""
    ns, _, image = str(ref).rpartition("/")
    for item in list_vm_images():
        if item["name"] == image and (not ns or item["namespace"] == ns):
            if not item["storage_class"]:
                raise ValueError(f"image {item['display']} is not ready yet")
            return item
    raise ValueError(f"image {ref} was not found")


def _modes_for_cdi(sc):
    """How a VM disk is claimed where CDI cannot tell: CDI takes a class's
    access and volume mode from its StorageProfile, which is empty for
    provisioners it does not know - k3s's local-path among them - and the
    disk is then refused (ErrClaimNotValid). Such a class gets a plain
    single-node file-backed disk, which is what it can give."""
    name = sc
    if not name:
        for item in (_get_or_none("/apis/storage.k8s.io/v1/storageclasses") or {}).get("items", []):
            if ((item.get("metadata") or {}).get("annotations") or {}).get(
                    "storageclass.kubernetes.io/is-default-class") == "true":
                name = item["metadata"]["name"]
                break
    if not name:
        return {}
    profile = _get_or_none(f"/apis/cdi.kubevirt.io/v1beta1/storageprofiles/{name}") or {}
    if (profile.get("status") or {}).get("claimPropertySets") or (profile.get("spec") or {}).get("claimPropertySets"):
        return {}
    return {"accessModes": ["ReadWriteOnce"], "volumeMode": "Filesystem"}


def _vm_mac():
    """A MAC of the kind KubeVirt gives, fixed here so cloud-init can find the
    interface its address belongs to whatever the guest names it."""
    import secrets as _secrets
    return "52:54:00:" + ":".join(f"{b:02x}" for b in _secrets.token_bytes(3))


def static_network(cfg, mac):
    """cloud-init's network config for one address on the VM's interface.

    Matched by MAC, so it lands on the right interface whether the guest calls
    it eth0, enp1s0 or ens3.
    """
    import ipaddress
    address = str(cfg.get("address") or "").strip()
    try:
        iface = ipaddress.ip_interface(f"{address}/{int(cfg.get('prefix') or 24)}")
    except ValueError as error:
        raise ValueError(f"{address or '(blank)'} with /{cfg.get('prefix')} is not an address like 192.0.2.51/24") from error
    net = iface.network
    if iface.version != 4 or iface.ip in (net.network_address, net.broadcast_address):
        raise ValueError(f"{iface.ip} cannot be a machine's address in {net}")
    gateway = str(cfg.get("gateway") or "").strip()
    if gateway:
        if ipaddress.ip_address(gateway) not in net:
            raise ValueError(f"the gateway {gateway} is outside {net}")
        if ipaddress.ip_address(gateway) == iface.ip:
            raise ValueError(f"{iface.ip} is the gateway's address")
    dns = [d for d in (cfg.get("dns") or ([gateway] if gateway else [])) if d]
    for d in dns:
        ipaddress.ip_address(d)
    lines = ["version: 2", "ethernets:", "  lan:", "    match:", f"      macaddress: \"{mac}\"",
             "    set-name: eth0", "    dhcp4: false", f"    addresses: [\"{iface.with_prefixlen}\"]"]
    if gateway:
        lines += ["    routes:", f"      - to: default", f"        via: {gateway}"]
    if dns:
        lines += ["    nameservers:", f"      addresses: [{', '.join(dns)}]"]
    return "\n".join(lines) + "\n", str(iface.ip)


def prepare_vm(cfg, platform=None, default_class=""):
    """Prepare a KubeVirt VM and dependencies without creating any resources.

    Harvester makes a VM's disks from its harvesterhci.io/volumeClaimTemplates
    annotation - block volumes every node can reach, so the VM can live-migrate,
    and a disk from an image is a claim on that image's own storage class. That
    is how its UI does it, so its pages and delete-with-disks treat these VMs
    as their own. Elsewhere, CDI fills a DataVolume and picks the access and
    volume modes its storage profile says the class supports - local-path
    cannot give ReadWriteMany - and with no CDI a blank disk is a plain claim,
    which KubeVirt formats itself. Downloading an image needs CDI either way.
    """
    platform = platform or {}
    harvester = bool(platform.get("harvester"))
    cdi = platform.get("cdi", True)
    name = _required_name(cfg.get("name"))
    ns = _required_name(cfg.get("namespace") or I.NS, "namespace")
    if _get_or_none(f"/apis/kubevirt.io/v1/namespaces/{ns}/virtualmachines/{name}") is not None:
        raise ValueError(f"a VM named {name} already exists in {ns}")
    cores = int(cfg.get("cores", 2))
    mem = cfg.get("memory", "2Gi")
    disk = int(cfg.get("disk_gb", 20))
    if not 1 <= cores <= 128:
        raise ValueError("between 1 and 128 cores")
    if not re.fullmatch(r"\d+(?:\.\d+)?(?:Mi|Gi)", str(mem)) or RESOURCES.quantity(mem) <= 0:
        raise ValueError("memory must be positive, like 2Gi or 512Mi")
    if not 1 <= disk <= 16384:
        raise ValueError("disk size must be between 1 and 16384 GiB")
    sc = str(cfg.get("storage_class") or default_class or "").strip()
    imported_dv = str(cfg.get("disk_import") or "").strip()
    image_ref = str(cfg.get("image_id") or "").strip()
    image_url = str(cfg.get("image_url") or "").strip()
    dv = imported_dv or f"{name}-disk"
    password = str(cfg.get("password") or "")
    install_iso = str(cfg.get("install_iso") or "").strip()
    if install_iso and (imported_dv or cfg.get("image_url") or cfg.get("image_id") or cfg.get("store_id")):
        raise ValueError("a VM installed from an ISO starts on a blank disk")
    # An installer asks for its own password; cloud-init is for cloud images.
    if not imported_dv and not install_iso and not cfg.get("cloud_init") and len(password) < 10:
        raise ValueError("root password must be at least 10 characters")
    if image_url:
        parsed = urllib.parse.urlparse(image_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError(f"{image_url} is not a download address; give the full http(s):// URL"
                             + (", or pick the Harvester image from the list" if harvester else ""))
    if image_ref and not harvester:
        raise ValueError("images from the image list are Harvester's; use an image URL on this cluster")
    # On Harvester a URL becomes one of its images, which needs no CDI.
    if (imported_dv or (image_url and not harvester)) and not cdi:
        raise ValueError("CDI (the containerized data importer) is not installed, so disk images cannot be "
                         "downloaded or imported; install it from kubevirt.io, or start from a blank disk")

    if imported_dv:
        _required_name(imported_dv, "imported disk")
        disk_obj = _get_or_none(
            f"/apis/cdi.kubevirt.io/v1beta1/namespaces/{ns}/datavolumes/{imported_dv}")
        if not disk_obj:
            raise ValueError(f"imported disk {ns}/{imported_dv} does not exist")
        if (disk_obj.get("status", {}) or {}).get("phase") != "Succeeded":
            raise ValueError("imported disk is not ready yet")
        users = _disk_consumers().get((ns, imported_dv), [])
        if users:
            raise ValueError(f"imported disk is already attached to VM {users[0]}")

    cloudinit = cfg.get("cloud_init") or (password and not install_iso and (
        "#cloud-config\n"
        f"hostname: {name}\n"
        "ssh_pwauth: true\n"
        # Quoted, as homestead_k3scluster.user_data does: bare, a "#", "*",
        # ": " or all digits would make YAML read another value or none. Not
        # ASCII-escaped: PyYAML reads a surrogate pair as two characters.
        f"password: {json.dumps(password, ensure_ascii=False)}\n"
        "chpasswd: {expire: false}\n"))

    annotations, templates = {}, []
    claims, secrets, downloads = [], [], []
    size = f"{disk}Gi"
    if imported_dv:
        root = {"name": "root", "dataVolume": {"name": dv}}
    elif harvester:
        claim = {"metadata": {"name": dv, "annotations": {}},
                 "spec": {"accessModes": ["ReadWriteMany"], "volumeMode": "Block",
                          "resources": {"requests": {"storage": size}}}}
        if image_url:
            # Harvester downloads it as one of its images, and the disk starts
            # as a copy of that - CDI's importer cannot be given its volumes.
            downloads.append({"namespace": ns, "url": image_url, "storage_class": sc, "claim": dv})
        elif image_ref:
            image = _harvester_image(image_ref)
            claim["metadata"]["annotations"]["harvesterhci.io/imageId"] = f"{image['namespace']}/{image['name']}"
            claim["spec"]["storageClassName"] = image["storage_class"]
            if image["size_gb"] and disk < image["size_gb"]:
                raise ValueError(f"the disk must be at least as big as the image ({image['size_gb']} GiB)")
        elif sc:
            claim["spec"]["storageClassName"] = sc
        annotations["harvesterhci.io/volumeClaimTemplates"] = json.dumps([claim])
        root = {"name": "root", "persistentVolumeClaim": {"claimName": dv}}
    elif cdi:
        storage = {"resources": {"requests": {"storage": size}}}
        if sc:
            storage["storageClassName"] = sc
        if harvester:
            # A downloaded image on Harvester still gets a disk that can migrate.
            storage.update(accessModes=["ReadWriteMany"], volumeMode="Block")
        else:
            storage.update(_modes_for_cdi(sc))
        templates = [{"metadata": {"name": dv},
                      "spec": {"source": {"http": {"url": image_url}} if image_url else {"blank": {}},
                               "storage": storage}}]
        root = {"name": "root", "dataVolume": {"name": dv}}
    else:
        claim = {"apiVersion": "v1", "kind": "PersistentVolumeClaim",
                 "metadata": {"name": dv, "namespace": ns, "labels": {NAMES.key("managed"): "true", "app": name}},
                 "spec": {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": size}}}}
        if sc:
            claim["spec"]["storageClassName"] = sc
        if _get_or_none(f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{dv}"):
            raise ValueError(f"a volume named {dv} already exists")
        claims.append(claim)
        root = {"name": "root", "persistentVolumeClaim": {"claimName": dv}}

    # Which network: the pod network (reached through a Service), or a VM
    # network bridged to the LAN, where the VM has an address of its own.
    network_isolated = VMNETWORK.isolation_requested(cfg)
    if network_isolated and any(cfg.get(key) for key in ("network", "static_ip", "nic_model", "mac", "add_nics", "nics")):
        raise ValueError("An isolated VM cannot have a network card or network address")
    network = str(cfg.get("network") or "pod").strip()
    interface = net = None
    network_data = address = mac = ""
    if not network_isolated:
        mac = str(cfg.get("mac") or _vm_mac()).lower()
        if not re.fullmatch(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", mac) or int(mac.split(":")[0], 16) & 1:
            raise ValueError("MAC must be a unicast hardware address like 52:54:00:12:34:56")
        # The card the guest sees: VirtIO unless asked, but a Windows guest has
        # no driver for it until its guest tools are in, so it came up offline.
        nic_model = str(cfg.get("nic_model") or "virtio").strip()
        if nic_model not in ("virtio", "e1000", "e1000e", "rtl8139"):
            raise ValueError("the network card is one of virtio, e1000, e1000e, rtl8139")
        if network == "pod":
            if cfg.get("static_ip"):
                raise ValueError("an address of its own needs a LAN network (bridged), not the pod network")
            interface, net = {"name": "default", "masquerade": {}, "model": nic_model, "macAddress": mac}, {"name": "default", "pod": {}}
        else:
            if not re.fullmatch(r"[a-z0-9-]+/[a-z0-9.-]+", network):
                raise ValueError(f"{network} is not a LAN network like default/vlan1")
            nad_ns, nad = network.split("/", 1)
            found = _get_or_none(f"/apis/k8s.cni.cncf.io/v1/namespaces/{nad_ns}/network-attachment-definitions/{nad}")
            if not found:
                raise ValueError(f"there is no LAN network {network}")
            config_text = (found.get("spec") or {}).get("config") or ""
            if '"macvlan"' in config_text:
                raise ValueError(f"{network} is a macvlan network, which carries containers only: "
                                 "a VM needs a macvtap network or one on a host bridge")
            # macvtap is joined through KubeVirt's binding for it, a bridge directly.
            binding = {"binding": {"name": "macvtap"}} if '"macvtap"' in config_text else {"bridge": {}}
            interface = {"name": "default", **binding, "model": nic_model, "macAddress": mac}
            net = {"name": "default", "multus": {"networkName": network}}
        network_data, address = ("", "")
        if cfg.get("static_ip"):
            network_data, address = static_network(cfg["static_ip"], mac)

    # The bus the guest was installed on: Windows set up on SATA has no
    # virtio driver to find its own boot disk with.
    disk_bus = str(cfg.get("disk_bus") or "virtio")
    if disk_bus not in ("virtio", "sata", "scsi"):
        raise ValueError("the disk bus is one of virtio, sata, scsi")
    disks = [{"name": "root", "disk": {"bus": disk_bus}, "bootOrder": 1}]
    volumes = [root]
    # More imported disks beside the boot one - a VM from Unraid with two vdisks.
    for index, extra in enumerate(cfg.get("extra_disks") or [], start=2):
        extra = extra if isinstance(extra, dict) else {"name": extra}
        extra_name = _required_name(extra.get("name"), "imported disk")
        extra_bus = str(extra.get("bus") or "virtio")
        if extra_bus not in ("virtio", "sata", "scsi") or extra_name == imported_dv:
            raise ValueError(f"imported disk {extra_name} cannot be attached as asked")
        found = _get_or_none(f"/apis/cdi.kubevirt.io/v1beta1/namespaces/{ns}/datavolumes/{extra_name}")
        if not found or ((found.get("status") or {}).get("phase")) != "Succeeded":
            raise ValueError(f"imported disk {ns}/{extra_name} is not ready")
        if _disk_consumers().get((ns, extra_name)):
            raise ValueError(f"imported disk {extra_name} is already attached to another VM")
        disks.append({"name": f"disk{index}", "disk": {"bus": extra_bus}})
        volumes.append({"name": f"disk{index}", "dataVolume": {"name": extra_name}})
    if install_iso:
        # The installer boots first, from its ISO held read-only; the blank
        # disk it installs onto boots once the ISO is ejected or empty.
        if not callable(I.iso_ready):
            raise ValueError("ISO images are not offered on this cluster")
        I.iso_ready(ns, install_iso)
        disks = [{"name": "root", "disk": {"bus": "virtio"}, "bootOrder": 2},
                 {"name": "install", "cdrom": {"bus": "sata"}, "bootOrder": 1}]
        volumes = [root, {"name": "install", "persistentVolumeClaim": {"claimName": install_iso}}]
        drivers_iso = str(cfg.get("drivers_iso") or "").strip()
        if drivers_iso:
            # Windows setup sees no disk on the virtio bus until it loads the
            # VirtIO drivers: their ISO in a second drive, as on Proxmox.
            if drivers_iso == install_iso:
                raise ValueError("the drivers ISO is the installer itself; choose the VirtIO drivers ISO")
            I.iso_ready(ns, drivers_iso)
            disks.append({"name": "drivers", "cdrom": {"bus": "sata"}})
            volumes.append({"name": "drivers", "persistentVolumeClaim": {"claimName": drivers_iso}})
    secret_name = ""
    if cloudinit or network_data:
        # In a Secret, as Harvester keeps them: a password or a join token is
        # not something to leave in the VM's own definition for every reader.
        secret_name = f"{name}-cloudinit"
        if _get_or_none(f"/api/v1/namespaces/{ns}/secrets/{secret_name}"):
            raise ValueError(f"a secret named {secret_name} already exists in {ns}")
        data = {"userdata": base64.b64encode((cloudinit or "#cloud-config\n").encode()).decode()}
        if network_data:
            data["networkdata"] = base64.b64encode(network_data.encode()).decode()
        secrets.append({
            "apiVersion": "v1", "kind": "Secret", "type": "Opaque",
            "metadata": {"name": secret_name, "namespace": ns, "labels": {NAMES.key("managed"): "true", "app": name}},
            "data": data})
        source = {"secretRef": {"name": secret_name}}
        if network_data:
            source["networkDataSecretRef"] = {"name": secret_name}
        disks.append({"name": "cloudinit", "disk": {"bus": "virtio"}})
        volumes.append({"name": "cloudinit", "cloudInitNoCloud": source})

    cpu = {"cores": cores}
    if cfg.get("cpu_model") in ("host-passthrough", "host-model"):
        cpu["model"] = cfg["cpu_model"]
    template_spec = {
        "domain": {
            "cpu": cpu,
            "memory": {"guest": mem},
            "resources": {"requests": {"memory": mem}},
            "devices": {
                "disks": disks,
                "interfaces": [interface] if interface else [],
                "autoattachPodInterface": False,
            },
        },
        "networks": [net] if net else [],
        "volumes": volumes,
    }
    if cfg.get("log_console"):
        # KubeVirt 1.1 and later keep what the VM prints on its serial
        # console as a log of its launcher pod, which is how a job shows a
        # VM installing itself. Harvester turns that off for every VM; this
        # turns it back on for this one. Older KubeVirt drops the field.
        template_spec["domain"]["devices"]["logSerialConsole"] = True
    if harvester:
        # Harvester's disks are shared block volumes, so a node drain can move
        # the VM. Elsewhere the cluster's own default applies: asking to
        # live-migrate a VM on a local disk only blocks the drain.
        template_spec["evictionStrategy"] = "LiveMigrate"
    vm = {
        "apiVersion": "kubevirt.io/v1", "kind": "VirtualMachine",
        "metadata": {"name": name, "namespace": ns,
                     "labels": {NAMES.key("managed"): "true", "app": name, **(cfg.get("labels") or {})},
                     **({"annotations": annotations} if annotations else {})},
        "spec": {
            # spec.running is deprecated; a run strategy is what KubeVirt and
            # Harvester both read, and what start and stop change.
            "runStrategy": "RerunOnFailure" if cfg.get("start", True) else "Halted",
            **({"dataVolumeTemplates": templates} if templates else {}),
            "template": {
                "metadata": {"labels": {"kubevirt.io/domain": name, "app": name}},
                "spec": template_spec,
            },
        },
    }
    VMNETWORK.apply_isolation(vm, cfg)
    effects = []
    if cfg.get("host_devices"):
        PASSTHROUGH.edit_vm(vm, ns, cfg["host_devices"], effects)
    if cfg.get("hardware"):
        # GPU output validates devices selected in the same creation wizard.
        import homestead_vm_hardware as HARDWARE
        HARDWARE.apply(vm, cfg["hardware"])
    return {"namespace": ns, "name": name, "vm": vm, "claims": claims, "secrets": secrets, "effects": effects,
            "downloads": downloads, "secret_name": secret_name,
            "result": {"ok": True, "vm": name, "datavolume": dv, "address": address,
                       "mac": mac}}


def _recheck_vm_creation(prepared):
    ns, name = prepared["namespace"], prepared["name"]
    targets = [f"/apis/kubevirt.io/v1/namespaces/{ns}/virtualmachines/{name}"]
    targets.extend(effect["path"] for effect in prepared.get("effects", []) if effect["kind"] == "configmap" and effect["body"])
    for kind, rows in (("persistentvolumeclaims", prepared["claims"]), ("secrets", prepared["secrets"])):
        targets.extend(f"/api/v1/namespaces/{ns}/{kind}/{row['metadata']['name']}" for row in rows)
    # Controller-made claims need the same collision protection as claims we
    # POST ourselves. A name in a template is not permission to adopt a disk.
    for template in prepared["vm"]["spec"].get("dataVolumeTemplates") or []:
        claim = _required_name(template["metadata"]["name"], "disk name")
        targets.extend([f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{claim}",
                        f"/apis/cdi.kubevirt.io/v1beta1/namespaces/{ns}/datavolumes/{claim}"])
    annotation = (prepared["vm"].get("metadata", {}).get("annotations") or {}).get("harvesterhci.io/volumeClaimTemplates", "[]")
    for template in json.loads(annotation):
        claim = _required_name(template["metadata"]["name"], "disk name")
        targets.append(f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{claim}")
    for path in targets:
        if _get_or_none(path) is not None:
            raise ValueError("A VM or planned dependency already exists; inspect it and review creation again")


def commit_vm(prepared, before_save=None, send=None):
    """Apply an already validated VM. Retain partial resources on any failure.

    An image download may outlast the original capacity snapshot. before_save
    receives the resolved manifest for re-admission before creating VM storage.
    send is a request-local write/journal adapter. Its owner must check any
    latched failure before acknowledging completion; ownership warnings can
    otherwise leave a created VM with a partially applied Secret update.
    """
    prepared = copy.deepcopy(prepared)
    send = send or I.ksend  # per-request journal hook; never replace global ksend
    ns, name, vm = prepared["namespace"], prepared["name"], prepared["vm"]
    _recheck_vm_creation(prepared)
    for download in prepared["downloads"]:
        image = HVIMAGE.download(I.kget, send, ns, download["url"], download["storage_class"])
        templates = json.loads(vm["metadata"]["annotations"]["harvesterhci.io/volumeClaimTemplates"])
        claim = next(row for row in templates if row["metadata"]["name"] == download["claim"])
        claim["metadata"].setdefault("annotations", {})["harvesterhci.io/imageId"] = f"{image['namespace']}/{image['name']}"
        claim["spec"]["storageClassName"] = image["storage_class"]
        vm["metadata"]["annotations"]["harvesterhci.io/volumeClaimTemplates"] = json.dumps(templates)
    _recheck_vm_creation(prepared)
    if before_save:
        before_save(prepared)
    created_roms = {}
    for effect in prepared.get("effects", []):
        if effect["kind"] == "configmap" and effect["body"]:
            # Creation never adopts or overwrites an existing ROM ConfigMap.
            receipt = send("POST", effect["path"].rsplit("/", 1)[0], effect["body"])
            created_roms[effect["body"]["metadata"]["name"]] = (receipt or {}).get("metadata") or {}
        elif effect["kind"] == "kubevirt-gates":
            PASSTHROUGH.ensure_gates(*effect["gates"], send=send)
    for claim in prepared["claims"]:
        send("POST", f"/api/v1/namespaces/{ns}/persistentvolumeclaims", claim)
    created_secrets = {}
    for secret in prepared["secrets"]:
        result = send("POST", f"/api/v1/namespaces/{ns}/secrets", secret)
        created_secrets[secret["metadata"]["name"]] = (result or {}).get("metadata") or {}
    secret_name = prepared["secret_name"]
    try:
        created = send("POST", f"/apis/kubevirt.io/v1/namespaces/{ns}/virtualmachines", vm)
    except urllib.error.HTTPError as error:
        # A failed response does not prove that creation had no effect. Keep
        # claims and Secrets for explicit inspection instead of deleting data.
        if prepared["downloads"]:
            try:
                why = json.loads(error.read().decode("utf-8", "replace")).get("message", "")
            except Exception:
                why = f"HTTP {error.code}"
            raise ValueError(f"Harvester would not take the VM yet ({why}). Its image keeps downloading: "
                             "choose it under Boot disk once it is ready.") from error
        raise
    # The Secret goes when the VM does.
    uid = ((created or {}).get("metadata") or {}).get("uid") if isinstance(created, dict) else ""
    created_meta = ((created or {}).get("metadata") or {}) if isinstance(created, dict) else {}
    prepared["result"]["vm_identity"] = {key: created_meta.get(key, "")
                                        for key in ("namespace", "name", "uid", "resourceVersion")}
    if secret_name and uid:
        try:
            secret_identity = created_secrets.get(secret_name) or {}
            if not secret_identity.get("uid") or not secret_identity.get("resourceVersion"):
                raise ValueError("Created Secret identity/version is unavailable")
            send("PATCH", f"/api/v1/namespaces/{ns}/secrets/{secret_name}",
                  {"metadata": {"uid": secret_identity["uid"], "resourceVersion": secret_identity["resourceVersion"],
                                "ownerReferences": [{"apiVersion": "kubevirt.io/v1", "kind": "VirtualMachine",
                                                     "name": name, "uid": uid}]}},
                  ctype="application/merge-patch+json")
        except Exception:
            prepared["result"]["warning"] = "VM created, but its login Secret ownership could not be recorded. Keep the Secret and inspect it before cleanup."
    for rom_name, identity in created_roms.items():
        if not uid or not identity.get("uid") or not identity.get("resourceVersion"):
            prepared["result"]["warning"] = " ".join(filter(None, [prepared["result"].get("warning"),
                "VM created, but vBIOS ConfigMap ownership needs inspection before cleanup."]))
            continue
        send("PATCH", f"/api/v1/namespaces/{ns}/configmaps/{rom_name}",
             {"metadata": {"uid": identity["uid"], "resourceVersion": identity["resourceVersion"],
                           "ownerReferences": [{"apiVersion": "kubevirt.io/v1", "kind": "VirtualMachine", "name": name, "uid": uid}]}},
             ctype="application/merge-patch+json")
    I._bust("flow", "ov")
    return prepared["result"]


def create_vm(cfg, platform=None, default_class=""):
    return commit_vm(prepare_vm(cfg, platform, default_class))


import homestead_imports as I  # noqa: E402  (imports at the bottom: homestead_imports imports this module)
