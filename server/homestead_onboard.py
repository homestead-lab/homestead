"""Adding a Harvester node, and taking a dead one back out.

A join plan is everything Harvester's installer needs to add one host without
questions: its name, disks, network, the cluster address and join token. The
installer is pointed at it one of three ways, all served from here:

  * a USB stick: a small image holding iPXE, which network-boots the installer
    from Homestead - no DHCP changes on the LAN;
  * PXE: a temporary proxy-DHCP pod that answers only the new host's MAC
    address and chainloads the same boot script;
  * the Harvester ISO: its boot menu is given the config address by hand.

Everything the installer fetches sits under /boot/<secret>/, a random path
that stops working when the plan expires, is revoked, or the node joins. The
join token is never read from the cluster: it is pasted in for one plan, kept
in a Secret, and deleted with the plan. The installer reports STARTED,
SUCCEEDED and FAILED back through webhooks, so progress shows in Activity.

The second half removes a node that is gone - following Harvester's own order:
Longhorn stops scheduling to it, the Kubernetes node goes, then the leftover
Cluster API machine and Longhorn node - after checking it will not cost etcd
quorum or the last copy of a volume.
"""
import base64
import calendar
import ipaddress
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.request

kget = ksend = None
NS = "lab"
DATA_DIR = "/data"
OPS = None
_lock = threading.Lock()

DEFAULT_HOURS = 24
MAX_HOURS = 24 * 7
RELEASES = "https://releases.rancher.com/harvester"
IPXE_EFI_URL = "https://boot.ipxe.org/x86_64-efi/ipxe.efi"
# Poseidon's dnsmasq image is built for exactly this: proxy DHCP and TFTP, with
# the iPXE binaries it chainloads already inside. Pinned, so a PXE server
# started today is the one that was tested.
PXE_IMAGE = ("quay.io/poseidon/dnsmasq:v0.5.0-52-g43ac44a"
             "@sha256:c0d0b53f6806ffe38b16e9f88532c7842a64bdc990c7d91f689623e13183cf7d")
PXE_HOURS_MAX = 6
ROLES = ("default", "management", "worker", "witness")
DNS = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")
MAC = re.compile(r"([0-9a-f]{2}:){5}[0-9a-f]{2}")


def bind(_kget, _ksend, namespace, data_dir, operations=None):
    global kget, ksend, NS, DATA_DIR, OPS
    kget, ksend, NS, DATA_DIR, OPS = _kget, _ksend, namespace, data_dir, operations


# ----------------------------------------------------------------- storage
def _path():
    return os.path.join(DATA_DIR, "onboard.json")


def _read():
    try:
        with open(_path(), encoding="utf-8") as handle:
            rows = json.load(handle)
        return rows if isinstance(rows, list) else []
    except (OSError, ValueError):
        return []


def _write(rows):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = _path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(rows, handle, indent=1)
    os.replace(tmp, _path())


def _store(plan):
    with _lock:
        rows = [row for row in _read() if row["id"] != plan["id"]]
        rows.append(plan)
        _write(rows[-50:])
    return plan


def _find(plan_id):
    return next((row for row in _read() if row["id"] == plan_id), None)


def _now():
    return time.time()


def _secret_name(plan_id):
    return f"homestead-onboard-{plan_id}"


def _get(path):
    try:
        return kget(path)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


# -------------------------------------------------------------- the plan
def _clean_list(value, pattern=None, limit=8):
    items = value if isinstance(value, list) else re.split(r"[\s,]+", str(value or ""))
    out = [str(x).strip() for x in items if str(x).strip()]
    if pattern:
        bad = [x for x in out if not re.fullmatch(pattern, x)]
        if bad:
            raise ValueError(f"not valid: {', '.join(bad[:3])}")
    return out[:limit]


def _hash_password(password):
    """A SHA-512 crypt hash when the platform has one; Harvester also takes plain text."""
    try:
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            import crypt  # noqa: PLC0415 - optional, deprecated in 3.11, gone in 3.13
        hashed = crypt.crypt(password, crypt.mksalt(crypt.METHOD_SHA512))
        if hashed and hashed.startswith("$6$"):
            return hashed
    except Exception:
        pass
    return password


def cluster_defaults():
    """What the plan form can fill in for itself: version, VIP, role advice."""
    version = ""
    setting = _get("/apis/harvesterhci.io/v1beta1/settings/server-version") or {}
    version = str(setting.get("value") or (setting.get("status") or {}).get("value") or "").lstrip("v")
    vip = ""
    cm = _get("/api/v1/namespaces/harvester-system/configmaps/vip") or {}
    vip = str((cm.get("data") or {}).get("ip") or "")
    nodes = (_get("/api/v1/nodes") or {}).get("items", [])
    names = sorted(n["metadata"]["name"] for n in nodes)
    if not version:
        for node in nodes:
            match = re.search(r"Harvester\s+v?([0-9][^\s]*)", (node.get("status", {}).get("nodeInfo") or {}).get("osImage", ""))
            if match:
                version = match.group(1)
                break
    return {"version": version, "server_url": f"https://{vip}:443" if vip else "",
            "vip": vip, "nodes": names, "releases": RELEASES}


def create_plan(body, homestead_url):
    """Validates a join plan and stores it, with the token in its own Secret."""
    hostname = str(body.get("hostname") or "").strip().lower()
    if not DNS.fullmatch(hostname):
        raise ValueError("hostname must use lowercase letters, numbers and dashes")
    role = body.get("role") or "default"
    if role not in ROLES:
        raise ValueError("role must be default, management, worker or witness")
    device = str(body.get("device") or "").strip()
    if not device.startswith("/dev/"):
        raise ValueError("the install disk is a device path such as /dev/sda or /dev/disk/by-id/…")
    data_disk = str(body.get("data_disk") or "").strip()
    if data_disk and not data_disk.startswith("/dev/"):
        raise ValueError("the data disk is a device path such as /dev/sdb")
    if data_disk and data_disk == device:
        data_disk = ""
    nic = str(body.get("nic") or "").strip()
    mac = str(body.get("mac") or "").strip().lower().replace("-", ":")
    if mac and not MAC.fullmatch(mac):
        raise ValueError("the MAC address looks like 52:54:00:12:34:56")
    if not nic and not mac:
        raise ValueError("name the management network port, or give its MAC address")
    method = body.get("method") or "dhcp"
    if method not in ("dhcp", "static"):
        raise ValueError("the address is dhcp or static")
    network = {"method": method}
    if method == "static":
        try:
            written = str(body.get("address") or "").strip()
            if "/" not in written:      # a bare address would quietly become a /32
                raise ValueError
            iface = ipaddress.ip_interface(written)
        except ValueError:
            raise ValueError("a static address is written with its prefix, such as 192.0.2.60/24")
        gateway = str(body.get("gateway") or "").strip()
        try:
            ipaddress.ip_address(gateway)
        except ValueError:
            raise ValueError("a static address needs a gateway")
        network.update(ip=str(iface.ip), subnet_mask=str(iface.network.netmask), gateway=gateway)
    vlan = body.get("vlan")
    if vlan not in (None, ""):
        vlan = int(vlan)
        if not 1 <= vlan <= 4094:
            raise ValueError("a VLAN is 1 to 4094")
        network["vlan_id"] = vlan
    mtu = body.get("mtu")
    if mtu not in (None, ""):
        mtu = int(mtu)
        if not 576 <= mtu <= 9216:
            raise ValueError("an MTU is between 576 and 9216")
        network["mtu"] = mtu
    dns = _clean_list(body.get("dns"), r"[0-9a-fA-F.:]+")
    ntp = _clean_list(body.get("ntp"), r"[A-Za-z0-9.:-]+")
    keys = [k for k in _clean_list(body.get("ssh_keys"), None, 20)
            if k.startswith(("ssh-", "ecdsa-", "sk-", "github:"))] if body.get("ssh_keys") else []
    password = str(body.get("password") or "")
    if len(password) < 8:
        raise ValueError("the rancher user's password needs at least 8 characters")
    server_url = str(body.get("server_url") or "").strip().rstrip("/")
    if not re.fullmatch(r"https://[^\s/]+(:\d+)?", server_url):
        raise ValueError("the cluster address is https://<cluster VIP>:443")
    token = str(body.get("token") or "").strip()
    if len(token) < 8 or any(c.isspace() for c in token):
        raise ValueError("paste the cluster token from a management node")
    version = str(body.get("version") or "").strip().lstrip("v")
    if not re.fullmatch(r"\d+\.\d+\.\d+(-[\w.]+)?", version):
        raise ValueError("the Harvester version looks like 1.4.1; it must match the cluster")
    mirror = str(body.get("mirror") or "").strip().rstrip("/")
    if mirror and not re.fullmatch(r"https?://\S+", mirror):
        raise ValueError("a mirror is an http:// or https:// address")
    homestead_url = str(body.get("homestead_url") or homestead_url or "").strip().rstrip("/")
    if not re.fullmatch(r"http://[^\s/]+(:\d+)?", homestead_url):
        raise ValueError("the new host reaches Homestead at http://<address>:<port>; "
                         "iPXE and the installer fetch from it over plain HTTP")
    hours = float(body.get("hours") or DEFAULT_HOURS)
    hours = max(1, min(MAX_HOURS, hours))

    existing = {n["metadata"]["name"] for n in (_get("/api/v1/nodes") or {}).get("items", [])}
    if hostname in existing:
        raise ValueError(f"a node called {hostname} is already in the cluster")
    for row in _read():
        if row.get("hostname") == hostname and row.get("status") in ("waiting", "installing"):
            raise ValueError(f"there is already an open join plan for {hostname}")

    plan_id = secrets.token_hex(6)
    plan = {
        "id": plan_id, "secret": secrets.token_urlsafe(24),
        "hostname": hostname, "role": role, "device": device, "data_disk": data_disk,
        "nic": nic, "mac": mac, "network": network, "dns": dns, "ntp": ntp, "ssh_keys": keys,
        "server_url": server_url, "version": version, "mirror": mirror,
        "homestead_url": homestead_url, "confirm_wipe": bool(body.get("confirm_wipe", True)),
        "skipchecks": bool(body.get("skipchecks")),
        "created": _now(), "expires": _now() + hours * 3600,
        "status": "waiting", "message": "Waiting for the host to boot", "events": [],
        "pxe": None, "operation": "",
    }
    secret = {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
              "metadata": {"name": _secret_name(plan_id), "namespace": NS,
                           "labels": {"homestead.io/onboard": plan_id}},
              "stringData": {"token": token, "password": _hash_password(password)}}
    ksend("POST", f"/api/v1/namespaces/{NS}/secrets", secret)
    _event(plan, "created", f"Plan created for {hostname}; valid for {hours:g} hours")
    if OPS:
        op = OPS.start("onboard", f"Join {hostname}", {"kind": "Node", "name": hostname},
                       "/system/cluster", {"plan": plan_id}, "Waiting for the host to boot")
        plan["operation"] = op.get("id", "")
    _store(plan)
    return public(plan)


def _credentials(plan):
    secret = _get(f"/api/v1/namespaces/{NS}/secrets/{_secret_name(plan['id'])}") or {}
    data = secret.get("data") or {}
    decode = lambda key: base64.b64decode(data.get(key, "")).decode("utf-8", "replace")  # noqa: E731
    return decode("token"), decode("password")


def _artifact(plan, kind):
    version = plan["version"]
    base = plan.get("mirror") or f"{RELEASES}/v{version}"
    names = {"kernel": f"harvester-v{version}-vmlinuz-amd64", "initrd": f"harvester-v{version}-initrd-amd64",
             "rootfs": f"harvester-v{version}-rootfs-amd64.squashfs", "iso": f"harvester-v{version}-amd64.iso"}
    return f"{base}/{names[kind]}"


def boot_base(plan):
    return f"{plan['homestead_url']}/boot/{plan['secret']}"


def public(plan, with_urls=True):
    """A plan as the page sees it: never the token, the password or the raw secret."""
    row = {k: v for k, v in plan.items() if k not in ("secret",)}
    row["expired"] = _now() > plan["expires"]
    if with_urls:
        base = boot_base(plan)
        row["urls"] = {"config": f"{base}/config.yaml", "script": f"{base}/boot.ipxe",
                       "usb": f"/api/onboard/usb?id={plan['id']}"}
        row["kernel_args"] = kernel_args(plan)
    return row


def plans():
    rows = sorted(_read(), key=lambda row: row.get("created", 0), reverse=True)
    return [public(row) for row in rows]


# ------------------------------------------------------ installer documents
def _yaml(value, indent=0):
    """Block YAML for plain data. Strings are JSON-quoted, which YAML reads as-is."""
    pad = "  " * indent
    if isinstance(value, dict):
        lines = []
        for key, item in value.items():
            if isinstance(item, (dict, list)) and item:
                lines.append(f"{pad}{key}:")
                lines.append(_yaml(item, indent + 1))
            else:
                lines.append(f"{pad}{key}: {_scalar(item)}")
        return "\n".join(lines)
    if isinstance(value, list):
        lines = []
        for item in value:
            if isinstance(item, dict) and item:
                inner = _yaml(item, indent + 1).split("\n")
                lines.append(f"{pad}- {inner[0].strip()}")
                lines.extend(inner[1:])
            else:
                lines.append(f"{pad}- {_scalar(item)}")
        return "\n".join(lines)
    return f"{pad}{_scalar(value)}"


def _scalar(value):
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (dict, list)):
        return "{}" if isinstance(value, dict) else "[]"
    return json.dumps(str(value))


def config_document(plan, redact=False):
    """Harvester's join configuration for this plan."""
    token, password = ("<cluster token>", "<password hash>") if redact else _credentials(plan)
    interface = {}
    if plan.get("nic"):
        interface["name"] = plan["nic"]
    if plan.get("mac"):
        interface["hwAddr"] = plan["mac"]
    management = {"interfaces": [interface], "default_route": True, **plan["network"]}
    base = boot_base(plan)
    install = {"mode": "join", "role": plan["role"], "management_interface": management,
               "device": plan["device"], "iso_url": _artifact(plan, "iso"), "tty": "tty1",
               "automatic": True,
               "webhooks": [{"event": event, "method": "POST", "url": f"{base}/event?e={event}",
                             "headers": {"Content-Type": ["application/json"]},
                             "payload": '{"host": "{{.Hostname}}"}'}
                            for event in ("STARTED", "SUCCEEDED", "FAILED")]}
    if plan.get("data_disk"):
        install["data_disk"] = plan["data_disk"]
    if plan.get("skipchecks"):
        install["skipchecks"] = True
    os_section = {"hostname": plan["hostname"], "password": password}
    if plan.get("ssh_keys"):
        os_section["ssh_authorized_keys"] = plan["ssh_keys"]
    if plan.get("dns"):
        os_section["dns_nameservers"] = plan["dns"]
    if plan.get("ntp"):
        os_section["ntp_servers"] = plan["ntp"]
    doc = {"scheme_version": 1, "server_url": plan["server_url"], "token": token,
           "os": os_section, "install": install}
    return "# Harvester join configuration for " + plan["hostname"] + ", written by Homestead\n" + _yaml(doc) + "\n"


def kernel_args(plan):
    base = boot_base(plan)
    return (f"initrd=initrd ip=dhcp net.ifnames=1 rd.cos.disable rd.noverifyssl console=tty1 "
            f"root=live:{_artifact(plan, 'rootfs')} harvester.install.automatic=true "
            f"harvester.install.config_url={base}/config.yaml")


def ipxe_script(plan):
    """The script both the USB stick and PXE end up running."""
    base = boot_base(plan)
    confirm = [
        "echo This installs Harvester " + plan["version"] + " as " + plan["hostname"],
        "echo and ERASES " + plan["device"] + (" and " + plan["data_disk"] if plan.get("data_disk") else "") + ".",
        "echo",
        "prompt --key i --timeout 30000 Press i within 30 seconds to install; anything else skips it && goto install || goto skip",
        ":skip",
        "echo Not installing. Carrying on to the next boot device.",
        "exit 1",
    ] if plan.get("confirm_wipe", True) else []
    return "\n".join([
        "#!ipxe",
        f"# Homestead join plan for {plan['hostname']}",
        "echo",
        f"echo Homestead: join {plan['hostname']} to the Harvester cluster",
        *confirm,
        ":install",
        f"kernel {base}/vmlinuz {kernel_args(plan)} || goto failed",
        f"initrd --name initrd {base}/initrd || goto failed",
        "boot || goto failed",
        ":failed",
        "echo The installer could not be started; the plan may have expired. Check Homestead.",
        "prompt --timeout 60000 Press a key to continue",
        "exit 1",
        "",
    ])


def autoexec_script(plan):
    """What the USB stick runs: bring the network up, then fetch the real script."""
    return "\n".join([
        "#!ipxe",
        f"# Homestead USB boot for {plan['hostname']} - edit the address below if Homestead moves",
        "dhcp || goto nonet",
        f"chain {boot_base(plan)}/boot.ipxe || goto failed",
        ":nonet",
        "echo No network address from DHCP. Plug the management port in and try again.",
        "goto wait",
        ":failed",
        f"echo Could not reach Homestead at {plan['homestead_url']}. The plan may have expired.",
        ":wait",
        "prompt --timeout 60000 Press a key to continue",
        "exit 1",
        "",
    ])


def readme(plan):
    return (f"Homestead USB join stick for {plan['hostname']}\r\n\r\n"
            f"Boot the new host from this stick in UEFI mode, with Secure Boot turned off.\r\n"
            f"It loads iPXE, gets an address by DHCP, and fetches the installer from Homestead at\r\n"
            f"{plan['homestead_url']}. Nothing on the stick holds the cluster token.\r\n\r\n"
            f"The installer ERASES {plan['device']}. "
            f"{'You will be asked to press i before anything is written.' if plan.get('confirm_wipe', True) else ''}\r\n"
            f"The stick stops working when the plan expires or the node has joined.\r\n")


_ipxe_cache = {}


def ipxe_binary():
    cached = os.path.join(DATA_DIR, "ipxe-x86_64.efi")
    if os.path.exists(cached) and os.path.getsize(cached) > 100_000:
        with open(cached, "rb") as handle:
            return handle.read()
    request = urllib.request.Request(IPXE_EFI_URL, headers={"User-Agent": "Homestead"})
    with urllib.request.urlopen(request, timeout=60) as response:
        blob = response.read()
    if len(blob) < 100_000 or blob[:2] != b"MZ":
        raise ValueError("the iPXE download was not an EFI program")
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(cached, "wb") as handle:
        handle.write(blob)
    return blob


def usb_image(plan_id):
    import homestead_fatimg as FAT
    plan = _find(plan_id)
    if not plan:
        raise ValueError("no such join plan")
    if not _usable(plan):
        raise ValueError("this plan has expired or finished; start a new one")
    autoexec = autoexec_script(plan).encode()
    image = FAT.build({"EFI/BOOT/BOOTX64.EFI": ipxe_binary(), "EFI/BOOT/autoexec.ipxe": autoexec,
                       "autoexec.ipxe": autoexec, "README.txt": readme(plan).encode()},
                      16, "HSJOIN")
    _event(plan, "usb", "USB image downloaded")
    _store(plan)
    return f"homestead-join-{plan['hostname']}.img", image


# ------------------------------------------------------ the public side
def _usable(plan):
    return plan and plan.get("status") in ("waiting", "installing") and _now() <= plan["expires"]


def by_secret(secret):
    """The plan behind a /boot/<secret>/ address, or None once it no longer applies."""
    if not secret or len(secret) < 16:
        return None
    for plan in _read():
        if secrets.compare_digest(plan.get("secret", ""), secret):
            return plan if _usable(plan) else None
    return None


def _event(plan, kind, message, source=""):
    plan.setdefault("events", []).append({"at": _now(), "kind": kind, "message": message, "from": source})
    plan["events"] = plan["events"][-40:]


def note_fetch(secret, what, source=""):
    plan = by_secret(secret)
    if not plan:
        return None
    labels = {"script": "Host fetched the boot script", "config": "Installer fetched its configuration",
              "kernel": "Host downloaded the installer kernel", "initrd": "Host downloaded the installer initrd"}
    recent = [e for e in plan.get("events", []) if e["kind"] == what and _now() - e["at"] < 60]
    if not recent:
        _event(plan, what, labels.get(what, what), source)
        if what == "config" and plan["status"] == "waiting":
            plan.update(status="installing", message="The installer has its configuration")
        _store(plan)
    return plan


def webhook(secret, event, source=""):
    """Harvester's installer reporting STARTED, SUCCEEDED or FAILED."""
    plan = by_secret(secret)
    if not plan:
        return None
    event = str(event or "").upper()
    if event == "STARTED":
        plan.update(status="installing", message="Installing Harvester")
        _event(plan, "started", "Installation started", source)
    elif event == "SUCCEEDED":
        plan.update(status="installing", message="Installed; rebooting and joining the cluster")
        _event(plan, "succeeded", "Installation finished; the host reboots and joins", source)
    elif event == "FAILED":
        plan.update(status="failed", message="The installer reported a failure; check the host's console")
        _event(plan, "failed", "Installation failed", source)
    else:
        return plan
    _store(plan)
    return plan


def stream_artifact(secret, kind, write_headers, write):
    """Hands the kernel or initrd through over plain HTTP, which every iPXE can fetch."""
    plan = note_fetch(secret, kind)
    if not plan:
        return False
    request = urllib.request.Request(_artifact(plan, kind), headers={"User-Agent": "Homestead"})
    with urllib.request.urlopen(request, timeout=60) as upstream:
        write_headers(upstream.headers.get("Content-Length"))
        while True:
            chunk = upstream.read(1024 * 256)
            if not chunk:
                break
            write(chunk)
    return True


# --------------------------------------------------------- PXE service
def _pod_name(plan_id):
    return f"homestead-pxe-{plan_id}"


def pxe_pod(plan, node, interface, subnet):
    """A proxy-DHCP server for one plan: it adds boot instructions, never addresses.

    It answers only the plan's MAC address, so no other machine on the LAN is
    offered an installer, and it stops by itself when its time is up.
    """
    if not plan.get("mac"):
        raise ValueError("PXE needs the new host's MAC address, so nothing else on the LAN is offered an install")
    network = ipaddress.ip_network(subnet, strict=False)
    if network.version != 4:
        raise ValueError("PXE proxy DHCP needs an IPv4 subnet")
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,15}", interface):
        raise ValueError("that is not an interface name")
    seconds = int(max(600, min(PXE_HOURS_MAX * 3600, plan["expires"] - _now())))
    script = f"{boot_base(plan)}/boot.ipxe"
    args = [
        "-d", "-q", "--port=0", "--log-dhcp",
        f"--interface={interface}", "--bind-interfaces",
        f"--dhcp-range={network.network_address},proxy,{network.netmask}",
        "--enable-tftp", "--tftp-root=/var/lib/tftpboot",
        f"--dhcp-mac=set:joining,{plan['mac']}",
        "--dhcp-userclass=set:ipxe,iPXE",
        "--tag-if=set:bootipxe,tag:joining,tag:!ipxe",
        "--tag-if=set:runscript,tag:joining,tag:ipxe",
        '--pxe-service=tag:bootipxe,x86PC,"Homestead: load iPXE",undionly.kpxe',
        '--pxe-service=tag:bootipxe,X86-64_EFI,"Homestead: load iPXE",ipxe.efi',
        f'--pxe-service=tag:runscript,x86PC,"Homestead join",{script}',
        f'--pxe-service=tag:runscript,X86-64_EFI,"Homestead join",{script}',
    ]
    return {
        "apiVersion": "v1", "kind": "Pod",
        "metadata": {"name": _pod_name(plan["id"]), "namespace": NS,
                     "labels": {"app": "homestead-pxe", "homestead.io/onboard": plan["id"],
                                "homestead.io/managed": "true"}},
        "spec": {
            "nodeName": node, "hostNetwork": True, "dnsPolicy": "ClusterFirstWithHostNet",
            "restartPolicy": "Never", "activeDeadlineSeconds": seconds,
            "terminationGracePeriodSeconds": 2,
            "tolerations": [{"operator": "Exists"}],
            "containers": [{
                "name": "dnsmasq", "image": PXE_IMAGE, "args": args,
                "securityContext": {"capabilities": {"add": ["NET_ADMIN", "NET_RAW", "NET_BIND_SERVICE"]}},
                "resources": {"requests": {"cpu": "10m", "memory": "16Mi"}, "limits": {"memory": "64Mi"}},
            }],
        },
    }


def pxe_start(plan_id, node, interface="mgmt-br", subnet=""):
    plan = _find(plan_id)
    if not _usable(plan):
        raise ValueError("this plan has expired or finished")
    nodes = {n["metadata"]["name"]: n for n in (_get("/api/v1/nodes") or {}).get("items", [])}
    if node not in nodes:
        raise ValueError("choose a node on the same network as the new host")
    if not subnet:
        address = next((a["address"] for a in nodes[node].get("status", {}).get("addresses", [])
                        if a.get("type") == "InternalIP"), "")
        if not address:
            raise ValueError("could not tell that node's subnet; give it, such as 192.0.2.0/24")
        subnet = f"{address}/24"
    pod = pxe_pod(plan, node, interface or "mgmt-br", subnet)
    existing = _get(f"/api/v1/namespaces/{NS}/pods/{_pod_name(plan_id)}")
    if existing:
        ksend("DELETE", f"/api/v1/namespaces/{NS}/pods/{_pod_name(plan_id)}")
        time.sleep(3)
    ksend("POST", f"/api/v1/namespaces/{NS}/pods", pod)
    plan["pxe"] = {"node": node, "interface": interface or "mgmt-br",
                   "subnet": str(ipaddress.ip_network(subnet, strict=False)), "started": _now()}
    _event(plan, "pxe", f"PXE service started on {node} for {plan['mac']}")
    _store(plan)
    return public(plan)


def pxe_stop(plan_id, reason="PXE service stopped"):
    plan = _find(plan_id)
    try:
        ksend("DELETE", f"/api/v1/namespaces/{NS}/pods/{_pod_name(plan_id)}")
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
    if plan and plan.get("pxe"):
        plan["pxe"] = None
        _event(plan, "pxe", reason)
        _store(plan)
    return public(plan) if plan else {"ok": True}


def pxe_status(plan_id):
    pod = _get(f"/api/v1/namespaces/{NS}/pods/{_pod_name(plan_id)}")
    if not pod:
        return {"running": False, "phase": "stopped", "log": []}
    phase = (pod.get("status") or {}).get("phase", "Pending")
    log = []
    try:
        raw = kget_text(f"/api/v1/namespaces/{NS}/pods/{_pod_name(plan_id)}/log?tailLines=200")
        plan = _find(plan_id) or {}
        mac = plan.get("mac", "")
        log = [line for line in raw.splitlines() if not mac or mac in line.lower()
               or "error" in line.lower() or "failed" in line.lower()][-30:]
    except Exception:
        pass
    return {"running": phase in ("Pending", "Running"), "phase": phase, "log": log}


kget_text = None


# ----------------------------------------------------------- lifecycle
def revoke(plan_id, reason="Plan cancelled"):
    """Ends a plan: its addresses stop working and its token is deleted."""
    plan = _find(plan_id)
    if not plan:
        raise ValueError("no such join plan")
    _finish(plan, "cancelled" if plan["status"] in ("waiting", "installing") else plan["status"], reason)
    return public(plan)


def _finish(plan, status, message):
    plan.update(status=status, message=message)
    _event(plan, status, message)
    try:
        ksend("DELETE", f"/api/v1/namespaces/{NS}/secrets/{_secret_name(plan['id'])}")
    except Exception:
        pass
    if plan.get("pxe"):
        try:
            ksend("DELETE", f"/api/v1/namespaces/{NS}/pods/{_pod_name(plan['id'])}")
        except Exception:
            pass
        plan["pxe"] = None
    _store(plan)


def tick():
    """Moves open plans along: joined when the node is Ready, closed when expired."""
    rows = [row for row in _read() if row.get("status") in ("waiting", "installing")]
    if not rows:
        return
    nodes = {n["metadata"]["name"]: n for n in (_get("/api/v1/nodes") or {}).get("items", [])}
    for plan in rows:
        node = nodes.get(plan["hostname"])
        if node:
            ready = any(c.get("type") == "Ready" and c.get("status") == "True"
                        for c in (node.get("status") or {}).get("conditions", []))
            if ready:
                _finish(plan, "joined", f"{plan['hostname']} has joined and is Ready")
                continue
            if not any(e["kind"] == "registered" for e in plan.get("events", [])):
                plan["message"] = f"{plan['hostname']} has registered; waiting for it to be Ready"
                _event(plan, "registered", plan["message"])
                _store(plan)
        if _now() > plan["expires"]:
            _finish(plan, "expired", "The plan expired before the node joined; its token was deleted")


def run():
    while True:
        try:
            tick()
        except Exception:
            pass
        time.sleep(15)


def op_state(item):
    plan = _find((item.get("ref") or {}).get("plan", ""))
    if not plan:
        return "failed", item.get("progress", 0), "This join plan's record is gone"
    kinds = {e["kind"] for e in plan.get("events", [])}
    progress = (100 if plan["status"] == "joined" else 85 if "registered" in kinds else
                70 if "succeeded" in kinds else 40 if "started" in kinds else
                20 if "config" in kinds else 10 if kinds & {"script", "kernel"} else 2)
    status = {"joined": "succeeded", "failed": "failed", "expired": "failed",
              "cancelled": "cancelled"}.get(plan["status"], "running")
    return status, progress, plan.get("message", "")


# ============================================================ cleanup
def _ready(node):
    return any(c.get("type") == "Ready" and c.get("status") == "True"
               for c in (node.get("status") or {}).get("conditions", []))


def _not_ready_since(node):
    for c in (node.get("status") or {}).get("conditions", []):
        if c.get("type") == "Ready" and c.get("status") != "True":
            return c.get("lastTransitionTime", "")
    return ""


def _roles(node):
    labels = node["metadata"].get("labels") or {}
    return sorted(k.split("/", 1)[1] for k in labels
                  if k.startswith("node-role.kubernetes.io/") and labels[k] in ("true", ""))


def _replicas_by_node():
    try:
        replicas = kget("/apis/longhorn.io/v1beta2/namespaces/longhorn-system/replicas").get("items", [])
    except Exception:
        return {}, {}
    by_volume, by_node = {}, {}
    for r in replicas:
        spec, status = r.get("spec") or {}, r.get("status") or {}
        node, volume = spec.get("nodeID", ""), spec.get("volumeName", "")
        healthy = status.get("currentState") == "running" and not spec.get("failedAt")
        by_volume.setdefault(volume, []).append((node, healthy))
        by_node.setdefault(node, set()).add(volume)
    return by_volume, by_node


def _age(stamp):
    """Seconds since a Kubernetes timestamp, or None when there is none."""
    try:
        return time.time() - calendar.timegm(time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        return None


def _list(path):
    try:
        return kget(path).get("items", [])
    except Exception:
        return []


def _plural(count, word):
    return f"{count} {word}{'' if count == 1 else 's'}"


def _stuck_on(name):
    """What a dead host still holds: pods, VMs, volume attachments and replica records."""
    pods = [p for p in _list(f"/api/v1/pods?fieldSelector=spec.nodeName%3D{name}")
            if (p.get("spec") or {}).get("nodeName") == name]
    vmis = [v for v in _list("/apis/kubevirt.io/v1/virtualmachineinstances")
            if (v.get("status") or {}).get("nodeName") == name]
    attachments = [a for a in _list("/apis/storage.k8s.io/v1/volumeattachments")
                   if (a.get("spec") or {}).get("nodeName") == name]
    replicas = [r for r in _list("/apis/longhorn.io/v1beta2/namespaces/longhorn-system/replicas")
                if (r.get("spec") or {}).get("nodeID") == name]
    return pods, vmis, attachments, replicas


def removal_plan(name):
    """What removing a node would do, and whether it is safe - before anything changes."""
    node = _get(f"/api/v1/nodes/{name}")
    if not node:
        raise ValueError(f"there is no node called {name}")
    nodes = (_get("/api/v1/nodes") or {}).get("items", [])
    ready = _ready(node)
    roles = _roles(node)
    since = _not_ready_since(node)
    blockers, warnings, steps = [], [], []
    etcd = [n for n in nodes if "etcd" in _roles(n)]
    control = [n for n in nodes if {"control-plane", "master"} & set(_roles(n))]
    if ready:
        blockers.append(f"{name} is Ready. A running node re-registers itself, so it is not removed from here: "
                        f"put it in maintenance mode in Harvester, run /opt/rke2/bin/rke2-uninstall.sh on it, "
                        f"power it off, and come back when it shows Not ready.")
    else:
        down = _age(since)
        if down is not None and down < 600:
            warnings.append(f"{name} stopped reporting {_plural(max(1, int(down // 60)), 'minute')} ago. "
                            f"It may only be rebooting; give it a few minutes.")
    if "etcd" in roles or {"control-plane", "master"} & set(roles):
        others_ready = sum(1 for n in etcd if n["metadata"]["name"] != name and _ready(n))
        remaining = len(etcd) - 1
        if len([n for n in control if n["metadata"]["name"] != name]) == 0:
            blockers.append(f"{name} is the only control-plane node; removing it ends the cluster.")
        elif remaining and others_ready < remaining // 2 + 1:
            blockers.append(f"Only {others_ready} of the other {remaining} etcd members are ready, "
                            f"so the cluster would not have quorum without {name}. Bring them back first.")
        elif remaining < 3:
            warnings.append(f"The cluster keeps {_plural(remaining, 'etcd member')}; "
                            f"one more failure would stop it. Add a node soon.")
        else:
            warnings.append("Harvester promotes a worker to take its control-plane place, when one is available.")
    by_volume, by_node = _replicas_by_node()
    lost, degraded = [], []
    for volume in sorted(by_node.get(name, ())):
        healthy_elsewhere = [n for n, ok in by_volume.get(volume, []) if ok and n != name]
        (degraded if healthy_elsewhere else lost).append(volume)
    if lost:
        warnings.append(f"{_plural(len(lost), 'volume')} with no healthy copy anywhere but {name}: "
                        f"{', '.join(lost[:6])}{' …' if len(lost) > 6 else ''}. "
                        f"Removing the node gives up on that data.")
    if degraded:
        warnings.append(f"{_plural(len(degraded), 'volume')} will rebuild the copy {name} held, "
                        f"on the remaining nodes.")
    pods, vmis, attachments, replicas = _stuck_on(name) if not ready else ([], [], [], [])
    annotations = node["metadata"].get("annotations") or {}
    machine = annotations.get("cluster.x-k8s.io/machine", "")
    machine_ns = annotations.get("cluster.x-k8s.io/cluster-namespace", "fleet-local")
    steps.append(f"Stop Longhorn scheduling new replicas to {name}")
    steps.append(f"Delete the Kubernetes node {name} (RKE2 removes its etcd membership)")
    steps.append(f"Delete its Cluster API machine {machine}" if machine
                 else "Delete any Cluster API machine left pointing at it")
    steps.append(f"Delete Longhorn's record of {name} once it holds no replicas")
    vm_names = sorted({(v.get("metadata") or {}).get("name", "") for v in vmis})
    gone_steps = [
        f"Force-delete the {_plural(len(pods), 'pod')} still bound to it, so their workloads start elsewhere",
        (f"Force-stop the {_plural(len(vmis), 'VM')} it was running ({', '.join(vm_names[:5])}), "
         f"so each restarts on another host" if vmis else "No VMs are recorded as running there"),
        f"Release {_plural(len(attachments), 'volume attachment')}, so those volumes can attach on another node",
        f"Delete the {_plural(len(replicas), 'replica record')} Longhorn keeps for it, "
        f"so it rebuilds them from the remaining copies",
        "Finish its Cluster API machine's deletion if finalizers hold it",
    ]
    return {"node": name, "ready": ready, "roles": roles, "since": since,
            "blockers": blockers, "warnings": warnings, "steps": steps, "gone_steps": gone_steps,
            "stuck": {"pods": len(pods), "vms": vm_names, "attachments": len(attachments),
                      "replicas": len(replicas)},
            "lost_volumes": lost, "rebuilt_volumes": degraded,
            "machine": {"name": machine, "namespace": machine_ns}, "ok": not blockers}


def _force_delete(path):
    try:
        ksend("DELETE", path, {"apiVersion": "v1", "kind": "DeleteOptions", "gracePeriodSeconds": 0})
        return True
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return False
        raise


def remove_node(name, accept_loss=False, gone=False):
    """Removes a dead node in Harvester's documented order, reporting each step.

    `gone` is for a host that will never come back: what it still holds - pods,
    VMs, volume attachments, replica records, a machine held by finalizers - is
    let go of too, rather than left for controllers that wait on the host.
    """
    plan = removal_plan(name)
    if plan["blockers"]:
        raise ValueError(plan["blockers"][0])
    if plan["lost_volumes"] and not accept_loss:
        raise ValueError(f"{_plural(len(plan['lost_volumes']), 'volume')} would lose the only copy; confirm that first")
    log = []
    lh = f"/apis/longhorn.io/v1beta2/namespaces/longhorn-system/nodes/{name}"
    if _get(lh):
        ksend("PATCH", lh, {"spec": {"allowScheduling": False}}, "application/merge-patch+json")
        log.append(f"Longhorn stopped scheduling to {name}")
    pods, vmis, attachments, replicas = _stuck_on(name) if gone else ([], [], [], [])
    if gone:
        stopped = sum(_force_delete(f"/apis/kubevirt.io/v1/namespaces/{v['metadata']['namespace']}/"
                                    f"virtualmachineinstances/{v['metadata']['name']}") for v in vmis)
        if stopped:
            log.append(f"Force-stopped the {_plural(stopped, 'VM')} it was running")
        deleted = sum(_force_delete(f"/api/v1/namespaces/{p['metadata']['namespace']}/pods/{p['metadata']['name']}")
                      for p in pods)
        if deleted:
            log.append(f"Force-deleted {_plural(deleted, 'pod')} bound to {name}")
        released = sum(_force_delete(f"/apis/storage.k8s.io/v1/volumeattachments/{a['metadata']['name']}")
                       for a in attachments)
        if released:
            log.append(f"Released {_plural(released, 'volume attachment')}")
    ksend("DELETE", f"/api/v1/nodes/{name}")
    log.append(f"Deleted node {name}")
    removed = _delete_machines_for(name, plan["machine"])
    log.extend(f"Deleted Cluster API machine {m}" for m in removed)
    if gone:
        for machine_name in removed:
            if _unstick_machine(machine_name):
                log.append(f"Cleared the finalizers holding machine {machine_name}")
        dropped = sum(_force_delete(f"/apis/longhorn.io/v1beta2/namespaces/longhorn-system/replicas/"
                                    f"{r['metadata']['name']}") for r in replicas)
        if dropped:
            log.append(f"Deleted {_plural(dropped, 'replica record')}; Longhorn rebuilds them from the remaining copies")
    try:
        ksend("DELETE", lh)
        log.append(f"Deleted Longhorn's record of {name}")
    except urllib.error.HTTPError as error:
        if error.code != 404:
            log.append(f"Longhorn kept its record of {name} for now: it still lists replicas there. "
                       f"Clean up again once they have rebuilt elsewhere, or force it if the host is gone for good.")
    if OPS:
        OPS.start("cluster-cleanup", f"Removed {name}", {"kind": "Node", "name": name},
                  "/system/cluster", {"log": log}, "; ".join(log))
    return {"ok": True, "node": name, "log": log}


def _machines():
    return _list("/apis/cluster.x-k8s.io/v1beta1/namespaces/fleet-local/machines")


def _unstick_machine(machine_name):
    """A machine whose deletion waits on a host that will never answer: let it go."""
    path = f"/apis/cluster.x-k8s.io/v1beta1/namespaces/fleet-local/machines/{machine_name}"
    machine = _get(path)
    if not machine or not machine["metadata"].get("deletionTimestamp"):
        return False
    ksend("PATCH", path, {"metadata": {"finalizers": None}}, "application/merge-patch+json")
    return True


def _delete_machines_for(name, known):
    removed = []
    for machine in _machines():
        meta = machine["metadata"]
        node_ref = ((machine.get("status") or {}).get("nodeRef") or {}).get("name", "")
        if node_ref == name or meta["name"] == known.get("name"):
            try:
                ksend("DELETE", f"/apis/cluster.x-k8s.io/v1beta1/namespaces/{meta['namespace']}/machines/{meta['name']}")
                removed.append(meta["name"])
            except urllib.error.HTTPError as error:
                if error.code != 404:
                    raise
    return removed


def cleanup_report():
    """What is left over: dead nodes, machines with no node, Longhorn nodes with no node."""
    nodes = (_get("/api/v1/nodes") or {}).get("items", [])
    names = {n["metadata"]["name"] for n in nodes}
    dead = [{"name": n["metadata"]["name"], "roles": _roles(n), "since": _not_ready_since(n)}
            for n in nodes if not _ready(n)]
    stale_machines = []
    for machine in _machines():
        node_ref = ((machine.get("status") or {}).get("nodeRef") or {}).get("name", "")
        phase = (machine.get("status") or {}).get("phase", "")
        deleting = bool(machine["metadata"].get("deletionTimestamp"))
        orphaned = (node_ref and node_ref not in names) or (not node_ref and phase in ("Failed", "Deleting"))
        if orphaned or (deleting and node_ref not in names):
            stale_machines.append({"name": machine["metadata"]["name"], "node": node_ref, "phase": phase,
                                   "stuck": deleting, "created": machine["metadata"].get("creationTimestamp", "")})
    lh_nodes = _list("/apis/longhorn.io/v1beta2/namespaces/longhorn-system/nodes")
    _, by_node = _replicas_by_node()
    stale_longhorn = [{"name": n["metadata"]["name"], "replicas": len(by_node.get(n["metadata"]["name"], ()))}
                      for n in lh_nodes if n["metadata"]["name"] not in names]
    finished = [p["id"] for p in _read() if p.get("status") not in ("waiting", "installing")]
    return {"dead_nodes": dead, "stale_machines": stale_machines, "stale_longhorn": stale_longhorn,
            "finished_plans": len(finished)}


def cleanup(kind, name, force=False):
    """Removes one leftover record the report found. `force` is for a host that is gone for good."""
    if kind == "machine":
        stale = {m["name"]: m for m in cleanup_report()["stale_machines"]}
        if name not in stale:
            raise ValueError("that machine is not a leftover; it still has a node")
        if stale[name]["stuck"]:
            if not force:
                raise ValueError(f"{name} is already being deleted and is waiting on its host; "
                                 f"if that host is gone for good, force it")
            _unstick_machine(name)
            return {"ok": True, "message": f"Cleared the finalizers holding machine {name}"}
        ksend("DELETE", f"/apis/cluster.x-k8s.io/v1beta1/namespaces/fleet-local/machines/{name}")
        if force:
            _unstick_machine(name)
        return {"ok": True, "message": f"Deleted leftover machine {name}"}
    if kind == "longhorn":
        stale = {n["name"]: n for n in cleanup_report()["stale_longhorn"]}
        if name not in stale:
            raise ValueError("that Longhorn node still has a Kubernetes node")
        path = f"/apis/longhorn.io/v1beta2/namespaces/longhorn-system/nodes/{name}"
        if stale[name]["replicas"]:
            if not force:
                raise ValueError(f"Longhorn still lists {_plural(stale[name]['replicas'], 'replica')} on {name}; "
                                 f"let them rebuild elsewhere, or force it if the host is gone for good")
            for replica in _list("/apis/longhorn.io/v1beta2/namespaces/longhorn-system/replicas"):
                if (replica.get("spec") or {}).get("nodeID") == name:
                    _force_delete(f"/apis/longhorn.io/v1beta2/namespaces/longhorn-system/replicas/"
                                  f"{replica['metadata']['name']}")
        # Longhorn refuses to delete a node that still allows scheduling.
        ksend("PATCH", path, {"spec": {"allowScheduling": False}}, "application/merge-patch+json")
        ksend("DELETE", path)
        return {"ok": True, "message": f"Deleted Longhorn's record of {name}"}
    if kind == "plans":
        with _lock:
            rows = [row for row in _read() if row.get("status") in ("waiting", "installing")]
            _write(rows)
        return {"ok": True, "message": "Cleared finished join plans"}
    raise ValueError("unknown cleanup")
