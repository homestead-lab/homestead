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
import copy
import re
import sys
import urllib.error

ANNOTATION = "homestead.io/env-secret"
LABEL = "homestead.io/env-for"
# A name ending in PASS is a password written as one word (DBPASS, ROOTPASS,
# SMTPPASS), except the ordinary words that end so: BYPASS, COMPASS (and
# ENCOMPASS), OVERPASS, UNDERPASS, TRESPASS, SURPASS, IMPASS. A saved app
# changes only at its next save, whose review shows the reference.
SECRETISH = re.compile(r"PASSWORD|PASSWD|PASSPHRASE|_PASS$|^PASS$|"
                       r"(?<!BY)(?<!COM)(?<!OVER)(?<!UNDER)(?<!TRES)(?<!SUR)(?<!IM)PASS$|_PWD$|TOKEN|SECRET|"
                       r"API_?KEY|APIKEY|PRIVATE_?KEY|ACCESS_?KEY|AUTH_?KEY|PEPPER|SALT|CREDENTIAL", re.I)
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


def unused(dep, name, data):
    """The keys of Secret name's data that dep does not use: the values a
    save may remove. Every container and init container counts, by env,
    envFrom (the whole Secret: then nothing is unused) and volumes (the
    keys a volume lists, or the whole Secret)."""
    spec = ((dep.get("spec") or {}).get("template") or {}).get("spec") or {}
    if _named_elsewhere(spec, name):
        return set()
    keys = set()
    for container in (spec.get("containers") or []) + (spec.get("initContainers") or []):
        for item in container.get("envFrom") or []:
            if (item.get("secretRef") or {}).get("name") == name:
                return set()
        for item in container.get("env") or []:
            ref = (item.get("valueFrom") or {}).get("secretKeyRef") or {}
            if ref.get("name") == name:
                keys.add(ref.get("key"))
    for volume in spec.get("volumes") or []:
        sources = [volume.get("secret") and dict(volume["secret"], name=volume["secret"].get("secretName"))]
        sources += [source.get("secret") for source in (volume.get("projected") or {}).get("sources") or []]
        for source in filter(None, sources):
            if source.get("name") != name:
                continue
            if not source.get("items"):
                return set()
            keys.update(item.get("key") for item in source["items"])
    return {key for key in data if key not in keys}


def _named_elsewhere(spec, name):
    """Whether the pod spec names the Secret anywhere unused() does not read
    key by key (a CSI or other storage volume's secretRef, imagePullSecrets):
    then every value is kept."""
    rest = copy.deepcopy(spec)
    for container in (rest.get("containers") or []) + (rest.get("initContainers") or []):
        # Mounts and devices name volumes, never Secrets.
        for field in ("env", "envFrom", "volumeMounts", "volumeDevices"):
            container.pop(field, None)
    rest["volumes"] = [volume for volume in rest.get("volumes") or [] if not volume.keys() & {"secret", "projected"}]

    def names(node):
        if isinstance(node, dict):
            return any(names(value) for value in node.values())
        if isinstance(node, list):
            return any(names(value) for value in node)
        return node == name
    return names(rest)


def _same_bytes(current, moved):
    """moved with each value the form sent back unchanged put back as the
    Secret holds it: a value that is not UTF-8 text reaches the form with
    replacement characters (values()), and must not be written over."""
    for key, encoded in list(moved.items()):
        held = current.get(key)
        if held is None or held == encoded:
            continue
        try:
            raw = base64.b64decode(held)
            raw.decode("utf-8")
        except UnicodeDecodeError:
            if base64.b64decode(encoded).decode("utf-8", "replace") == raw.decode("utf-8", "replace"):
                moved[key] = held
        except ValueError:
            continue
    return moved


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
    YAML for git, which must decide as a save would. Unlike values(), an
    unread Secret may answer {} here: only that preview uses it, and the save
    itself reads the Secret again (externalize refuses a failed read) and
    keeps every reference the form did not name (#1001)."""
    return held_or_unknown(ns, dep, read) or {}


def held_or_unknown(ns, dep, read):
    """held(), but None when the Secret could not be read."""
    if not own(dep):
        return {}
    try:
        return _ours(_read(f"/api/v1/namespaces/{ns}/secrets/{own(dep)}", read), dep)
    except Exception:
        return None


def removed(current, proposed, data, masked=()):
    """What a save of proposed deletes from the app's Secret, for its review:
    exactly the keys prune() removes after that save (unused(), on the app
    as the save writes it), one entry per variable of current that refers
    to the key ({"key", "container", "name", "value_kept"}), or one named by
    the key itself when nothing names it (container None). value_kept says
    the value lives on under a key the saved app uses, or as a value it
    sets (a renamed variable): only the others are lost. data is
    held_or_unknown(); None when it could not be read, which is "could not
    check", never "none". proposed is left as it is."""
    name = own(current)
    if not name:
        return []
    if data is None:
        return None
    saved = copy.deepcopy(proposed)
    literals = {str(item["value"]) for container in _containers(saved)
                for item in container.get("env") or [] if item.get("value")}
    as_saved(saved, current, masked, data)
    gone = unused(saved, name, data)
    still = (literals | {_decoded(value) for key, value in data.items() if key not in gone}) - {None}
    spec = ((current.get("spec") or {}).get("template") or {}).get("spec") or {}
    users = {}
    for container in (spec.get("containers") or []) + (spec.get("initContainers") or []):
        for item in container.get("env") or []:
            ref = (item.get("valueFrom") or {}).get("secretKeyRef") or {}
            if ref.get("name") == name:
                users.setdefault(ref.get("key"), []).append((container.get("name"), item.get("name")))
    out = []
    for key in sorted(gone):
        value = _decoded(data[key])
        for container, variable in users.get(key) or [(None, key)]:
            out.append({"key": key, "container": container, "name": variable,
                        "value_kept": value is not None and value in still})
    return out


def _decoded(encoded):
    try:
        return base64.b64decode(encoded).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


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
    nothing secret-looking, and no Secret yet, is left exactly as it was.
    Values dep no longer refers to are removed in the same write: for a new
    app, or one that only gains a container, where nothing in use can go.
    Edit uses stage() and prune() instead."""
    return _write(ns, dep, read, send, masked, drop_unused=True)[0]


def stage(ns, dep, read, send, masked=()):
    """externalize() for a save that may still fail: new and changed values
    are written, and every value already kept stays, so the app as it is now
    still finds everything it refers to whatever happens to the save. Changes
    dep in place; returns the Secret as written (or as read, when nothing
    changed) for prune(), or None when there is none."""
    return _write(ns, dep, read, send, masked, drop_unused=False)[1]


def prune(ns, dep, staged, send, allowed=None):
    """After the Deployment dep was saved: remove from the Secret staged()
    returned the values dep no longer refers to, and of those only the keys
    in allowed when it is given (the list the person confirmed: a key that
    appeared since stays for a later save to list). The write carries that
    Secret's resourceVersion, so a Secret another save changed since is left
    alone. The save already succeeded: a failure here only leaves values
    nothing uses, which the next save removes, so it is reported to the log
    and not raised. Returns the keys removed."""
    if not staged:
        return []
    name = staged["metadata"]["name"]
    data = staged.get("data") or {}
    gone = sorted(unused(dep, name, data) & (set(data) if allowed is None else set(allowed)))
    if not gone:
        return []
    body = dict(staged, data={key: value for key, value in data.items() if key not in gone})
    body["metadata"] = {k: v for k, v in staged["metadata"].items() if k != "managedFields"}
    try:
        send("PUT", f"/api/v1/namespaces/{ns}/secrets/{name}", body)
    except (OSError, ValueError) as error:
        # What a write can raise: an HTTP error or a lost connection (OSError,
        # URLError and timeouts among them) or a write guard's refusal
        # (ValueError). The save stands, so it is logged, not raised.
        print(f"Unused values stay in Secret {ns}/{name} until the next save: {error}", file=sys.stderr)
        return []
    return gone


def _write(ns, dep, read, send, masked, drop_unused):
    name = secret_name(dep)
    path = f"/api/v1/namespaces/{ns}/secrets/{name}"
    # What the Secret holds now is read first when there is one: a variable
    # renamed in the form keeps its value there (to_move, shared with the
    # YAML for git so the save moves exactly what that shows).
    current = _read(path, read) if own(dep) else None
    moved = _move(dep, name, to_move(dep, masked, _ours(current, dep)))
    _same_bytes((current or {}).get("data") or {}, moved)
    if not moved and not own(dep):
        return dep, None  # nothing secret here, and no Secret of its own to tidy
    if not own(dep):
        current = _read(path, read)
    if current is None and not moved:
        return dep, None
    owner = dep["metadata"]["name"]
    if current is not None and ((current.get("metadata") or {}).get("labels") or {}).get(LABEL) is None:
        raise ValueError(f"Secret {name} already exists in {ns}. It is not the one Homestead keeps for {owner}'s variables")
    merged = {**((current or {}).get("data") or {}), **moved}
    drop = unused(dep, name, merged) if drop_unused else set()
    data = {key: value for key, value in merged.items() if key not in drop}
    labels = {LABEL: owner, "app.kubernetes.io/managed-by": "homestead"}
    written = current
    if current is None:
        written = send("POST", f"/api/v1/namespaces/{ns}/secrets",
                       {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
                        "metadata": {"name": name, "namespace": ns, "labels": labels}, "data": data})
    elif data != (current.get("data") or {}):
        current["data"] = data
        current["metadata"].pop("managedFields", None)
        current["metadata"].setdefault("labels", {}).update(labels)
        written = send("PUT", path, current)
    dep["metadata"].setdefault("annotations", {})[ANNOTATION] = name
    return dep, written if isinstance(written, dict) and written.get("metadata") else None


def values(ns, dep, read):
    """{container: {variable: value}} for the variables kept in the
    workload's own Secret, so editing shows them like any other; None when
    the Secret could not be read (#1001), which is not "no values": a save
    from a form without them would delete them. No Secret at all is {}."""
    name = (dep.get("metadata", {}).get("annotations") or {}).get(ANNOTATION)
    if not name:
        return {}
    try:
        # Only the Secret Homestead keeps for the app (its label), as held()
        # and externalize() check: never one the annotation was pointed at.
        data = _ours(_read(f"/api/v1/namespaces/{ns}/secrets/{name}", read), dep)
    except Exception:
        return None
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


# What Edit says when the app's own Secret could not be read as it opened;
# the form sends "secret_env_unread" back and the save is refused (#1001).
UNREAD = ("This app's secret settings could not be read, so Edit cannot show them. "
          "Saving is turned off: it would remove them. Close Edit and open it again.")


def refuse_unread(cfg):
    """Refuse a save whose form was built while the Secret could not be read."""
    if cfg.get("secret_env_unread"):
        raise ValueError("This app's secret settings could not be read when Edit opened, "
                         "so saving would remove them. Nothing was changed: close Edit and open it again.")


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
