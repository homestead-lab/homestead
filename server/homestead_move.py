"""Moving a workload from one Homestead cluster to another.

Volume data travels through the shared Longhorn backup target, which is what
makes it portable. The definition - image, ports, environment, which claim
mounts where - travels directly between the two Homesteads instead, because a
JSON document does not need an S3 client and its signing to cross a LAN.

The destination pulls. It is the cluster that has to create things, knows its
own storage classes and addresses, and is where someone sits watching the move
land; a push would mean the source holding credentials for the destination.

This module is that first conversation: what has the other cluster got, and
what of it can actually be moved.
"""
import base64
import json
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

import homestead_names as NAMES

kget = ksend = None
NS = "lab"
VERSION = ""
TIMEOUT = 20
# What the two halves of a move say to each other. It goes up only when one
# side would misread the other, so two different releases can still move
# workloads between them while this number matches.
PROTOCOL = 1
# The first release that could send a workload, for Homesteads too old to say
# what protocol they speak.
FIRST_SENDER = (2, 8, 58)
# Definitions Homestead did not write cannot be rebuilt from its own model, so
# a workload carrying them is reported as such rather than moved in part.
UNMODELLED = ("configMap", "secret", "csi", "nfs", "iscsi", "hostPath")
_tokens = {}


def bind(_kget, _ksend, namespace, version=""):
    global kget, ksend, NS, VERSION
    kget, ksend, NS, VERSION = _kget, _ksend, namespace, version
    NAMES.bind(_kget)


# ------------------------------------------------------------------ this side
def _claim(namespace, name):
    try:
        return kget(f"/api/v1/namespaces/{namespace}/persistentvolumeclaims/{name}")
    except Exception:
        return None


def _size_gb(claim):
    request = (((claim or {}).get("spec", {}) or {}).get("resources", {}) or {}
               ).get("requests", {}).get("storage", "")
    digits = "".join(ch for ch in str(request) if ch.isdigit())
    return int(digits) if digits else 0


def _workload_row(deployment):
    """One workload, described the way the far cluster would have to rebuild it."""
    meta = deployment.get("metadata", {}) or {}
    spec = (deployment.get("spec", {}) or {}).get("template", {}).get("spec", {}) or {}
    namespace, name = meta.get("namespace", ""), meta.get("name", "")
    containers = spec.get("containers", []) or []
    by_volume = {volume.get("name"): volume for volume in spec.get("volumes", []) or []}

    volumes, blockers, warnings = [], [], []
    for container in containers:
        for mount in container.get("volumeMounts", []) or []:
            source = by_volume.get(mount.get("name")) or {}
            claim_name = (source.get("persistentVolumeClaim") or {}).get("claimName")
            if claim_name:
                claim = _claim(namespace, claim_name)
                volumes.append({
                    "claim": claim_name, "path": mount.get("mountPath", ""),
                    "sub_path": mount.get("subPath", ""),
                    "read_only": bool(mount.get("readOnly")),
                    "size_gb": _size_gb(claim),
                    "storage_class": ((claim or {}).get("spec", {}) or {}).get(
                        "storageClassName", ""),
                    "access_modes": ((claim or {}).get("spec", {}) or {}).get(
                        "accessModes", []),
                })
                continue
            if "emptyDir" in source:
                continue            # rebuilt empty on the far side, as intended
            host = (source.get("hostPath") or {}).get("path", "")
            if host == "/dev" or host.startswith("/dev/"):
                # A device - an iGPU, a Coral, a Zigbee stick - is what hardware
                # features pass through. It travels as it is; the destination
                # just needs a host that has the same device.
                warnings.append(f"{mount.get('mountPath', '?')} passes the host's {host} through; "
                                "the destination needs a host with the same device")
                continue
            carried = next((kind for kind in UNMODELLED if kind in source), "")
            if carried:
                blockers.append(f"{mount.get('mountPath', '?')} comes from a {carried} "
                                "Homestead did not create")

    return {
        "name": name, "namespace": namespace, "kind": "container",
        "image": containers[0].get("image", "") if containers else "",
        "replicas": int((deployment.get("spec", {}) or {}).get("replicas", 0) or 0),
        "running": int((deployment.get("status", {}) or {}).get("readyReplicas", 0) or 0) > 0,
        "containers": [c.get("name", "") for c in containers],
        "volumes": volumes,
        "ports": [{"container": p.get("containerPort"), "protocol": p.get("protocol", "TCP")}
                  for c in containers for p in (c.get("ports") or [])],
        "hardware": [x for x in NAMES.read(meta.get("annotations", {}) or {},
                                           "hardware").split(",") if x],
        "movable": not blockers,
        "blockers": blockers,
        "warnings": sorted(set(warnings)),
    }


def _vm_row(vm, running_names):
    """A VM, described the way the far cluster would have to rebuild it.

    Its disks are Longhorn claims and travel like any other. Its cloud-init
    Secrets are part of it and travel too. What cannot come is anything that
    names this cluster's hardware or disks directly.
    """
    meta = vm.get("metadata", {}) or {}
    namespace, name = meta.get("namespace", ""), meta.get("name", "")
    template = (((vm.get("spec", {}) or {}).get("template", {}) or {}).get("spec", {}) or {})
    domain = template.get("domain", {}) or {}
    volumes, blockers, warnings = [], [], []
    for volume in template.get("volumes", []) or []:
        claim_name = ((volume.get("persistentVolumeClaim") or {}).get("claimName")
                      or (volume.get("dataVolume") or {}).get("name"))
        if claim_name:
            claim = _claim(namespace, claim_name)
            volumes.append({
                "claim": claim_name, "path": volume.get("name", ""), "sub_path": "",
                "read_only": False, "size_gb": _size_gb(claim),
                "storage_class": ((claim or {}).get("spec", {}) or {}).get("storageClassName", ""),
                "access_modes": ((claim or {}).get("spec", {}) or {}).get("accessModes", []),
            })
            continue
        if "hostDisk" in volume:
            blockers.append(f"disk {volume.get('name', '?')} is a file on one of this cluster's hosts")
        elif "configMap" in volume or "secret" in volume:
            blockers.append(f"disk {volume.get('name', '?')} comes from a "
                            f"{'configMap' if 'configMap' in volume else 'secret'} "
                            "Homestead did not create")
    devices = domain.get("devices", {}) or {}
    if devices.get("hostDevices") or devices.get("gpus"):
        warnings.append("passes host devices through; the destination needs the same hardware")
    networks = [(n.get("multus") or {}).get("networkName") for n in template.get("networks", []) or []]
    if any(networks):
        warnings.append("uses the " + ", ".join(n for n in networks if n) +
                        " network, which must exist on the destination")
    return {
        "name": name, "namespace": namespace, "kind": "vm",
        "image": "", "replicas": 1, "running": name in running_names,
        "containers": [], "volumes": volumes, "ports": [], "hardware": [],
        "cores": (domain.get("cpu", {}) or {}).get("cores", 0),
        "memory": (domain.get("memory", {}) or {}).get("guest", ""),
        "movable": not blockers, "blockers": blockers, "warnings": warnings,
    }


def inventory():
    """What this cluster has that another one could take."""
    try:
        deployments = kget("/apis/apps/v1/deployments").get("items", [])
    except Exception as error:
        raise ValueError(f"could not read workloads: {str(error)[:120]}") from error
    rows = [_workload_row(d) for d in deployments
            if (d["metadata"].get("namespace") or "") == NS]
    rows.sort(key=lambda row: row["name"])
    try:
        machines = [vm for vm in kget("/apis/kubevirt.io/v1/virtualmachines").get("items", [])
                    if (vm["metadata"].get("namespace") or "") == NS]
        running = {vmi["metadata"]["name"] for vmi in
                   kget("/apis/kubevirt.io/v1/virtualmachineinstances").get("items", [])
                   if (vmi["metadata"].get("namespace") or "") == NS}
    except Exception:
        machines, running = [], set()     # no KubeVirt: a cluster with no VMs
    vms = sorted((_vm_row(vm, running) for vm in machines), key=lambda row: row["name"])
    return {"namespace": NS, "workloads": rows, "vms": vms, "version": VERSION,
            "protocol": PROTOCOL,
            "movable": sum(1 for row in rows + vms if row["movable"])}


def hello():
    """Which Homestead this is, as another one asks before moving anything."""
    return {"version": VERSION, "protocol": PROTOCOL, "namespace": NS}


# --------------------------------------------------------------- the far side
def _secret_name(name):
    return f"homestead-cluster-{name}"


def list_clusters():
    """Other Homesteads this one knows about. Never includes their passwords."""
    try:
        found = kget(f"/api/v1/namespaces/{NS}/configmaps/homestead-clusters")
        rows = json.loads((found.get("data") or {}).get("clusters.json", "[]"))
        return rows if isinstance(rows, list) else []
    except Exception:
        return []


def _save_clusters(rows):
    body = {"apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": "homestead-clusters", "namespace": NS,
                         "labels": {NAMES.key("managed"): "true"}},
            "data": {"clusters.json": json.dumps(rows, indent=2)}}
    try:
        kget(f"/api/v1/namespaces/{NS}/configmaps/homestead-clusters")
        ksend("PUT", f"/api/v1/namespaces/{NS}/configmaps/homestead-clusters", body)
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        ksend("POST", f"/api/v1/namespaces/{NS}/configmaps", body)
    return rows


def add_cluster(name, url, user, password):
    name = str(name or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?", name):
        raise ValueError("name must be lowercase letters, numbers and dashes")
    url = str(url or "").strip().rstrip("/")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("address must be a http:// or https:// URL")
    rows = [row for row in list_clusters() if row["name"] != name]
    rows.append({"name": name, "url": url, "user": user,
                 "added": time.strftime("%Y-%m-%d %H:%M")})
    _save_clusters(rows)
    secret = {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
              "metadata": {"name": _secret_name(name), "namespace": NS},
              "stringData": {"password": password or ""}}
    try:
        kget(f"/api/v1/namespaces/{NS}/secrets/{_secret_name(name)}")
        ksend("PUT", f"/api/v1/namespaces/{NS}/secrets/{_secret_name(name)}", secret)
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        ksend("POST", f"/api/v1/namespaces/{NS}/secrets", secret)
    _tokens.pop(name, None)
    return rows


def remove_cluster(name):
    rows = [row for row in list_clusters() if row["name"] != name]
    _save_clusters(rows)
    _tokens.pop(name, None)
    try:
        ksend("DELETE", f"/api/v1/namespaces/{NS}/secrets/{_secret_name(name)}")
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
    return rows


def _cluster(name):
    row = next((x for x in list_clusters() if x["name"] == name), None)
    if not row:
        raise ValueError(f"no cluster named {name}")
    return row


def _password(name):
    try:
        secret = kget(f"/api/v1/namespaces/{NS}/secrets/{_secret_name(name)}")
    except Exception:
        return ""
    raw = (secret.get("data") or {}).get("password", "")
    try:
        return base64.b64decode(raw).decode()
    except Exception:
        return ""


def _open(request):
    # Plain HTTP is the normal case on a LAN VIP. HTTPS is verified in the
    # ordinary way: a move is not a reason to stop checking a certificate.
    context = ssl.create_default_context() if request.full_url.startswith("https") else None
    return urllib.request.urlopen(request, timeout=TIMEOUT, context=context)


def _call(row, path, token="", body=None):
    request = urllib.request.Request(
        row["url"] + path,
        data=json.dumps(body).encode() if body is not None else None,
        method="POST" if body is not None else "GET")
    request.add_header("Accept", "application/json")
    if body is not None:
        request.add_header("Content-Type", "application/json")
        request.add_header("X-Homestead-Auth", "1")
    if token:
        request.add_header("Cookie", f"homestead_session={token}")
    with _open(request) as response:
        return json.loads(response.read().decode() or "{}"), response


class Missing(ValueError):
    """The other Homestead does not have that route: it is an older release."""


class Unreachable(Exception):
    """The other cluster could not be asked - worth trying again shortly.

    Kept apart from ValueError, which means it was asked and said no. A move
    waits out the first and stops on the second.
    """


def _token(name, force=False):
    """A session on the far Homestead, kept only in memory."""
    if not force and name in _tokens:
        return _tokens[name]
    row = _cluster(name)
    try:
        _, response = _call(row, "/api/auth/login", body={
            "username": row["user"], "password": _password(name), "remember": False})
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            raise ValueError(f"{name} refused those Homestead credentials") from error
        if error.code >= 500:
            raise Unreachable(f"{name} returned HTTP {error.code}") from error
        raise ValueError(f"{name} returned HTTP {error.code}") from error
    except Exception as error:
        raise Unreachable(f"could not reach {name}: {str(error)[:120]}") from error
    cookie = response.headers.get("Set-Cookie", "")
    token = cookie.split("homestead_session=", 1)[-1].split(";", 1)[0] if cookie else ""
    if not token:
        raise ValueError(f"{name} signed in but sent no session")
    _tokens[name] = token
    return token


def _reason(error):
    try:
        body = json.loads(error.read().decode("utf-8", "replace") or "{}")
        return str(body.get("error") or "")[:240]
    except Exception:
        return ""


def remote(name, path, body=None):
    """Call another Homestead as the stored account, signing in again if needed.

    A 401 is a lapsed session and gets one fresh sign-in. A 403 is the account
    lacking the role - a move needs admin there - and is said plainly.
    """
    row = _cluster(name)
    for attempt in (0, 1):
        try:
            payload, _ = _call(row, path, token=_token(name, force=bool(attempt)), body=body)
            return payload
        except urllib.error.HTTPError as error:
            reason = _reason(error)
            if error.code == 401 and not attempt:
                continue
            if error.code == 403:
                raise ValueError(f"{name}: {reason or 'the account there lacks the access this needs'}"
                                 ) from error
            if error.code >= 500:
                raise Unreachable(f"{name}: {reason or f'HTTP {error.code}'}") from error
            if error.code == 404:
                raise Missing(f"{name}: {reason or 'HTTP 404'}") from error
            raise ValueError(f"{name}: {reason or f'HTTP {error.code}'}") from error
        except (ValueError, Unreachable):
            raise
        except Exception as error:
            raise Unreachable(f"could not reach {name}: {str(error)[:120]}") from error
    raise ValueError(f"{name} would not accept a fresh sign-in")


def remote_inventory(name):
    """Ask another cluster what it has. The first half of every move."""
    try:
        payload = remote(name, "/api/move/inventory")
    except Unreachable as error:
        raise ValueError(str(error)) from error
    row = _cluster(name)
    payload["cluster"] = name
    payload["url"] = row["url"]
    return payload


def _release(version):
    try:
        return tuple(int(part) for part in str(version).lstrip("v").split(".")[:3])
    except ValueError:
        return ()


def check_cluster(name):
    """Whether this Homestead and another can move workloads between them.

    Moves go both ways, so a protocol mismatch in either direction stops them
    and says which side to update. Different releases on the same protocol
    are fine, and reported only so the difference is not a surprise.
    """
    base = {"name": name, "local_version": VERSION, "local_protocol": PROTOCOL,
            "version": "", "protocol": None}
    try:
        there = remote(name, "/api/move/hello")
    except Missing:
        # Older than the handshake: ask the settings page for the release.
        try:
            info = (remote(name, "/api/settings").get("info") or {})
        except Unreachable as error:
            return {**base, "state": "unreachable", "message": str(error)}
        except ValueError as error:
            return {**base, "state": "refused", "message": str(error)}
        version = str(info.get("version") or "")
        release = _release(version)
        there = {"version": version,
                 "protocol": 1 if release and release >= FIRST_SENDER else 0}
    except Unreachable as error:
        return {**base, "state": "unreachable", "message": str(error)}
    except ValueError as error:
        return {**base, "state": "refused", "message": str(error)}

    version, protocol = str(there.get("version") or ""), int(there.get("protocol") or 0)
    result = {**base, "version": version, "protocol": protocol}
    shown = version or "an unknown release"
    if protocol < PROTOCOL:
        return {**result, "state": "behind", "compatible": False,
                "message": f"{name} runs Homestead {shown}, too old to move workloads "
                           f"with this one ({VERSION}). Update {name} first."}
    if protocol > PROTOCOL:
        return {**result, "state": "ahead", "compatible": False,
                "message": f"{name} runs Homestead {shown}, newer than this one ({VERSION}) "
                           f"in how it moves workloads. Update this Homestead first."}
    if version == VERSION:
        return {**result, "state": "same", "compatible": True,
                "message": f"Both run Homestead {VERSION}."}
    newer = name if _release(version) > _release(VERSION) else "this Homestead"
    return {**result, "state": "differs", "compatible": True,
            "message": f"{name} runs {shown} and this one {VERSION}. Moves work between "
                       f"them; {newer} is the newer of the two."}


# ------------------------------------------------------------ getting ready
def readiness(name):
    """What a move from this cluster still needs, for its card: that the two
    Homesteads can talk, and that the far side has backup storage the
    volumes can travel through, at an address this cluster can reach."""
    out = {"version": check_cluster(name), "storage": {}, "target": {}}
    try:
        store = remote(name, "/api/objectstore")
        out["storage"] = {k: store.get(k) for k in ("deployed", "ready", "endpoint", "reachable_off_cluster")}
    except Exception as error:
        out["storage"] = {"error": str(error)[:200]}
    try:
        target = remote(name, "/api/move/target")
        # The keys stay on the server; the card only needs to know it is there.
        out["target"] = {"configured": True, "url": target.get("url", ""),
                         "reachable_off_cluster": bool(target.get("reachable_off_cluster"))}
    except Exception as error:
        out["target"] = {"configured": False, "error": str(error)[:200]}
    release = _release(out["version"].get("version") or "")
    out["update_first"] = bool(release and release < RUSTFS_SINCE and not out["target"].get("configured"))
    out["ready"] = bool(out["target"].get("configured") and out["target"].get("reachable_off_cluster")
                        and out["version"].get("compatible") is not False)
    return out


# Releases before this deploy MinIO, whose images can no longer be pulled.
RUSTFS_SINCE = (2, 8, 111)


def _needs_update(name):
    """The far Homestead's release, when it is too old to set up storage that
    can start; empty when it is fine."""
    version = str((check_cluster(name) or {}).get("version") or "")
    release = _release(version)
    return version if release and release < RUSTFS_SINCE else ""


def setup_storage(name, size_gb=100, lb_ip=""):
    """Put backup storage on the far cluster, as its own Data protection page
    would: MinIO on a Longhorn volume, with Longhorn's backups pointed at it.
    Done as the stored account, which a move already needs to be admin."""
    old = _needs_update(name)
    if old:
        raise ValueError(f"{name} runs Homestead {old}, which sets up backup storage with MinIO - and MinIO's "
                         "images can no longer be downloaded. Update it to 2.8.111 or later first, then set it up: "
                         "kubectl -n lab set image deployment/homestead homestead=ghcr.io/wjcloudy/homestead:2.8.111")
    size_gb = int(size_gb or 100)
    result = remote(name, "/api/objectstore/deploy",
                    {"size_gb": size_gb, "lb_ip": str(lb_ip or "").strip(), "point_longhorn": True})
    where = result.get("endpoint") or ""
    # Releases before 2.8.110 gave Longhorn the keys but never its target, so
    # a move still found "no backup target". It is set here too, through a
    # route every release has - unless the target already points elsewhere.
    try:
        remote(name, "/api/move/target")
    except ValueError:
        store = remote(name, "/api/objectstore")
        secret = (result.get("longhorn") or {}).get("secret") or "homestead-backup-credentials"
        remote(name, "/api/lh/target", {"url": store.get("backup_url") or "s3://homestead-backups@us-east-1/",
                                        "secret": secret, "poll": "5m"})
    return {"ok": True, "endpoint": where,
            "detail": f"backup storage is starting on {name}" + (f" at {where}" if where else "")
                      + "; its first start can take a minute while the image downloads"}
