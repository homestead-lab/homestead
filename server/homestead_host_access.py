"""What a workload gets on the host it runs on, and who may give it that.

Privileged mode, added capabilities, the host's network, processes or IPC,
and folders of the host's own filesystem each hand a container the node it
runs on - and from a node, the cluster. Only an admin may give a workload
any of them. An operator deploys and edits ordinary workloads otherwise.
Hardware features that require privileged execution also require an admin.

Checked on the request thread, against the role of the person asking, or for
a job as the person who started it (require_target_as). Work
Homestead does itself - its own pods, reconciling in the background - has no
person behind it and is not checked here.
"""
import contextlib
import copy
import threading
import homestead_names as NAMES

_request = threading.local()
_system_namespaces = frozenset()


def bind_targets(system_namespaces):
    global _system_namespaces
    _system_namespaces = frozenset(system_namespaces)


def require_target(obj, namespace=None, kind=None):
    """Operators cannot run code inside a management or host-privileged identity.
    kind: the object's kind as the write's URL names it; else its own kind."""
    if _allowed():
        return
    _require_shape(obj, kind)
    meta = (obj or {}).get("metadata") or {}
    ns = namespace or meta.get("namespace", "")
    # Every set of labels the object carries: its own, a CronJob's
    # jobTemplate's, and each pod template's - each checked on its own.
    job_meta = _dict(_dict(_dict((obj or {}).get("spec"), "spec").get("jobTemplate"), "jobTemplate").get("metadata"),
                     "jobTemplate metadata")
    label_sets = [_dict(m.get("labels"), "labels") for m in [meta, job_meta] + [t for t, _ in _templates(obj)]]
    name = meta.get("name", "")
    reserved = lambda value: value == NAMES.BRAND or str(value).startswith(NAMES.BRAND + "-")
    protected = (ns in _system_namespaces or reserved(name) or
                 any(reserved(labels.get("app", "")) or labels.get(NAMES.key("task")) for labels in label_sets) or
                 bool(grants(obj)))
    for _, spec in _templates(obj):
        # serviceAccount is the older name of serviceAccountName; the API server still honours it.
        for field in ("serviceAccountName", "serviceAccount"):
            protected = protected or spec.get(field, "default") not in ("", "default", None)
        for volume in _dicts(spec.get("volumes"), "volumes"):
            protected = protected or reserved(_dict(volume.get("secret"), "secret").get("secretName", ""))
            protected = protected or reserved(_dict(volume.get("persistentVolumeClaim"), "claim").get("claimName", ""))
            for source in _dicts(_dict(volume.get("projected"), "projected").get("sources"), "sources"):
                protected = protected or bool(source.get("serviceAccountToken"))
    if protected:
        raise PermissionError("only an admin can operate management, system, service-account or host-privileged workloads")


class Refused(ValueError):
    """An operator asked for host access only an admin can give."""


def set_role(role):
    _request.role = role


@contextlib.contextmanager
def acting_as(as_role):
    """A job's write made as the person who started it, so the fence on
    Kubernetes writes checks it as it would their own request."""
    before = role()
    set_role(as_role)
    try:
        yield
    finally:
        set_role(before)


def require_target_as(obj, namespace, as_role):
    """require_target for a job with no request behind it, made as the
    person who started it: as_role, or no check for an admin or Homestead."""
    if as_role in (None, "admin"):
        return
    with acting_as(as_role):
        require_target(obj, namespace)


def role():
    return getattr(_request, "role", None)


def _allowed():
    return role() in (None, "admin")


def cfg_grants(cfg):
    """Host access a deploy or Compose config asks for."""
    cfg = cfg or {}
    out = set()
    if cfg.get("privileged"):
        out.add("privileged mode")
    for cap in cfg.get("cap_add") or []:
        out.add(f"the {str(cap).upper()} capability")
    if cfg.get("tun"):
        out.add("the host's tunnel device")
    if cfg.get("network_mode") == "host":
        out.add("the host's network")
    for volume in cfg.get("volumes") or []:
        if isinstance(volume, dict) and volume.get("type") == "host":
            out.add(f"the host folder {volume.get('source') or '?'}")
    return out


# Where each kind keeps the pod spec it runs: a Pod in its own spec, a
# CronJob in spec.jobTemplate, the others in spec.template.
KINDS = {"pods": "Pod", "deployments": "Deployment", "statefulsets": "StatefulSet",
         "daemonsets": "DaemonSet", "replicasets": "ReplicaSet", "jobs": "Job", "cronjobs": "CronJob"}
_TEMPLATE_KEY = {"Pod": None, "CronJob": "jobTemplate", "Deployment": "template", "StatefulSet": "template",
                 "DaemonSet": "template", "ReplicaSet": "template", "Job": "template"}


def _require_shape(obj, kind=None):
    """Refuse a body carrying a pod template its kind does not have: the
    API server runs only its kind's own, so a second one would only be read
    by this check."""
    kind = kind or (obj or {}).get("kind")
    if kind not in _TEMPLATE_KEY:
        return
    spec = (obj or {}).get("spec")
    if not isinstance(spec, dict):
        return
    for key in ("template", "jobTemplate"):
        if key in spec and key != _TEMPLATE_KEY[kind]:
            raise ValueError(f"a {kind}'s spec has no {key}")


def _templates(obj):
    """Every pod template a body could carry, as (metadata, pod spec): its
    own spec (a Pod), spec.template and spec.jobTemplate's template. All of
    them are checked, so the check never reads a different one than the
    API server runs."""
    as_dict = lambda value: value if isinstance(value, dict) else {}
    spec = as_dict(as_dict(obj).get("spec"))
    job = as_dict(as_dict(spec.get("jobTemplate")).get("spec"))
    out = [({}, spec)]
    for template in (spec.get("template"), job.get("template")):
        if isinstance(template, dict):
            out.append((as_dict(template.get("metadata")), as_dict(template.get("spec"))))
    return out


def patched(current, patch):
    """The object a PATCH would leave, so it can be checked like a PUT: a
    JSON Patch (a list of operations) or a merge patch (an object; a
    strategic merge's lists are taken whole, which keeps every item the
    patch adds in view). A patch that cannot be applied here is refused."""
    if isinstance(patch, list):
        return _json_patch(copy.deepcopy(current), patch)
    if isinstance(patch, dict):
        return _merge(copy.deepcopy(current), patch)
    raise ValueError("that change is not a patch Homestead can check")


def _merge(target, patch):
    if not isinstance(patch, dict):
        return copy.deepcopy(patch)
    target = target if isinstance(target, dict) else {}
    for key, value in patch.items():
        if value is None:
            target.pop(key, None)
        else:
            target[key] = _merge(target.get(key), value)
    return target


def _json_patch(doc, operations):
    def walk(pointer):
        if not isinstance(pointer, str) or not pointer.startswith("/"):
            raise ValueError(f"{pointer!r} is not a patch path")
        parts = [p.replace("~1", "/").replace("~0", "~") for p in pointer[1:].split("/")]
        parent = doc
        for part in parts[:-1]:
            parent = parent[int(part)] if isinstance(parent, list) else parent[part]
        return parent, parts[-1]

    def get(pointer):
        if pointer == "":
            return doc
        parent, key = walk(pointer)
        return parent[int(key)] if isinstance(parent, list) else parent[key]

    def add(pointer, value):
        parent, key = walk(pointer)
        if isinstance(parent, list):
            parent.insert(len(parent) if key == "-" else int(key), value)
        elif isinstance(parent, dict):
            parent[key] = value
        else:
            raise ValueError(f"{pointer} is not in the object")

    def remove(pointer):
        parent, key = walk(pointer)
        return parent.pop(int(key) if isinstance(parent, list) else key)

    try:
        for operation in operations:
            op, path = operation["op"], operation["path"]
            if op == "add":
                add(path, copy.deepcopy(operation["value"]))
            elif op == "remove":
                remove(path)
            elif op == "replace":
                get(path)
                remove(path)
                add(path, copy.deepcopy(operation["value"]))
            elif op == "move":
                add(path, remove(operation["from"]))
            elif op == "copy":
                add(path, copy.deepcopy(get(operation["from"])))
            elif op == "test":
                if get(path) != operation["value"]:
                    raise ValueError(f"{path} is not what the change expects")
            else:
                raise ValueError(f"{op!r} is not a patch operation")
    except (KeyError, IndexError, TypeError, AttributeError) as error:
        raise ValueError(f"that change cannot be applied: {error}") from None
    return doc


def grants(obj, device_paths=()):
    """Host access a Deployment's pods have. A device an admin defined as a
    hardware feature is not counted, nor the privileged mode it needs.
    Every pod template the object carries counts."""
    devices = set(device_paths or ())
    out = set()
    for _, spec in _templates(obj):
        out |= _spec_grants(spec, devices)
    return out


def _dict(value, what):
    """value as an object; missing is empty, anything else is refused, so a
    write the check cannot read is never let through."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{what} must be an object")
    return value


def _dicts(value, what):
    """value as a list of objects, on the same terms as _dict."""
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(f"{what} must be a list of objects")
    return value


def _spec_grants(spec, devices):
    out = set()
    for key, label in (("hostNetwork", "the host's network"), ("hostPID", "the host's processes"),
                       ("hostIPC", "the host's IPC")):
        if spec.get(key):
            out.add(label)
    passthrough = False
    for volume in _dicts(spec.get("volumes"), "volumes"):
        path = _dict(volume.get("hostPath"), "hostPath").get("path")
        if path is None:
            continue
        if path in devices:
            passthrough = True
            continue
        out.add(f"the host folder {path}")
    for container in (_dicts(spec.get("containers"), "containers") + _dicts(spec.get("initContainers"), "initContainers")
                      + _dicts(spec.get("ephemeralContainers"), "ephemeralContainers")):
        security = _dict(container.get("securityContext"), "securityContext")
        if security.get("privileged") and not passthrough:
            out.add("privileged mode")
        added = _dict(security.get("capabilities"), "capabilities").get("add")
        if added is not None and not isinstance(added, list):
            raise ValueError("capabilities to add must be a list")
        for cap in added or []:
            out.add(f"the {str(cap).upper()} capability")
    return out


def require_cfg(configs):
    """Refuse a deploy or Compose batch asking for host access, unless an admin asks."""
    if _allowed():
        return
    asked = set()
    for cfg in configs:
        asked |= cfg_grants(cfg)
    if asked:
        raise Refused(f"only an admin can give a workload {', '.join(sorted(asked))}")


def require_edit(current, proposed, device_paths=()):
    """Only admins edit a workload with host access, including existing grants."""
    if _allowed():
        return
    added = grants(proposed, device_paths) - grants(current, device_paths)
    if added:
        raise Refused(f"only an admin can give a workload {', '.join(sorted(added))}")
    require_target(current)
    require_target(proposed)
