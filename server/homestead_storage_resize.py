"""Shared Kubernetes expansion prerequisites for volume, VM and share editors."""
import urllib.error
import urllib.parse


def storage_class(pvc):
    return ((pvc.get("metadata") or {}).get("annotations") or {}).get(
        "volume.beta.kubernetes.io/storage-class", (pvc.get("spec") or {}).get("storageClassName") or "")


def options(pvc, read):
    name = storage_class(pvc)
    result = {"can_expand": False, "storage_class": name, "reason": "", "missing_class": False}
    if (pvc.get("status") or {}).get("phase") != "Bound":
        result["reason"] = "This claim must be bound to a volume before it can be enlarged."
    elif not name:
        result["reason"] = "This claim has no StorageClass, so Kubernetes cannot expand it. Create a larger volume and migrate the data."
    else:
        try:
            sc = read("/apis/storage.k8s.io/v1/storageclasses/" + urllib.parse.quote(name, safe=""))
        except urllib.error.HTTPError as error:
            result["missing_class"] = error.code == 404
            result["reason"] = ("This claim's StorageClass no longer exists. Open the volume's Edit dialog in Storage to check whether resize support can be repaired."
                                if error.code == 404 else "Could not check the StorageClass. Check access to the Kubernetes API and try again.")
        else:
            if sc.get("allowVolumeExpansion") is True:
                result["can_expand"] = True
            else:
                result["reason"] = ("This StorageClass does not allow volume expansion. An administrator can enable it if the storage driver supports resizing, "
                                    "or you can create a larger volume and migrate the data.")
    return result


def require(pvc, read):
    result = options(pvc, read)
    if not result["can_expand"]:
        raise ValueError(result["reason"])
