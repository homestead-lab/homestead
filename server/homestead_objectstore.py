"""Object storage for backups, because Longhorn and Velero both need some.

Longhorn writes volume backups to an S3 bucket, and that bucket is what makes a
backup portable: a volume restored on another cluster is read from there, not
from the cluster it came from. Neither Harvester nor Longhorn provides one, so
without somewhere to write, none of the backup machinery Homestead already has
can be used at all.

This stands up an S3 server on a Longhorn volume - RustFS, a drop-in for
MinIO, whose images are no longer published for anyone to pull - creates the
bucket, and points Longhorn at it. That is
honest about what it is: storage inside the cluster it protects, which is what
makes it useful for moving workloads to another cluster and useless as the only
copy of anything. It is exposed on its own LAN address precisely so the other
cluster can read it.
"""
import base64
import datetime
import hashlib
import hmac
import re
import secrets
import urllib.error
import urllib.parse
import urllib.request

import homestead_names as NAMES

kget = ksend = create_pvc = None
NS = "lab"
LHNS = "longhorn-system"
NAME = "homestead-objectstore"
SECRET = "homestead-objectstore-keys"
LONGHORN_SECRET = "homestead-backup-credentials"
BUCKET = "homestead-backups"
REGION = "us-east-1"
PORT = 9000
CONSOLE_PORT = 9001
IMAGE = "rustfs/rustfs:1.0.0"
RUSTFS = "rustfs/rustfs:"
# MinIO's images stopped being published; one already running with backups in
# it is left as it is rather than swapped for a server that may not read its
# files. Anything else - a fresh store, or a MinIO that cannot start - gets
# RustFS.
MINIO = "minio/minio"
UID = 10001                      # RustFS runs as this user, and needs its volume
_bucket = {"ok": False}
VIP_ANNOTATION = "kube-vip.io/loadbalancerIPs"
import homestead_platform as PLATFORM
import homestead_networking as NETWORK
import homestead_longhorn as LH


def bind(_kget, _ksend, _create_pvc, namespace):
    global kget, ksend, create_pvc, NS
    kget, ksend, create_pvc, NS = _kget, _ksend, _create_pvc, namespace
    NAMES.bind(_kget)


def _get(path):
    try:
        return kget(path)
    except Exception:
        return None


def _decode(secret, key):
    raw = (secret.get("data") or {}).get(key, "")
    try:
        return base64.b64decode(raw).decode()
    except Exception:
        return ""


def _port(service=None):
    """The port the store answers on: what its Service says, 9000 unless an
    admin chose another because 9000 was taken on the shared address."""
    if service is None:
        service = _get(f"/api/v1/namespaces/{NS}/services/{NAME}")
    for row in ((service or {}).get("spec") or {}).get("ports") or []:
        if row.get("name") == "s3" and row.get("port"):
            return int(row["port"])
    return PORT


def _console_port(port):
    return CONSOLE_PORT if port == PORT else port + 1


def endpoint(service=None):
    """Where the bucket answers, as another cluster reaches it. An address
    chosen for the store itself wins; else its copy on Homestead's VIP
    (homestead_self_address), made where the store sat on the nodes' own
    addresses. The copy keeps the port it was made with, so preferring it
    over a chosen address would send backups to an old address and port."""
    if service is None:
        main = _get(f"/api/v1/namespaces/{NS}/services/{NAME}")
        vip = _get(f"/api/v1/namespaces/{NS}/services/{NAME}-vip")
        service = main if (main and _current_address(main)) or not vip else vip
    if not service:
        return ""
    ingress = ((service.get("status", {}) or {}).get("loadBalancer", {}) or {}).get("ingress", [])
    address = next((row.get("ip") or row.get("hostname") for row in ingress
                    if row.get("ip") or row.get("hostname")), "")
    address = address or (service.get("metadata", {}).get("annotations", {})
                          or {}).get(VIP_ANNOTATION, "")
    port = _port(service)
    if address:
        return f"http://{address}:{port}"
    # In-cluster only: usable by Longhorn here, not by another cluster.
    return f"http://{NAME}.{NS}.svc:{port}"


def status():
    """What exists, whether it is serving, and whether Longhorn is pointed at it."""
    deployment = _get(f"/apis/apps/v1/namespaces/{NS}/deployments/{NAME}")
    service = _get(f"/api/v1/namespaces/{NS}/services/{NAME}")
    claim = _get(f"/api/v1/namespaces/{NS}/persistentvolumeclaims/{NAME}")
    ready = int(((deployment or {}).get("status", {}) or {}).get("readyReplicas", 0) or 0)
    where = endpoint(service)
    bucket = False
    if deployment and ready:
        try:
            bucket = ensure_bucket()
        except Exception:
            bucket = False
    containers = (((deployment or {}).get("spec") or {}).get("template") or {}).get("spec", {}).get("containers") or [{}]
    wanted = int(((deployment or {}).get("spec") or {}).get("replicas", 1) or 0) if deployment else 0
    return {
        "bucket_ready": bucket,
        # Turned off: set up, but stopped, so moves out of this cluster wait.
        "stopped": bool(deployment) and wanted == 0,
        "server": containers[0].get("image", "") if deployment else "",
        "deployed": bool(deployment),
        "ready": bool(deployment) and ready > 0,
        "endpoint": where,
        "port": _port(service) if service else PORT,
        "reachable_off_cluster": bool(where) and ".svc:" not in where,
        "bucket": BUCKET,
        "size_gb": _claim_size(claim),
        "backup_url": backup_url(),
        "image": IMAGE,
    }


def _rustfs_version(image):
    """(1, 0, 0, rc) of a RustFS image tag - a release sorts after its
    release candidates - or None for anything else (MinIO, a digest, latest)."""
    if not str(image or "").startswith(RUSTFS):
        return None
    tag = image[len(RUSTFS):].lstrip("v")
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:-rc\.(\d+))?(?:-glibc)?", tag)
    if not match:
        return None
    major, minor, patch, rc = match.groups()
    return (int(major), int(minor), int(patch), int(rc) if rc else 10 ** 6)


def keep_in_step():
    """The store runs the RustFS Homestead pins, as its SMB and NFS servers
    do: an older RustFS moves on - only the image changes; its keys, volume
    and address stay - and nothing else is touched: MinIO, a newer or
    unrecognised tag, or a store that is off. Returns the image moved to, or ""."""
    current = _get(f"/apis/apps/v1/namespaces/{NS}/deployments/{NAME}")
    if not current or not int((current.get("spec") or {}).get("replicas", 1) or 0):
        return ""
    containers = (((current.get("spec") or {}).get("template") or {}).get("spec") or {}).get("containers") or []
    if len(containers) != 1:
        return ""
    running, wanted = _rustfs_version(containers[0].get("image")), _rustfs_version(IMAGE)
    if not running or running >= wanted:
        return ""
    ksend("PATCH", f"/apis/apps/v1/namespaces/{NS}/deployments/{NAME}",
          {"spec": {"template": {"spec": {"containers": [{"name": containers[0]["name"], "image": IMAGE}]}}}},
          ctype="application/strategic-merge-patch+json")
    return IMAGE


def _claim_size(claim):
    if not claim:
        return 0
    request = (((claim.get("spec", {}) or {}).get("resources", {}) or {})
               .get("requests", {}) or {}).get("storage", "")
    digits = "".join(ch for ch in str(request) if ch.isdigit())
    return int(digits) if digits else 0


def backup_url():
    """The Longhorn backup target this bucket corresponds to."""
    return f"s3://{BUCKET}@{REGION}/"


def _sign(method, url, access, secret, body=b""):
    """AWS Signature Version 4 headers for one S3 request: what every
    S3-compatible server checks, so the bucket can be made on any of them."""
    parsed = urllib.parse.urlsplit(url)
    now = datetime.datetime.now(datetime.timezone.utc)
    stamp, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    payload = hashlib.sha256(body).hexdigest()
    headers = {"host": parsed.netloc, "x-amz-content-sha256": payload, "x-amz-date": stamp}
    signed = ";".join(sorted(headers))
    canonical = "\n".join([method, urllib.parse.quote(parsed.path or "/"), parsed.query,
                           "".join(f"{k}:{headers[k]}\n" for k in sorted(headers)), signed, payload])
    scope = f"{day}/{REGION}/s3/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", stamp, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    key = ("AWS4" + secret).encode()
    for part in (day, REGION, "s3", "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    return {"Host": parsed.netloc, "x-amz-content-sha256": payload, "x-amz-date": stamp,
            "Authorization": f"AWS4-HMAC-SHA256 Credential={access}/{scope}, SignedHeaders={signed}, Signature={signature}"}


def _s3(method, url, access, secret):
    request = urllib.request.Request(url, method=method, data=b"" if method == "PUT" else None,
                                     headers=_sign(method, url, access, secret))
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code


def ensure_bucket(base=None):
    """Make the bucket Longhorn writes to, if it is not there. Longhorn does
    not make it, and neither does the server; a target without one only
    ever reports itself unavailable."""
    if _bucket["ok"]:
        return True
    keys = credentials()
    url = f"{base or f'http://{NAME}.{NS}.svc:{_port()}'}/{BUCKET}"
    if _s3("HEAD", url, keys["access_key"], keys["secret_key"]) == 200:
        _bucket["ok"] = True
        return True
    code = _s3("PUT", url, keys["access_key"], keys["secret_key"])
    # 409 is someone having made it first, which is as good.
    _bucket["ok"] = code in (200, 409)
    return _bucket["ok"]


def credentials():
    """The keys, generated once and kept in a Secret."""
    existing = _get(f"/api/v1/namespaces/{NS}/secrets/{SECRET}")
    if existing:
        access = _decode(existing, "accesskey")
        secret = _decode(existing, "secretkey")
        if access and secret:
            return {"access_key": access, "secret_key": secret}
    return {"access_key": "homestead", "secret_key": secrets.token_urlsafe(36)}


def _secret_body(name, namespace, data):
    return {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
            "metadata": {"name": name, "namespace": namespace,
                         "labels": {NAMES.key("managed"): "true"}},
            "data": {key: base64.b64encode(value.encode()).decode()
                     for key, value in data.items()}}


def _apply(path, name, body):
    try:
        current = kget(f"{path}/{name}")
        body["metadata"]["resourceVersion"] = current["metadata"]["resourceVersion"]
        return ksend("PUT", f"{path}/{name}", body)
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        return ksend("POST", path, body)


VIP_KEYS = ("kube-vip.io/loadbalancerIPs", "metallb.universe.tf/loadBalancerIPs")


def _shared_plan(port):
    return NETWORK.service_plan({
        "namespace": NS, "name": f"{NAME}-plan", "workload": NAME, "type": "LoadBalancer",
        "vip_mode": "shared",
        "ports": [{"port": port, "target_port": "s3", "name": "s3"},
                  {"port": _console_port(port), "target_port": "console", "name": "console"}]},
        require_workload=False)


def shared_address(port=PORT):
    """Homestead's shared address for the store, checked as an app sharing it
    would be: its ports free there, and not the cluster's own address. Empty
    where Services go on the nodes' own addresses instead (k3s's ServiceLB).
    A taken port is refused with a free one named, so the admin can pick it."""
    try:
        return _shared_plan(port)["vip"]
    except (ValueError, PermissionError) as error:
        free = ""
        for candidate in (9000, 9010, 9020, 9100, 19000, 29000):
            if candidate == port:
                continue
            try:
                _shared_plan(candidate)
                free = f" Port {candidate} is free there."
                break
            except (ValueError, PermissionError):
                continue
        raise ValueError(f"The backup storage cannot share Homestead's address on port {port}: {error}. "
                         f"Choose another port for it, or give it an address of its own.{free}") from error


def _current_address(service):
    annotations = ((service or {}).get("metadata") or {}).get("annotations") or {}
    return next((annotations[key] for key in VIP_KEYS if annotations.get(key)), "")


def deploy(cfg=None):
    """Create the bucket store, and point Longhorn at it unless told not to.

    Its address: the one asked for; else, for a store already running, the
    one it has; else Homestead's shared address, beside the apps that share
    it - the store answers on its own port there.
    """
    cfg = cfg or {}
    size_gb = int(cfg.get("size_gb") or 100)
    if not 5 <= size_gb <= 16384:
        raise ValueError("object storage size must be between 5 and 16384 GiB")
    address = str(cfg.get("lb_ip") or "").strip()
    existing = _get(f"/api/v1/namespaces/{NS}/services/{NAME}")
    # The port asked for; else the one a running store has; else 9000.
    try:
        port = int(cfg.get("port") or 0) or (_port(existing) if existing else PORT)
    except (TypeError, ValueError):
        raise ValueError("the backup storage port is a number")
    if not 1 <= port <= 65534:
        raise ValueError("the backup storage port is between 1 and 65534 (the one above it is its console)")
    if address:
        NETWORK.check_address(address)
    elif existing and cfg.get("vip_mode") != "shared" and port == _port(existing):
        address = _current_address(existing)
    else:
        address = shared_address(port)
    keys = credentials()

    _apply(f"/api/v1/namespaces/{NS}/secrets", SECRET,
           _secret_body(SECRET, NS, {"accesskey": keys["access_key"],
                                     "secretkey": keys["secret_key"]}))
    if not _get(f"/api/v1/namespaces/{NS}/persistentvolumeclaims/{NAME}"):
        create_pvc(NS, NAME, size_gb, cfg.get("storage_class") or None, "ReadWriteOnce")

    labels = {"app": NAME, NAMES.key("managed"): "true"}
    current = _get(f"/apis/apps/v1/namespaces/{NS}/deployments/{NAME}")
    running_image = ((((current or {}).get("spec") or {}).get("template") or {}).get("spec", {}).get("containers") or [{}])[0].get("image", "")
    serving = int(((current or {}).get("status") or {}).get("readyReplicas", 0) or 0) > 0
    keep_minio = MINIO in running_image and serving
    _bucket["ok"] = False
    deployment = {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": NAME, "namespace": NS, "labels": labels},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": NAME}},
            # One writer to one ReadWriteOnce volume: replacing the pod has to
            # wait for the old one to let go of it.
            "strategy": {"type": "Recreate"},
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    # The server's own user owns the volume, so a fresh
                    # Longhorn volume (owned by root) can be written.
                    "securityContext": {"fsGroup": UID, "fsGroupChangePolicy": "OnRootMismatch"},
                    "containers": [{
                    "name": "minio" if keep_minio else "s3", "image": running_image if keep_minio else IMAGE,
                    **({"args": ["server", "/data", "--console-address", f":{CONSOLE_PORT}"]} if keep_minio else {}),
                    "env": [
                        {"name": "MINIO_ROOT_USER" if keep_minio else "RUSTFS_ACCESS_KEY", "valueFrom": {"secretKeyRef": {
                            "name": SECRET, "key": "accesskey"}}},
                        {"name": "MINIO_ROOT_PASSWORD" if keep_minio else "RUSTFS_SECRET_KEY", "valueFrom": {"secretKeyRef": {
                            "name": SECRET, "key": "secretkey"}}},
                    ] + ([] if keep_minio else [{"name": "RUSTFS_VOLUMES", "value": "/data"}]),
                    "ports": [{"containerPort": PORT, "name": "s3"},
                              {"containerPort": CONSOLE_PORT, "name": "console"}],
                    "readinessProbe": {"httpGet": {"path": "/minio/health/ready", "port": "s3"},
                                       "initialDelaySeconds": 5, "periodSeconds": 15},
                    "resources": {"requests": {"cpu": "50m", "memory": "256Mi"},
                                  "limits": {"memory": "1Gi"}},
                    "volumeMounts": [{"name": "data", "mountPath": "/data"}],
                }],
                         "volumes": [{"name": "data",
                                      "persistentVolumeClaim": {"claimName": NAME}}]},
            },
        },
    }
    _apply(f"/apis/apps/v1/namespaces/{NS}/deployments", NAME, deployment)

    service = {
        "apiVersion": "v1", "kind": "Service",
        "metadata": {"name": NAME, "namespace": NS, "labels": labels,
                     "annotations": PLATFORM.vip_annotations(address)},
        "spec": {"type": "LoadBalancer", "selector": {"app": NAME}, **PLATFORM.vip_spec(address),
                 "ports": [{"name": "s3", "port": port, "targetPort": "s3"},
                           {"name": "console", "port": _console_port(port),
                            "targetPort": "console"}]},
    }
    _apply(f"/api/v1/namespaces/{NS}/services", NAME, service)

    result = {"ok": True, "endpoint": endpoint(), "bucket": BUCKET,
              "access_key": keys["access_key"], "server": running_image if keep_minio else IMAGE}
    if cfg.get("point_longhorn", True):
        result["longhorn"] = point_longhorn()
    return result


def transfers():
    """Whether workloads may move out of this cluster: its store running."""
    state = status()
    try:
        backups_here = (LH.backup_target() or {}).get("url") == backup_url()
    except Exception:
        backups_here = False
    return {"allowed": state["deployed"] and not state["stopped"], "deployed": state["deployed"],
            "ready": state["ready"], "stopped": state["stopped"], "endpoint": state["endpoint"],
            "reachable_off_cluster": state["reachable_off_cluster"], "size_gb": state["size_gb"],
            "backups_here": backups_here}


def set_transfers(allow, size_gb=100, lb_ip="", vip_mode="", port=0):
    """Turn moves out of this cluster on or off: start the store (setting it
    up the first time) or stop it. Stopping keeps its volume, and with it the
    backups already made; they are there when it starts again."""
    path = f"/apis/apps/v1/namespaces/{NS}/deployments/{NAME}"
    deployment = _get(path)
    if allow:
        if not deployment:
            result = deploy({"size_gb": size_gb, "lb_ip": lb_ip, "vip_mode": vip_mode, "port": port,
                             "point_longhorn": True})
            return {**transfers(), "detail": f"moves out are on: backup storage is starting at {result.get('endpoint') or 'its address'}"}
        ksend("PATCH", path, {"spec": {"replicas": 1}}, ctype="application/merge-patch+json")
        return {**transfers(), "allowed": True, "detail": "moves out are on: backup storage is starting again"}
    if deployment:
        ksend("PATCH", path, {"spec": {"replicas": 0}}, ctype="application/merge-patch+json")
    state = transfers()
    return {**state, "allowed": False,
            "detail": "moves out are off: backup storage is stopped, its volume kept"
                      + ("; Longhorn backs up here, so its backups pause until it starts again" if state["backups_here"] else "")}


def point_longhorn(replace=False):
    """Give Longhorn the keys and the URL, so backups have somewhere to go.

    The endpoint deliberately uses the LAN address rather than the in-cluster
    name: a backup only reachable from inside this cluster cannot be restored
    onto the cluster you are moving to.
    """
    keys = credentials()
    service = _get(f"/api/v1/namespaces/{NS}/services/{NAME}")
    if not service:
        raise ValueError("the object store is not deployed yet")
    where = endpoint()
    _apply(f"/api/v1/namespaces/{LHNS}/secrets", LONGHORN_SECRET,
           _secret_body(LONGHORN_SECRET, LHNS, {
               "AWS_ACCESS_KEY_ID": keys["access_key"],
               "AWS_SECRET_ACCESS_KEY": keys["secret_key"],
               "AWS_ENDPOINTS": where,
               # MinIO over plain HTTP on a LAN: Longhorn refuses that unless
               # told, and a self-signed certificate would be no better.
               "VIRTUAL_HOSTED_STYLE": "false",
           }))
    # The keys alone are not enough: Longhorn backs up wherever its target
    # says. A target already pointing somewhere else - an NFS share - is left
    # alone unless asked, since changing it moves where every backup goes.
    current = LH.backup_target()
    kept = ""
    if current.get("configured") and current.get("url") != backup_url() and not replace:
        kept = current.get("url", "")
    else:
        LH.set_backup_target(backup_url(), LONGHORN_SECRET)
    # Backups work either way; only the far cluster cares which address this is.
    return {"url": backup_url(), "secret": LONGHORN_SECRET, "endpoint": where, "kept_target": kept,
            "reachable_off_cluster": ".svc:" not in where,
            "detail": (f"Longhorn still backs up to {kept}; point it here from Data protection to change that" if kept
                       else "Longhorn will back up here" + ("" if ".svc:" not in where else
                       ", but only this cluster can read it until the store has a LAN address"))}


def remove(keep_data=True):
    """Take the store away. The volume stays unless it is explicitly released."""
    removed = []
    for path in (f"/apis/apps/v1/namespaces/{NS}/deployments/{NAME}",
                 f"/api/v1/namespaces/{NS}/services/{NAME}",
                 f"/api/v1/namespaces/{NS}/services/{NAME}-vip"):
        try:
            ksend("DELETE", path)
            removed.append(path.rsplit("/", 2)[-2])
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
    if not keep_data:
        try:
            ksend("DELETE", f"/api/v1/namespaces/{NS}/persistentvolumeclaims/{NAME}")
            removed.append("volume")
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
    return {"ok": True, "removed": removed,
            "detail": "object storage removed" + ("" if keep_data else ", including its volume")}
