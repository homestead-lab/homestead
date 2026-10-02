"""What a workload gets on the host it runs on, and who may give it that.

Privileged mode, added capabilities, the host's network, processes or IPC,
and folders of the host's own filesystem each hand a container the node it
runs on - and from a node, the cluster. Only an admin may give a workload
any of them. An operator deploys and edits ordinary workloads otherwise.
Hardware features that require privileged execution also require an admin.

Checked on the request thread, against the role of the person asking. Work
Homestead does itself - its own pods, reconciling in the background - has no
person behind it and is not checked here.
"""
import threading
import homestead_names as NAMES

_request = threading.local()
_system_namespaces = frozenset()


def bind_targets(system_namespaces):
    global _system_namespaces
    _system_namespaces = frozenset(system_namespaces)


def require_target(obj, namespace=None):
    """Operators cannot run code inside a management or host-privileged identity."""
    if _allowed():
        return
    meta = (obj or {}).get("metadata") or {}
    ns = namespace or meta.get("namespace", "")
    spec = _podspec(obj)
    template_meta = (((obj or {}).get("spec") or {}).get("template") or {}).get("metadata") or {}
    labels = {**(meta.get("labels") or {}), **(template_meta.get("labels") or {})}
    name = meta.get("name", "")
    reserved = lambda value: value == NAMES.BRAND or str(value).startswith(NAMES.BRAND + "-")
    protected = (ns in _system_namespaces or reserved(name) or reserved(labels.get("app", "")) or
                 labels.get(NAMES.key("task")) or
                 spec.get("serviceAccountName", "default") not in ("", "default") or
                 bool(grants(obj)))
    for volume in spec.get("volumes") or []:
        protected = protected or reserved((volume.get("secret") or {}).get("secretName", ""))
        protected = protected or reserved((volume.get("persistentVolumeClaim") or {}).get("claimName", ""))
        for source in (volume.get("projected") or {}).get("sources") or []:
            protected = protected or bool(source.get("serviceAccountToken"))
    if protected:
        raise PermissionError("only an admin can operate management, system, service-account or host-privileged workloads")


class Refused(ValueError):
    """An operator asked for host access only an admin can give."""


def set_role(role):
    _request.role = role


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


def _podspec(obj):
    spec = (obj or {}).get("spec") or {}
    return ((spec.get("template") or {}).get("spec") or {}) if "template" in spec else spec


def grants(obj, device_paths=()):
    """Host access a Deployment's pods have. A device an admin defined as a
    hardware feature is not counted, nor the privileged mode it needs."""
    spec = _podspec(obj)
    devices = set(device_paths or ())
    out = set()
    for key, label in (("hostNetwork", "the host's network"), ("hostPID", "the host's processes"),
                       ("hostIPC", "the host's IPC")):
        if spec.get(key):
            out.add(label)
    passthrough = False
    for volume in spec.get("volumes") or []:
        path = (volume.get("hostPath") or {}).get("path")
        if path is None:
            continue
        if path in devices:
            passthrough = True
            continue
        out.add(f"the host folder {path}")
    for container in (spec.get("containers") or []) + (spec.get("initContainers") or []):
        security = container.get("securityContext") or {}
        if security.get("privileged") and not passthrough:
            out.add("privileged mode")
        for cap in (security.get("capabilities") or {}).get("add") or []:
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
