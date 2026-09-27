"""Use the existing Homestead Service for the temporary maintenance page.

No Service edits, new VIPs, public ports or account-secret copies. A ConfigMap
controller owner prevents ReplicaSet adoption of the label-matching helper.
Readiness admits it only after the source is fenced, and withdraws it on success.
"""
import copy

from homestead_storage_journal import Held, identity, shape


def review(read, deployment):
    ns, name = deployment["metadata"]["namespace"], deployment["metadata"]["name"]
    template = deployment["spec"]["template"]
    labels = template.get("metadata", {}).get("labels", {})
    app = next(c for c in template["spec"]["containers"] if c["name"] == name)
    port = next((e["value"] for e in app.get("env", []) if e.get("name") == "PORT"), "8080")
    if not str(port).isdigit() or not 1024 <= int(port) <= 65535 or int(port) == 8081:
        raise Held("The maintenance page needs Homestead's normal unprivileged HTTP port")
    port = int(port)
    names = {p["name"]: p["containerPort"] for p in app.get("ports", []) if p.get("name")}
    result = read(f"/api/v1/namespaces/{ns}/services")
    if not isinstance(result.get("items"), list) or result.get("metadata", {}).get("continue"):
        raise Held("Homestead's Service inventory is incomplete")
    services, selectors, aliases = [], {}, set()
    for service in result["items"]:
        spec, meta = service.get("spec", {}), service.get("metadata", {})
        selector = spec.get("selector", {})
        if not selector or not all(labels.get(k) == v for k, v in selector.items()):
            continue
        if (meta.get("namespace") != ns or meta.get("deletionTimestamp") or spec.get("publishNotReadyAddresses")
                or spec.get("externalTrafficPolicy", "Cluster") != "Cluster"
                or spec.get("internalTrafficPolicy", "Cluster") != "Cluster"):
            raise Held("Homestead's Service must route to ready pods across the cluster for a data move")
        for listener in spec.get("ports", []):
            target = listener.get("targetPort", listener.get("port"))
            resolved = names.get(target) if isinstance(target, str) else target
            if resolved != port or listener.get("protocol", "TCP") != "TCP":
                raise Held("A Homestead Service has an additional port the maintenance page cannot serve")
            if isinstance(target, str): aliases.add(target)
        if not spec.get("ports"):
            raise Held("Homestead's Service has no HTTP listener")
        selectors.update(selector)
        services.append({"name": meta["name"], "uid": identity(service)["uid"], "shape": shape(service)})
    if not services or any(k.startswith("homestead.io/handoff") for k in selectors) or "progress" in aliases:
        raise Held("A standard Homestead Service is required to keep progress on the existing address")
    route = {"port": port, "labels": selectors, "names": sorted(aliases)}
    validate(route)
    return route, sorted(services, key=lambda s: s["name"])


def validate(route):
    if (not isinstance(route, dict) or set(route) != {"port", "labels", "names"}
            or type(route["port"]) is not int or not 1024 <= route["port"] <= 65535 or route["port"] == 8081
            or not isinstance(route["labels"], dict) or not route["labels"]
            or not all(isinstance(k, str) and isinstance(v, str) and len(k) <= 253 and len(v) <= 63 for k, v in route["labels"].items())
            or not isinstance(route["names"], list) or len(set(route["names"])) != len(route["names"])
            or any(not isinstance(n, str) or not n or len(n) > 15 or n == "progress" for n in route["names"])):
        raise Held("The maintenance route is invalid")


def apply(pod, service, route, anchor_name, anchor_uid):
    validate(route)
    if any(k in pod["metadata"]["labels"] and pod["metadata"]["labels"][k] != v for k, v in route["labels"].items()):
        raise Held("Homestead's Service selector conflicts with the maintenance helper")
    pod["metadata"]["labels"].update(copy.deepcopy(route["labels"]))
    pod["metadata"]["ownerReferences"] = [{"apiVersion": "v1", "kind": "ConfigMap", "name": anchor_name,
        "uid": anchor_uid, "controller": True, "blockOwnerDeletion": False}]
    app = pod["spec"]["containers"][0]
    app["ports"].extend({"name": n, "containerPort": route["port"], "protocol": "TCP"} for n in route["names"])
    app["readinessProbe"]["httpGet"]["path"] = "/readyz"
    service["spec"]["publishNotReadyAddresses"] = True  # private setup handshake, never the app Service
