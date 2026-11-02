"""The volume claims Homestead makes: which ones a deploy creates, and each
claim's manifest. Deploy creates them (create_pvc in server.py sends the
manifest) and its review shows the same manifests as YAML for git (#920).

The managed label is how Homestead recognises a volume it made; it stays in
the YAML, so a claim git applies is still one of Homestead's volumes.
"""
import homestead_names as NAMES
from homestead_names_helpers import _dns_name

# The class a claim goes on when none is chosen: server.py's current default,
# looked up each time (it can change when the cluster's default does).
default_class = lambda: "longhorn-r2"


def bind(storage_class):
    global default_class
    default_class = storage_class


def new_claims(volumes):
    """The volumes a deploy creates: one per name, however many folders of it
    are mounted, sized for the largest any of them asks."""
    claims = {}
    for v in volumes:
        if v.get("type") != "pvc" or not v.get("create"):
            continue
        name = _dns_name(v.get("source"), "volume name")
        size = max(1, int(v.get("size_gb") or 5))
        if name in claims:
            claims[name]["size_gb"] = max(claims[name]["size_gb"], size)
            continue
        claims[name] = {"name": name, "size_gb": size,
                        "storage_class": v.get("storage_class") or default_class(),
                        "access_mode": v.get("access_mode") or "ReadWriteOnce"}
    return list(claims.values())


def manifest(ns, name, size_gb, storage_class, access_mode="ReadWriteOnce"):
    """The PersistentVolumeClaim Homestead creates for a volume."""
    return {"apiVersion": "v1", "kind": "PersistentVolumeClaim",
            "metadata": {"name": name, "namespace": ns, "labels": {NAMES.key("managed"): "true"}},
            "spec": {"accessModes": [access_mode], "storageClassName": storage_class,
                     "resources": {"requests": {"storage": f"{size_gb}Gi"}}}}
