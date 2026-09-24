"""Harvester's own way to start a disk from a URL: a VM image it downloads.

On Harvester a download is not CDI's to do. Its Longhorn serves VM disks as
shared block volumes, which CDI's importer pod cannot be given reliably - the
import stalls with the volume failing to map. Harvester instead downloads the
URL into a VirtualMachineImage (a Longhorn backing image, with a storage class
of its own), and a disk made on that class starts as a copy of it. That is
what its UI does, the image stays in its list for the next VM, and the
download's progress is Harvester's to report.
"""
import posixpath
import re
import secrets
import urllib.parse

import homestead_names as NAMES

LONGHORN = "driver.longhorn.io"
# What a Harvester image's storage class carries over from the class chosen
# for the disk, so the disk gets the copies and placement asked for.
CARRIED = ("numberOfReplicas", "staleReplicaTimeout", "diskSelector", "nodeSelector", "dataLocality")


def _images(ns):
    return f"/apis/harvesterhci.io/v1beta1/namespaces/{ns}/virtualmachineimages"


def display_name(url):
    name = posixpath.basename(urllib.parse.urlparse(url).path) or "image"
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name)[:63] or "image"


def storage_class(image):
    """The class a disk from this image is made on: Harvester's own name for it."""
    return ((image.get("status") or {}).get("storageClassName")
            or "longhorn-" + (image.get("metadata") or {}).get("name", ""))


def _parameters(kget, klass):
    params = {"numberOfReplicas": "3", "staleReplicaTimeout": "30", "migratable": "true"}
    if klass:
        try:
            sc = kget(f"/apis/storage.k8s.io/v1/storageclasses/{klass}")
        except Exception:
            sc = {}
        if sc.get("provisioner") == LONGHORN:
            for key in CARRIED:
                if (sc.get("parameters") or {}).get(key):
                    params[key] = str(sc["parameters"][key])
    # VM disks on Harvester migrate; the image's class must make them so.
    params["migratable"] = "true"
    return params


def download(kget, ksend, ns, url, klass=""):
    """An image downloading (or downloaded) from url, in ns. One already made
    from the same address is used again rather than fetched twice."""
    for item in (kget(_images(ns)).get("items") or []):
        spec = item.get("spec") or {}
        failed = any(c.get("type") == "Imported" and c.get("status") == "False" and c.get("reason") == "ImportFailed"
                     for c in (item.get("status") or {}).get("conditions") or [])
        if spec.get("url") == url and not failed and not (item.get("metadata") or {}).get("deletionTimestamp"):
            return {"name": item["metadata"]["name"], "namespace": ns, "display": spec.get("displayName", ""),
                    "storage_class": storage_class(item), "reused": True}
    name = "image-" + secrets.token_hex(3)
    body = {"apiVersion": "harvesterhci.io/v1beta1", "kind": "VirtualMachineImage",
            "metadata": {"name": name, "namespace": ns, "labels": {NAMES.key("managed"): "true"}},
            "spec": {"displayName": display_name(url), "sourceType": "download", "url": url, "retry": 3,
                     "storageClassParameters": _parameters(kget, klass)}}
    ksend("POST", _images(ns), body)
    return {"name": name, "namespace": ns, "display": body["spec"]["displayName"],
            "storage_class": "longhorn-" + name, "reused": False}
