"""Reviewed workload firewalls backed by ordinary Kubernetes NetworkPolicies.

Policies are additive. Inventory never claims that an API object proves packet
enforcement, and the editor only owns its own, losslessly understood policies.
"""
import copy
import hashlib
import ipaddress
import json
import re
import urllib.error

import homestead_names as NAMES

kget = ksend = None
platform = lambda: {}
NS = "lab"
POLICIES = "/apis/networking.k8s.io/v1/networkpolicies"
CONFIG = NAMES.key("firewall-config")
OWNER = NAMES.key("firewall")
KINDS = {"Deployment": "deployments", "StatefulSet": "statefulsets", "DaemonSet": "daemonsets",
         "VirtualMachine": "virtualmachines"}
SYSTEM = {"kube-system", "kube-public", "kube-node-lease", "cattle-system", "harvester-system",
          "longhorn-system", "kubevirt", "cdi"}


def bind(_kget, _ksend, _platform, namespace):
    global kget, ksend, platform, NS
    kget, ksend, platform, NS = _kget, _ksend, _platform, namespace


def _word(value, label):
    if not isinstance(value, str) or len(value) > 253 or not re.fullmatch(r"[a-z0-9]([-a-z0-9.]*[a-z0-9])?", value):
        raise ValueError(f"Enter a valid {label}")
    return value


def _path(namespace, name=""):
    return f"/apis/networking.k8s.io/v1/namespaces/{_word(namespace, 'namespace')}/networkpolicies" + (
        f"/{_word(name, 'policy name')}" if name else "")


def _items(path, optional=False):
    try:
        return kget(path).get("items", [])
    except urllib.error.HTTPError as error:
        if optional and error.code == 404:
            return []
        raise


def matches(selector, labels):
    if any(labels.get(k) != v for k, v in (selector.get("matchLabels") or {}).items()):
        return False
    for expr in selector.get("matchExpressions") or []:
        key, op, values = expr["key"], expr["operator"], expr.get("values", [])
        if op not in ("In", "NotIn", "Exists", "DoesNotExist"):
            raise ValueError("Unsupported label selector; inspect the policy in Resources")
        if ((op == "In" and labels.get(key) not in values) or
                (op == "NotIn" and labels.get(key) in values) or
                (op == "Exists" and key not in labels) or (op == "DoesNotExist" and key in labels)):
            return False
    return True


def _protected(namespace, labels, name=""):
    return namespace in SYSTEM or (namespace == NS and
        (name.startswith("homestead") or labels.get("app", "").startswith("homestead") or
         labels.get("app.kubernetes.io/name") == "homestead"))


def _target(obj, kind):
    meta, spec = obj.get("metadata", {}), obj.get("spec", {})
    template = spec.get("template") or {}
    pod = template.get("spec") or {}
    labels = (template.get("metadata") or {}).get("labels") or {}
    namespace, name = meta["namespace"], meta["name"]
    selector = copy.deepcopy(spec.get("selector") or {})
    warnings, blocked = [], ""
    if kind == "VirtualMachine":
        # KubeVirt puts this stable, VM-specific label on every launcher pod.
        # Template labels can be shared by unrelated VMs and change on edits.
        labels = {**labels, "vm.kubevirt.io/name": name}
        selector = {"matchLabels": {"vm.kubevirt.io/name": name}}
        networks = pod.get("networks") or []
        implicit = not networks and pod.get("domain", {}).get("devices", {}).get("autoattachPodInterface") is not False
        if not implicit and not any("pod" in network for network in networks):
            blocked = "This VM has no pod-network interface. Its LAN interfaces need a guest or network firewall."
        if any("multus" in network for network in networks):
            warnings.append("VM LAN/Multus interfaces are outside this policy; only its pod-network traffic is covered.")
    if pod.get("hostNetwork"):
        blocked = "This workload uses host networking; configure its host or upstream firewall."
    if not selector or not (selector.get("matchLabels") or selector.get("matchExpressions")):
        blocked = "This workload has no specific pod selector."
    if _protected(namespace, labels, name):
        blocked = "Manage platform and Homestead networking through their owning configuration."
    if meta.get("deletionTimestamp"):
        blocked = "This workload is being deleted; refresh before configuring its firewall."
    if (template.get("metadata") or {}).get("annotations", {}).get("k8s.v1.cni.cncf.io/networks"):
        warnings.append("Additional Multus interfaces are outside this policy.")
    return {"namespace": namespace, "name": name, "kind": kind, "uid": meta.get("uid", ""),
            "selector": selector, "labels": labels, "blocked": blocked, "warnings": warnings}


def targets():
    result = []
    for kind, resource in KINDS.items():
        group = "kubevirt.io/v1" if kind == "VirtualMachine" else "apps/v1"
        result.extend(_target(obj, kind) for obj in _items(f"/apis/{group}/{resource}", optional=kind == "VirtualMachine"))
    return sorted(result, key=lambda row: (row["namespace"], row["name"], row["kind"]))


def _target_identity(target):
    if not isinstance(target, dict) or not isinstance(target.get("kind"), str) or target["kind"] not in KINDS:
        raise ValueError("Choose a workload")
    ns, name = _word(target.get("namespace"), "namespace"), _word(target.get("name"), "workload name")
    if not isinstance(target.get("uid"), str) or not target["uid"].strip():
        raise ValueError("Choose a workload with a current identity")
    return target["kind"], ns, name


def _resolve(target):
    kind, ns, name = _target_identity(target)
    group = "kubevirt.io/v1" if kind == "VirtualMachine" else "apps/v1"
    row = _target(kget(f"/apis/{group}/namespaces/{ns}/{KINDS[kind]}/{name}"), kind)
    if not target.get("uid") or row["uid"] != target["uid"]:
        raise ValueError("The workload was replaced; refresh and select it again")
    if row["blocked"]:
        raise ValueError(row["blocked"])
    return row


def provider():
    """A detected controller is evidence of support, never proof of enforcement."""
    names = {row.get("metadata", {}).get("name", "") for row in
             _items("/apis/apps/v1/namespaces/kube-system/daemonsets")}
    found = [label for marker, label in (("cilium", "Cilium"), ("calico-node", "Calico"),
             ("rke2-canal", "Canal"), ("kube-router", "kube-router")) if marker in names]
    if found:
        return {"name": ", ".join(found), "detail": "Policy-capable network provider detected. Packet enforcement has not been tested."}
    if platform().get("distribution") == "k3s":
        return {"name": "K3s", "detail": "K3s includes a network-policy controller by default. Verify it has not been disabled; packet enforcement has not been tested."}
    return {"name": "Unverified", "detail": "No known policy controller detected. Policies only filter traffic when your network provider enforces Kubernetes NetworkPolicy."}


def _rules(rows, direction):
    if not isinstance(rows, list) or len(rows) > 40:
        raise ValueError("Use at most 40 rules in each direction")
    result = []
    for row in rows:
        if not isinstance(row, dict) or set(row) - {"peer", "value", "protocol", "ports"}:
            raise ValueError("Invalid firewall rule")
        if not isinstance(row.get("value", ""), str) or not isinstance(row.get("ports", ""), str):
            raise ValueError("Peer addresses and ports must be text")
        peer, value = row.get("peer"), row.get("value", "").strip()
        rule = {}
        if peer == "cidr":
            try:
                value = str(ipaddress.ip_network(value, strict=False))
            except ValueError:
                raise ValueError("Enter an IP address or CIDR, such as 192.168.1.0/24") from None
            rule["from" if direction == "ingress" else "to"] = [{"ipBlock": {"cidr": value}}]
        elif peer == "namespace":
            rule["from" if direction == "ingress" else "to"] = [{"namespaceSelector": {
                "matchLabels": {"kubernetes.io/metadata.name": _word(value, "peer namespace")}}}]
        elif peer != "any" or value:
            raise ValueError("Choose anywhere, a namespace, or an IP range")
        protocol = row.get("protocol", "TCP")
        ports = row.get("ports", "").strip()
        if protocol not in ("TCP", "UDP", "SCTP", "Any"):
            raise ValueError("Choose TCP, UDP, SCTP, or any protocol")
        if protocol == "Any":
            if ports:
                raise ValueError("Choose a protocol to specify ports")
        else:
            entries = []
            for port in ports.split(",") if ports else []:
                port = port.strip()
                if not re.fullmatch(r"[0-9]{1,5}", port) or not 1 <= int(port) <= 65535:
                    raise ValueError("Ports must be numbers from 1 to 65535, separated by commas")
                entries.append({"protocol": protocol, "port": int(port)})
            if len(entries) > 32:
                raise ValueError("Use at most 32 ports per rule")
            rule["ports"] = entries or [{"protocol": protocol}]
        result.append(rule)
    return result


def _spec(config, selector):
    spec = {"podSelector": copy.deepcopy(selector), "policyTypes": []}
    for direction in ("ingress", "egress"):
        mode = config.get(direction)
        if mode not in ("unchanged", "restricted"):
            raise ValueError("Choose unrestricted by this policy or allow listed traffic")
        rules = _rules(config.get(direction + "_rules", []), direction)
        if mode == "restricted":
            spec["policyTypes"].append(direction.title())
            spec[direction] = rules
        elif rules:
            raise ValueError("Rules require a restricted direction")
    if type(config.get("allow_dns", False)) is not bool:
        raise ValueError("DNS access must be on or off")
    if config.get("allow_dns"):
        if config.get("egress") != "restricted":
            raise ValueError("The DNS exception requires restricted outbound traffic")
        spec["egress"].append({"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
                                      "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}}}],
                               "ports": [{"protocol": proto, "port": 53} for proto in ("UDP", "TCP")]})
    if not spec["policyTypes"]:
        raise ValueError("Restrict at least one direction, or remove the policy")
    return spec


def _stored_spec(spec):
    # The API omits empty rule lists when serializing NetworkPolicy. An
    # explicit policyType plus an absent list still means default deny.
    result = copy.deepcopy(spec)
    for direction in ("ingress", "egress"):
        if result.get(direction) == []:
            result.pop(direction)
    return result


def _editable(obj):
    meta = obj.get("metadata", {})
    if meta.get("labels", {}).get(OWNER) != "v1" or meta.get("ownerReferences") or meta.get("deletionTimestamp"):
        return None
    try:
        config = json.loads(meta.get("annotations", {}).get(CONFIG, ""))
        _, target_ns, _ = _target_identity(config.get("target"))
        if (target_ns != meta["namespace"] or config.get("name") != meta["name"] or config.get("namespace") != meta["namespace"] or
                _stored_spec(_spec(config, config["selector"])) != _stored_spec(obj.get("spec", {}))):
            return None
        return config
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


def inventory():
    policies = []
    for obj in _items(POLICIES):
        meta = obj["metadata"]
        config = _editable(obj)
        policies.append({"namespace": meta["namespace"], "name": meta["name"], "uid": meta.get("uid"),
                         "resource_version": meta.get("resourceVersion"), "config": config,
                         "managed": bool(config), "spec": obj.get("spec", {})})
    return {"provider": provider(), "targets": targets(),
            "namespaces": sorted(row["metadata"]["name"] for row in _items("/api/v1/namespaces")),
            "policies": sorted(policies, key=lambda row: (row["namespace"], row["name"]))}


def _existing(cfg):
    try:
        existing = kget(_path(cfg["namespace"], cfg["name"]))
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        if cfg.get("uid") or cfg.get("resource_version"):
            raise ValueError("The policy was removed; refresh before continuing") from None
        return None
    if not cfg.get("uid") or not cfg.get("resource_version"):
        raise ValueError("A policy with this name already exists; choose another name or edit it")
    meta = existing["metadata"]
    if meta.get("uid") != cfg["uid"] or meta.get("resourceVersion") != cfg["resource_version"]:
        raise ValueError("The policy changed; refresh and review it again")
    if not _editable(existing):
        raise ValueError("This policy is managed elsewhere or has unsupported changes; inspect it in Resources")
    return existing


def preview(cfg):
    if not isinstance(cfg, dict):
        raise ValueError("Invalid firewall configuration")
    target = _resolve(cfg.get("target"))
    namespace = _word(cfg.get("namespace"), "namespace")
    if namespace != target["namespace"]:
        raise ValueError("The policy and workload must be in the same namespace")
    name = _word(cfg.get("name"), "policy name")
    if not name.startswith("homestead-fw-"):
        raise ValueError("Policy names must start with homestead-fw-")
    config = {key: copy.deepcopy(cfg[key]) for key in ("target", "ingress", "egress", "ingress_rules", "egress_rules", "allow_dns") if key in cfg}
    config.update(name=name, namespace=namespace, selector=target["selector"])
    spec = _spec(config, target["selector"])
    existing = _existing(cfg)
    if existing and _editable(existing)["target"] != config["target"]:
        raise ValueError("A policy cannot be moved to another workload; create a new policy")
    pods = _items(f"/api/v1/namespaces/{namespace}/pods")
    selected = [pod for pod in pods if matches(target["selector"], pod.get("metadata", {}).get("labels", {}))]
    for pod in selected:
        meta = pod.get("metadata", {})
        if pod.get("spec", {}).get("hostNetwork") or _protected(namespace, meta.get("labels", {}), meta.get("name", "")):
            raise ValueError("This selector includes host-network or platform pods; it cannot be managed here")
    policies = _items(_path(namespace))
    overlaps = [obj for obj in policies if obj["metadata"]["name"] != name and any(
        matches(obj.get("spec", {}).get("podSelector", {}), labels)
        for labels in [target["labels"]] + [pod["metadata"].get("labels", {}) for pod in selected])]
    warnings = list(target["warnings"])
    warnings.append("Policies add allowed traffic together. Other matching policies can allow traffic this policy omits; rules have no priority order.")
    if overlaps:
        warnings.append("Other matching policies: " + ", ".join(obj["metadata"]["name"] for obj in overlaps))
    if not selected:
        warnings.append("No running pods currently match. The policy will apply when matching pods start.")
    if config.get("egress") == "restricted":
        warnings.append("Restricted outbound traffic can stop updates, storage access, and API calls. Add every required destination.")
    if config.get("allow_dns"):
        warnings.append("The DNS exception allows kube-dns/CoreDNS pods in kube-system. NodeLocal DNS or custom resolvers need their own IP rule.")
    evidence = provider()
    warnings.append(evidence["detail"])
    manifest = {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
                "metadata": {"name": name, "namespace": namespace,
                             "labels": {OWNER: "v1"}, "annotations": {CONFIG: json.dumps(config, sort_keys=True)}}, "spec": spec}
    if existing:
        manifest["metadata"] = copy.deepcopy(existing["metadata"])
        manifest["metadata"].pop("managedFields", None)
        manifest["metadata"].setdefault("annotations", {})[CONFIG] = json.dumps(config, sort_keys=True)
    matched = sorted(pod["metadata"]["name"] for pod in selected)
    # Labels affect overlap with other policies; pod status does not affect scope.
    snapshot = {"manifest": manifest, "target": target, "provider": evidence,
                "pods": [{"name": pod["metadata"]["name"], "uid": pod["metadata"].get("uid"),
                          "labels": pod["metadata"].get("labels", {})}
                         for pod in sorted(selected, key=lambda row: row["metadata"]["name"])],
                "policies": sorted((obj["metadata"]["name"], obj["metadata"].get("uid"),
                                     obj["metadata"].get("resourceVersion")) for obj in policies)}
    token = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()
    return {"manifest": manifest, "review": token, "pods": matched, "warnings": warnings,
            "overlapping": [obj["metadata"]["name"] for obj in overlaps], "update": bool(existing)}


def save(cfg):
    result = preview(cfg)
    if not cfg.get("review") or cfg["review"] != result["review"]:
        raise ValueError("The firewall plan changed or was not reviewed; review it again before applying")
    obj = result["manifest"]
    ksend("PUT" if result["update"] else "POST", _path(cfg["namespace"], cfg["name"] if result["update"] else ""), obj)
    return {"ok": True, "message": "Firewall policy saved. Verify connectivity and network-provider enforcement."}


def remove(cfg):
    existing = _existing(cfg)
    if not existing:
        raise ValueError("The policy no longer exists")
    meta = existing["metadata"]
    ksend("DELETE", _path(meta["namespace"], meta["name"]), {"apiVersion": "v1", "kind": "DeleteOptions",
          "preconditions": {"uid": meta["uid"], "resourceVersion": meta["resourceVersion"]}})
    return {"ok": True, "message": "Policy removed; remaining policies still apply."}
