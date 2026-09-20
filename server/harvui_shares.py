"""Samba share inventory and in-place administration.

Share metadata is stored in a ConfigMap, while credentials live in a Secret.
The public inventory deliberately exposes only whether a password exists.  The
legacy deployment/ConfigMap formats are read so existing installations migrate
without losing access the next time a share is changed.
"""
import base64
import hashlib
import json
import re
import time
import urllib.error


kget = ksend = create_pvc = None
NAMESPACE = "lab"
CACHE = {}
CONFIGMAP = "harvui-shares"
SECRET = "harvui-share-credentials"


def bind(_kget, _ksend, _create_pvc, namespace, cache):
    global kget, ksend, create_pvc, NAMESPACE, CACHE
    kget, ksend, create_pvc, NAMESPACE, CACHE = _kget, _ksend, _create_pvc, namespace, cache


def _name(value):
    value = str(value or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,28}[a-z0-9])?", value):
        raise ValueError("share name must be 2-30 lowercase letters, numbers or dashes")
    if len(value) < 2:
        raise ValueError("share name must be 2-30 lowercase letters, numbers or dashes")
    return value


def _user(value):
    value = str(value or "lab").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,32}", value):
        raise ValueError("username must be 1-32 letters, numbers, dots, dashes or underscores")
    return value


def _size(value):
    try:
        value = int(value)
    except (TypeError, ValueError):
        raise ValueError("share size must be a whole number of GB")
    if value < 1 or value > 65536:
        raise ValueError("share size must be between 1 and 65536 GB")
    return value


def _not_found(error):
    return isinstance(error, urllib.error.HTTPError) and error.code == 404


def _get_optional(path):
    try:
        return kget(path)
    except Exception as error:
        if _not_found(error):
            return None
        raise


def _quantity_gb(value):
    """Return a Kubernetes storage quantity in binary GiB."""
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([KMGTPE]i?|)", str(value or "").strip())
    if not match:
        return 0
    number, suffix = float(match.group(1)), match.group(2)
    binary = {"Ki": 1, "Mi": 2, "Gi": 3, "Ti": 4, "Pi": 5, "Ei": 6}
    decimal = {"K": 1, "M": 2, "G": 3, "T": 4, "P": 5, "E": 6}
    if suffix in binary:
        amount = number * (1024 ** binary[suffix])
    elif suffix in decimal:
        amount = number * (1000 ** decimal[suffix])
    else:
        amount = number
    gib = amount / (1024 ** 3)
    return int(round(gib)) if abs(gib - round(gib)) < 0.001 else round(gib, 2)


def _deployment_state():
    """Read legacy share definitions and user passwords from the deployment."""
    dep = _get_optional(f"/apis/apps/v1/namespaces/{NAMESPACE}/deployments/samba")
    if not dep:
        return [], {}, None
    spec = ((dep.get("spec") or {}).get("template") or {}).get("spec") or {}
    containers = spec.get("containers") or []
    if not containers:
        return [], {}, dep
    container = next((row for row in containers if row.get("name") == "samba"), containers[0])
    claim_by_path = {}
    volumes = {row.get("name"): row for row in spec.get("volumes", []) or []}
    for mount in container.get("volumeMounts", []) or []:
        claim = (volumes.get(mount.get("name")) or {}).get("persistentVolumeClaim") or {}
        if claim.get("claimName"):
            claim_by_path[mount.get("mountPath", "")] = claim["claimName"]
    args = container.get("args", []) or []
    users, rows = {}, []
    for index, arg in enumerate(args):
        if arg == "-u" and index + 1 < len(args):
            parts = args[index + 1].split(";", 1)
            if len(parts) == 2:
                users[parts[0]] = parts[1]
        if arg != "-s" or index + 1 >= len(args):
            continue
        parts = args[index + 1].split(";")
        if len(parts) < 2:
            continue
        name, path = parts[0], parts[1]
        pvc = claim_by_path.get(path, "")
        if not pvc:
            continue
        rows.append({
            "name": name, "pvc": pvc, "path": path, "size_gb": 0,
            "user": parts[5] if len(parts) > 5 and parts[5] else "lab",
            "public": (parts[4] if len(parts) > 4 else "no").lower() == "yes",
            "read_only": (parts[3] if len(parts) > 3 else "no").lower() == "yes",
            "created": "existing",
        })
    return rows, users, dep


def _config_rows():
    obj = _get_optional(f"/api/v1/namespaces/{NAMESPACE}/configmaps/{CONFIGMAP}")
    if not obj:
        return [], None
    try:
        rows = json.loads((obj.get("data") or {}).get("shares.json", "[]"))
    except (TypeError, ValueError):
        rows = []
    return rows if isinstance(rows, list) else [], obj


def _credentials():
    obj = _get_optional(f"/api/v1/namespaces/{NAMESPACE}/secrets/{SECRET}")
    if not obj:
        return {}, None
    try:
        raw = base64.b64decode((obj.get("data") or {}).get("credentials.json", "e30="))
        value = json.loads(raw.decode())
    except (TypeError, ValueError):
        value = {}
    return value if isinstance(value, dict) else {}, obj


def _state():
    configured, config_obj = _config_rows()
    discovered, deployment_users, deployment = _deployment_state()
    credentials, secret_obj = _credentials()
    rows = [dict(row) for row in configured if isinstance(row, dict)]
    names = {row.get("name") for row in rows}
    rows.extend(row for row in discovered if row.get("name") not in names)
    for row in rows:
        name = str(row.get("name") or "")
        user = str(row.get("user") or "lab")
        legacy = row.pop("password", None)
        if row.get("public"):
            credentials.pop(name, None)
        elif legacy and name not in credentials:
            credentials[name] = {"user": user, "password": str(legacy)}
        if not row.get("public") and name not in credentials and deployment_users.get(user):
            credentials[name] = {"user": user, "password": deployment_users[user]}
        row["read_only"] = bool(row.get("read_only", False))
    return rows, credentials, config_obj, secret_obj, deployment


def _pvc(name):
    return kget(f"/api/v1/namespaces/{NAMESPACE}/persistentvolumeclaims/{name}")


def _pvc_size(obj):
    spec = (((obj.get("spec") or {}).get("resources") or {}).get("requests") or {}).get("storage")
    status = ((obj.get("status") or {}).get("capacity") or {}).get("storage")
    return _quantity_gb(status or spec), _quantity_gb(spec)


def _public(row, credentials, pvc=None):
    clean = {key: value for key, value in row.items()
             if key not in ("password", "has_password")}
    if pvc:
        actual, requested = _pvc_size(pvc)
        clean["size_gb"] = requested or clean.get("size_gb", 0)
        clean["actual_size_gb"] = actual or clean["size_gb"]
        clean["pvc_status"] = (pvc.get("status") or {}).get("phase", "")
    clean["has_password"] = bool((credentials.get(row.get("name")) or {}).get("password"))
    clean["read_only"] = bool(clean.get("read_only", False))
    return clean


def list_shares():
    rows, credentials, _, _, _ = _state()
    out = []
    for row in rows:
        try:
            pvc = _pvc(row.get("pvc", "")) if row.get("pvc") else None
        except Exception:
            pvc = None
        out.append(_public(row, credentials, pvc))
    return sorted(out, key=lambda row: row.get("name", ""))


def _metadata(name, current=None):
    meta = {"name": name, "namespace": NAMESPACE}
    version = ((current or {}).get("metadata") or {}).get("resourceVersion")
    if version:
        meta["resourceVersion"] = version
    return meta


def _save_config(rows, current=None):
    clean = [{key: value for key, value in row.items()
              if key not in ("password", "has_password", "actual_size_gb", "pvc_status")}
             for row in rows]
    body = {"apiVersion": "v1", "kind": "ConfigMap",
            "metadata": _metadata(CONFIGMAP, current),
            "data": {"shares.json": json.dumps(clean, indent=2)}}
    if current:
        return ksend("PUT", f"/api/v1/namespaces/{NAMESPACE}/configmaps/{CONFIGMAP}", body)
    return ksend("POST", f"/api/v1/namespaces/{NAMESPACE}/configmaps", body)


def _save_credentials(credentials, current=None):
    clean = {name: {"user": str(value.get("user") or "lab"),
                    "password": str(value.get("password") or "")}
             for name, value in credentials.items() if value.get("password")}
    body = {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
            "metadata": _metadata(SECRET, current),
            "data": {"credentials.json": base64.b64encode(
                json.dumps(clean, separators=(",", ":")).encode()).decode()}}
    if current:
        return ksend("PUT", f"/api/v1/namespaces/{NAMESPACE}/secrets/{SECRET}", body)
    return ksend("POST", f"/api/v1/namespaces/{NAMESPACE}/secrets", body)


def _validate_access(rows, credentials):
    users = {}
    for row in rows:
        if row.get("public"):
            continue
        name, user = row["name"], _user(row.get("user"))
        secret = credentials.get(name) or {}
        password = str(secret.get("password") or "")
        if not password:
            raise ValueError(f"private share {name} needs a password")
        if user in users and users[user] != password:
            raise ValueError(f"private shares using {user} must use the same password")
        users[user] = password
    return users


def _volume_name(name):
    return "hs-" + hashlib.sha1(name.encode()).hexdigest()[:12]


def apply_samba(rows, credentials, deployment=None):
    deployment = deployment or _deployment_state()[2]
    if not deployment:
        raise ValueError("the samba deployment is not installed")
    users = _validate_access(rows, credentials)
    spec = deployment["spec"]["template"]["spec"]
    container = next((row for row in spec["containers"] if row.get("name") == "samba"),
                     spec["containers"][0])
    managed = {mount.get("name") for mount in container.get("volumeMounts", []) or []
               if re.fullmatch(r"(?:sh\d+|hs-[a-f0-9]{12})", str(mount.get("name") or ""))}
    mounts = [mount for mount in container.get("volumeMounts", []) or []
              if mount.get("name") not in managed]
    volumes = [volume for volume in spec.get("volumes", []) or []
               if volume.get("name") not in managed]
    args = ["-p"]
    for row in sorted(rows, key=lambda item: item["name"]):
        if not row.get("pvc"):
            continue
        path = row.get("path") or f"/shares/{row['name']}"
        volume_name = _volume_name(row["name"])
        readonly = bool(row.get("read_only", False))
        mounts.append({"name": volume_name, "mountPath": path, "readOnly": readonly})
        volumes.append({"name": volume_name,
                        "persistentVolumeClaim": {"claimName": row["pvc"]}})
        args += ["-s", f"{row['name']};{path};yes;{'yes' if readonly else 'no'};"
                       f"{'yes' if row.get('public') else 'no'};{_user(row.get('user'))}"]
    for user, password in sorted(users.items()):
        args += ["-u", f"{user};{password}"]
    args += ["-g", "server min protocol = SMB2"]
    container["args"], container["volumeMounts"], spec["volumes"] = args, mounts, volumes
    deployment["spec"]["template"].setdefault("metadata", {}).setdefault(
        "annotations", {})["harvui.io/share-update-at"] = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return ksend("PUT", f"/apis/apps/v1/namespaces/{NAMESPACE}/deployments/samba", deployment)


def _clear_cache():
    for key in list(CACHE):
        if key.startswith(("vol", "stor", "flow", "work")):
            CACHE.pop(key, None)


def create_share(name, size_gb, user, password, public, read_only=False):
    name, size_gb, user = _name(name), _size(size_gb), _user(user)
    rows, credentials, config_obj, secret_obj, deployment = _state()
    if any(row.get("name") == name for row in rows):
        raise ValueError("share already exists; use Edit to change it")
    if not public and not password:
        raise ValueError("a password is required for a private share")
    pvc_name = f"share-{name}"
    try:
        create_pvc(NAMESPACE, pvc_name, size_gb)
    except urllib.error.HTTPError as error:
        if error.code == 409:
            raise ValueError(f"PVC {pvc_name} already exists and was not changed")
        raise
    row = {"name": name, "pvc": pvc_name, "path": f"/shares/{name}",
           "size_gb": size_gb, "user": user, "public": bool(public),
           "read_only": bool(read_only),
           "created": time.strftime("%Y-%m-%d %H:%M")}
    rows.append(row)
    if not public:
        credentials[name] = {"user": user, "password": str(password)}
    _validate_access(rows, credentials)
    _save_credentials(credentials, secret_obj)
    _save_config(rows, config_obj)
    result = apply_samba(rows, credentials, deployment)
    _clear_cache()
    return {"shares": [_public(item, credentials) for item in rows],
            "deployment": result, "message": f"Share {name} created"}


def edit_share(name, size_gb, user, password, public, read_only=False):
    name, size_gb, user = _name(name), _size(size_gb), _user(user)
    rows, credentials, config_obj, secret_obj, deployment = _state()
    row = next((item for item in rows if item.get("name") == name), None)
    if not row:
        raise ValueError("share not found")
    pvc = _pvc(row.get("pvc", ""))
    _, old_size = _pvc_size(pvc)
    if old_size and size_gb < old_size:
        raise ValueError(f"Longhorn volumes cannot shrink; choose at least {old_size} GB")
    previous = (bool(row.get("public")), str(row.get("user") or "lab"),
                bool(row.get("read_only", False)))
    row.update(size_gb=size_gb, user=user, public=bool(public),
               read_only=bool(read_only))
    password_changed = bool(password)
    if public:
        password_changed = password_changed or name in credentials
        credentials.pop(name, None)
    elif password:
        credentials[name] = {"user": user, "password": str(password)}
    elif name in credentials:
        credentials[name]["user"] = user
    access_changed = previous != (bool(public), user, bool(read_only)) or password_changed
    _validate_access(rows, credentials)
    if not old_size or size_gb > old_size:
        pvc["spec"]["resources"]["requests"]["storage"] = f"{size_gb}Gi"
        ksend("PUT", f"/api/v1/namespaces/{NAMESPACE}/persistentvolumeclaims/{row['pvc']}", pvc)
    _save_credentials(credentials, secret_obj)
    _save_config(rows, config_obj)
    result = apply_samba(rows, credentials, deployment) if access_changed else None
    _clear_cache()
    return {"shares": [_public(item, credentials) for item in rows],
            "deployment": result, "deployment_updated": access_changed,
            "message": (f"Share {name} updated; Samba is restarting" if access_changed
                        else f"Share {name} size updated")}


def delete_share(name):
    name = _name(name)
    rows, credentials, config_obj, secret_obj, deployment = _state()
    keep = [row for row in rows if row.get("name") != name]
    if len(keep) == len(rows):
        return {"shares": [_public(item, credentials) for item in keep],
                "deployment": None, "message": "Share was already absent"}
    credentials.pop(name, None)
    _save_credentials(credentials, secret_obj)
    _save_config(keep, config_obj)
    result = apply_samba(keep, credentials, deployment)
    _clear_cache()
    return {"shares": [_public(item, credentials) for item in keep],
            "deployment": result, "message": f"Share {name} removed; its PVC was kept"}
