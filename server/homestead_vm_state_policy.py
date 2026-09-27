"""KubeVirt backend-state feature/selector evidence; no cluster writes.

CBT uses VM OR namespace selectors (outer VM labels, not launcher labels).
IncrementalBackup remains opt-in through v1.9.0; persistent state is GA from
v1.6.0. Unknown versions are explicitly unverified, never guessed as disabled.
"""
import re
from urllib.parse import quote


def matches(selector, labels):
    if selector is None:
        return False
    if not isinstance(selector, dict) or set(selector) - {"matchLabels", "matchExpressions"}:
        raise ValueError("VM-state label selector is invalid")
    if not isinstance(labels, dict):
        raise ValueError("VM-state labels are unavailable")
    required = selector.get("matchLabels") or {}
    expressions = selector.get("matchExpressions") or []
    if not isinstance(required, dict) or not isinstance(expressions, list):
        raise ValueError("VM-state label selector is invalid")
    if any(not isinstance(key, str) or not key or not isinstance(value, str) for key, value in required.items()):
        raise ValueError("VM-state label selector is invalid")
    selected = all(labels.get(key) == value for key, value in required.items())
    for expression in expressions:
        if not isinstance(expression, dict) or set(expression) - {"key", "operator", "values"}:
            raise ValueError("VM-state selector expression is invalid")
        key, operator, values = (expression.get(field) for field in ("key", "operator", "values"))
        values = [] if values is None else values
        if not isinstance(key, str) or not key or not isinstance(values, list) or any(not isinstance(value, str) for value in values):
            raise ValueError("VM-state selector expression is invalid")
        if operator in ("In", "NotIn") and values:
            found = key in labels and labels[key] in values
            selected &= found if operator == "In" else not found
        elif operator in ("Exists", "DoesNotExist") and not values:
            selected &= key in labels if operator == "Exists" else key not in labels
        else:
            raise ValueError("VM-state selector operator/values are invalid")
    return selected


def selection(vm, spec, config, read, *, version=None, cold=True):
    result = {"automatic": False, "dependencies": {}, "warnings": [], "blockers": []}
    if not cold:
        return result
    domain = spec.get("domain") or {}
    tpm = (domain.get("devices") or {}).get("tpm") or {}
    efi = ((domain.get("firmware") or {}).get("bootloader") or {}).get("efi") or {}
    persistent = tpm.get("persistent") is True or efi.get("persistent") is True
    config = config or {}
    match = re.fullmatch(r"v?1\.(\d+)\.\d+", version or "")
    minor = int(match[1]) if match else None
    gates = (config.get("developerConfiguration") or {}).get("featureGates") or []
    if not isinstance(gates, list) or any(not isinstance(gate, str) for gate in gates):
        result["blockers"].append("KubeVirt feature-gate configuration is invalid")
        return result
    if persistent:
        if minor is not None and 3 <= minor <= 5 and "VMPersistentState" not in gates:
            result["blockers"].append("This KubeVirt version requires the VMPersistentState feature gate for persistent TPM/EFI")
        elif (minor is None or not 3 <= minor <= 9) and "VMPersistentState" not in gates:
            result["warnings"].append("Persistent-state feature availability is unverified on this KubeVirt build")
    selectors = config.get("changedBlockTrackingLabelSelectors")
    if selectors is None:
        return result
    if minor is not None and 3 <= minor < 6 and "IncrementalBackup" in gates:
        result["blockers"].append("Configured incremental-backup selection requires KubeVirt 1.6 or newer")
        return result
    if "IncrementalBackup" not in gates:
        if minor is not None and 3 <= minor <= 9:
            return result
        result["warnings"].append("Incremental-backup defaults are unverified on this KubeVirt build; configured selectors are conservatively included")
    try:
        if not isinstance(selectors, dict) or set(selectors) - {"namespaceLabelSelector", "virtualMachineLabelSelector"}:
            raise ValueError("Backup-state selector configuration is invalid")
        selected = matches(selectors.get("virtualMachineLabelSelector"), (vm.get("metadata") or {}).get("labels") or {})
        if not selected and selectors.get("namespaceLabelSelector") is not None:
            name = vm["metadata"]["namespace"]
            path = "/api/v1/namespaces/" + quote(name, safe="")
            try:
                namespace = read(path)
            except Exception:
                raise ValueError("Backup-state namespace selection could not be verified") from None
            meta = namespace.get("metadata") or {}
            if meta.get("name") != name or not meta.get("uid") or not meta.get("resourceVersion") or meta.get("deletionTimestamp"):
                raise ValueError("Backup-state namespace identity is incomplete or deleting")
            result["dependencies"][path] = {field: meta.get(field) for field in ("namespace", "name", "uid", "resourceVersion")}
            selected = matches(selectors["namespaceLabelSelector"], meta.get("labels") or {})
        result["automatic"] = selected
        if selected:
            result["warnings"].append("KubeVirt backup-tracking selectors require persistent VM state; this does not configure a backup target or prove a usable backup exists")
    except ValueError as error:
        result["blockers"].append(str(error))
    except Exception:
        result["blockers"].append("Backup-state selection could not be verified")
    return result
