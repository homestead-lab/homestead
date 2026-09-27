"""Cold CPU evidence and an operational RAM floor, not a launcher renderer.

References: KubeVirt v1.3.1, v1.5.0, v1.6.0 and v1.9.0
pkg/virt-controller/services/renderresources.go (WithCPUPinning,
WithoutDedicatedCPU and GetMemoryOverhead). Observed version, not target
version, selects the known supplemental-pool rule. Unknown/vendor versions
use conservative planning and remain explicitly unverified.
"""
import re
from decimal import Decimal, ROUND_CEILING

import homestead_pod_resources as RESOURCES

MIB = 1024**2
PARITY = "alpha.kubevirt.io/EmulatorThreadCompleteToEvenParity"


def positive(value, label):
    try:
        number = Decimal(str(value))
    except Exception:
        raise ValueError(f"invalid VM {label}") from None
    if not number.is_finite() or number <= 0:
        raise ValueError(f"VM {label} must be positive")
    return number


def observed_version(kubevirt):
    status = (kubevirt or {}).get("status") or {}
    observed = status.get("observedKubeVirtVersion")
    target = status.get("targetKubeVirtVersion")
    if not observed or (target and target != observed):
        return None
    return observed


def project(vm, spec, config, version=None):
    domain = spec.get("domain") or {}
    cpu = domain.get("cpu") or {}
    resources = domain.get("resources") or {}
    requests, limits = resources.get("requests") or {}, resources.get("limits") or {}
    warnings, blockers = [], []
    topology = any(cpu.get(key) for key in ("cores", "sockets", "threads"))
    vcpus = 1
    for key in ("cores", "sockets", "threads"):
        count = positive(cpu.get(key, 1), key)
        if count != int(count):
            raise ValueError(f"VM {key} must be a whole number")
        vcpus *= int(count)
    if not topology:
        # Admission defaults topology from limits before requests, rounding up.
        configured = limits.get("cpu", requests.get("cpu"))
        if configured is not None:
            vcpus = max(1, (RESOURCES.quantity(configured, "cpu") + 999) // 1000)
    io_count = (domain.get("ioThreads") or {}).get("supplementalPoolThreadCount")
    threads = 0
    if io_count is not None:
        count = positive(io_count, "supplemental IO-thread count")
        if count != int(count) or count > 4294967295:
            raise ValueError("VM supplemental IO-thread count must be a positive uint32")
        threads = int(count)
        if domain.get("ioThreadsPolicy") != "supplementalPool":
            blockers.append("Supplemental IO threads require the supplementalPool IO-thread policy")
    elif domain.get("ioThreadsPolicy") == "supplementalPool":
        blockers.append("Supplemental IO-thread pool size is unresolved")
    # Do not claim a custom build or a future version follows a known renderer.
    match = re.fullmatch(r"v?1\.(\d+)\.\d+", version or "")
    modern = bool(match and 6 <= int(match[1]) <= 9)
    legacy = bool(match and 3 <= int(match[1]) <= 5)
    if not (modern or legacy):
        warnings.append("KubeVirt renderer version is unknown, changing or outside the verified range; CPU/thread estimates need launcher verification")
    if threads and not modern:
        warnings.append("Supplemental IO-thread CPU accounting differs on this KubeVirt version; a conservative CPU allowance is used, not an exact scheduler request")
    explicit = requests.get("cpu", limits.get("cpu"))
    dedicated = cpu.get("dedicatedCpuPlacement") is True
    annotations = ((vm.get("spec") or {}).get("template", {}).get("metadata") or {}).get("annotations") or {}
    if dedicated:
        amount = RESOURCES.quantity(explicit, "cpu") if explicit is not None else vcpus * 1000
        if amount % 1000 or not amount:
            blockers.append("dedicated VM CPU requests must be positive whole cores")
        if topology and explicit is not None and amount != vcpus * 1000:
            blockers.append("dedicated VM CPU topology conflicts with its CPU request")
        if requests.get("cpu") is not None and limits.get("cpu") is not None and RESOURCES.quantity(limits["cpu"], "cpu") != amount:
            blockers.append("dedicated VM CPU request and limit differ")
        amount += threads * 1000
        if cpu.get("isolateEmulatorThread"):
            # 1.6+ parity considers both the already-added pool and additionalCPUs.
            parity = amount // 1000 + (threads if modern else 0)
            extra = 2 if PARITY in annotations and (not (modern or legacy) or parity % 2 == 0) else 1
            amount += extra * 1000
            warnings.append(f"isolated emulator CPU includes {extra} extra core(s); host SMT/CPU-manager admission remains required")
        warnings.append("dedicated CPU topology, NUMA and CPU-manager allocations still require kubelet admission")
    elif explicit is not None:
        amount = RESOURCES.quantity(explicit, "cpu")
        if threads and not modern:
            amount += threads * 1000
    else:
        ratio = positive((config.get("developerConfiguration") or {}).get("cpuAllocationRatio", 10), "CPU allocation ratio")
        counted = vcpus + (threads if modern else 0)
        amount = int((Decimal(counted * 1000) / ratio).to_integral_value(rounding=ROUND_CEILING))
        if not modern:
            amount += threads * 1000
    # This floor deliberately uses the larger verified graphics/QoS allowance
    # and per-thread memory even where an older renderer only charges one IO
    # thread. It is operational headroom, NOT a Kubernetes memory request.
    memory_request = RESOURCES.quantity(requests.get("memory") or (domain.get("memory") or {}).get("guest") or limits.get("memory"))
    floor = (220 + 8 * (vcpus + max(1, threads))) * MIB + (memory_request + 511) // 512
    if (domain.get("devices") or {}).get("autoattachGraphicsDevice") is not False:
        floor += 32 * MIB
    guaranteed = dedicated or all(RESOURCES.quantity(requests.get(key), key) > 0 and
        RESOURCES.quantity(requests.get(key), key) == RESOURCES.quantity(limits.get(key), key)
        for key in ("cpu", "memory"))
    if guaranteed:
        floor += 100 * MIB
    return {"cpu_millis": amount, "memory_floor": floor, "dedicated": dedicated,
            "cpu_is_conservative": bool(threads and not modern),
            "vcpus": vcpus, "io_threads": threads, "warnings": warnings, "blockers": blockers}
