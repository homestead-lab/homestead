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


def externalize(ns, dep, read, send, masked=()):
    """Move secret-looking literal values into the workload's Secret, writing
    the Secret first. Changes dep in place and returns it; a Deployment with
    nothing secret-looking, and no Secret yet, is left exactly as it was."""
    name = secret_name(dep)
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
    if not moved and not own(dep):
        return dep  # nothing secret here, and no Secret of its own to tidy
    path = f"/api/v1/namespaces/{ns}/secrets/{name}"
    try:
        current = read(path)
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        current = None
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
        data = read(f"/api/v1/namespaces/{ns}/secrets/{name}").get("data") or {}
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
