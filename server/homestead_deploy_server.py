"""The deploy, Compose, import and operations code that lived in server.py
(track 1, parts 6 and 8): a deployment's extra containers, volumes, network,
hardware device paths, generated secrets, profile check and capacity
manifest; reviewed deploys, reviewed edits and the rollout review's context;
Compose's report, preparation, preview and apply; deploy options and
bindings; import previews, reviewed imports, import namespaces and their
capacity plan; copy admission; pull progress and service listeners; deleting
a workload; a rebalance move; the move engine's loop; and the Helm routes
server.py's handler answered.

server.py binds what it still owns through bind_routes(): each helper in
ROUTE_NEEDS is looked up in server.py when it is called, or, once moved
(server.MOVED), in the module it moved to, so a test that patches it there
reaches it here too; each name in VALUES is server.py's own value (the same
object, for _cache)."""
import copy
import re
import secrets
import time
import urllib.request, urllib.parse, urllib.error
import homestead_capacity_review as CAPACITY_REVIEW
import homestead_compose as COMPOSE
import homestead_deploy as DEPLOY
import homestead_env_secrets as ENVSEC
import homestead_hardware as HW
import homestead_helm as HELM
import homestead_host_access as HOSTACCESS
import homestead_icons as ICONS
import homestead_import_job as IMPORT_JOB
import homestead_imports as IMP
import homestead_lan as LAN
import homestead_leader as LEADER
import homestead_lifecycle as LC
import homestead_move_phases as MOVE_PHASES
import homestead_move_state as MOVE_STATE
import homestead_names as NAMES
import homestead_namespaces as NSMOD
import homestead_networking as NETWORK
import homestead_operations as OPS
import homestead_place as PLACE
import homestead_pullwatch as PULLWATCH
import homestead_rollout_capacity as ROLLOUT_CAPACITY
import homestead_volowner as VOLOWNER
import homestead_workloads as WORKLOADS

ROUTE_NEEDS = ("_dns_name", "_pvc_rows", "beat", "create_pvc", "edit_lan", "get_app_settings", "guard_managed_smb", "guard_self", "kget", "ksend", "new_claims", "require_workload_target", "selectable_storage_classes", "shared_storage_classes", "storage_class_facts", "storage_classes")
VALUES = ("DATA_DIR", "DEFAULT_NS", "SYS_NS", "_cache")


def bind_routes(helpers, **values):
    missing = set(ROUTE_NEEDS) - set(helpers)
    if missing:
        raise TypeError("homestead_deploy_server needs " + ", ".join(sorted(missing)))
    if set(values) != set(VALUES):
        raise TypeError("homestead_deploy_server is bound with " + ", ".join(sorted(set(values) ^ set(VALUES))))
    globals().update({name: helpers[name] for name in ROUTE_NEEDS})
    globals().update(values)


def additional_container_configs(cfg):
    """Explicit extra definitions in a new pod; the original deploy stays compatible."""
    rows = cfg.get("additional_containers") or []
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("additional containers must be a list of definitions")
    if rows and cfg.get("target_mode", "new") != "new":
        raise ValueError("add containers to an existing workload from its editor")
    if len(rows) > 31:
        raise ValueError("a new workload supports up to 32 containers")
    result = []
    for row in rows:
        allowed = {"name", "image", "env", "ports", "volumes", "hardware", "cpu", "memory", "memory_limit",
                   "privileges", "privileged", "cap_add", "tun", "volume_owners", "new", "container_name"}
        if set(row) - allowed - {"namespace", "workload_name", "network_mode", "target_mode", "app_profile", "env_bindings"}:
            raise ValueError("additional containers use their own image, resources, ports, environment, hardware and storage; pod settings are shared")
        item = {key: copy.deepcopy(value) for key, value in row.items() if key in allowed}
        item.update(namespace=cfg.get("namespace") or DEFAULT_NS,
                    workload_name=cfg.get("workload_name") or cfg.get("name"),
                    container_name=row.get("name"), name=row.get("name"),
                    network_mode=cfg.get("network_mode"), target_mode="new")
        privileges = item.pop("privileges", {}) or {}
        if not isinstance(privileges, dict) or set(privileges) - {"privileged", "cap_add", "tun"}:
            raise ValueError("invalid additional container privileges")
        item.update(privileges)
        item = ensure_profile_compatible(DEPLOY.analyze_deploy_intent(item))
        result.append(item)
    return result


def deployment_volumes(cfg):
    return [volume for item in [cfg, *additional_container_configs(cfg)]
            for volume in item.get("volumes") or []]


def prepare_deploy_network(cfg):
    extras = additional_container_configs(cfg)
    if not extras:
        return NETWORK.prepare_deploy(cfg)
    combined = dict(cfg, ports=[port for item in [cfg, *extras] for port in item.get("ports") or []])
    planned = NETWORK.prepare_deploy(combined)
    return dict(planned, ports=cfg.get("ports") or [])


def import_namespace(value):
    """Where an import goes: the default, or an app namespace that exists."""
    ns = str(value or DEFAULT_NS).strip()
    if ns == DEFAULT_NS:
        return ns
    ns = _dns_name(ns, "namespace")
    if ns in SYS_NS or ns not in NSMOD.names(False):
        raise ValueError(f"namespace {ns} is not one an app can be imported into; create it under Namespaces first")
    return ns


def import_capacity_plan(body):
    """Review both sequential phases before an import creates any resources."""
    cfg = copy.deepcopy(body)
    ns = cfg["namespace"] = import_namespace(body.get("namespace"))
    guard_managed_smb(ns, cfg.get("name"))
    cfg = NETWORK.prepare_deploy(cfg)  # read-only VIP selection and validation
    prepared = IMP.prepare_import(cfg)
    if prepared["job"] and cfg.get("source_consistency") not in ("stopped", "snapshot"):
        raise ValueError("Choose whether the source writers are stopped or the copied paths are a consistent snapshot")
    inventory, pods = IMP.import_inventory(prepared, kget)
    threshold = get_app_settings()["thresholds"]["memory"]["critical"]
    claims = {row["name"]: row for row in prepared["volumes"] if row["create"]}
    nodes = PLACE.get_nodes()
    phases = []
    for kind, title in (("job", "Copy files"), ("deployment", "Imported application")):
        manifest = prepared[kind]
        if manifest:
            plan = PLACE.manifest_plan(manifest, ns, manifest["metadata"]["name"], 1,
                                       threshold, planned_claims=claims, pod_snapshot=pods,
                                       nodes_snapshot=nodes)
            phases.append({"title": title, "capacity": plan})
    warnings = ["No resources are created until you confirm. Copy and application are reviewed separately, not as concurrent workloads.",
                "Imported files can replace files with the same names in existing volumes. Source consistency is not guaranteed; stop the source app or use a consistent backup.",
                "Storage provisioning, actual free filesystem space and future placement are not guaranteed by this review.",
                "The application stays stopped while copying. Starting it later requires a fresh capacity check."]
    capacity = {"blocked": any(row["capacity"]["blocked"] for row in phases),
                "requires_confirmation": True, "warnings": warnings}
    context = {"action": "import", "namespace": ns, **inventory, "prepared": prepared}
    return cfg, prepared, phases, capacity, context


def capacity_manifest(config, existing=None):
    """Construct a read-only manifest and planned claims without provisioning."""
    cfg = DEPLOY.analyze_deploy_intent(copy.deepcopy(config))
    ns = _dns_name(cfg.get("namespace") or DEFAULT_NS, "namespace")
    cfg["namespace"] = ns
    joining = cfg.get("target_mode") == "existing"
    if joining:
        target = _dns_name(cfg.get("target_workload"), "existing workload")
        if existing is None:
            existing = kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{target}")
        dep, _ = DEPLOY.build_sidecar_deployment(cfg, existing)
    elif cfg.get("target_mode", "new") == "new":
        dep, _ = DEPLOY.build_deployment(cfg)
    else:
        raise ValueError("deployment target must be new or existing")
    claims = {row["name"]: row for row in new_claims(deployment_volumes(cfg))}
    for row in claims.values():
        if row["access_mode"] not in ("ReadWriteOnce", "ReadWriteMany", "ReadOnlyMany", "ReadWriteOncePod"):
            raise ValueError("invalid volume access mode")
        # StorageClass names may contain dots, unlike our workload names.
        if not re.fullmatch(r"[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?", row["storage_class"]):
            raise ValueError("invalid storage class name")
    pspec = dep["spec"]["template"]["spec"]
    if not joining and claims and not pspec.get("initContainers"):
        # Owner discovery reads the image later and may add this init stage.
        # Include its request now without fetching layers or writing a helper.
        pspec["initContainers"] = [VOLOWNER.init_container([])]
    return cfg, dep, claims, existing


def deploy_capacity_plan(config, existing=None):
    """Read-only new-workload or shared-pod rollout preview."""
    cfg, dep, claims, existing = capacity_manifest(config, existing)
    ns = cfg["namespace"]
    threshold = get_app_settings()["thresholds"]["memory"]["critical"]
    if cfg.get("target_mode") == "existing":
        return ROLLOUT_CAPACITY.plan(existing, dep, ns, kget, PLACE.get_nodes, PLACE.manifest_plan,
                                     threshold, planned_claims=claims)
    plan = PLACE.manifest_plan(dep, ns, dep["metadata"]["name"], dep["spec"]["replicas"],
                               threshold, planned_claims=claims)
    # The host chosen in Deploy is a preference (#844): say so when it can't take the app.
    host = cfg.get("node")
    # With no copies to start, nothing is placed, so there is nothing to warn of.
    if host and plan.get("additional") and not any(row.get("name") == host and row.get("eligible") for row in plan.get("candidates") or []):
        plan["warnings"] = [*plan.get("warnings", []), f"the chosen host {host} cannot take this app now, so it starts on another host"]
        plan["requires_confirmation"] = True
    return plan


def reviewed_deploy(b):
    """Deploy/App Store endpoint guard; Compose needs a whole-batch review."""
    b = ensure_profile_compatible(DEPLOY.analyze_deploy_intent(copy.deepcopy(b)))
    HOSTACCESS.require_cfg([b, *additional_container_configs(b)])
    if b.get("target_mode", "new") not in ("new", "existing"):
        raise ValueError("deployment target must be new or existing")
    current = None
    context = None
    if b.get("target_mode") == "existing":
        ns = _dns_name(b.get("namespace") or DEFAULT_NS, "namespace")
        target = _dns_name(b.get("target_workload"), "existing workload")
        current = kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{target}")
        context = rollout_review_context(current)
    # Bind the original input and, for a shared pod, the fresh controller
    # version. The final PUT keeps this resourceVersion for optimistic locking.
    plan = deploy_capacity_plan(b, existing=current)
    CAPACITY_REVIEW.enforce(b, plan, context)
    return DEPLOY.run_deploy(b, reviewed_current=current) if current is not None else DEPLOY.run_deploy(b)


def compose_report(b):
    """Read a pasted Compose file against what this namespace already holds."""
    ns = str(b.get("namespace") or DEFAULT_NS)
    _dns_name(ns, "namespace")
    text = str(b.get("text") or "")
    if len(text) > 256 * 1024:
        raise ValueError("a Compose file over 256 KiB is more than Homestead will read")
    workloads = {d["metadata"]["name"] for d in
                 ROLLOUT_CAPACITY.items(kget, f"/apis/apps/v1/namespaces/{ns}/deployments")}
    claims = {c["metadata"]["name"] for c in
              ROLLOUT_CAPACITY.items(kget, f"/api/v1/namespaces/{ns}/persistentvolumeclaims")}
    vip_mode = b.get("vip_mode") if b.get("vip_mode") in ("shared", "auto", "nodes") else "shared"
    if NETWORK.node_addresses_only():
        # On k3s every service shares the nodes' addresses, so ports must differ.
        vip_mode = "shared"
    return COMPOSE.convert(text, b.get("variables") or "", ns, workloads, claims,
                           HW.features(), vip_mode)


def compose_preparation(b):
    """Resolve the selected batch without provisioning or persisting inputs."""
    report = compose_report(b)
    chosen = set(b.get("services") or [row["name"] for row in report["services"]])
    if chosen - {row["name"] for row in report["services"]}:
        raise ValueError("selected Compose service no longer exists; review again")
    rows = {row["name"]: row for row in report["services"] if row["name"] in chosen}
    if not rows:
        raise ValueError("choose at least one service to create")
    problems = report["errors"] + [dict(e, service=name) for name, row in rows.items() for e in row["errors"]]
    if problems:
        first = problems[0]
        where = f"{first['service']}: " if first.get("service") else ""
        raise ValueError(f"fix the file first: {where}{first['message']}"
                         + (f" (line {first['line']})" if first.get("line") else ""))
    configs, entries, claims = [], [], {}
    for name in report["order"]:
        if name not in rows:
            continue
        cfg = ensure_profile_compatible(DEPLOY.analyze_deploy_intent(copy.deepcopy(rows[name]["config"])))
        guard_managed_smb(cfg.get("namespace") or DEFAULT_NS, cfg.get("workload_name") or cfg.get("name"))
        cfg, dep, pending, _ = capacity_manifest(cfg)
        for claim, descriptor in pending.items():
            if claim in claims and claims[claim] != descriptor:
                raise ValueError("inconsistent shared PVC settings: " + claim)
            claims[claim] = descriptor
        configs.append(cfg)
        entries.append({"name": name, "deployment": dep, "replicas": dep["spec"]["replicas"]})
    return configs, entries, claims


def compose_preview(b):
    configs, entries, claims = compose_preparation(b)
    HOSTACCESS.require_cfg(configs)
    return {"capacity": DEPLOY.compose_capacity(entries, claims),
            "capacity_token": CAPACITY_REVIEW.issue(b, {"action": "compose", "configs": configs})}


def compose_apply(b):
    """Guard the entire batch before any writes, then recheck its remainder."""
    configs, entries, claims = compose_preparation(b)
    HOSTACCESS.require_cfg(configs)
    plan = DEPLOY.compose_capacity(entries, claims)
    if plan["status"] == "unknown":
        raise CAPACITY_REVIEW.Rejected("Complete batch placement could not be verified; split the batch and review again", plan)
    CAPACITY_REVIEW.enforce(b, plan, {"action": "compose", "configs": configs})
    created, controllers = [], {}
    for cfg, entry in zip(configs, entries):
        name = entry["name"]
        try:
            if created:
                fresh = DEPLOY.compose_capacity(entries, claims, controllers)
                if fresh["blocked"]:
                    return {"ok": False, "created": created, "failed": name,
                            "error": "Remaining batch no longer fits the checked capacity; review again", "capacity": fresh}
            result = DEPLOY.run_deploy(copy.deepcopy(cfg))
            created.append(result["name"])
            # Never assume the Deployment has pods just because POST returned.
            ns = cfg["namespace"]
            controller = kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{result['name']}")
            if not controller.get("metadata", {}).get("uid"):
                raise ValueError("created workload identity is unavailable; stopped before the next service")
            controllers[name] = controller
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")[:300]
            return {"ok": False, "created": created, "failed": name, "error": f"HTTP {error.code}: {detail}"}
        except Exception as error:
            return {"ok": False, "created": created, "failed": name, "error": str(error)}
    return {"ok": True, "created": created}


def deploy_options(ns):
    """Return the live choices needed by the deployment editor."""
    deployments = []
    for item in kget(f"/apis/apps/v1/namespaces/{ns}/deployments").get("items", []):
        pspec = item.get("spec", {}).get("template", {}).get("spec", {})
        volumes = []
        for volume in pspec.get("volumes", []) or []:
            row = {"name": volume.get("name", ""), "kind": "other", "source": ""}
            if volume.get("persistentVolumeClaim"):
                row.update({"kind": "pvc", "source": volume["persistentVolumeClaim"].get("claimName", "")})
            elif volume.get("hostPath"):
                row.update({"kind": "host", "source": volume["hostPath"].get("path", "")})
            elif volume.get("emptyDir") is not None:
                row["kind"] = "emptyDir"
            elif volume.get("configMap"):
                row.update({"kind": "configMap", "source": volume["configMap"].get("name", "")})
            elif volume.get("secret"):
                row.update({"kind": "secret", "source": volume["secret"].get("secretName", "")})
            volumes.append(row)
        deployments.append({
            "name": item["metadata"]["name"],
            "containers": [c.get("name", "") for c in pspec.get("containers", []) or []],
            "volumes": volumes,
        })
    classes = storage_classes()
    return {"deployments": sorted(deployments, key=lambda x: x["name"]),
            "pvcs": _pvc_rows(ns),
            "storage_classes": selectable_storage_classes(classes),
            "shared_storage_classes": shared_storage_classes(classes),
            "storage_class_facts": storage_class_facts(classes)}


def operation_item(operation_id):
    """A job-tray item as it stands now, or None."""
    return next((item for item in OPS.snapshot() if item.get("id") == operation_id), None) if operation_id else None


def apply_deploy_bindings(cfg):
    """Resolve values that depend on the reviewed cluster-side network plan."""
    bindings = cfg.get("env_bindings") or {}
    if not bindings:
        return cfg
    updated = dict(cfg)
    updated["env"] = dict(cfg.get("env") or {})
    for key, binding in bindings.items():
        if binding == "vip":
            vip = cfg.get("lb_ip") or ""
            if not vip:
                raise ValueError(f"{key} requires a dedicated Service VIP")
            updated["env"][key] = vip
    return updated


def redact_deployment_preview(deployment, cfg=None):
    """Return a manifest safe to display, including for existing shared pods."""
    if not deployment:
        return deployment
    sensitive = {item.get("key") for item in ((cfg or {}).get("env_meta") or [])
                 if item.get("masked") and item.get("key")}
    safe = copy.deepcopy(deployment)
    containers = safe.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
    for container in containers:
        for item in container.get("env") or []:
            key = str(item.get("name") or "")
            if key in sensitive or re.search(r"(?:PASSWORD|PASS|TOKEN|SECRET|API_?KEY|PRIVATE_?KEY)$", key, re.I):
                if "value" in item:
                    item["value"] = "••••••"
    return safe


def rebalance_move(ns, name, node):
    """One container of a rebalance: the manual move's capacity check and
    placement - a preference, not a pin - or a reason to leave it."""
    body = {"ns": ns, "name": name, "node": node, "pin": False}
    proposed, capacity, _ = WORKLOADS.move_capacity_plan(body)
    if capacity.get("blocked"):
        raise ValueError("; ".join(capacity.get("blockers") or []) or f"{node} cannot take it now")
    meta = proposed["metadata"]
    ksend("PUT", f"/apis/apps/v1/namespaces/{meta['namespace']}/deployments/{meta['name']}", proposed)
    PLACE._bust("wl", "ov", "flow", "nodes", "impact:")


def delete_workload(ns, name):
    """A workload deleted, with every Service that points at it and its own
    LAN network; the Services removed are returned."""
    require_workload_target(ns, name)
    guard_self(ns, name, deleting=True)
    guard_managed_smb(ns, name)
    try:
        LAN.remove_nad(ns, name)       # its own LAN network, if it had one
    except Exception:
        pass
    # Every Service selecting these pods, not just the one sharing the
    # workload's name: a sidecar or a hand-made listener is named
    # differently and would otherwise keep its VIP port forever.
    services = set(NETWORK.workload_service_names(ns, name)) | {name}
    try:
        current = kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")
    except Exception:
        current = None
    ksend("DELETE", f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")
    if current:
        try:
            ENVSEC.remove(ns, current, kget, ksend)  # its variables' Secret goes with it
        except Exception:
            pass
    removed = []
    for service in sorted(services):
        try:
            ksend("DELETE", f"/api/v1/namespaces/{ns}/services/{service}")
            removed.append(service)
        except urllib.error.HTTPError:
            pass
    _cache.pop("wl", None); _cache.pop("ov", None); _cache.pop("network", None)
    return removed


def _moves_loop():
    """The move engine, on the leader only: a move's next step is taken once."""
    while True:
        if LEADER.is_leader():
            try:
                MOVE_PHASES.tick_all()
                beat("moves", MOVE_STATE.TICK_SECONDS, leader_only=True)
            except Exception as error:
                beat("moves", MOVE_STATE.TICK_SECONDS, error, leader_only=True)
        time.sleep(MOVE_STATE.TICK_SECONDS)


def rollout_review_context(current):
    meta = current.get("metadata") or {}
    if not meta.get("uid") or not meta.get("resourceVersion"):
        raise ValueError("workload identity/version is unavailable; refresh before changing its pod")
    return {"uid": meta["uid"], "resourceVersion": meta["resourceVersion"]}


def preview_import(body):
    _, prepared, phases, capacity, context = import_capacity_plan(body)
    return {"phases": phases, "capacity": capacity, "volumes": prepared["volumes"],
            "capacity_token": CAPACITY_REVIEW.issue(body, context)}


def reviewed_import(body):
    cfg, prepared, _, capacity, context = import_capacity_plan(body)
    CAPACITY_REVIEW.enforce(body, capacity, context)
    # Logo persistence cannot change scheduling, but must follow admission.
    persist_icon_config(cfg)
    # Re-admit after the potentially slow logo fetch, not merely against the
    # inventory from before it. Registry/network/claim drift needs new review.
    _, prepared, _, capacity, context = import_capacity_plan(body)
    CAPACITY_REVIEW.enforce(body, capacity, context)
    context = copy.deepcopy(context)  # persisted logo must not mutate the signed review
    if prepared["deployment"]:
        annotations = prepared["deployment"]["metadata"].setdefault("annotations", {})
        if cfg.get("icon"):
            annotations[NAMES.key("icon")] = cfg["icon"]
            annotations[NAMES.key("icon-source")] = cfg.get("icon_source", "")
    return IMPORT_JOB.dispatch(body, prepared, context, kget, ksend, OPS, create_pvc, copy_admission)


def hardware_device_paths():
    """The host paths an admin has offered as hardware features."""
    try:
        return {f.get("host_path") for f in HW.features() if f.get("host_path")}
    except Exception:
        return set()


def apply_reviewed_edit(b, prepared, hold=False):
    """Commit an already reviewed edit. Copy setup journals before calling this."""
    persist_icon_config(b)
    if "icon" in b:
        ann = prepared["deployment"]["metadata"].setdefault("annotations", {})
        for key in ("icon", "icon-source"):
            ann.pop(NAMES.key(key), None)
        if b["icon"]:
            ann[NAMES.key("icon")] = b["icon"]
            ann[NAMES.key("icon-source")] = b.get("icon_source", b["icon"])
    result = LC.edit_workload(b, hold=hold, prepared=prepared)
    if "lan" in b:
        lan_message = edit_lan(b["ns"], result.get("name") or b["name"], b.get("lan"))
        if lan_message:
            result["lan"] = lan_message
            if b.get("lan"):
                b["network_mode"] = "lan"
    ports = [port for container in b.get("containers") or [] for port in container.get("ports") or []]
    # The Address step, as in Deploy: how clients reach it, and on which VIP.
    address = b.get("address") if isinstance(b.get("address"), dict) else {}
    vip_mode = address.get("vip_mode") or "shared"
    if b.get("manage_ports") or any("expose" in port for port in ports):
        message = NETWORK.sync_workload_ports(b["ns"], result.get("name") or b["name"], ports,
                                              network_mode=address.get("network_mode") or b.get("network_mode"),
                                              vip_mode={"auto": "automatic"}.get(vip_mode, vip_mode),
                                              vip=address.get("lb_ip", "") if vip_mode == "manual" else "")
        if message:
            result["network"] = message
            _cache.pop("network", None)
    if address.get("network_mode") in ("loadbalancer", "internal"):
        message = NETWORK.set_workload_address(b["ns"], result.get("name") or b["name"], address["network_mode"],
                                               vip_mode, address.get("lb_ip", ""))
        if message:
            result["network"] = "; ".join(filter(None, [result.get("network"), message]))
            _cache.pop("network", None)
    return result


def copy_admission(dep):
    return PLACE.manifest_plan(dep, dep["metadata"]["namespace"], dep["metadata"]["name"], dep["spec"]["replicas"],
        get_app_settings()["thresholds"]["memory"]["critical"], read=kget,
        nodes_snapshot=PLACE.get_nodes(), pod_snapshot=ROLLOUT_CAPACITY.items(kget, "/api/v1/pods"))


def apply_generated_secrets(cfg):
    """Fill catalogue password defaults without trusting a public feed value."""
    generate = {item.get("key") for item in (cfg.get("env_meta") or [])
                if item.get("generate") and item.get("key")}
    if not generate:
        return cfg
    updated = dict(cfg)
    updated["env"] = dict(cfg.get("env") or {})
    for key in generate:
        if not updated["env"].get(key):
            updated["env"][key] = secrets.token_urlsafe(18)
    return updated


def ensure_profile_compatible(cfg):
    profile = cfg.get("app_profile") or {}
    if profile.get("blocked"):
        raise ValueError(profile.get("label") or "this App Store template is not directly compatible")
    return cfg


def _pull_progress(node, image):
    PULLWATCH.sweep()
    return PULLWATCH.progress(node, image)


def _service_listeners(deployment, services):
    """Map a container port onto the Service port that publishes it."""
    selector = (deployment["spec"].get("selector", {}) or {}).get("matchLabels", {}) or {}
    listeners = {}
    for service in services or []:
        chosen = (service.get("spec", {}) or {}).get("selector") or {}
        if not chosen or not all(selector.get(key) == value for key, value in chosen.items()):
            continue
        for port in (service.get("spec", {}) or {}).get("ports", []) or []:
            try:
                target = int(port.get("targetPort", port.get("port")))
                listeners[(str(port.get("protocol") or "TCP").upper(), target)] = int(port["port"])
            except (TypeError, ValueError):
                continue
    return listeners


# The routes server.py's handler answered for this code.
def _helm(request):
    b = request.body
    action = request.path.rsplit("/", 1)[1]
    result = (HELM.install(b) if action == "install" else HELM.upgrade(b) if action == "upgrade"
              else HELM.uninstall(b.get("namespace", ""), b.get("name", "")))
    _cache.pop("helm", None)
    name = result.get("name") or b.get("name", "")
    job = f"helm-{'delete' if action == 'uninstall' else 'install'}-{name}"
    result["operation"] = OPS.start("helm", f"Helm {action} {name}",
                                    {"kind": "HelmChart", "name": name, "namespace": HELM.CONTROLLER_NS},
                                    "/helm", {"namespace": HELM.CONTROLLER_NS, "name": job,
                                              "action": action},
                                    "Waiting for the Helm controller")
    return result


def _delete_workload(request):
    # Only /api/workload/<ns>/<name>: any other path under it is not a route
    # (do_DELETE answers PermissionError 403, in the guard's words for one).
    found = re.fullmatch(r"/api/workload/([a-z0-9.-]+)/([a-z0-9.-]+)", request.path)
    if not found:
        raise PermissionError("this route has no declared authorization policy")
    return {"ok": True, "services": delete_workload(*found.groups())}


ROUTES = {
    ("DELETE", "/api/workload/*"): ("operator", _delete_workload),
    ("POST", "/api/helm/install"): ("admin", _helm),
    ("POST", "/api/helm/upgrade"): ("admin", _helm),
    ("POST", "/api/helm/uninstall"): ("admin", _helm),
}


def persist_icon_config(cfg):
    """Replace a remote logo with a persistent same-origin cache reference."""
    if "icon" not in cfg:
        return cfg
    source = str(cfg.get("icon") or "").strip()
    if not source:
        cfg["icon"] = ""
        cfg["icon_source"] = ""
        return cfg
    cfg["icon"] = ICONS.persist(source, DATA_DIR)
    cfg["icon_source"] = source
    return cfg
