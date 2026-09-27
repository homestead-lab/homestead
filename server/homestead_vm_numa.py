"""NUMA policy prerequisites; physical evidence is not CPU-manager admission.

KubeVirt validates guestMappingPassthrough against dedicated CPU placement and
hugepages. NUMA is opt-in in v1.3, GA from v1.4. The host probe supplies physical
CPU/core/page topology separately; online CPUs are not free dedicated CPUs.
"""
import re


def policy(spec, config, version=None):
    result = {"blockers": [], "warnings": []}
    domain = spec.get("domain") or {}
    cpu = domain.get("cpu") or {}
    numa = cpu.get("numa")
    if numa is None:
        return result
    if not isinstance(numa, dict) or set(numa) - {"guestMappingPassthrough"}:
        result["blockers"].append("VM NUMA policy is unsupported or invalid")
        return result
    mapping = numa.get("guestMappingPassthrough")
    if mapping is None:
        return result
    if not isinstance(mapping, dict) or mapping:
        result["blockers"].append("VM NUMA guest-mapping settings are unsupported or invalid")
    if cpu.get("dedicatedCpuPlacement") is not True:
        result["blockers"].append("NUMA guest mapping requires dedicated CPU placement")
    if not (domain.get("memory") or {}).get("hugepages"):
        result["blockers"].append("NUMA guest mapping requires hugepage-backed guest memory")
    match = re.fullmatch(r"v?1\.(\d+)\.\d+", version or "")
    minor = int(match[1]) if match else None
    gates = (config.get("developerConfiguration") or {}).get("featureGates")
    gates = [] if gates is None else gates
    if not isinstance(gates, list) or any(not isinstance(gate, str) for gate in gates):
        result["blockers"].append("KubeVirt NUMA feature-gate configuration is invalid")
    elif minor == 3 and "NUMA" not in gates:
        result["blockers"].append("KubeVirt 1.3 requires the NUMA feature gate for guest mapping")
    elif minor is None or not 3 <= minor <= 9:
        result["blockers"].append("NUMA guest-mapping policy is unverified on this KubeVirt build or during its upgrade")
    if (config.get("developerConfiguration") or {}).get("useEmulation") is True:
        result["blockers"].append("NUMA guest mapping requires dedicated hardware CPU placement, not software emulation")
    result["warnings"].append("NUMA guest mapping needs matching local CPU and hugepage allocations; node-wide totals and online CPU counts do not prove those allocations are available")
    return result
