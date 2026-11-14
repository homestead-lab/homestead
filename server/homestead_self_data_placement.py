"""Whether Homestead's own data is placed so that it comes back after any
restart (docs/plans/resilience-and-validated-updates.md, section 3.1).

On 2026-11-10 Homestead's data was a shared (ReadWriteMany) Longhorn volume
with one copy, on a second disk that had been under Longhorn's DiskPressure
for a day. An undrained restart left the volume down, and Homestead with it.
Two things about that placement made it fragile, and this check names both on
an existing install:

- shared: a ReadWriteMany volume is served by a share-manager pod, so
  Homestead depends on one more thing to start. Only several copies of
  Homestead need a shared volume. With one copy, a volume one host mounts at
  a time (ReadWriteOnce) does without it.
- off the system disk: the only copy is on a disk other than the one the
  host runs from (tagged "os"). A copy on the system disk shares its fate
  with the host, not with a second disk.

A copy on a disk that takes no new copies is homestead_lhcapacity's alert
(salvage-blocked), not this one. Nothing here moves anything: each finding
names the reviewed move that fixes it, and Homestead never moves its own data
without a person's review.
"""
import urllib.error

import homestead_data_copies as DATA_COPIES
import homestead_node_parity as NODE_PARITY

LH = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system"


def _whole(replica):
    spec, status = replica.get("spec") or {}, replica.get("status") or {}
    return not spec.get("failedAt") and (status.get("currentState") == "running" or bool(spec.get("healthyAt")))


def _disk_tags(lhnodes):
    """{(node, disk uuid): tags}, and the nodes with a disk tagged "os"."""
    tags, system = {}, set()
    for node in lhnodes or []:
        name, spec = (node.get("metadata") or {}).get("name", ""), node.get("spec") or {}
        for disk, state in ((node.get("status") or {}).get("diskStatus") or {}).items():
            found = list(((spec.get("disks") or {}).get(disk) or {}).get("tags") or [])
            tags[(name, state.get("diskUUID") or "")] = found
            if "os" in found:
                system.add(name)
    return tags, system


def findings(volume, modes, own_copies, replicas, lhnodes):
    """What makes Homestead's data fragile, as [{"kind", ...}]: "shared" and
    "off-system-disk" (see the module's docstring)."""
    out = []
    if "ReadWriteMany" in (modes or []) and own_copies <= 1:
        out.append({"kind": "shared"})
    whole = [r.get("spec") or {} for r in replicas or []
             if (r.get("spec") or {}).get("volumeName") == (volume.get("metadata") or {}).get("name") and _whole(r)]
    if len(whole) == 1:
        tags, system = _disk_tags(lhnodes)
        copy = whole[0]
        found = tags.get((copy.get("nodeID") or "", copy.get("diskID") or ""))
        # Unknown tags, or no disk tagged "os" on its host: nothing to offer.
        if found is not None and "os" not in found and copy.get("nodeID") in system:
            out.append({"kind": "off-system-disk", "node": copy.get("nodeID"), "path": copy.get("diskPath") or ""})
    return out


def status():
    """findings() for the cluster as it is now, with what the fixes need:
    {"claim", "storage_class", "findings", "hosts"}. {"findings": []} when
    Homestead's data is not on Longhorn. Raises when something cannot be
    read, so an alert already raised is held."""
    kget = NODE_PARITY.kget
    ns, name = NODE_PARITY.OWN
    try:
        own = next((v for v in DATA_COPIES.own_data_volumes() if v["volume"]), None)
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        own = None       # no Deployment of Homestead's own (run outside the cluster): nothing to check
    if not own:
        return {"findings": []}
    pvc = kget(f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{own['claim']}") or {}
    deployment = kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}") or {}
    replicas = (kget(f"{LH}/replicas?labelSelector=longhornvolume%3D{own['pv']}") or {}).get("items")
    lhnodes = (kget(f"{LH}/nodes") or {}).get("items")
    if not isinstance(replicas, list) or not isinstance(lhnodes, list):
        raise ValueError("Longhorn's copies and disks could not be read")
    found = findings(own["volume"], (pvc.get("spec") or {}).get("accessModes") or [],
                     int((deployment.get("spec") or {}).get("replicas") or 1), replicas, lhnodes)
    return {"claim": own["claim"], "storage_class": own["storage_class"], "findings": found, "hosts": len(lhnodes)}


def alert_facts(state):
    """One alert per finding, with the reviewed move that fixes it."""
    facts = []
    for row in (state or {}).get("findings") or []:
        if row["kind"] == "shared":
            title = "Homestead's data is a shared volume, which only several copies of Homestead need"
            body = (f"{state.get('claim')} is ReadWriteMany, so it is served by a share-manager pod, and Homestead "
                    "runs one copy. After an unclean restart that pod can leave the volume down, and Homestead with it. "
                    "Move the data to a volume that one host mounts at a time: in Settings, About, Redundancy, "
                    "choose Use a volume one host mounts. Homestead stops for a short time while its data is copied.")
        else:
            title = "Homestead's data is not on the system disk"
            body = (f"Its only copy is on {row.get('path') or 'a second disk'} on {row.get('node')}, so a problem with that "
                    "disk stops Homestead even while the host is fine. Move the copy to the disk tagged os with Move copies "
                    "on Volumes. Homestead keeps running while it moves.")
        facts.append({"signals": {"placement": 1}, "key": f"self-data-placement:{row['kind']}", "category": "degraded",
                      "severity": "info", "title": title,
                      "resolved": "Homestead's data is placed to come back after a restart",
                      "body": body, "href": "/settings?tab=about" if row["kind"] == "shared" else "/volumes"})
    return facts
