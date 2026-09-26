"""Resource arithmetic for previews, not a replacement Kubernetes scheduler."""
import re
from decimal import Decimal, ROUND_CEILING


def quantity(value, resource="memory"):
    """Bytes/counts, or CPU millicores; preserve Kubernetes decimal quantities."""
    if value in (None, ""):
        return 0
    match = re.fullmatch(r"([+]?(?:\d+(?:\.\d*)?|\.\d+))([eE][+-]?\d+|[KMGTPE]i|[numkKMGTPE]|)", str(value))
    if not match:
        raise ValueError(f"invalid {resource} resource quantity")
    number, suffix = Decimal(match[1]), match[2]
    if suffix.endswith("i"):
        number *= Decimal(1024) ** ("KMGTPE".index(suffix[0]) + 1)
    elif suffix.startswith(("e", "E")) and len(suffix) > 1:
        number *= Decimal(10) ** int(suffix[1:])
    else:
        number *= Decimal(10) ** {"": 0, "n": -9, "u": -6, "m": -3, "k": 3, "K": 3,
                                 "M": 6, "G": 9, "T": 12, "P": 15, "E": 18}[suffix]
    if resource == "cpu":
        number *= 1000
    return int(number.to_integral_value(rounding=ROUND_CEILING))


def request(container, resource):
    resources = container.get("resources") or {}
    requests, limits = resources.get("requests") or {}, resources.get("limits") or {}
    return quantity(requests.get(resource, limits.get(resource)), resource)


def effective(spec, value):
    """Init stages overlap earlier restartable init sidecars, not later ones."""
    app = sum(value(c) for c in spec.get("containers") or [])
    sidecars = peak = 0
    for container in spec.get("initContainers") or []:
        amount = value(container)
        if container.get("restartPolicy") == "Always":
            sidecars += amount
            peak = max(peak, sidecars)
        else:
            peak = max(peak, sidecars + amount)
    return max(app + sidecars, peak)


def pod_request(spec, resource):
    result = effective(spec, lambda c: request(c, resource))
    pod = spec.get("resources") or {}
    requests, limits = pod.get("requests") or {}, pod.get("limits") or {}
    if resource in ("cpu", "memory") or resource.startswith("hugepages-"):
        if resource in requests or resource in limits:
            result = quantity(requests.get(resource, limits.get(resource)), resource)
    return result + quantity((spec.get("overhead") or {}).get(resource), resource)


def memory_estimate(spec):
    resources = spec.get("resources") or {}
    pod_limit = quantity((resources.get("limits") or {}).get("memory"))
    if pod_limit:
        return max(pod_request(spec, "memory"), pod_limit + quantity((spec.get("overhead") or {}).get("memory"))), []
    unbounded = [c.get("name") or "container" for c in (spec.get("containers") or []) + (spec.get("initContainers") or [])
                 if not quantity(((c.get("resources") or {}).get("limits") or {}).get("memory"))]
    amount = effective(spec, lambda c: max(request(c, "memory"), quantity(((c.get("resources") or {}).get("limits") or {}).get("memory"))))
    return max(pod_request(spec, "memory"), amount + quantity((spec.get("overhead") or {}).get("memory"))), unbounded


def resource_names(spec):
    names = {"cpu", "memory"}
    for item in [spec] + (spec.get("containers") or []) + (spec.get("initContainers") or []):
        for bucket in ("requests", "limits"):
            names.update(((item.get("resources") or {}).get(bucket) or {}).keys())
    names.update((spec.get("overhead") or {}).keys())
    return names


def reservations(pods):
    """Count assigned nonterminal pods, including terminating and system pods.

    Resize status uses a conservative per-container high water mark. Unassigned
    pods are reported as competition, not invented as reservations on a host.
    """
    totals, pending, resize, dra = {}, 0, False, False
    for pod in pods:
        status, spec = pod.get("status") or {}, pod.get("spec") or {}
        if status.get("phase") in ("Succeeded", "Failed"):
            continue
        node = spec.get("nodeName")
        if not node:
            pending += 1
            continue
        booked = totals.setdefault(node, {"pods": 0})
        booked["pods"] += 1
        statuses = {c.get("name"): c for c in (status.get("containerStatuses") or []) + (status.get("initContainerStatuses") or [])}
        dra |= bool(spec.get("resourceClaims") or status.get("nodeAllocatableResourceClaimStatuses"))
        names = resource_names(spec)
        for cs in [status] + list(statuses.values()):
            names.update((cs.get("allocatedResources") or {}).keys())
            names.update(((cs.get("resources") or {}).get("requests") or {}).keys())
        for resource in names:
            amount = pod_request(spec, resource)
            def high_water(container):
                cs = statuses.get(container.get("name")) or {}
                return max(request(container, resource), quantity((cs.get("allocatedResources") or {}).get(resource), resource),
                           quantity(((cs.get("resources") or {}).get("requests") or {}).get(resource), resource))
            status_amount = max(quantity((status.get("allocatedResources") or {}).get(resource), resource),
                                quantity(((status.get("resources") or {}).get("requests") or {}).get(resource), resource),
                                effective(spec, high_water)) + quantity((spec.get("overhead") or {}).get(resource), resource)
            resize |= status_amount > amount
            booked[resource] = booked.get(resource, 0) + max(amount, status_amount)
    return totals, pending, resize, dra
