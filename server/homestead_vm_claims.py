"""Resolve planned VM claim modes without provisioning or adopting disks."""
import json
import urllib.error
from urllib.parse import quote


def plans(vm, read, extra=(), downloads=()):
    result = {}
    templates = list(extra)
    templates += json.loads((vm.get("metadata", {}).get("annotations") or {}).get("harvesterhci.io/volumeClaimTemplates", "[]"))
    for template in vm.get("spec", {}).get("dataVolumeTemplates") or []:
        spec = template.get("spec") or {}
        templates.append({"metadata": template["metadata"], "spec": spec.get("storage") or spec.get("pvc") or {}})
    pending_images = {row["claim"]: row for row in downloads}
    for template in templates:
        name = template["metadata"]["name"]
        spec = template.get("spec") or {}
        klass = spec.get("storageClassName") or (pending_images.get(name) or {}).get("storage_class") or ""
        if not klass:
            classes = read("/apis/storage.k8s.io/v1/storageclasses")
            if not isinstance(classes.get("items"), list) or (classes.get("metadata") or {}).get("continue"):
                raise ValueError("Storage class inventory is incomplete")
            defaults = [row["metadata"]["name"] for row in classes["items"]
                        if (row.get("metadata", {}).get("annotations") or {}).get("storageclass.kubernetes.io/is-default-class") == "true"]
            if len(defaults) != 1:
                raise ValueError("Choose an explicit VM storage class; no single default could be verified")
            klass = defaults[0]
        storage_class = read("/apis/storage.k8s.io/v1/storageclasses/" + quote(klass, safe=""))
        metadata = storage_class.get("metadata") or {}
        if metadata.get("name") != klass or metadata.get("deletionTimestamp") or not metadata.get("uid") or not metadata.get("resourceVersion"):
            raise ValueError(f"VM disk {name} storage class identity/readiness could not be verified")
        modes, volume_mode = spec.get("accessModes") or [], spec.get("volumeMode")
        if not modes:
            try:
                profile = read("/apis/cdi.kubevirt.io/v1beta1/storageprofiles/" + quote(klass, safe=""))
            except urllib.error.HTTPError as error:
                if error.code != 404:
                    raise
                profile = {}
            properties = (profile.get("status") or {}).get("claimPropertySets") or (profile.get("spec") or {}).get("claimPropertySets") or []
            choices = [row for row in properties if not volume_mode or row.get("volumeMode") == volume_mode]
            if not choices or not choices[0].get("accessModes"):
                raise ValueError(f"VM disk {name} access mode is unresolved; choose a storage class with a usable CDI profile")
            modes = choices[0]["accessModes"]
            volume_mode = choices[0].get("volumeMode")
        if len(modes) != 1 or modes[0] not in ("ReadWriteOnce", "ReadWriteOncePod", "ReadWriteMany", "ReadOnlyMany"):
            raise ValueError(f"VM disk {name} needs an unambiguous supported access mode")
        if name in result:
            raise ValueError(f"VM disk {name} is defined more than once")
        result[name] = {"name": name, "access_mode": modes[0], "storage_class": klass,
                        "volume_mode": volume_mode or "Filesystem",
                        "size": ((spec.get("resources") or {}).get("requests") or {}).get("storage", "")}
    return result
