"""Docker Compose files, read into Homestead deployments.

A Compose file describes containers on one Docker host; Homestead runs them on
a Kubernetes cluster. Most of a service carries straight across - image, ports,
environment, command - and the rest has to be translated or left behind:

  * a named volume becomes a new Longhorn claim of the same name;
  * a host folder becomes a new claim too, because a pod may land on any node,
    and its current contents are not copied (Import does that);
  * each service becomes its own workload, and a Service named after it keeps
    the name other services use to reach it working;
  * devices are matched to Homestead's hardware features.

What cannot be carried is said, per service and with the line it came from,
so the file can be fixed in the editor before anything is created. Nothing
here talks to the cluster: the caller passes in what already exists.
"""
import posixpath
import re
import shlex

import homestead_yaml as YAML

# How a service's name resolves for the others, and what a workload may be called.
DNS = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")

# Ports a service listens on when the file does not say, for the images other
# services most often reach by name. Used only when another service refers to
# it and it declares no port of its own; the note says so.
KNOWN_PORTS = {
    "postgres": 5432, "postgis": 5432, "timescaledb": 5432,
    "mariadb": 3306, "mysql": 3306, "redis": 6379, "valkey": 6379, "keydb": 6379,
    "mongo": 27017, "mongodb": 27017, "memcached": 11211, "rabbitmq": 5672,
    "eclipse-mosquitto": 1883, "mosquitto": 1883, "elasticsearch": 9200,
    "opensearch": 9200, "influxdb": 8086, "clickhouse-server": 8123, "nats": 4222,
}

# Keys that only mean something to Docker, listed together rather than one
# warning each.
IGNORED = {
    "labels", "logging", "networks", "extra_hosts", "dns", "dns_search", "dns_opt",
    "hostname", "domainname", "sysctls", "ulimits", "security_opt", "stop_signal",
    "stop_grace_period", "init", "stdin_open", "tty", "pid", "ipc", "platform",
    "pull_policy", "links", "external_links", "cgroup_parent", "cgroup", "oom_score_adj",
    "oom_kill_disable", "read_only", "isolation", "group_add", "mac_address", "userns_mode",
    "device_cgroup_rules", "blkio_config", "cpu_shares", "cpu_quota", "cpu_period",
    "cpuset", "cpu_count", "cpu_percent", "cpu_rt_runtime", "cpu_rt_period", "runtime",
    "annotations", "attach", "develop", "storage_opt", "mem_swappiness", "memswap_limit",
    "cap_drop", "healthcheck", "restart", "container_name", "profiles", "pids_limit",
}
KNOWN = IGNORED | {
    "image", "build", "command", "entrypoint", "working_dir", "user", "privileged",
    "cap_add", "network_mode", "ports", "expose", "environment", "env_file", "volumes",
    "tmpfs", "shm_size", "devices", "deploy", "mem_limit", "mem_reservation", "cpus",
    "depends_on", "secrets", "configs", "extends", "gpus", "scale",
}


class _Report:
    """Messages with the line each one is about."""

    def __init__(self):
        self.errors, self.warnings, self.notes = [], [], []

    def error(self, message, line=0):
        self.errors.append({"line": line, "message": message})

    def warn(self, message, line=0):
        self.warnings.append({"line": line, "message": message})

    def note(self, message, line=0):
        self.notes.append({"line": line, "message": message})


# ------------------------------------------------------------- variables
def parse_variables(text):
    """KEY=VALUE lines, as a .env file writes them."""
    values = {}
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        values[key] = value
    return values


_VARIABLE = re.compile(r"\$\$|\$\{([A-Za-z_][A-Za-z0-9_]*)(?:(:?[-?+])([^}]*))?\}|\$([A-Za-z_][A-Za-z0-9_]*)")


def interpolate(source, values):
    """Fill ${VAR}, ${VAR:-default} and the rest the way Compose does.

    Returns the new text, the variables used, and messages for any that are
    missing. A comment line is left alone, so a commented-out example with an
    unset variable is not an error.
    """
    used, missing, errors = [], [], []
    out = []
    for number, line in enumerate(str(source or "").splitlines(), 1):
        if line.lstrip().startswith("#"):
            out.append(line)
            continue

        def fill(match, number=number):
            if match.group(0) == "$$":
                return "$"
            name = match.group(1) or match.group(4)
            operator, word = match.group(2) or "", match.group(3) or ""
            if name not in used:
                used.append(name)
            value = values.get(name)
            empty = value is None or (operator.startswith(":") and value == "")
            if operator in (":-", "-"):
                return word if (empty if operator == ":-" else value is None) else value
            if operator in (":+", "+"):
                return word if not (empty if operator == ":+" else value is None) else ""
            if operator in (":?", "?"):
                if empty if operator == ":?" else value is None:
                    errors.append({"line": number, "message":
                                   f"${{{name}}} is required: {word or 'it has no value'}"})
                    return ""
                return value
            if value is None:
                missing.append({"name": name, "line": number})    # every use, for its line
                return ""
            return value

        out.append(_VARIABLE.sub(fill, line))
    return "\n".join(out) + ("\n" if str(source or "").endswith("\n") else ""), used, missing, errors


# ---------------------------------------------------------------- helpers
def dns_name(value):
    name = re.sub(r"[^a-z0-9-]+", "-", str(value or "").lower()).strip("-")
    name = re.sub(r"-{2,}", "-", name)[:63].strip("-")
    return name


def _as_list(value):
    if value is None:
        return []
    return list(value) if isinstance(value, list) else [value]


def _words(value, what, report, line):
    if value is None:
        return None
    if isinstance(value, list):
        return [str(x) for x in value]
    try:
        return shlex.split(str(value))
    except ValueError as error:
        report.error(f"{what} could not be split into words: {error}", line)
        return None


_SIZE = re.compile(r"\s*([0-9]+(?:\.[0-9]+)?)\s*([kmgt]?)(i?b?)\s*", re.I)


def memory_mib(value):
    """512m, 1g, 1.5G, 268435456 - as Compose and Docker write memory - in MiB."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return max(1, int(round(value / (1024 * 1024))))
    match = _SIZE.fullmatch(str(value))
    if not match:
        raise ValueError(f"\"{value}\" is not a size like 512m or 2g")
    number, unit = float(match.group(1)), match.group(2).lower()
    scale = {"": 1 / (1024 * 1024), "k": 1 / 1024, "m": 1, "g": 1024, "t": 1024 * 1024}[unit]
    return max(1, int(round(number * scale)))


def cpu_millis(value):
    try:
        cores = float(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError(f"\"{value}\" is not a number of CPUs like 0.5 or 2")
    if cores <= 0:
        raise ValueError("CPUs must be more than zero")
    return f"{max(1, int(round(cores * 1000)))}m"


def _port_numbers(text):
    if "-" in text:
        low, high = text.split("-", 1)
        low, high = int(low), int(high)
        if high < low or high - low > 19:
            raise ValueError(f"port range {text} is backwards or longer than 20 ports")
        return list(range(low, high + 1))
    return [int(text)]


def parse_port(item):
    """One ports: entry as [(container, host, protocol)], and a note if any."""
    if isinstance(item, dict):
        target = item.get("target")
        if target is None:
            raise ValueError("a long-form port needs a target")
        published = item.get("published")
        protocol = str(item.get("protocol") or "tcp").upper()
        containers = _port_numbers(str(target))
        hosts = _port_numbers(str(published)) if published not in (None, "") else containers
        note = "host_ip is not used: the address comes from Homestead" if item.get("host_ip") else ""
    else:
        text = str(item).strip()
        protocol = "TCP"
        if "/" in text:
            text, proto = text.rsplit("/", 1)
            protocol = proto.upper()
        parts = text.rsplit(":", 2) if text.count(":") >= 2 else text.split(":")
        note = ""
        if len(parts) == 3:
            note = f"the {parts[0] or 'bound'} host address is not used: the address comes from Homestead"
            parts = parts[1:]
        if len(parts) == 1:
            containers = _port_numbers(parts[0])
            hosts = containers
        else:
            hosts = _port_numbers(parts[0]) if parts[0] else _port_numbers(parts[1])
            containers = _port_numbers(parts[1])
    if protocol not in ("TCP", "UDP", "SCTP"):
        raise ValueError(f"protocol {protocol.lower()} is not tcp or udp")
    if len(hosts) != len(containers):
        raise ValueError("the host and container port ranges are different lengths")
    for number in hosts + containers:
        if not 1 <= number <= 65535:
            raise ValueError(f"{number} is not a port number")
    return [(c, h, protocol) for c, h in zip(containers, hosts)], note


def _image_base(image):
    return str(image or "").rsplit("/", 1)[-1].split(":", 1)[0].split("@", 1)[0].lower()


def _env(value, variables, report, line, emptied=()):
    """The container's environment, from either the map or the KEY=VALUE list.

    `emptied` holds the lines where a ${VAR} with no value was filled in, so
    "KEY: ${UNSET}" is an empty value (already warned about) rather than a
    KEY: with nothing after it, which Compose takes from its own shell.
    """
    env = {}
    if value is None:
        return env
    if isinstance(value, dict):
        items = [(key, val, YAML.line_of(value, key, line)) for key, val in value.items()]
    else:
        items = []
        for index, entry in enumerate(_as_list(value)):
            key, sep, val = str(entry).partition("=")
            items.append((key, val if sep else None, YAML.line_of(value, index, line)))
    for key, val, where in items:
        key = str(key).strip()
        if not key:
            continue
        if val is None:
            if where in emptied:
                env[key] = ""
            elif key in variables:
                env[key] = variables[key]
            else:
                report.warn(f"{key} takes its value from the shell running Compose; give it one "
                            f"under Variables or it is left out", where)
            continue
        env[key] = ("true" if val else "false") if isinstance(val, bool) else str(val)
    return env


# ---------------------------------------------------------------- volumes
def _split_volume(item):
    """Short or long volume syntax as (kind, source, target, read_only, extra)."""
    if isinstance(item, dict):
        kind = str(item.get("type") or ("volume" if item.get("source") else "volume"))
        source = item.get("source")
        target = item.get("target")
        read_only = bool(item.get("read_only"))
        extra = item.get("tmpfs") or {}
        if kind == "bind" and source is None:
            raise ValueError("a bind mount needs a source")
        return kind, None if source is None else str(source), target, read_only, extra
    text = str(item).strip()
    parts = text.split(":")
    mode = ""
    if len(parts) >= 2 and re.fullmatch(r"(ro|rw|z|Z|cached|delegated|consistent|nocopy)(,[a-zA-Z]+)*", parts[-1]):
        mode = parts.pop()
    if len(parts) == 1:
        return "anonymous", None, parts[0], "ro" in mode.split(","), {}
    if len(parts) != 2:
        raise ValueError(f"\"{text}\" is not source:target[:mode]")
    source, target = parts
    kind = "bind" if source.startswith(("/", ".", "~")) else "volume"
    return kind, source, target, "ro" in mode.split(","), {}


_DROPPED_BINDS = {
    "/etc/localtime": "the node's clock zone does not travel into a pod; set TZ in environment instead",
    "/etc/timezone": "the node's clock zone does not travel into a pod; set TZ in environment instead",
}
_RUNTIME_SOCKETS = ("/var/run/docker.sock", "/run/docker.sock", "/run/containerd/containerd.sock")


def _looks_like_file(path):
    base = posixpath.basename(str(path).rstrip("/"))
    return bool(re.search(r"\.[A-Za-z0-9]{1,6}$", base)) and not base.startswith(".")


# ---------------------------------------------------------------- service
class _Context:
    def __init__(self, doc, namespace, existing_workloads, existing_claims, features, vip_mode):
        self.doc = doc
        self.namespace = namespace
        self.existing_workloads = set(existing_workloads or ())
        self.existing_claims = set(existing_claims or ())
        self.features = list(features or [])
        self.vip_mode = vip_mode
        self.top_volumes = doc.get("volumes") if isinstance(doc.get("volumes"), dict) else {}
        # Which services use each claim, so one used twice becomes shared storage.
        self.claim_users = {}
        # Lines where an unset ${VAR} was filled in as nothing.
        self.emptied = set()


def _feature_for(device, features):
    paths = [p for p in (device.get("host_path"), device.get("container_path")) if p]
    for feature in features:
        own = [p for p in (feature.get("host_path"), feature.get("container_path")) if p]
        if any(dp == fp or dp.startswith(fp.rstrip("/") + "/") or fp.startswith(dp.rstrip("/") + "/")
               for fp in own for dp in paths):
            return feature
    return None


def _service(key, svc, ctx, variables):
    report = _Report()
    line = YAML.line_of(ctx.doc["services"], key)
    name = dns_name(key)
    row = {"name": name, "source_name": key, "line": line}
    if not isinstance(svc, dict):
        report.error(f"service {key} is not a mapping of settings", line)
        return row, report, {}
    at = lambda field: YAML.line_of(svc, field, line)  # noqa: E731

    if not name or not DNS.fullmatch(name):
        report.error(f"{key} cannot be made into a workload name", line)
    elif name != key:
        report.warn(f"runs as \"{name}\": Kubernetes names use lowercase letters, numbers and dashes, "
                     f"so other services must reach it as {name}, not {key}", line)
    if name in ctx.existing_workloads:
        report.error(f"a workload named {name} already exists in {ctx.namespace}", line)

    unknown = sorted(k for k in svc if k not in KNOWN and not str(k).startswith("x-"))
    if unknown:
        report.warn("not recognised, so left out: " + ", ".join(unknown), line)
    ignored = sorted(k for k in svc if k in IGNORED and k not in ("container_name", "restart", "healthcheck"))
    if ignored:
        report.note("Docker-only settings left out: " + ", ".join(ignored), line)
    if svc.get("container_name") and dns_name(svc["container_name"]) != name:
        report.note(f"container_name {svc['container_name']} is not used; the workload is called {name}",
                    at("container_name"))
    if str(svc.get("restart", "")).strip("'\"") == "no":
        report.warn("restart: \"no\" has no equivalent; Kubernetes restarts a container that exits", at("restart"))
    if "healthcheck" in svc:
        report.note("the healthcheck is not carried over; Kubernetes watches the container itself",
                    at("healthcheck"))

    image = svc.get("image")
    if "build" in svc and not image:
        report.error("has build: but no image:. Homestead runs images from a registry and does not build "
                     "them; push the image and set image:", at("build"))
    elif "build" in svc:
        report.note("build: is ignored; the image is pulled from its registry", at("build"))
    elif not image:
        report.error("has no image:", line)
    if "extends" in svc:
        report.error("extends: is not supported; copy the settings into this service", at("extends"))
    for field in ("secrets", "configs"):
        if svc.get(field):
            report.error(f"{field}: are files Compose mounts from this host; they are not carried over. "
                         f"Put the values in environment or on a volume", at(field))
    if svc.get("env_file"):
        report.warn("env_file is a file on the Compose host and is not read; paste it under Variables "
                    "and list what the container needs under environment", at("env_file"))
    if svc.get("gpus") or _nested(svc, "deploy", "resources", "reservations", "devices"):
        report.warn("GPU reservations are not read; choose a hardware feature for this workload instead",
                    at("gpus") if "gpus" in svc else at("deploy"))

    cfg = {
        "name": name, "workload_name": name, "container_name": name,
        "image": str(image or ""), "namespace": ctx.namespace, "replicas": 1,
        "cpu": "50m", "memory": "128Mi", "ports": [], "env": {}, "volumes": [],
        "hardware": [], "template_devices": [], "target_mode": "new",
        "network_mode": "loadbalancer", "vip_mode": ctx.vip_mode, "lb_ip": "",
        "env_bindings": {}, "compose_service": key,
    }

    # What runs.
    entrypoint = _words(svc.get("entrypoint"), "entrypoint", report, at("entrypoint"))
    command = _words(svc.get("command"), "command", report, at("command"))
    if entrypoint:
        cfg["command"] = entrypoint
    if command:
        cfg["args"] = command
    if svc.get("working_dir"):
        cfg["working_dir"] = str(svc["working_dir"])
    if svc.get("user") not in (None, ""):
        user = str(svc["user"])
        match = re.fullmatch(r"(\d+)(?::(\d+))?", user)
        if match:
            cfg["run_as_user"] = int(match.group(1))
            if match.group(2):
                cfg["run_as_group"] = int(match.group(2))
        else:
            report.warn(f"user {user} is a name; only numeric users carry over, such as 1000:1000", at("user"))
    if svc.get("privileged"):
        cfg["privileged"] = True
        report.note("runs privileged, as the file asks", at("privileged"))
    if svc.get("cap_add"):
        cfg["cap_add"] = [str(c).upper().replace("CAP_", "", 1) for c in _as_list(svc["cap_add"])]

    network = str(svc.get("network_mode") or "")
    if network == "host":
        cfg["network_mode"] = "host"
        report.note("uses the node's network, as network_mode: host asks; ports are not published separately",
                    at("network_mode"))
    elif network.startswith(("service:", "container:")):
        report.error(f"network_mode {network} shares another container's network, which Homestead does not "
                     f"build from Compose; add it as a container in that workload from the Deploy page",
                     at("network_mode"))
    elif network == "none":
        report.warn("network_mode: none is not carried over; the workload gets the cluster network",
                    at("network_mode"))

    # Resources: Homestead reserves, it does not cap.
    try:
        reservations = _nested(svc, "deploy", "resources", "reservations") or {}
        limits = _nested(svc, "deploy", "resources", "limits") or {}
        cpus = reservations.get("cpus")
        memory = reservations.get("memory") or svc.get("mem_reservation")
        if cpus not in (None, ""):
            cfg["cpu"] = cpu_millis(cpus)
        if memory not in (None, ""):
            cfg["memory"] = f"{memory_mib(memory)}Mi"
        if limits or svc.get("mem_limit") or svc.get("cpus"):
            report.note("limits are not enforced: Homestead reserves CPU and memory for a workload "
                        "but does not cap it", at("deploy") if limits else at("mem_limit" if "mem_limit" in svc else "cpus"))
        replicas = _nested(svc, "deploy", "replicas")
        if replicas is None:
            replicas = svc.get("scale")
        if replicas is not None:
            cfg["replicas"] = int(replicas)
    except (TypeError, ValueError) as error:
        report.error(str(error), at("deploy"))

    cfg["env"] = _env(svc.get("environment"), variables, report, at("environment"), ctx.emptied)

    # Ports.
    published = svc.get("ports")
    for index, item in enumerate(_as_list(published)):
        where = YAML.line_of(published, index, at("ports"))
        try:
            pairs, note = parse_port(item)
        except (TypeError, ValueError) as error:
            report.error(f"port {item}: {error}", where)
            continue
        if note:
            report.note(note, where)
        for container, host, protocol in pairs:
            cfg["ports"].append({"container": container, "host": host, "protocol": protocol, "expose": True})
    internal = []
    for index, item in enumerate(_as_list(svc.get("expose"))):
        where = YAML.line_of(svc.get("expose"), index, at("expose"))
        try:
            text = str(item)
            protocol = "TCP"
            if "/" in text:
                text, protocol = text.split("/", 1)
            for number in _port_numbers(text):
                internal.append({"container": number, "host": number,
                                 "protocol": protocol.upper(), "expose": True})
        except ValueError as error:
            report.error(f"expose {item}: {error}", where)
    if internal and cfg["ports"]:
        known = {(p["container"], p["protocol"]) for p in cfg["ports"]}
        extra = [p for p in internal if (p["container"], p["protocol"]) not in known]
        if extra:
            report.note("expose: ports share the workload's LAN address with its published ports: "
                        + ", ".join(str(p["container"]) for p in extra), at("expose"))
        cfg["ports"].extend(extra)
    elif internal:
        cfg["ports"] = internal
        cfg["network_mode"] = "internal"
        report.note("reachable inside the cluster only, as expose: without ports: means", at("expose"))
    if cfg["network_mode"] == "host":
        for port in cfg["ports"]:
            port["expose"] = False

    # Storage. A /dev path mounted as a volume is a device, so both lists feed it.
    device_lines = []
    volumes = svc.get("volumes")
    for index, item in enumerate(_as_list(volumes)):
        where = YAML.line_of(volumes, index, at("volumes"))
        try:
            kind, source, target, read_only, extra = _split_volume(item)
        except (TypeError, ValueError) as error:
            report.error(f"volume {item}: {error}", where)
            continue
        if not target or not str(target).startswith("/"):
            report.error(f"volume {item}: the path inside the container must start with /", where)
            continue
        target = str(target)
        if kind == "tmpfs":
            size = memory_mib((extra or {}).get("size")) if (extra or {}).get("size") else 256
            cfg["volumes"].append(_memory_row(target, size))
            continue
        if kind == "anonymous":
            cfg["volumes"].append({"path": target, "source": "", "kind": "ephemeral", "type": "emptyDir",
                                   "read_only": read_only})
            report.note(f"{target} has no source, so it gets temporary pod storage that starts empty", where)
            continue
        if kind == "bind":
            clean = source.rstrip("/") or "/"
            if clean in _RUNTIME_SOCKETS:
                report.error(f"{clean} controls the Docker host's containers. There is no Docker on a "
                             f"Harvester node, and handing a pod the runtime is not something Homestead does",
                             where)
                continue
            if clean in _DROPPED_BINDS:
                report.note(f"{clean} is left out: {_DROPPED_BINDS[clean]}", where)
                if "TZ" not in cfg["env"]:
                    report.warn("set TZ in environment (for example TZ: Europe/London) for the local time zone",
                                where)
                continue
            if clean.startswith("/dev/"):
                cfg["template_devices"].append({"host_path": clean, "container_path": target})
                device_lines.append(where)
                continue
            if clean.startswith(("/proc", "/sys")):
                report.warn(f"{clean} is a view of the Docker host's kernel and is left out", where)
                continue
            claim = dns_name(f"{name}-{posixpath.basename(clean.rstrip('/')) or 'root'}")
            if _looks_like_file(clean) and _looks_like_file(target):
                report.warn(f"{source} looks like a single file. It becomes a folder on a new volume, so the "
                            f"app will not find its file until you put it on the volume", where)
            cfg["volumes"].append(_claim_row(claim, target, read_only, ctx, name, template_source=source))
            report.note(f"{source} becomes new volume {claim}; its current contents are not copied. "
                        f"Import › Container source can bring them across", where)
            continue
        # A named volume.
        declared = ctx.top_volumes.get(source) if isinstance(ctx.top_volumes, dict) else None
        declared = declared if isinstance(declared, dict) else {}
        external = declared.get("external")
        claim = dns_name(declared.get("name") or (external.get("name") if isinstance(external, dict) else "")
                         or source)
        if source not in (ctx.top_volumes or {}):
            report.warn(f"volume {source} is not declared under the top-level volumes:; it is created anyway",
                        where)
        if external:
            if claim not in ctx.existing_claims:
                report.error(f"external volume {claim} does not exist in {ctx.namespace}", where)
                continue
        cfg["volumes"].append(_claim_row(claim, target, read_only, ctx, name))

    for index, item in enumerate(_as_list(svc.get("tmpfs"))):
        where = YAML.line_of(svc.get("tmpfs"), index, at("tmpfs"))
        path, _, options = str(item).partition(":")
        size = 256
        match = re.search(r"size=([0-9.]+[kmgtKMGT]?)", options)
        try:
            if match:
                size = memory_mib(match.group(1))
        except ValueError as error:
            report.error(f"tmpfs {item}: {error}", where)
            continue
        cfg["volumes"].append(_memory_row(path, size))
    if svc.get("shm_size") not in (None, ""):
        try:
            cfg["volumes"].append(_memory_row("/dev/shm", memory_mib(svc["shm_size"])))
        except ValueError as error:
            report.error(f"shm_size: {error}", at("shm_size"))

    # Devices.
    devices = svc.get("devices")
    for index, item in enumerate(_as_list(devices)):
        where = YAML.line_of(devices, index, at("devices"))
        if isinstance(item, dict):
            host, target = item.get("source"), item.get("target") or item.get("source")
        else:
            parts = str(item).split(":")
            host, target = parts[0], parts[1] if len(parts) > 1 else parts[0]
        cfg["template_devices"].append({"host_path": str(host), "container_path": str(target)})
        device_lines.append(where)
    for device, where in zip(cfg["template_devices"], device_lines):
        feature = _feature_for(device, ctx.features)
        if feature:
            if feature["id"] not in cfg["hardware"]:
                cfg["hardware"].append(feature["id"])
            report.note(f"{device['host_path']} uses hardware feature {feature.get('name') or feature['id']}",
                        where)
        else:
            report.error(f"no hardware feature covers {device['host_path']}. Add one under Nodes › Hardware "
                         f"features, then check again", where)

    depends = svc.get("depends_on")
    after = list(depends) if isinstance(depends, (list, dict)) else []
    row.update({"image": cfg["image"], "after": [dns_name(x) for x in after]})
    if isinstance(depends, dict) and any(isinstance(v, dict) and v.get("condition") not in (None, "service_started")
                                         for v in depends.values()):
        report.note("depends_on conditions are not waited for; each workload starts as soon as it can, "
                    "and retries until what it needs is up", at("depends_on"))
    return row, report, cfg


def _nested(obj, *keys):
    for key in keys:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def _memory_row(path, size_mib):
    shm = path.rstrip("/") == "/dev/shm"
    # The picker keeps a RAM disk's size in its size box, in MiB.
    return {"path": path, "source": "", "kind": "shm" if shm else "memory", "type": "emptyDir",
            "medium": "memory", "size_limit": f"{size_mib}Mi", "size_gb": size_mib, "read_only": False}


def _claim_row(claim, target, read_only, ctx, service, template_source=""):
    exists = claim in ctx.existing_claims
    ctx.claim_users.setdefault(claim, set()).add(service)
    row = {"path": target, "source": claim, "kind": "existing" if exists else "new-rwo", "type": "pvc",
           "create": not exists, "size_gb": 5, "access_mode": "ReadWriteOnce", "storage_class": "",
           "read_only": read_only}
    if template_source:
        row["template_source"] = template_source
        row["template_origin"] = "Compose"
    return row


# ------------------------------------------------------------------ file
def convert(source, variables_text="", namespace="lab", existing_workloads=(), existing_claims=(),
            features=(), vip_mode="shared"):
    """Read a Compose file into one deploy configuration per service.

    Returns a report: file-level errors and warnings, each service with its
    configuration and its own messages, and the order to create them in.
    """
    variables = parse_variables(variables_text)
    result = {"ok": False, "errors": [], "warnings": [], "services": [], "order": [],
              "variables": {"used": [], "missing": []}, "project": ""}
    text, used, missing, errors = interpolate(source, variables)
    result["variables"] = {"used": used, "missing": list(dict.fromkeys(m["name"] for m in missing))}
    result["errors"].extend(errors)
    for item in missing:
        result["warnings"].append({"line": item["line"], "message":
                                   f"${{{item['name']}}} has no value, so it is empty; set it under Variables"})
    try:
        doc = YAML.loads(text)
    except YAML.YamlError as error:
        result["errors"].append({"line": error.line, "message": error.message})
        return result
    if not isinstance(doc, dict) or not isinstance(doc.get("services"), dict) or not doc["services"]:
        result["errors"].append({"line": getattr(doc, "line", 1) or 1,
                                 "message": "a Compose file needs a services: mapping with at least one service"})
        return result
    result["project"] = str(doc.get("name") or "")
    for key in ("secrets", "configs"):
        if doc.get(key):
            result["warnings"].append({"line": YAML.line_of(doc, key), "message":
                                       f"top-level {key}: are not carried over"})
    if doc.get("networks"):
        result["warnings"].append({"line": YAML.line_of(doc, "networks"), "message":
                                   "networks: are not carried over; every workload in a namespace can reach "
                                   "the others by name"})

    ctx = _Context(doc, namespace, existing_workloads, existing_claims, features, vip_mode)
    ctx.emptied = {item["line"] for item in missing}
    rows, configs = [], {}
    for key, svc in doc["services"].items():
        row, report, cfg = _service(str(key), svc, ctx, variables)
        row["errors"], row["warnings"], row["notes"] = report.errors, report.warnings, report.notes
        rows.append(row)
        configs[row["name"]] = cfg

    _check_together(rows, configs, ctx)
    for row in rows:
        cfg = configs.get(row["name"]) or {}
        row["config"] = cfg
        row["summary"] = _summary(cfg)
        cfg["app_profile"] = {"label": "Imported from Docker Compose", "level": "review", "intent": "compose",
                              "notes": [m["message"] for m in row["warnings"] + row["notes"]][:12]}
    result["services"] = rows
    result["order"] = _order(rows, result)
    result["ok"] = not result["errors"] and not any(row["errors"] for row in rows)
    return result


def _check_together(rows, configs, ctx):
    """What only shows with every service in view: names, shared volumes, who reaches whom."""
    by_row = {row["name"]: row for row in rows}
    seen = {}
    for row in rows:
        if row["name"] in seen and row["name"]:
            row["errors"].append({"line": row["line"], "message":
                                  f"{row['source_name']} and {seen[row['name']]} both become {row['name']}"})
        seen[row["name"]] = row["source_name"]

    # A claim several services mount has to be one they can share.
    for claim, users in ctx.claim_users.items():
        if len(users) < 2:
            continue
        for name in users:
            for volume in configs[name]["volumes"]:
                if volume.get("source") == claim and volume.get("create"):
                    volume.update({"kind": "new-rwx", "access_mode": "ReadWriteMany"})
            by_row[name]["notes"].append({"line": by_row[name]["line"], "message":
                                          f"{claim} is shared with {', '.join(sorted(users - {name}))}, "
                                          f"so it is created as shared (RWX) storage"})

    # The same port on the shared address cannot be two services.
    if ctx.vip_mode == "shared":
        taken = {}
        for row in rows:
            cfg = configs[row["name"]]
            if cfg.get("network_mode") != "loadbalancer":
                continue
            for port in cfg["ports"]:
                key = (port["host"], port["protocol"])
                if key in taken and taken[key] != row["name"]:
                    row["errors"].append({"line": row["line"], "message":
                                          f"port {port['host']}/{port['protocol'].lower()} is also used by "
                                          f"{taken[key]} on the shared address. Change one, or choose "
                                          f"\"A new address per service\""})
                taken.setdefault(key, row["name"])

    # Another service reaching this one by name needs a Service to find it.
    text_of = {row["name"]: " ".join([*map(str, configs[row["name"]].get("env", {}).values()),
                                      *configs[row["name"]].get("command", []),
                                      *configs[row["name"]].get("args", [])])
               for row in rows}
    for row in rows:
        cfg = configs[row["name"]]
        if cfg.get("ports") or cfg.get("network_mode") == "host":
            continue
        names = {row["name"], row["source_name"]}
        callers = sorted(other["name"] for other in rows if other is not row and (
            row["name"] in other.get("after", []) or
            any(re.search(rf"(^|[^A-Za-z0-9_.-]){re.escape(n)}([^A-Za-z0-9_-]|$)", text_of[other["name"]])
                for n in names)))
        if not callers:
            continue
        port = KNOWN_PORTS.get(_image_base(cfg.get("image")))
        if port:
            cfg["ports"] = [{"container": port, "host": port, "protocol": "TCP", "expose": True}]
            cfg["network_mode"] = "internal"
            row["notes"].append({"line": row["line"], "message":
                                 f"{', '.join(callers)} reach{'es' if len(callers) == 1 else ''} it by name, so it "
                                 f"gets an address inside the cluster on port {port}, its usual port"})
        else:
            row["warnings"].append({"line": row["line"], "message":
                                    f"{', '.join(callers)} may reach it by name, but it declares no port. Add "
                                    f"expose: with the port it listens on, so it gets an address"})


def _order(rows, result):
    """Create what others depend on first; a cycle is reported, not looped on."""
    names = [row["name"] for row in rows]
    after = {row["name"]: [a for a in row.get("after", []) if a in names] for row in rows}
    for row in rows:
        for missing in set(row.get("after", [])) - set(names):
            row["errors"].append({"line": row["line"], "message": f"depends on {missing}, which is not in this file"})
    order, state = [], {}

    def visit(name, trail):
        if state.get(name) == "done":
            return
        if state.get(name) == "visiting":
            result["errors"].append({"line": 0, "message": "depends_on goes round in a circle: "
                                     + " → ".join(trail + [name])})
            return
        state[name] = "visiting"
        for dependency in after[name]:
            visit(dependency, trail + [name])
        state[name] = "done"
        order.append(name)

    for name in names:
        visit(name, [])
    return order


def _summary(cfg):
    return {
        "ports": [f"{p['host']}→{p['container']}/{p['protocol'].lower()}" for p in cfg.get("ports", [])],
        "lan": cfg.get("network_mode") == "loadbalancer" and any(p.get("expose") for p in cfg.get("ports", [])),
        "network": cfg.get("network_mode"),
        "env": len(cfg.get("env", {})),
        "volumes": [{"path": v["path"], "kind": v["kind"], "source": v.get("source", ""),
                     "template_source": v.get("template_source", "")} for v in cfg.get("volumes", [])],
        "hardware": list(cfg.get("hardware", [])),
        "command": " ".join(cfg.get("command", []) + cfg.get("args", [])),
    }
