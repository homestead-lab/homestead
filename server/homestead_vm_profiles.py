"""Ask KubeVirt to resolve profiles without persisting a VM or profile change."""
import copy
import urllib.error
from urllib.parse import quote


def expand(vm, read, send=None):
    """GET saved intent, or PUT proposed intent to the non-persisting expander.

    Never substitute the current saved VM for a proposed edit. Older APIs or
    insufficient RBAC must fail closed, not invent default CPU/memory values.
    """
    if not any(vm.get("spec", {}).get(key) for key in ("instancetype", "preference")):
        return None
    metadata = vm.get("metadata") or {}
    keys = ("namespace", "name", "uid", "resourceVersion")
    if any(not metadata.get(key) for key in keys) or metadata.get("deletionTimestamp"):
        raise ValueError("VM identity is unavailable for profile expansion; refresh it first")
    base = "/apis/subresources.kubevirt.io/v1/namespaces/" + quote(metadata["namespace"], safe="")
    try:
        if send is None:
            result = read(base + "/virtualmachines/" + quote(metadata["name"], safe="") + "/expand-spec")
        else:
            # This endpoint only returns an expanded copy. It does not update
            # the named VirtualMachine (unlike PUT /virtualmachines/{name}).
            result = send("PUT", base + "/expand-vm-spec", copy.deepcopy(vm))
    except urllib.error.HTTPError as error:
        raise ValueError(f"KubeVirt profile expansion is unavailable (HTTP {error.code}). Check the cluster API and Homestead RBAC; no VM change was sent.") from error
    if not isinstance(result, dict):
        raise ValueError("KubeVirt returned an invalid profile expansion")
    observed = result.get("metadata") or {}
    if not isinstance(observed, dict) or any(observed.get(key) != metadata[key] for key in keys) or observed.get("deletionTimestamp"):
        raise ValueError("The VM changed during profile expansion; refresh its review")
    spec = result.get("spec")
    template = spec.get("template") if isinstance(spec, dict) else None
    expanded = template.get("spec") if isinstance(template, dict) else None
    if not isinstance(expanded, dict) or not isinstance(expanded.get("domain"), dict) or not expanded["domain"]:
        raise ValueError("KubeVirt could not expand this VM's instance type/preferences")
    if vm["spec"].get("instancetype"):
        domain = expanded["domain"]
        cpu, memory = domain.get("cpu"), domain.get("memory")
        if (not isinstance(cpu, dict) or not any(isinstance(cpu.get(key), int) and cpu[key] > 0 for key in ("cores", "sockets", "threads"))
                or not isinstance(memory, dict) or not memory.get("guest")):
            raise ValueError("KubeVirt did not resolve the instance type's CPU and guest memory")
    return copy.deepcopy(expanded)
