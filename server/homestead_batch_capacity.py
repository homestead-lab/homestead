"""Bounded joint placement search; examples, never scheduler reservations.

Synthetic pods consume requests, ports, PVCs and topology in the same planner
as individual deployments. Search considers pod order as well as hosts.
Only an exhaustive search may report a proven checked-constraint shortfall.
"""
import copy
import time

import homestead_place as PLACE
import homestead_pod_resources as RESOURCES


def plan(entries, namespace, pods, nodes, claims, threshold, *, budget=256, seconds=2, read=None):
    warnings = {"This is a snapshot, not a reservation. Kubernetes chooses placement; the example below is not enforced.",
                "Other controllers' pending replicas and concurrent admissions are not fully simulated.",
                "Admission webhooks may change the final pod spec; provisioning and application readiness are not guaranteed."}
    summaries, envelopes = [], []
    initial = []
    def cautions(review):
        return [w for w in review["warnings"] if not w.startswith(("no ready host", "the requested ", "no scheduling order"))]
    for entry in entries:
        dep, count = entry["deployment"], entry["replicas"]
        if not isinstance(count, int) or not 0 <= count <= 100:
            raise ValueError("replicas must be between 0 and 100")
        review = PLACE.manifest_plan(dep, namespace, entry["name"], count, threshold,
                                     planned_claims=claims, pod_snapshot=pods, nodes_snapshot=nodes, read=read)
        initial.append(review)
        warnings.update(cautions(review))
        summaries.append({"name": entry["name"], "replicas": count, "pod_request_gb": review["pod_request_gb"],
                          "pod_memory_gb": review["pod_memory_gb"], "pod_cpu_request_percent": review["pod_cpu_request_percent"]})
    # Memory limits are a pressure estimate, not scheduler reservations. Show
    # every candidate host's conservative upper envelope, not only a good fit.
    booked, _, _, _ = RESOURCES.reservations(pods)
    for node in nodes:
        base = max(float(node.get("mem_used_gb") or 0), booked.get(node["name"], {}).get("memory", 0) / 1024**3)
        # Recently assigned batch pods can still grow towards their limits;
        # low startup telemetry must not erase that known pressure estimate.
        base += node.get("batch_starting_headroom_gb", 0)
        added = 0
        for entry, review in zip(entries, initial):
            candidate = next((c for c in review["candidates"] if c["name"] == node["name"]), {})
            if candidate.get("projected_pods"):
                estimate = PLACE._pod_memory(entry["deployment"]["spec"]["template"]["spec"])[0] / 1024**3
                added += candidate["projected_pods"] * estimate
        capacity = float(node.get("mem_cap_gb") or 0)
        percent = round((base + added) / capacity * 100, 1) if capacity else None
        known = bool(node.get("mem_metrics_available", True) and capacity)
        envelopes.append({"name": node["name"], "baseline_gb": round(base, 2), "upper_gb": round(base + added, 2),
                          "upper_percent": percent if known else None, "metrics_available": known})
        if added and known and percent >= threshold:
            warnings.add(f"{node['name']}: conservative batch RAM upper estimate {percent}% exceeds warning threshold {threshold}%")
    total = sum(e["replicas"] for e in entries)
    remaining = tuple(e["replicas"] for e in entries)
    steps, limited = 0, False
    deadline = time.monotonic() + seconds
    memo = set()
    failures = set()

    def search(left, added, placements):
        nonlocal steps, limited
        if not any(left):
            return placements
        if steps >= budget or time.monotonic() >= deadline:
            limited = True
            return None
        # Placement history order is immaterial once the synthetic pod set is
        # identical. Deduplicate permutations without hiding host alternatives.
        key = (left, tuple(sorted((p["service"], p["host"]) for p in placements)))
        if key in memo:
            return None
        memo.add(key)
        for index, entry in enumerate(entries):
            if not left[index]:
                continue
            steps += 1
            if steps > budget or time.monotonic() >= deadline:
                limited = True
                return None
            review = PLACE.manifest_plan(entry["deployment"], namespace, entry["name"], 1, threshold,
                                         planned_claims=claims, pod_snapshot=pods + added, nodes_snapshot=nodes, read=read)
            warnings.update(cautions(review))
            for candidate in review["candidates"]:
                if not candidate["eligible"]:
                    failures.update(f"{entry['name']} on {candidate['name']}: {reason}" for reason in candidate["reasons"])
                    continue
                pod = copy.deepcopy(entry["deployment"]["spec"]["template"])
                pod.setdefault("metadata", {}).update(namespace=namespace, name=f"planned-{index}-{left[index]}")
                pod["spec"]["nodeName"] = candidate["name"]
                pod["status"] = {"phase": "Pending"}
                after = list(left)
                after[index] -= 1
                result = search(tuple(after), added + [pod], placements + [{"service": entry["name"], "host": candidate["name"]}])
                if result is not None:
                    return result
                if limited:
                    return None
        return None

    # Bound recursion and work for untrusted pasted files. Large batches must
    # be split, not silently fall back to individual unchecked deployments.
    if total > 64 or len(entries) > 32:
        limited, example = True, None
    else:
        example = search(remaining, [], [])
    status = "fits" if example is not None else "unknown" if limited else "blocked"
    if status == "unknown":
        warnings.add("Joint placement search reached its bound; split the batch to obtain a complete review before creating workloads.")
    elif status == "blocked":
        warnings.add("No joint placement fits this batch under the checked snapshot constraints.")
    # Unknown search outcomes are fail-closed for batch creation. Unknown
    # metrics/resource features within a found example remain explicit warnings.
    return {"status": status, "blocked": status != "fits", "requires_confirmation": True,
            "pods": total, "services": summaries, "example": example or [], "nodes": envelopes,
            "warnings": sorted(warnings), "reasons": sorted(failures)[:20] if status != "fits" else [],
            "search_steps": steps}
