"""Deploy: the Kubernetes objects an app becomes.

What a deploy config implies (analyze_deploy_intent), the Deployment and
Service for a new app, one with extra containers, a container added to an
existing app, and the tier a new app starts in. Moved unchanged from
homestead_deploy (#623): read and replace these names here. Names server.py
binds on homestead_deploy are read through it (D.x), so a replacement there
still applies.
"""
import json
import re

import homestead_failover as FAILOVER
import homestead_hardware as HW
import homestead_host_access as HOSTACCESS
import homestead_lan as LAN
import homestead_workload_edit as WORKLOAD_EDIT
import homestead_memory as MEMORY
import homestead_names as NAMES
import homestead_vip_registry as REG
import homestead_platform as PLATFORM
import homestead_privileges as PRIV
import homestead_update_registry as REGISTRY
import homestead_uptime as UPTIME
import homestead_volowner as VOLOWNER


def analyze_deploy_intent(cfg):
    """Derive portable Kubernetes guidance from template semantics, never app names."""
    updated = dict(cfg)
    updated["ports"] = [dict(item) for item in (cfg.get("ports") or [])]
    updated["env_bindings"] = dict(cfg.get("env_bindings") or {})
    notes, dependencies, intents = [], [], []
    exposed = [item for item in updated["ports"] if item.get("expose", True)]
    dns = any(int(item.get("container") or 0) == 53 for item in exposed)
    if dns and updated.get("network_mode") != "host":
        updated["network_mode"], updated["vip_mode"] = "loadbalancer", "auto"
        intents.append("network")
        notes.append("A dedicated automatic VIP is selected because this template exposes DNS port 53.")
        bind_names = {"SERVERIP", "SERVER_IP", "LOCAL_IPV4", "FTLCONF_LOCAL_IPV4"}
        for key in (updated.get("env") or {}):
            if re.sub(r"[^A-Z0-9_]", "", key.upper()) in bind_names:
                updated["env_bindings"][key] = "vip"
        if updated["env_bindings"]:
            notes.append("Address variables are filled from the allocated VIP at deploy time.")
    for port in updated["ports"]:
        if int(port.get("container") or 0) == 67 and str(port.get("protocol") or "TCP").upper() == "UDP":
            port["expose"], port["required"] = False, False
            port["description"] = "Optional: expose only when this workload provides DHCP"
            if "network" not in intents:
                intents.append("network")
            notes.append("DHCP port 67 stays disabled unless you explicitly expose it.")

    volumes = updated.get("volumes") or []

    def inferred_role(item):
        if item.get("role"):
            return item["role"]
        path = str(item.get("path") or "").rstrip("/").lower() or "/"
        context = " ".join(str(item.get(key) or "") for key in
                           ("path", "source", "template_source", "label", "description")).lower()
        tokens = set(re.split(r"[^a-z0-9]+", context))
        if path in ("/etc/localtime", "/var/run/docker.sock", "/run/containerd/containerd.sock"):
            return "system"
        if tokens & {"cache", "caches", "transcode", "transcoding", "temp", "temporary", "tmp"}:
            return "cache"
        if path == "/config" or path.endswith("/config") or "appdata" in context:
            return "config"
        if tokens & {"media", "movie", "movies", "tv", "music", "photo", "photos", "video",
                     "videos", "recording", "recordings", "download", "downloads"}:
            return "media"
        if path == "/data" or path.endswith("/data"):
            return "data"
        return "config"

    media = [item.get("path") for item in volumes if inferred_role(item) == "media"]
    cache = [item.get("path") for item in volumes if inferred_role(item) == "cache"]
    data_paths = [item.get("path") for item in volumes if inferred_role(item) == "data"]
    if media:
        intents.append("storage")
        notes.append("Choose existing/shared media storage for: " + ", ".join(filter(None, media)) + ".")
    if cache:
        if "storage" not in intents:
            intents.append("storage")
        notes.append("Temporary pod storage is selected for cache/transcode paths: " + ", ".join(filter(None, cache)) + ".")
    if data_paths:
        if "storage" not in intents:
            intents.append("storage")
        notes.append("Persistent data paths start as editable Longhorn claims; choose RWX when multiple replicas or workloads must attach: " + ", ".join(filter(None, data_paths)) + ".")
    if any(item.get("access_mode") == "ReadWriteMany" for item in volumes):
        if "storage" not in intents:
            intents.append("storage")
        notes.append("Template wording indicates shared storage; review the proposed RWX claim and size.")

    generated = [item.get("key") for item in (updated.get("env_meta") or []) if item.get("generate")]
    if generated:
        intents.append("security")
        notes.append("Public defaults for secret fields are discarded and generated locally.")
    option_fields = [item.get("key") for item in (updated.get("env_meta") or []) if item.get("options")]
    if option_fields:
        notes.append("Enumerated template values are presented as selectors instead of literal option strings.")

    env_keys = {re.sub(r"[^A-Z0-9_]", "", key.upper()) for key in (updated.get("env") or {})}
    if any(key.endswith(("DB_HOST", "DATABASE_HOST", "MYSQL_HOST", "POSTGRES_HOST")) for key in env_keys):
        dependencies.append({"kind": "database", "name": "External database endpoint",
                             "required": True, "managed": False})
    if any(key.endswith(("REDIS_HOST", "CACHE_HOST")) for key in env_keys):
        dependencies.append({"kind": "cache", "name": "External cache endpoint",
                             "required": True, "managed": False})

    runtime_socket = next((item for item in volumes
                           if item.get("path") in ("/var/run/docker.sock", "/run/containerd/containerd.sock")), None)
    blocked = bool(runtime_socket)
    if blocked:
        intents.append("safety")
        dependencies.append({"kind": "runtime", "name": "Host container-runtime control",
                             "required": True, "managed": False})
        notes.append("This template requests a host container-runtime socket and can create or control other containers; it needs a Kubernetes-specific deployment design.")
    if dependencies:
        intents.append("dependency")
        notes.append("Review the external services listed below before deployment.")
    if updated.get("template_devices"):
        intents.append("hardware")
        notes.append("Imported device paths are matched to reusable hardware features; verify eligible hosts before deploying.")
    network = str(updated.get("template_network") or "bridge").lower()
    if network == "host":
        intents.append("network")
        notes.append("The source requests host networking; review node port collisions and failover, or switch to a Service VIP.")
    elif network not in ("bridge", "default", "") and not dns:
        intents.append("network")
        notes.append("The source custom network is represented by a dedicated Kubernetes VIP.")
    if not notes:
        notes.append("Review the imported ports, variables, and storage choices before deploying.")

    intents = list(dict.fromkeys(intents)) or ["template"]
    level = "dependency" if blocked or dependencies else "guided" if dns else "review"
    label = ("Needs Kubernetes design" if blocked else "Dependency review" if dependencies else
             "Dedicated VIP" if dns else "Storage review" if "storage" in intents else "Template review")
    updated["app_profile"] = {"intent": intents[0], "intents": intents, "level": level,
                              "label": label, "notes": notes, "dependencies": dependencies,
                              "blocked": blocked}
    return updated


def _build_single_deployment(cfg):
    name = D._dns_name(cfg.get("workload_name") or cfg.get("name"), "workload name")
    container_name = D._dns_name(cfg.get("container_name") or cfg.get("name"), "container name")
    ns = cfg.get("namespace", D.DEFAULT_NS)
    env = [{"name": k, "value": str(v)} for k, v in (cfg.get("env") or {}).items()]
    mounts, volumes, named, owned = [], [], {}, []
    for i, v in enumerate(cfg.get("volumes") or []):
        if v.get("type") == "pod":
            raise ValueError("existing pod volumes can only be used when joining an existing workload")
        # Several folders of one claim share a single volume entry and differ
        # only by subPath, which is how an import lands one volume per app.
        key = (v.get("type") or "pvc", v.get("source") or "")
        reusable = key[0] in ("pvc", "host") and key[1]
        vn = named.get(key) if reusable else None
        if not vn:
            vn = f"vol{len(volumes)}"
            if reusable:
                named[key] = vn
            if v.get("type") == "host":
                volumes.append({"name": vn, "hostPath": {"path": v["source"]}})
            elif v.get("type") == "emptyDir":
                # A RAM-backed scratch volume is what a tmpfs mount becomes:
                # same speed, same volatility, and bounded so it cannot eat the
                # node's memory.
                empty = {}
                if str(v.get("medium", "")).lower() == "memory":
                    empty["medium"] = "Memory"
                if v.get("size_limit"):
                    empty["sizeLimit"] = str(v["size_limit"])
                volumes.append({"name": vn, "emptyDir": empty})
            else:
                volumes.append({"name": vn, "persistentVolumeClaim": {"claimName": v["source"]}})
        mount = {"name": vn, "mountPath": v["path"]}
        owner = (cfg.get("volume_owners") or {}).get(v.get("path"))
        if owner and v.get("type") == "pvc" and v.get("create"):
            owned.append((vn, str(v.get("sub_path") or ""), *owner))
        if v.get("sub_path"):
            mount["subPath"] = str(v["sub_path"]).strip("/")
        if v.get("read_only"):
            mount["readOnly"] = True
        mounts.append(mount)
    ports = [{"containerPort": int(p["container"]),
              "name": (p.get("name") or f"p{p['container']}-{str(p.get('protocol', 'TCP')).lower()}")[:15],
              "protocol": str(p.get("protocol", "TCP")).upper()}
             for p in cfg.get("ports") or []]
    c = {"name": container_name, "image": REGISTRY.with_tag(cfg["image"]), "imagePullPolicy": "IfNotPresent"}
    # Arguments to the image's own entrypoint - cloudflared's "tunnel run",
    # say. A list of plain strings, passed as they are; never a shell.
    args = cfg.get("args") or []
    if not isinstance(args, list) or len(args) > 32 or any(not isinstance(a, str) or len(a) > 512 or "\x00" in a for a in args):
        raise ValueError("arguments are a list of up to 32 short strings")
    if args: c["args"] = list(args)
    if env: c["env"] = env
    if ports: c["ports"] = ports
    if mounts: c["volumeMounts"] = mounts
    res = {}
    if cfg.get("cpu"): res.setdefault("requests", {})["cpu"] = cfg["cpu"]
    memory_request = str(cfg.get("memory") or "").strip()
    memory_limit = str(cfg.get("memory_limit") or "").strip()
    MEMORY.validate(memory_request, memory_limit, container_name)
    if memory_request: res.setdefault("requests", {})["memory"] = memory_request
    if memory_limit: res.setdefault("limits", {})["memory"] = memory_limit
    if res: c["resources"] = res
    if cfg.get("privileged"): c["securityContext"] = {"privileged": True}
    apply_container_settings(c, cfg)

    podspec = {"containers": [c], "automountServiceAccountToken": False}
    if volumes: podspec["volumes"] = volumes
    if owned:
        # A new volume is root's; the image may run as a user of its own.
        podspec["initContainers"] = [VOLOWNER.init_container(owned)]
    hardware = set(cfg.get("hardware") or [])
    if cfg.get("gpu"):
        hardware.add("igpu")
    devices = {f["id"]: HW.mount_spec(f) for f in HW.features()}
    unknown = hardware - set(devices)
    if unknown:
        raise ValueError("unknown hardware feature(s): " + ", ".join(sorted(unknown)))
    for hw in hardware:
        if hw not in devices:
            continue
        dev = devices[hw]
        podspec.setdefault("nodeSelector", {})[dev["label"]] = "true"
        podspec["containers"][0].setdefault("securityContext", {})["privileged"] = True
        podspec["containers"][0].setdefault("volumeMounts", []).append(
            {"name": dev["name"], "mountPath": dev["container_path"]})
        podspec.setdefault("volumes", []).append(
            {"name": dev["name"], "hostPath": {"path": dev["host_path"], "type": dev["path_type"]}})
    if any(key in cfg for key in ("privileged", "cap_add", "tun")):
        PRIV.apply(podspec["containers"][0], podspec, cfg, hardware=bool(hardware))
    if cfg.get("fs_group") is not None:
        # The kubelet gives the volume to this group and makes it group
        # writable, which is what lets a container that is not root write to
        # appdata it did not create.
        podspec.setdefault("securityContext", {})["fsGroup"] = int(cfg["fs_group"])
    if cfg.get("network_mode") == "host":
        podspec["hostNetwork"] = True
        podspec["dnsPolicy"] = "ClusterFirstWithHostNet"
    FAILOVER.apply(podspec, cfg.get("failover") or "move")
    lan_address = cfg.get("lan") if cfg.get("network_mode") == "lan" else None
    # How it is monitored, if not automatically: an annotation, read by homestead_uptime.
    monitoring = UPTIME.check_setting((cfg.get("monitoring") or {}).get("mode") or "auto",
                                      (cfg.get("monitoring") or {}).get("path"))
    dep = {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": name, "namespace": ns, "labels": {"app": name, NAMES.key("managed"): "true"},
                     "annotations": ({**({NAMES.key("icon"): cfg.get("icon", "")} if cfg.get("icon") else {}),
                                      **({NAMES.key("icon-source"): cfg.get("icon_source", cfg.get("icon", ""))} if cfg.get("icon") else {}),
                                      **({NAMES.key("hardware"): ",".join(sorted(hardware))} if hardware else {}),
                                      **({NAMES.key("uptime"): monitoring} if monitoring else {})})},
        "spec": {"replicas": int(cfg.get("replicas", 1)), "strategy": {"type": "Recreate"},
                 "selector": {"matchLabels": {"app": name}},
                 "template": {"metadata": {"labels": {"app": name, "lab-workload": "true"}}, "spec": podspec}},
    }
    svc = None
    exposed = [p for p in cfg.get("ports") or [] if p.get("expose")]
    if exposed and cfg.get("network_mode") not in ("host", "lan"):
        mode = cfg.get("vip_mode", "shared")
        vip = cfg.get("lb_ip") if mode in ("manual", "automatic") else ((cfg.get("lb_ip") or REG.shared_vip()) if mode == "shared" else "")
        svc_type = "ClusterIP" if cfg.get("network_mode") == "internal" else "LoadBalancer"
        svc = {
            "apiVersion": "v1", "kind": "Service",
            "metadata": {"name": name, "namespace": ns, "labels": {"app": name, NAMES.key("managed"): "true"},
                         "annotations": PLATFORM.vip_annotations(vip) if svc_type == "LoadBalancer" else {}},
            "spec": {"type": svc_type, "selector": {"app": name},
                     **(PLATFORM.vip_spec(vip) if svc_type == "LoadBalancer" else {}),
                      "ports": [{"name": (p.get("name") or f"p{p['container']}-{str(p.get('protocol', 'TCP')).lower()}")[:15],
                                 "port": int(p.get("host") or p["container"]),
                                 "targetPort": int(p["container"]),
                                 "protocol": str(p.get("protocol", "TCP")).upper()} for p in exposed]},
        }
    if lan_address:
        LAN.apply_to_template(dep, ns, name, lan_address)
    return dep, svc


def deploy_tier(cfg):
    """The tier a new app is deployed at (#431): "critical" or "low", or "" for
    normal, which writes no class at all."""
    tier = cfg.get("priority")
    if tier in (None, "", "normal"):
        return ""
    if tier not in ("critical", "low"):
        raise ValueError("the tier must be critical, normal or low")
    return tier


def build_deployment(cfg):
    tier = deploy_tier(cfg)
    dep, service = _build_deployment_with_extras(cfg)
    if tier:
        WORKLOAD_EDIT._apply_priority(dep["spec"]["template"]["spec"], tier)
    # Its hosts, spread and neighbours, written as Edit writes them (#844).
    # Only what was chosen: an empty host would still stamp the pods as moved.
    WORKLOAD_EDIT.apply_placement(dep, {key: cfg[key] for key in ("placement", "node") if cfg.get(key)})
    return dep, service


def _build_deployment_with_extras(cfg):
    extras = D.additional_container_configs(cfg)
    if not extras:
        return _build_single_deployment(cfg)
    HOSTACCESS.require_cfg([cfg, *extras])
    dep, _ = _build_single_deployment(cfg)
    changes = [dict(item, new=True, name=item["container_name"], privileges={
        key: item[key] for key in ("privileged", "cap_add", "tun") if key in item}) for item in extras]
    prepared = WORKLOAD_EDIT.prepare_edit({"ns": dep["metadata"]["namespace"], "name": dep["metadata"]["name"],
                                "containers": changes}, current=dep)
    dep = prepared["deployment"]
    pspec = dep["spec"]["template"]["spec"]
    used_names = {container["name"] for group in ("containers", "initContainers") for container in pspec.get(group) or []}
    for item, container in zip(extras, pspec["containers"][1:]):
        mounts = {mount["mountPath"]: mount for mount in container.get("volumeMounts") or []}
        owners = [(mounts[path]["name"], mounts[path].get("subPath", ""), *owner)
                  for path, owner in (item.get("volume_owners") or {}).items() if path in mounts]
        if owners:
            helper = VOLOWNER.init_container(owners)
            helper["name"] = WORKLOAD_EDIT._unique_volume_name("hs-owner-" + container["name"], used_names)
            pspec.setdefault("initContainers", []).append(helper)
    all_ports = [port for item in [cfg, *extras] for port in item.get("ports") or []]
    service_ports = [dict(port, name=port.get("name") or f"p{port.get('host') or port['container']}-{str(port.get('protocol') or 'TCP').lower()}") for port in all_ports]
    _, service = _build_single_deployment(dict(cfg, ports=service_ports))
    if service:
        names = [port["name"] for port in service["spec"]["ports"]]
        if len(names) != len(set(names)):
            raise ValueError("exposed port names must be unique across all containers")
    return dep, service


def apply_container_settings(container, cfg):
    """Command, working directory, user and capabilities, when a config has them.

    Compose's entrypoint and command are Kubernetes' command and args: one
    replaces the image's ENTRYPOINT, the other its CMD.
    """
    for key, field in (("command", "command"), ("args", "args")):
        value = cfg.get(key)
        if value:
            if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
                raise ValueError(f"{key} must be a list of words")
            container[field] = list(value)
    if cfg.get("working_dir"):
        container["workingDir"] = str(cfg["working_dir"])
    security = container.setdefault("securityContext", {})
    for key, field in (("run_as_user", "runAsUser"), ("run_as_group", "runAsGroup")):
        if cfg.get(key) is not None and cfg.get(key) != "":
            security[field] = int(cfg[key])
    if cfg.get("cap_add"):
        caps = [str(x).upper() for x in cfg["cap_add"]]
        if not all(re.fullmatch(r"[A-Z_]{2,40}", cap) for cap in caps):
            raise ValueError("capabilities are names like NET_ADMIN")
        security.setdefault("capabilities", {})["add"] = caps
    if not security:
        container.pop("securityContext", None)
    return container


def _unique_volume_name(base, used):
    base = re.sub(r"[^a-z0-9-]", "-", base.lower()).strip("-")[:55] or "volume"
    candidate, suffix = base, 2
    while candidate in used:
        tail = f"-{suffix}"
        candidate = base[:63 - len(tail)].rstrip("-") + tail
        suffix += 1
    used.add(candidate)
    return candidate


def build_sidecar_deployment(cfg, current):
    HOSTACCESS.require_target(current, cfg.get("namespace") or D.DEFAULT_NS)
    """Add one container to an existing Deployment pod template.

    Kubernetes cannot modify a running Pod. Updating the controller template causes a
    reviewed rollout, so all containers in the workload restart together.
    """
    container_name = D._dns_name(cfg.get("container_name") or cfg.get("name"), "container name")
    target = D._dns_name(cfg.get("target_workload"), "existing workload")
    if current.get("metadata", {}).get("name") != target:
        raise ValueError("existing workload does not match the selected Deployment")
    if cfg.get("network_mode") == "host":
        raise ValueError("host networking cannot be enabled while joining an existing workload")

    updated = json.loads(json.dumps(current))
    pspec = updated.setdefault("spec", {}).setdefault("template", {}).setdefault("spec", {})
    containers = pspec.setdefault("containers", [])
    if any(c.get("name") == container_name for c in containers):
        raise ValueError(f"container {container_name} already exists in {target}")

    env = [{"name": k, "value": str(v)} for k, v in (cfg.get("env") or {}).items()]
    ports = [{"containerPort": int(p["container"]),
              "name": (p.get("name") or f"p{p['container']}-{str(p.get('protocol', 'TCP')).lower()}")[:15],
              "protocol": str(p.get("protocol", "TCP")).upper()}
             for p in cfg.get("ports") or [] if p.get("container")]
    container = {"name": container_name, "image": REGISTRY.with_tag(cfg["image"]), "imagePullPolicy": "IfNotPresent"}
    if env:
        container["env"] = env
    if ports:
        container["ports"] = ports
    resources = {}
    if cfg.get("cpu"):
        resources.setdefault("requests", {})["cpu"] = cfg["cpu"]
    memory_request = str(cfg.get("memory") or "").strip()
    memory_limit = str(cfg.get("memory_limit") or "").strip()
    MEMORY.validate(memory_request, memory_limit, container_name)
    if memory_request:
        resources.setdefault("requests", {})["memory"] = memory_request
    if memory_limit:
        resources.setdefault("limits", {})["memory"] = memory_limit
    if resources:
        container["resources"] = resources
    if cfg.get("privileged"):
        container["securityContext"] = {"privileged": True}

    pod_volumes = pspec.setdefault("volumes", [])
    existing_by_name = {v.get("name"): v for v in pod_volumes}
    used = set(existing_by_name)
    mounts = []
    for index, item in enumerate(cfg.get("volumes") or []):
        mount_path = (item.get("path") or "").strip()
        if not mount_path:
            continue
        if item.get("type") == "pod":
            volume_name = item.get("source", "")
            if volume_name not in existing_by_name:
                raise ValueError(f"pod volume {volume_name or '(blank)'} does not exist in {target}")
        else:
            source = (item.get("source") or "").strip()
            if not source and item.get("type") != "emptyDir":
                raise ValueError(f"storage source is required for {mount_path}")
            volume_name = ""
            if item.get("type") not in ("host", "emptyDir"):
                volume_name = next((v.get("name") for v in pod_volumes
                                    if v.get("persistentVolumeClaim", {}).get("claimName") == source), "")
            if not volume_name:
                volume_name = _unique_volume_name(f"hs-{container_name}-{index + 1}", used)
                if item.get("type") == "host":
                    pod_volumes.append({"name": volume_name, "hostPath": {"path": source}})
                elif item.get("type") == "emptyDir":
                    pod_volumes.append({"name": volume_name, "emptyDir": {}})
                else:
                    pod_volumes.append({"name": volume_name,
                                        "persistentVolumeClaim": {"claimName": source}})
        mount = {"name": volume_name, "mountPath": mount_path}
        if item.get("sub_path") and item.get("type") != "emptyDir":
            mount["subPath"] = str(item["sub_path"]).strip("/")
        if item.get("read_only"):
            mount["readOnly"] = True
        mounts.append(mount)

    hardware = set(cfg.get("hardware") or [])
    if cfg.get("gpu"):
        hardware.add("igpu")
    devices = {feature["id"]: HW.mount_spec(feature) for feature in HW.features()}
    unknown = hardware - set(devices)
    if unknown:
        raise ValueError("unknown hardware feature(s): " + ", ".join(sorted(unknown)))
    for feature_id in hardware:
        device = devices[feature_id]
        pspec.setdefault("nodeSelector", {})[device["label"]] = "true"
        container.setdefault("securityContext", {})["privileged"] = True
        volume_name = next((v.get("name") for v in pod_volumes
                            if v.get("hostPath", {}).get("path") == device["host_path"]), "")
        if not volume_name:
            volume_name = _unique_volume_name(device["name"], used)
            pod_volumes.append({"name": volume_name, "hostPath": {
                "path": device["host_path"], "type": device["path_type"]}})
        mounts.append({"name": volume_name, "mountPath": device["container_path"]})
    if mounts:
        container["volumeMounts"] = mounts
    containers.append(container)

    selector = current.get("spec", {}).get("selector", {}).get("matchLabels", {})
    service_cfg = dict(cfg)
    service_cfg["name"] = container_name
    # Only the Service portion is needed here. Pod storage and hardware have
    # already been merged above and pod-volume references are join-only.
    service_cfg["volumes"] = []
    service_cfg["hardware"] = []
    service_cfg["gpu"] = False
    service_cfg.pop("priority", None)   # a joined app keeps the tier it has
    for key in ("placement", "node"):   # and the placement it has (#844)
        service_cfg.pop(key, None)
    _, service = build_deployment(service_cfg)
    if service:
        service["spec"]["selector"] = selector
    annotation_name = ("sidecar-" + container_name)[:63].rstrip("-")
    updated.setdefault("metadata", {}).setdefault("annotations", {})[
        NAMES.key(annotation_name)] = cfg.get("image", "")
    return updated, service


# Imported last, so this module can be imported before or after homestead_deploy:
# both only look these names up when a function runs.
import homestead_deploy as D  # noqa: E402
