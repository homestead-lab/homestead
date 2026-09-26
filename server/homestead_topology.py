"""Read-only pod affinity / spread preflight, not a scheduler reservation.

Only explicit required rules are enforced. Unknown inventory and custom
schedulers remain warnings. Batch search is bounded; exhaustion is unknown,
never evidence that a valid scheduling order does not exist.
"""
from urllib.parse import quote
import time

REQUIRED = "requiredDuringSchedulingIgnoredDuringExecution"


class Unknown(ValueError):
    pass


def selected(selector, labels):
    if selector is None:
        return False
    for key, value in (selector.get("matchLabels") or {}).items():
        if labels.get(key) != value:
            return False
    for expr in selector.get("matchExpressions") or []:
        key, op, values = expr.get("key"), expr.get("operator"), expr.get("values") or []
        if op not in ("In", "NotIn", "Exists", "DoesNotExist"):
            raise Unknown("unsupported label selector")
        if ((op == "In" and (key not in labels or labels[key] not in values)) or
                (op == "NotIn" and key in labels and labels[key] in values) or
                (op == "Exists" and key not in labels) or
                (op == "DoesNotExist" and key in labels)):
            return False
    return True


def terms(pod, kind):
    return (((pod.get("spec") or {}).get("affinity") or {}).get(kind) or {}).get(REQUIRED) or []


class Snapshot:
    def __init__(self, template, namespace, nodes, pods, get, affinity_matches, tolerates):
        self.pod = {"metadata": {**(template.get("metadata") or {}), "namespace": namespace},
                    "spec": template["spec"]}
        self.spec = template["spec"]
        self.nodes = {node["name"]: node for node in nodes}
        self.pods = [p for p in pods or [] if (p.get("spec") or {}).get("nodeName") and
                     (p.get("status") or {}).get("phase") not in ("Succeeded", "Failed")]
        self.inventory_known = pods is not None and all(p["spec"]["nodeName"] in self.nodes for p in self.pods)
        self.get, self.affinity_matches, self.tolerates = get, affinity_matches, tolerates
        self.namespaces = {}
        self.spread = [c for c in self.spec.get("topologySpreadConstraints") or []
                       if c.get("whenUnsatisfiable") == "DoNotSchedule"]
        self.active = bool(terms(self.pod, "podAffinity") or terms(self.pod, "podAntiAffinity") or
                           self.spread or any(terms(p, "podAntiAffinity") for p in self.pods))

    def namespace_labels(self, name):
        if name not in self.namespaces:
            try:
                result = self.get("/api/v1/namespaces/" + quote(name, safe=""))
                if (result.get("metadata") or {}).get("name") != name:
                    raise ValueError("incomplete namespace")
                self.namespaces[name] = result["metadata"].get("labels") or {}
            except Exception:
                self.namespaces[name] = None
        if self.namespaces[name] is None:
            raise Unknown("namespace labels unavailable")
        return self.namespaces[name]

    def matches(self, rule, owner, target, namespace=True):
        own = owner.get("metadata") or {}
        meta = target.get("metadata") or {}
        labels = meta.get("labels") or {}
        if not selected(rule.get("labelSelector"), labels):
            return False
        for field, equal in (("matchLabelKeys", True), ("mismatchLabelKeys", False)):
            for key in rule.get(field) or []:
                own_labels = own.get("labels") or {}
                if key == "pod-template-hash" and key not in own_labels and owner is self.pod:
                    raise Unknown("controller-generated revision label unavailable")
                if key in own_labels:
                    match = key in labels and labels[key] == own_labels[key]
                    if match != equal:
                        return False
        if not namespace:
            return True
        names = rule.get("namespaces") or []
        selector = rule.get("namespaceSelector")
        if meta.get("namespace") in names:
            return True
        if selector == {}:
            return True
        if selector is not None:
            return selected(selector, self.namespace_labels(meta.get("namespace", "default")))
        return not names and meta.get("namespace") == own.get("namespace")

    def domain(self, pod, key):
        host = (pod.get("spec") or {}).get("nodeName")
        return (self.nodes.get(host, {}).get("labels") or {}).get(key)

    def _affinity(self, node, pods):
        reasons, labels = [], node.get("labels") or {}
        required = terms(self.pod, "podAffinity")
        if required:
            # Kubernetes counts pods matching ALL incoming affinity terms.
            matching = [p for p in pods if all(self.matches(t, self.pod, p) for t in required)]
            counts = {(t["topologyKey"], self.domain(p, t["topologyKey"])) for p in matching
                      for t in required if self.domain(p, t["topologyKey"]) is not None}
            bootstrap = not counts and all(self.matches(t, self.pod, self.pod) for t in required)
            for rule in required:
                key = rule["topologyKey"]
                if key not in labels or (not bootstrap and (key, labels[key]) not in counts):
                    reasons.append(f"required pod affinity has no matching peer in {key}={labels.get(key, '(missing)')}")
        for rule in terms(self.pod, "podAntiAffinity"):
            key = rule["topologyKey"]
            if key in labels and any(self.domain(p, key) == labels[key] and self.matches(rule, self.pod, p) for p in pods):
                reasons.append(f"required pod anti-affinity conflicts in {key}={labels[key]}")
        for peer in pods:
            for rule in terms(peer, "podAntiAffinity"):
                key = rule["topologyKey"]
                if key in labels and self.domain(peer, key) == labels[key] and self.matches(rule, peer, self.pod):
                    meta = peer.get("metadata") or {}
                    reasons.append(f"existing pod {meta.get('namespace', '')}/{meta.get('name', '?')} requires anti-affinity in {key}={labels[key]}")
        return reasons

    def _spread(self, node, pods):
        reasons = []
        keys = {c["topologyKey"] for c in self.spread}
        for rule in self.spread:
            key = rule["topologyKey"]
            labels = node.get("labels") or {}
            if key not in labels:
                reasons.append(f"topology spread requires node label {key}")
                continue
            domains, hosts = {}, set()
            for host in self.nodes.values():
                host_labels = host.get("labels") or {}
                if not keys.issubset(host_labels):
                    continue
                if rule.get("nodeAffinityPolicy", "Honor") == "Honor":
                    if (any(host_labels.get(k) != v for k, v in (self.spec.get("nodeSelector") or {}).items()) or
                            not self.affinity_matches(self.spec, host)):
                        continue
                if rule.get("nodeTaintsPolicy", "Ignore") == "Honor" and any(
                        t.get("effect") in ("NoSchedule", "NoExecute") and
                        not self.tolerates(t, self.spec.get("tolerations") or []) for t in host.get("taints") or []):
                    continue
                domains.setdefault(host_labels[key], 0)
                hosts.add(host["name"])
            for pod in pods:
                meta = pod.get("metadata") or {}
                if (pod["spec"].get("nodeName") in hosts and not meta.get("deletionTimestamp") and
                        meta.get("namespace") == self.pod["metadata"]["namespace"] and
                        self.matches(rule, self.pod, pod, namespace=False)):
                    domains[self.domain(pod, key)] += 1
            minimum = min(domains.values(), default=0) if len(domains) >= int(rule.get("minDomains", 1)) else 0
            increment = int(self.matches(rule, self.pod, self.pod, namespace=False))
            skew = domains.get(labels[key], 0) + increment - minimum
            if skew > int(rule["maxSkew"]):
                reasons.append(f"topology spread {key}={labels[key]} would have skew {skew} (maximum {rule['maxSkew']})")
        return reasons

    def check(self, node, extra=()):
        if self.spec.get("nodeName"):
            return [], ["direct node assignment bypasses scheduler pod affinity and topology spread"] if self.active else []
        if self.spec.get("schedulerName", "default-scheduler") != "default-scheduler":
            return [], ["custom scheduler rules cannot be verified by the placement preview"]
        if not self.inventory_known:
            return [], ["pod affinity and topology spread inventory is incomplete; placement is unknown"]
        if not self.active:
            return [], []
        try:
            pods = self.pods + list(extra)
            return self._affinity(node, pods) + self._spread(node, pods), []
        except (Unknown, KeyError, ValueError, TypeError):
            return [], ["pod affinity or topology spread could not be verified (selector or namespace data unavailable)"]

    def batch(self, capacities, count, same_node=False, budget=2000):
        """Find a scheduling order, or prove none within the bounded search.

        Identical new pods mean a per-node count vector fully identifies state.
        Unknown checks may admit extra possibilities, never rule valid ones out.
        """
        hosts = [name for name, slots in capacities.items() if slots > 0]
        seen, furthest, exhausted, unverified = set(), 0, False, False
        deadline = time.monotonic() + 1.0

        def search(state):
            nonlocal furthest, exhausted, unverified
            placed = sum(state)
            furthest = max(furthest, placed)
            if placed == count:
                return True
            if state in seen:
                return False
            if len(seen) >= budget or time.monotonic() >= deadline:
                exhausted = True
                return False
            seen.add(state)
            extra = [{**self.pod, "spec": {**self.spec, "nodeName": hosts[i]}}
                     for i, number in enumerate(state) for _ in range(number)]
            for i, name in enumerate(hosts):
                if state[i] >= capacities[name] or (same_node and placed and not state[i]):
                    continue
                reasons, warnings = self.check(self.nodes[name], extra)
                unverified = unverified or bool(warnings)
                if reasons:
                    continue
                next_state = list(state)
                next_state[i] += 1
                if search(tuple(next_state)):
                    return True
                if exhausted:
                    break
            return False

        fits = search(tuple(0 for _ in hosts))
        return {"status": ("unknown" if unverified else "fits") if fits else "unknown" if exhausted else "blocked",
                "slots": furthest, "search_exhausted": exhausted}
