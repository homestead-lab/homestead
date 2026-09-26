"""Read-only host-port and PVC constraints for workload start previews."""
import urllib.error


def host_ports(spec):
    containers = (spec.get("containers") or []) + [c for c in spec.get("initContainers") or [] if c.get("restartPolicy") == "Always"]
    result = set()
    for container in containers:
        for port in container.get("ports") or []:
            number = int(port.get("hostPort") or (port.get("containerPort") if spec.get("hostNetwork") else 0) or 0)
            if number:
                result.add((port.get("hostIP") or "0.0.0.0", (port.get("protocol") or "TCP").upper(), number))
    return result


def conflict(left, right):
    wildcard = {"0.0.0.0", "::"}
    return left[1:] == right[1:] and (left[0] == right[0] or left[0] in wildcard or right[0] in wildcard)


def active_pods(pods):
    return [p for p in pods or [] if (p.get("status") or {}).get("phase") not in ("Succeeded", "Failed")]


def claims_used(pod, namespace, claim):
    return (pod.get("metadata") or {}).get("namespace") == namespace and any(
        (v.get("persistentVolumeClaim") or {}).get("claimName") == claim for v in (pod.get("spec") or {}).get("volumes") or [])


class Snapshot:
    def __init__(self, spec, namespace, get, pods, planned_claims=None):
        self.spec, self.namespace, self.pods = spec, namespace, pods
        self.ports = host_ports(spec)
        self.claims = []
        self.same_node = self.single_pod = False
        cache = {}
        def read(path):
            if path not in cache:
                try:
                    value = get(path)
                    cache[path] = value if isinstance(value, dict) and value.get("metadata", {}).get("name") else {"_unknown": True}
                except urllib.error.HTTPError as error:
                    cache[path] = {"_missing": True} if error.code == 404 else {"_unknown": True}
                except Exception:
                    cache[path] = {"_unknown": True}
            return cache[path]
        for claim in sorted({(v.get("persistentVolumeClaim") or {}).get("claimName") for v in spec.get("volumes") or []} - {None, ""}):
            pvc = read(f"/api/v1/namespaces/{namespace}/persistentvolumeclaims/{claim}")
            proposed = (planned_claims or {}).get(claim)
            # Only a verified 404 may become a planned PVC. An API outage must
            # never hide a real volume or manufacture its access mode/topology.
            if pvc.get("_missing") and proposed:
                pvc = {"metadata": {"name": claim}, "_planned": True,
                       "spec": {"accessModes": [proposed["access_mode"]],
                                "storageClassName": proposed["storage_class"]}}
            modes = (pvc.get("spec") or {}).get("accessModes") or []
            self.same_node |= "ReadWriteOnce" in modes and "ReadWriteMany" not in modes
            self.single_pod |= "ReadWriteOncePod" in modes
            volume = (pvc.get("spec") or {}).get("volumeName")
            pv = read(f"/api/v1/persistentvolumes/{volume}") if volume else None
            class_name = (pvc.get("spec") or {}).get("storageClassName")
            sc = read(f"/apis/storage.k8s.io/v1/storageclasses/{class_name}") if class_name and not volume else None
            self.claims.append({"name": claim, "pvc": pvc, "pv": pv, "class": sc, "modes": modes})

    def check(self, node, affinity_matches):
        reasons, warnings = [], []
        maximum = 1 if self.ports else None
        pods = active_pods(self.pods)
        if self.spec.get("hostNetwork"):
            warnings.append("host networking may use undeclared host listeners; only declared ports can be checked")
        if self.ports and self.pods is None:
            warnings.append("host-port usage is unavailable")
        for pod in pods:
            if (pod.get("spec") or {}).get("nodeName") != node["name"]:
                continue
            existing = host_ports(pod.get("spec") or {})
            for port in sorted(self.ports):
                if any(conflict(port, used) for used in existing):
                    meta = pod.get("metadata") or {}
                    reasons.append(f"host port {port[1]}/{port[2]} ({port[0]}) is used by {meta.get('namespace', '?')}/{meta.get('name', '?')}")
        for item in self.claims:
            name, pvc, pv, sc, modes = (item[key] for key in ("name", "pvc", "pv", "class", "modes"))
            if pvc.get("_missing"):
                reasons.append(f"PVC {name} does not exist in {self.namespace}")
                continue
            if pvc.get("_unknown"):
                warnings.append(f"PVC {name} could not be verified")
                continue
            if pvc.get("metadata", {}).get("deletionTimestamp") or (pvc.get("status") or {}).get("phase") == "Lost":
                reasons.append(f"PVC {name} is deleting or Lost")
                continue
            if "ReadWriteOncePod" in modes:
                maximum = 1
                if any(claims_used(pod, self.namespace, name) for pod in pods):
                    reasons.append(f"PVC {name} is ReadWriteOncePod and already used by another pod")
            elif "ReadWriteOnce" in modes and "ReadWriteMany" not in modes:
                elsewhere = sorted({(pod.get("spec") or {}).get("nodeName") for pod in pods
                                    if claims_used(pod, self.namespace, name) and (pod.get("spec") or {}).get("nodeName") not in (None, "", node["name"])})
                if elsewhere:
                    reasons.append(f"PVC {name} is ReadWriteOnce and used on " + ", ".join(elsewhere))
            if modes and self.pods is None:
                warnings.append(f"PVC {name} consumers could not be checked")
            if not modes:
                warnings.append(f"PVC {name} access mode is unknown")
            if pvc.get("_planned"):
                warnings.append(f"PVC {name} is planned, not provisioned; storage capacity and attachment remain unverified")
                if sc and sc.get("provisioner") == "driver.longhorn.io" and str((sc.get("parameters") or {}).get("migratable", "")).lower() == "true":
                    reasons.append(f"PVC {name}'s storage class is for migratable VM disks, not container filesystems")
            if pv:
                if pv.get("_missing"):
                    reasons.append(f"PVC {name}'s bound PV is missing")
                elif pv.get("_unknown"):
                    warnings.append(f"PVC {name}'s volume topology could not be verified")
                else:
                    pv_spec = pv.get("spec") or {}
                    ref = pv_spec.get("claimRef") or {}
                    if (pv.get("metadata", {}).get("deletionTimestamp") or (pv.get("status") or {}).get("phase") in ("Released", "Failed") or
                            (ref.get("uid") and pvc["metadata"].get("uid") and ref["uid"] != pvc["metadata"]["uid"]) or
                            (ref.get("name") and (ref["name"] != name or ref.get("namespace") != self.namespace))):
                        reasons.append(f"PVC {name}'s PV is unavailable or bound to a different claim")
                    required = (pv_spec.get("nodeAffinity") or {}).get("required")
                    if required is not None and not affinity_matches({"affinity": {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": required}}}, node):
                        reasons.append(f"PVC {name}'s volume node affinity does not match")
                    if (pv_spec.get("hostPath") or pv_spec.get("local")) and required is None:
                        warnings.append(f"PVC {name} uses local storage without verified node affinity")
            else:
                if sc and sc.get("_missing"):
                    reasons.append(f"PVC {name}'s storage class does not exist")
                elif sc and not sc.get("_unknown") and sc.get("volumeBindingMode") == "WaitForFirstConsumer":
                    if self.spec.get("nodeName"):
                        reasons.append(f"PVC {name} waits for a scheduler decision; nodeName bypasses its binding")
                    terms = sc.get("allowedTopologies") or []
                    if terms and not any(all((node.get("labels") or {}).get(expr.get("key")) in (expr.get("values") or [])
                                                 for expr in term.get("matchLabelExpressions") or []) for term in terms):
                        reasons.append(f"PVC {name}'s storage class topology does not match")
                    warnings.append(f"PVC {name} will be provisioned after scheduling; storage capacity is unverified")
                else:
                    warnings.append(f"PVC {name} is not bound; provisioning and topology need review")
        if any(v.get("hostPath") for v in self.spec.get("volumes") or []):
            warnings.append("hostPath contents are node-local and their data availability is unverified")
        if any(v.get("ephemeral") for v in self.spec.get("volumes") or []):
            warnings.append("ephemeral PVC provisioning and capacity need scheduler review")
        return reasons, warnings, maximum
