"""A k3s cluster made of VMs on this one - to try k3s, an app, or Homestead
itself on a cluster of its own, without spare machines.

Each VM gets an address of its own on a VM network bridged to the LAN, so
the cluster is reached - and joins - the way one built from real machines
would be. The first VM is the server; the rest join it with a token made
here, before anything starts, so no step has to read it back off a VM. What
each runs comes from cloud-init: Homestead's bootstrap script, as a green-
field install would use, or k3s's own installer for a bare cluster. Asked for
KubeVirt, the nodes get the host's CPU as it is, so VMs can run inside them
where the host allows nested virtualisation, and emulated where it does not.

The build is a job: the VMs start, k3s answers on the server's address,
and - when it was asked for - the Homestead inside answers on port 8088.
"""
import copy
import ipaddress
import json
import re
import secrets
import socket
import time
import urllib.parse

BOOTSTRAP = "https://raw.githubusercontent.com/wjcloudy/homestead/main/scripts/bootstrap-k3s.sh"
K3S = "https://get.k3s.io"
UBUNTU = "https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img"
LABEL = "homestead.io/k3s-cluster"
ROLE = "homestead.io/k3s-role"
LOG = "/var/log/homestead-k3s.log"
SETUPS = {
    "homestead": "k3s, Longhorn and Homestead - what a new install gets",
    "local": "k3s and Homestead, on k3s's own local-path storage",
    "k3s": "k3s alone",
}
START_LIMIT = 45 * 60

kget = None
create = None       # makes one VM, with its address checked and recorded
check = None        # why an address cannot be a VM's, or ""
remove = None       # legacy binding only; failed batches must never call it


def bind(_kget, _create, _check, _remove=None):
    global kget, create, check, remove
    kget, create, check, remove = _kget, _create, _check, _remove


def review(cfg):
    """The plan, with anything already at each address named - before a
    single VM is made."""
    built = plan(cfg)
    for node in built["nodes"]:
        node["problem"] = check(node["address"]) if check else ""
    built["ok"] = not any(node["problem"] for node in built["nodes"])
    return built


def _answers(ip, port, timeout=2.0):
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def plan(cfg):
    """The VMs this makes, each with its role and address, or why it cannot."""
    prefix = str(cfg.get("name") or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,26}[a-z0-9])?", prefix):
        raise ValueError("the cluster's name is lowercase letters, numbers and dashes, up to 28")
    servers, agents = int(cfg.get("servers") or 1), int(cfg.get("agents") or 0)
    if servers not in (1, 3):
        raise ValueError("a k3s cluster has one server, or three so it survives one failing")
    if not 0 <= agents <= 6:
        raise ValueError("up to six workers")
    network = str(cfg.get("network") or "")
    if not network or network == "pod":
        raise ValueError("choose a LAN network (bridged): the nodes need addresses of their own")
    addresses = [str(a).strip() for a in cfg.get("addresses") or [] if str(a).strip()]
    count = servers + agents
    if len(addresses) != count:
        raise ValueError(f"{count} node{'s' if count != 1 else ''} need{'' if count != 1 else 's'} "
                         f"{count} address{'es' if count != 1 else ''}; {len(addresses)} given")
    if len(set(addresses)) != count:
        raise ValueError("each node needs an address of its own")
    for address in addresses:
        try:
            if ipaddress.ip_address(address).version != 4:
                raise ValueError()
        except ValueError:
            raise ValueError("each node needs a valid IPv4 address") from None
    version = str(cfg.get("k3s_version") or "")
    if version and not re.fullmatch(r"v\d+\.\d+\.\d+\+k3s\d+", version):
        raise ValueError("k3s version must be a published version such as v1.34.1+k3s1")
    if not 1 <= int(cfg.get("cores", 2)) <= 128:
        raise ValueError("between 1 and 128 cores per VM")
    memory = str(cfg.get("memory") or "4Gi")
    if not re.fullmatch(r"\d+(\.\d+)?(Mi|Gi)", memory) or float(memory[:-2]) <= 0:
        raise ValueError("memory per VM is like 4Gi or 512Mi")
    if not 1 <= int(cfg.get("disk_gb", 40)) <= 16384:
        raise ValueError("disk size per VM must be between 1 and 16384 GiB")
    if len(str(cfg.get("password") or "")) < 10:
        raise ValueError("the login password must be at least 10 characters")
    setup = str(cfg.get("setup") or "homestead")
    if setup not in SETUPS:
        raise ValueError("what it runs is one of: " + ", ".join(SETUPS))
    kubevirt = bool(cfg.get("kubevirt"))
    if kubevirt and setup == "k3s":
        raise ValueError("KubeVirt comes with the setups that install Homestead; k3s alone installs nothing more")
    nodes = [{"name": f"{prefix}-server-{i + 1}", "role": "server", "address": addresses[i]} for i in range(servers)]
    nodes += [{"name": f"{prefix}-agent-{i + 1}", "role": "agent", "address": addresses[servers + i]}
              for i in range(agents)]
    return {"name": prefix, "nodes": nodes, "setup": setup, "first": nodes[0]["address"], "kubevirt": kubevirt,
            "url": f"http://{nodes[0]['address']}:8088" if setup != "k3s" else ""}


def user_data(node, first, token, password, setup, k3s_version="", kubevirt=False):
    """cloud-init for one node: a login, the guest agent (so its address shows
    here), and the line that makes it a server or joins it to the first."""
    version = f" --k3s-version {k3s_version}" if k3s_version else ""
    if setup == "k3s":
        env = f"K3S_TOKEN={token}" + (f" INSTALL_K3S_VERSION={k3s_version}" if k3s_version else "")
        if node["address"] == first:
            line = f"curl -sfL {K3S} | {env} sh -s - server --cluster-init"
        elif node["role"] == "server":
            line = f"curl -sfL {K3S} | {env} sh -s - server --server https://{first}:6443"
        else:
            line = f"curl -sfL {K3S} | {env} K3S_URL=https://{first}:6443 sh -s - agent"
    elif node["address"] == first:
        storage = " --no-longhorn" if setup == "local" else ""
        vms = " --kubevirt" if kubevirt else ""
        line = f"curl -sfL {BOOTSTRAP} | K3S_TOKEN={token} sh -s - server{storage}{vms}{version}"
    elif node["role"] == "server":
        line = f"curl -sfL {BOOTSTRAP} | sh -s - join https://{first}:6443 {token}{version}"
    else:
        line = f"curl -sfL {BOOTSTRAP} | sh -s - agent https://{first}:6443 {token}{version}"
    return "\n".join([
        "#cloud-config",
        f"hostname: {node['name']}",
        "ssh_pwauth: true",
        f"password: {json.dumps(password)}",
        "chpasswd: {expire: false}",
        "package_update: true",
        "packages: [qemu-guest-agent, curl]",
        "runcmd:",
        "  - [systemctl, enable, --now, qemu-guest-agent]",
        # Printed to the serial console as well as kept in LOG, so the job's
        # log in Homestead shows the install as it happens.
        f"  - [sh, -c, {json.dumps(line + f' 2>&1 | tee {LOG}')}]",
        ""])


def prepare(cfg, token=None):
    """Build the entire batch without creating anything or persisting secrets.

    A supplied token lets an admission layer reproduce the exact reviewed
    cloud-init. Generated MACs/other reviewed values can be supplied per node.
    """
    cfg = copy.deepcopy(cfg)
    built = review(cfg)
    taken = [f"{n['name']}: {n['problem']}" for n in built["nodes"] if n["problem"]]
    if taken:
        raise ValueError("; ".join(taken))
    token = token or secrets.token_hex(24)
    if not re.fullmatch(r"[a-zA-Z0-9_-]{16,128}", token):
        raise ValueError("invalid internal cluster join token")
    ns = str(cfg.get("namespace") or "lab")
    static = {"prefix": int(cfg.get("prefix") or 24), "gateway": str(cfg.get("gateway") or ""),
              "dns": [d for d in (cfg.get("dns") or []) if d]}
    configs = []
    for node in built["nodes"]:
        vm = {"name": node["name"], "namespace": ns, "cores": int(cfg.get("cores") or 2),
              "memory": str(cfg.get("memory") or "4Gi"), "disk_gb": int(cfg.get("disk_gb") or 40),
              "storage_class": cfg.get("storage_class") or "", "network": cfg["network"],
              "image_id": cfg.get("image_id") or "", "image_url": "" if cfg.get("image_id") else (cfg.get("image_url") or UBUNTU),
              "static_ip": dict(static, address=node["address"]),
              "cloud_init": user_data(node, built["first"], token, str(cfg["password"]), built["setup"],
                                      str(cfg.get("k3s_version") or ""), built["kubevirt"]),
              # VMs inside these VMs need the host's CPU as it is, virtualisation
              # included - where the host allows nesting. Without it KubeVirt in
              # the cluster emulates.
              **({"cpu_model": "host-passthrough"} if built["kubevirt"] else {}),
              "labels": {LABEL: built["name"], ROLE: node["role"]},
              # Its console kept as a log, so the job's Log shows the install.
              "log_console": True,
              "ipam_note": f"k3s cluster {built['name']}, {node['role']}"}
        if (cfg.get("macs") or {}).get(node["name"]):
            vm["mac"] = cfg["macs"][node["name"]]
        configs.append(vm)
    return {"namespace": ns, "plan": built, "configs": configs}


def start(cfg, ops):
    return commit(prepare(cfg), ops)


def commit(prepared, ops, *, create_one=None, before_node=None):
    """Journal before writes; stop on uncertainty and retain every partial VM.

    Only public resource identities/phase information enter the operation.
    Request bodies, login passwords and join tokens remain in memory only.
    before_node is the fresh batch-admission hook, not an automatic retry.
    """
    ns, built = prepared["namespace"], prepared["plan"]
    if (not built["nodes"] or len(built["nodes"]) != len(prepared["configs"]) or
            any(node["name"] != cfg.get("name") or cfg.get("namespace") != ns
                for node, cfg in zip(built["nodes"], prepared["configs"]))):
        raise ValueError("Prepared VM batch is incomplete or inconsistent; nothing was created")
    create_one = create_one or create
    operation = ops.start("k3s-cluster", f"k3s cluster {built['name']}",
                          {"kind": "VirtualMachine", "name": built["nodes"][0]["name"], "namespace": ns}, "/vms",
                          {"namespace": ns, "name": built["name"], "nodes": copy.deepcopy(built["nodes"]),
                           "first": built["first"], "setup": built["setup"], "started": time.time(),
                           "phase": "provisioning", "phase_at": time.time(), "retain_resources": True,
                           "created": [], "attempted": ""}, "Checking batch before creating VMs")
    made = []
    for node, vm in zip(built["nodes"], prepared["configs"]):
        try:
            ops.record_phase(operation["id"], "provisioning", int(10 * len(made) / len(built["nodes"])),
                             f"Checking {node['name']} before creation", created=copy.deepcopy(made), attempted="")
            if before_node:
                before_node(vm, copy.deepcopy(made))
            # Persist the possible target before dispatch. A lost response is
            # not proof that this VM, its claims or its Secret were not made.
            ops.record_phase(operation["id"], "provisioning", int(10 * len(made) / len(built["nodes"])),
                             f"Creating {node['name']}; awaiting Kubernetes receipt", attempted=node["name"])
            result = create_one(copy.deepcopy(vm))
            identity = (result or {}).get("vm_identity") or {}
            if (identity.get("namespace") != ns or identity.get("name") != node["name"] or
                    not identity.get("uid") or not identity.get("resourceVersion")):
                raise ValueError("VM creation identity was not returned")
            made.append({"name": node["name"], "identity": identity})
            ops.record_phase(operation["id"], "provisioning", int(10 * len(made) / len(built["nodes"])),
                             f"Created {node['name']}; retaining its resources", created=copy.deepcopy(made), attempted="")
        except Exception as error:
            # Exception bodies can include a rejected Secret or cloud-init.
            # Persist safe diagnostics only, never the raw request/error body.
            message = (f"Stopped at {node['name']}. {len(made)} creation receipt(s) recorded. "
                       "Resources are retained, including any uncertain creation; inspect the job and VMs before retrying.")
            try:
                ops.record_phase(operation["id"], "failed", int(10 * len(made) / len(built["nodes"])),
                                 message, created=copy.deepcopy(made), failure_type=type(error).__name__)
            except Exception:
                # The preceding durable intent still names the possible write.
                pass
            raise ValueError(f"{message} Job {operation['id']}.") from None
    return ops.record_phase(operation["id"], "awaiting-ready", 10,
                            f"Created {len(made)} VMs; waiting for guest startup", created=made, attempted="")


def status(item):
    """VMs running, then k3s answering, then the Homestead inside."""
    ref = item["ref"]
    waited = time.time() - float(ref.get("started") or time.time())
    names = [n["name"] for n in ref["nodes"]]
    if ref.get("phase") == "provisioning":
        if waited > START_LIMIT:
            return "failed", item.get("progress", 0), "VM provisioning was not confirmed complete. Partial VMs, disks and Secrets are retained; inspect them before a new review. Nothing was replayed."
        return "running", item.get("progress", 0), item.get("message") or "VM provisioning is in progress; partial resources are retained"
    if ref.get("phase") == "failed":
        return "failed", item.get("progress", 0), item.get("message") or "VM provisioning stopped; inspect retained resources"
    running = 0
    for name in names:
        try:
            if ref.get("retain_resources"):
                receipt = next((row for row in ref.get("created") or [] if row["name"] == name), None)
                if not receipt:
                    return "failed", item.get("progress", 0), f"No creation receipt for {name}; inspect retained resources"
                vm = kget(f"/apis/kubevirt.io/v1/namespaces/{ref['namespace']}/virtualmachines/{urllib.parse.quote(name)}")
                if (vm.get("metadata") or {}).get("uid") != receipt["identity"]["uid"]:
                    return "failed", item.get("progress", 0), f"{name} was replaced; the original batch is not confirmed ready"
            vmi = kget(f"/apis/kubevirt.io/v1/namespaces/{ref['namespace']}/virtualmachineinstances/"
                       f"{urllib.parse.quote(name)}")
            if ref.get("retain_resources"):
                owners = (vmi.get("metadata") or {}).get("ownerReferences") or []
                if not any(owner.get("controller") is True and owner.get("kind") == "VirtualMachine" and
                           owner.get("apiVersion") == "kubevirt.io/v1" and owner.get("name") == name and
                           owner.get("uid") == receipt["identity"]["uid"] for owner in owners):
                    continue
            running += (vmi.get("status") or {}).get("phase") == "Running"
        except Exception:
            pass
    first = ref["first"]
    if waited > START_LIMIT:
        return "failed", item.get("progress", 0), (
            f"After {int(waited // 60)} minutes the cluster is not up. Open {names[0]}'s console and read {LOG}; "
            "most often the VMs cannot reach the internet from the LAN network, or an address clashes.")
    if running < len(names):
        return "running", 10 + int(40 * running / len(names)), f"{running} of {len(names)} VMs running"
    if not _answers(first, 6443):
        return "running", 60, f"VMs running; installing k3s on {names[0]} ({first}) - a few minutes"
    if ref.get("setup") == "k3s":
        return "succeeded", 100, (f"k3s answers at https://{first}:6443. The kubeconfig is "
                                  f"/etc/rancher/k3s/k3s.yaml on {names[0]}; the login is ubuntu with your password")
    if not _answers(first, 8088):
        return "running", 80, f"k3s is up; installing {'Longhorn and ' if ref.get('setup') == 'homestead' else ''}Homestead inside it"
    return "succeeded", 100, (f"The cluster is up: its Homestead answers at http://{first}:8088 - open it and "
                              "create the first account there. The nodes' login is ubuntu with your password")
