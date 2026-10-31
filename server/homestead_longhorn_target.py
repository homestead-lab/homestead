"""
Longhorn's backup target: reading it, setting it (on Harvester through its
backup-target setting), its credentials Secret, and the state a cluster move
saves and puts back when it borrows the target.

Split out of homestead_longhorn (#623); what bind() sets, the shared helpers
and the routes stay there.
"""
import base64
import json
import homestead_kube as KUBE
import homestead_names as NAMES
import re
import urllib.error


def backup_target():
    try:
        items = LH.kget(f"{LH.API}/namespaces/{LH.LHNS}/backuptargets").get("items", [])
    except Exception:
        return {"configured": False, "url": "", "available": False,
                "reason": "backuptargets CRD unreadable"}
    if not items:
        return {"configured": False, "url": "", "available": False, "reason": "none defined",
                "harvester": _harvester_setting() is not None}
    t = next((item for item in items if item.get("metadata", {}).get("name") == "default"), items[0])
    sp, st = t.get("spec", {}), t.get("status", {}) or {}
    conds = {c["type"]: c for c in st.get("conditions", []) or []}
    avail = conds.get("Unavailable", {})
    return {
        "configured": bool(sp.get("backupTargetURL")),
        "name": t["metadata"]["name"],
        "url": sp.get("backupTargetURL", ""),
        "secret": sp.get("credentialSecret", ""),
        "interval": sp.get("pollInterval", ""),
        "available": avail.get("status") != "True",
        "reason": avail.get("message", "") or avail.get("reason", ""),
        "secret_missing": bool(sp.get("credentialSecret")) and KUBE.get_optional(f"/api/v1/namespaces/{LH.LHNS}/secrets/{sp.get('credentialSecret')}", read=LH.kget) is None,
        # Set through Harvester's own setting there, which VM backups use too.
        "harvester": _harvester_setting() is not None,
    }


TARGET_SECRET = "homestead-backup-target"
SCHEMES = ("s3://", "nfs://", "cifs://", "azblob://")


def _target_secret(name, keys):
    """The Secret Longhorn reads S3 keys from, made from what was typed."""
    data = {"AWS_ACCESS_KEY_ID": keys["access_key"], "AWS_SECRET_ACCESS_KEY": keys["secret_key"]}
    if keys.get("endpoint"):
        data["AWS_ENDPOINTS"] = keys["endpoint"]

    def change(secret):
        # The typed keys replace their own; anything else in the Secret (a
        # CA certificate, an addressing style) stays. No endpoint typed means
        # none, as before.
        secret["data"] = {key: value for key, value in (secret.get("data") or {}).items()
                          if key not in data and key != "AWS_ENDPOINTS"}
        secret["stringData"] = dict(data)
        return secret

    def create():
        return {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
                "metadata": {"name": name, "namespace": LH.LHNS, "labels": {NAMES.key("managed"): "true"}},
                "stringData": dict(data)}
    LH._update(f"/api/v1/namespaces/{LH.LHNS}/secrets/{name}", change, create)


# On Harvester the backup target is Harvester's own setting: it writes
# Longhorn's from it, and VM backups use it too. Set in Longhorn alone, it is
# overwritten the next time Harvester applies its setting - blank, if that
# was never set.
HARVESTER_TARGET = "/apis/harvesterhci.io/v1beta1/settings/backup-target"


def _harvester_setting():
    try:
        return LH.kget(HARVESTER_TARGET)
    except Exception:
        return None


def _seconds(poll):
    match = re.fullmatch(r"(\d+)\s*([smh]?)", str(poll or "5m").strip())
    if not match:
        return 300
    return int(match.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600}[match.group(2)]


def harvester_target_value(url, keys, poll, current=None):
    """Harvester's backup-target setting for a Longhorn-style URL: nfs://host:/path,
    or s3://bucket@region with the keys (kept from before when none are typed)."""
    current = current or {}
    value = {"refreshIntervalInSeconds": _seconds(poll)}
    if not url:
        return {}
    if url.startswith("nfs://"):
        return dict(value, type="nfs", endpoint=url)
    match = re.fullmatch(r"s3://([^@/]+)@([^/]+)/?", url)
    if not url.startswith("s3://"):
        raise ValueError("Harvester writes backups to NFS or S3: nfs://host:/path or s3://bucket@region")
    if not match:
        raise ValueError("on Harvester an S3 target is s3://bucket@region - Harvester writes to the top of the bucket, "
                         "so leave any path off")
    access = keys.get("access_key") or current.get("accessKeyId", "")
    secret = keys.get("secret_key") or current.get("secretAccessKey", "")
    if not (access and secret):
        raise ValueError("an S3 target needs its access key and secret key")
    return dict(value, type="s3", bucketName=match.group(1), bucketRegion=match.group(2),
                endpoint=keys.get("endpoint") if "endpoint" in keys else current.get("endpoint", ""),
                accessKeyId=access, secretAccessKey=secret,
                virtualHostedStyle=bool(current.get("virtualHostedStyle", False)))


def _set_harvester_target(setting, url, poll, keys):
    try:
        current = json.loads(setting.get("value") or "{}")
    except ValueError:
        current = {}
    if keys.get("endpoint") and not re.match(r"^https?://", keys["endpoint"]):
        raise ValueError("the endpoint is a URL, such as http://192.0.2.20:9000")
    value = harvester_target_value(url, keys, poll, current)
    setting["value"] = json.dumps(value) if value else ""
    setting.get("metadata", {}).pop("managedFields", None)
    try:
        LH.ksend("PUT", f"{HARVESTER_TARGET}", setting)
    except urllib.error.HTTPError as error:
        # Kubernetes includes Harvester's validation reason in the response.
        # str(HTTPError) loses it, particularly when enabling moves wraps it.
        message = str(error)
        try:
            response = json.loads(error.read().decode("utf-8", "replace"))
            if isinstance(response, dict) and isinstance(response.get("message"), str):
                message = response["message"] or message
        except (ValueError, OSError):
            pass
        # Validation errors may echo the submitted setting, including keys.
        for target in (value, current):
            for field in ("accessKeyId", "secretAccessKey"):
                credential = target.get(field)
                if credential:
                    message = message.replace(json.dumps(credential)[1:-1], "[redacted]")
                    message = message.replace(credential, "[redacted]")
        raise ValueError(f"Harvester refused the backup target (HTTP {error.code}): {message[:1200]}") from error
    LH._bust("lhtarget")
    return {"ok": True, "url": url, "harvester": True}


def _secret_keys(name):
    """S3 keys, and the endpoint, from a Secret in Longhorn's namespace."""
    secret = KUBE.get_optional(f"/api/v1/namespaces/{LH.LHNS}/secrets/{name}", read=LH.kget) or {}
    data = secret.get("data") or {}

    def read(key):
        try:
            return base64.b64decode(data.get(key) or "").decode().strip()
        except Exception:
            return ""
    keys = {"access_key": read("AWS_ACCESS_KEY_ID"), "secret_key": read("AWS_SECRET_ACCESS_KEY")}
    if read("AWS_ENDPOINTS"):
        keys["endpoint"] = read("AWS_ENDPOINTS")
    return keys


def on_harvester():
    """Whether the backup target is Harvester's setting, which holds the keys
    itself, rather than Longhorn's own, which names a Secret."""
    return _harvester_setting() is not None


def _backup_target_object():
    # Unlike the status card, a safety snapshot must fail on an unreadable API.
    for path, harvester in ((HARVESTER_TARGET, True),
                            (f"{LH.API}/namespaces/{LH.LHNS}/backuptargets/default", False)):
        try:
            return path, harvester, LH.kget(path)
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
    return path, False, None


def _backup_target_state(harvester, obj):
    if harvester:
        raw = (obj or {}).get("value") or ""
        try:
            value = json.loads(raw) if raw else {}
            if not isinstance(value, dict):
                value = raw
        except ValueError:
            value = raw
        return {"harvester": True, "value": value}
    spec = (obj or {}).get("spec") or {}
    return {"harvester": False, "value": {
        key: spec.get(key, "5m" if key == "pollInterval" else "")
        for key in ("backupTargetURL", "credentialSecret", "pollInterval")}}


def backup_target_state():
    """Private, complete setting for temporary borrowing; may contain S3 keys."""
    _, harvester, obj = _backup_target_object()
    return _backup_target_state(harvester, obj)


def transfer_target_state(previous, url, secret, credentials):
    if previous["harvester"]:
        keys = {"access_key": credentials.get("AWS_ACCESS_KEY_ID", ""),
                "secret_key": credentials.get("AWS_SECRET_ACCESS_KEY", ""),
                "endpoint": credentials.get("AWS_ENDPOINTS", "")}
        current = {"virtualHostedStyle": str(credentials.get("VIRTUAL_HOSTED_STYLE", "false")).lower() == "true"}
        return {"harvester": True, "value": harvester_target_value(url, keys, "30s", current)}
    return {"harvester": False, "value": {
        "backupTargetURL": url, "credentialSecret": secret, "pollInterval": "30s"}}


def replace_backup_target_state(expected, replacement):
    """Replace only our own setting, fenced against concurrent admin changes."""
    path, harvester, obj = _backup_target_object()
    if _backup_target_state(harvester, obj) != expected or harvester != replacement["harvester"]:
        return False
    if expected == replacement:
        return True
    value = replacement["value"]
    patch = {"metadata": {"resourceVersion": (obj or {}).get("metadata", {}).get("resourceVersion", "")}}
    if harvester:
        patch["value"] = (json.dumps(value) if value else "") if isinstance(value, dict) else value
    else:
        patch["spec"] = value
    if obj is None:
        patch.update(apiVersion="longhorn.io/v1beta2", kind="BackupTarget")
        patch["metadata"] = {"name": "default", "namespace": LH.LHNS}
        LH.ksend("POST", path.rsplit("/", 1)[0], patch)
    else:
        LH.ksend("PATCH", path, patch, ctype="application/merge-patch+json")
    LH._bust("lhtarget")
    return True


def set_backup_target(url, secret="", poll="5m", keys=None):
    url, secret = str(url or "").strip(), str(secret or "").strip()
    if url and not url.startswith(SCHEMES):
        raise ValueError("a backup target starts with s3://, nfs://, cifs:// or azblob://")
    keys = {k: str(v or "").strip() for k, v in (keys or {}).items()}
    setting = _harvester_setting()
    if setting is not None:
        # Harvester's setting holds the keys themselves, not a Secret's name:
        # given only the name (backup storage, a move), read them from it.
        if not (keys.get("access_key") and keys.get("secret_key")) and secret:
            keys = {**_secret_keys(secret), **{k: v for k, v in keys.items() if v}}
        return _set_harvester_target(setting, url, poll, keys)
    if keys.get("access_key") or keys.get("secret_key"):
        if not (keys.get("access_key") and keys.get("secret_key")):
            raise ValueError("an S3 target needs both the access key and the secret key")
        if keys.get("endpoint") and not re.match(r"^https?://", keys["endpoint"]):
            raise ValueError("the endpoint is a URL, such as http://192.0.2.20:9000")
        secret = secret or TARGET_SECRET
        if not RESTORE._valid_k8s_name(secret):
            raise ValueError("the secret name is lowercase letters, numbers, dots and dashes")
        _target_secret(secret, keys)
    elif url.startswith("s3://") and not secret:
        raise ValueError("an S3 target needs its keys, or the name of a Secret that holds them")
    name = "default"
    spec = {"backupTargetURL": url, "credentialSecret": secret or "", "pollInterval": poll}

    def change(target):
        target.setdefault("spec", {}).update(spec)
        return target
    LH._update(f"{LH.API}/namespaces/{LH.LHNS}/backuptargets/{name}", change,
            lambda: {"apiVersion": "longhorn.io/v1beta2", "kind": "BackupTarget",
                     "metadata": {"name": name, "namespace": LH.LHNS}, "spec": dict(spec)})
    LH._bust("lhtarget")
    return {"ok": True, "url": url}


# Last, so that importing any of these modules first works: homestead_longhorn
# imports this one.
import homestead_longhorn_restore as RESTORE  # noqa: E402
import homestead_longhorn as LH  # noqa: E402
