"""Secret-looking environment values kept in a Secret, not in the Deployment.

A token typed into a masked field, or an imported variable named like a
password, used to be written into the Deployment as a plain value - readable
by anyone who can read Deployments, and in every copy of its manifest. Just
before a Deployment is written, those values move into one Secret per
workload, and the container refers to them with secretKeyRef.

The Deployment records the Secret's name in an annotation, so the Secret
follows a rename and goes when the workload is deleted. Editing shows the
values as ordinary variables again (see values()), and saving moves them
back.
"""
import base64
import re
import urllib.error

ANNOTATION = "homestead.io/env-secret"
LABEL = "homestead.io/env-for"
SECRETISH = re.compile(r"PASSWORD|PASSWD|PASSPHRASE|_PASS$|^PASS$|_PWD$|TOKEN|SECRET|API_?KEY|APIKEY|"
                       r"PRIVATE_?KEY|ACCESS_?KEY|AUTH_?KEY|PEPPER|SALT|CREDENTIAL", re.I)
KEY = re.compile(r"[^-._a-zA-Z0-9]")


def secretish(name, masked=()):
    return name in masked or bool(SECRETISH.search(str(name or "")))


def secret_name(dep):
    meta = dep.get("metadata") or {}
    return (meta.get("annotations") or {}).get(ANNOTATION) or f"{meta['name']}-env"[:253]


def _containers(dep):
    return ((dep.get("spec") or {}).get("template") or {}).get("spec", {}).get("containers") or []


def _refs(dep, name):
    """Keys of this Secret the Deployment still refers to."""
    return {((item.get("valueFrom") or {}).get("secretKeyRef") or {}).get("key")
            for container in _containers(dep) for item in container.get("env") or []
            if ((item.get("valueFrom") or {}).get("secretKeyRef") or {}).get("name") == name}


def _move(dep, name, masked):
    """Turn each secret-looking literal into a reference to Secret name, in
    place; {key: base64 value} of what moved."""
    moved = {}
    for container in _containers(dep):
        for item in container.get("env") or []:
            var, value = item.get("name"), item.get("value")
            if "valueFrom" in item or not value or not secretish(var, masked):
                continue
            key = KEY.sub("_", f"{container['name']}.{var}")
            moved[key] = base64.b64encode(str(value).encode()).decode()
            item.pop("value")
            item["valueFrom"] = {"secretKeyRef": {"name": name, "key": key}}
    return moved


def kept(dep):
    """Names of the variables the workload keeps in its own Secret now."""
    name = own(dep)
    return {item.get("name") for container in _containers(dep) for item in container.get("env") or []
            if name and ((item.get("valueFrom") or {}).get("secretKeyRef") or {}).get("name") == name}


# A credential in the value itself, whatever the variable is called: a URL
# with a password in it (postgres://user:pass@host), or a token, key or
# signature in a query string (https://hooks.example.com/x?token=...).
CREDENTIAL = re.compile(r"[a-z][a-z0-9+.-]*://[^/\s:@]*:[^/\s@]+@"
                        r"|[?&;](?:access_?token|refresh_?token|token|api_?key|apikey|key|secret|password|passwd|pwd"
                        r"|sig|signature|auth|code)=[^&\s]+", re.I)


def secret_value(value):
    return bool(CREDENTIAL.search(str(value or "")))


def _read(path, read):
    try:
        return read(path)
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        return None


def _ours(secret, dep):
    """The data of the workload's own Secret; {} for none, or another's."""
    if not secret or ((secret.get("metadata") or {}).get("labels") or {}).get(LABEL) is None:
        return {}
    return dict(secret.get("data") or {})


def held(ns, dep, read):
    """The data its own Secret holds now ({} when none or unreadable): for the
    YAML for git, which must decide as a save would."""
    if not own(dep):
        return {}
    try:
        return _ours(_read(f"/api/v1/namespaces/{ns}/secrets/{own(dep)}", read), dep)
    except Exception:
        return {}


def to_move(dep, masked=(), data=None):
    """Names of the variables a save moves into the Secret, beyond names that
    look secret (secretish): fields masked in the form, values with a
    credential in them, and values - or keys - the Secret holds already.
    data is the Secret's data as Kubernetes keeps it (base64)."""
    data = data or {}
    values = set()
    for encoded in data.values():
        try:
            values.add(base64.b64decode(encoded).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue
    names = set(masked or ())
    for container in _containers(dep):
        for item in container.get("env") or []:
            value = item.get("value")
            if "valueFrom" in item or not value:
                continue
            if (str(value) in values or secret_value(value)
                    or KEY.sub("_", f"{container.get('name')}.{item.get('name')}") in data):
                names.add(item.get("name"))
    return names


def as_saved(dep, current, masked=(), data=None):
    """dep as a save would write it, with nothing written (the YAML for git,
    #735): to_move decides for both, so the save moves exactly what this
    shows as a reference. current is the Deployment before the edit; data
    the Secret's data now (held()). Its kept variables count even when
    the Secret could not be read. Changes dep in place; returns {"name",
    "keys"} of the Secret the result refers to, or {} when none."""
    names = to_move(dep, set(masked or ()) | kept(current), data)
    name = secret_name(current)
    _move(dep, name, names)
    keys = _refs(dep, name) - {None}
    if not keys:
        return {}
    # A save records which Secret is the app's own; the YAML does the same.
    dep.setdefault("metadata", {}).setdefault("annotations", {})[ANNOTATION] = name
    return {"name": name, "keys": sorted(keys)}


def externalize(ns, dep, read, send, masked=()):
    """Move secret-looking literal values into the workload's Secret, writing
    the Secret first. Changes dep in place and returns it; a Deployment with
    nothing secret-looking, and no Secret yet, is left exactly as it was."""
    name = secret_name(dep)
    path = f"/api/v1/namespaces/{ns}/secrets/{name}"
    # What the Secret holds now is read first when there is one: a variable
    # renamed in the form keeps its value there (to_move, shared with the
    # YAML for git so the save moves exactly what that shows).
    current = _read(path, read) if own(dep) else None
    moved = _move(dep, name, to_move(dep, masked, _ours(current, dep)))
    if not moved and not own(dep):
        return dep  # nothing secret here, and no Secret of its own to tidy
    if not own(dep):
        current = _read(path, read)
    if current is None and not moved:
        return dep
    owner = dep["metadata"]["name"]
    if current is not None and ((current.get("metadata") or {}).get("labels") or {}).get(LABEL) is None:
        raise ValueError(f"Secret {name} already exists in {ns} and is not the one Homestead keeps for {owner}'s variables")
    used = _refs(dep, name)
    data = {key: value for key, value in {**((current or {}).get("data") or {}), **moved}.items() if key in used}
    labels = {LABEL: owner, "app.kubernetes.io/managed-by": "homestead"}
    if current is None:
        send("POST", f"/api/v1/namespaces/{ns}/secrets",
             {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
              "metadata": {"name": name, "namespace": ns, "labels": labels}, "data": data})
    elif data != (current.get("data") or {}):
        current["data"] = data
        current["metadata"].pop("managedFields", None)
        current["metadata"].setdefault("labels", {}).update(labels)
        send("PUT", path, current)
    dep["metadata"].setdefault("annotations", {})[ANNOTATION] = name
    return dep


def values(ns, dep, read):
    """{container: {variable: value}} for the variables kept in the
    workload's own Secret, so editing shows them like any other."""
    name = (dep.get("metadata", {}).get("annotations") or {}).get(ANNOTATION)
    if not name:
        return {}
    try:
        # Only the Secret Homestead keeps for the app (its label), as held()
        # and externalize() check: never one the annotation was pointed at.
        data = _ours(read(f"/api/v1/namespaces/{ns}/secrets/{name}"), dep)
    except Exception:
        return {}
    out = {}
    for container in _containers(dep):
        for item in container.get("env") or []:
            ref = (item.get("valueFrom") or {}).get("secretKeyRef") or {}
            if ref.get("name") == name and ref.get("key") in data:
                out.setdefault(container["name"], {})[item["name"]] = \
                    base64.b64decode(data[ref["key"]]).decode("utf-8", "replace")
    return out


# What the Edit form shows for a Secret-held value to someone who may not
# save the form (a viewer, or an operator on a workload only an admin may
# edit): the variable's name stays, and that it has a value.
MASK = "••••••"


def mask(payload):
    """The Edit form (workload_edit_payload) with every Secret-held value
    replaced by MASK, in place; returns it."""
    for container in payload.get("containers") or []:
        held = set(container.get("secret_env") or [])
        container["env"] = {key: MASK if key in held and value not in (None, "") else value
                            for key, value in (container.get("env") or {}).items()}
        container["secret_env_hidden"] = bool(held)
    if payload.get("containers") and "env" in payload:
        payload["env"] = payload["containers"][0]["env"]
    return payload


def own(dep):
    """The workload's own Secret, whose references editing may replace."""
    return (dep.get("metadata", {}).get("annotations") or {}).get(ANNOTATION, "")


def remove(ns, dep, read, send):
    """Delete the workload's Secret with it - only one Homestead made for it."""
    name = own(dep)
    if not name:
        return
    path = f"/api/v1/namespaces/{ns}/secrets/{name}"
    try:
        current = read(path)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return
        raise
    # The annotation names it; the label says Homestead made it, under this
    # workload's name or one it had before a rename.
    if LABEL in ((current.get("metadata") or {}).get("labels") or {}):
        send("DELETE", path)
