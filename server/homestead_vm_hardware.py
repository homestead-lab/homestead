"""A VM's virtual hardware beyond CPU count and memory, read from and written
to its KubeVirt template: CPU model and topology, dedicated CPUs, firmware,
TPM, machine type, guest enlightenments, clock, the devices KubeVirt adds by
default, hugepages and what happens on node drain.

Only the fields a form changes are written. What KubeVirt would refuse is
refused here with the reason (Secure Boot without UEFI), and what depends on
the cluster is said as a requirement rather than guessed (dedicated CPUs need
the kubelet's static CPU manager; persistent EFI and TPM state need KubeVirt's
VMPersistentState feature on older releases).
"""
import copy
import re

MODELS = ("host-passthrough", "host-model")
FIRMWARE = ("bios", "uefi")
TPM = ("off", "on", "persistent")
HUGEPAGES = ("", "2Mi", "1Gi")
EVICTION = ("", "LiveMigrate", "LiveMigrateIfPossible", "None")
MACHINES = ("q35", "pc-q35", "virt")
TIMEZONE = re.compile(r"[A-Za-z0-9_+\-]+(/[A-Za-z0-9_+\-]+){0,2}")
CPU_MODEL = re.compile(r"[A-Za-z0-9._\-]{1,64}")
BOOT_OUTPUT = "homestead.io/boot-output"
# Hyper-V enlightenments a Windows guest runs better with, as Proxmox and
# KubeVirt's own Windows examples set them.
HYPERV = {"relaxed": {}, "vapic": {}, "spinlocks": {"spinlocks": 8191}, "vpindex": {}, "synic": {},
          "synictimer": {"direct": {}}, "tlbflush": {}, "frequencies": {}, "reenlightenment": {},
          "ipi": {}, "runtime": {}, "reset": {}}
TABLET = {"type": "tablet", "bus": "usb", "name": "tablet"}


def read(vm):
    """The hardware settings a VM has now, in the form's terms."""
    tspec = ((vm.get("spec") or {}).get("template") or {}).get("spec") or {}
    dom = tspec.get("domain") or {}
    cpu, devices = dom.get("cpu") or {}, dom.get("devices") or {}
    firmware = dom.get("firmware") or {}
    loader = firmware.get("bootloader") or {}
    efi = loader.get("efi")
    features = dom.get("features") or {}
    clock = dom.get("clock") or {}
    tpm = devices.get("tpm")
    graphics = devices.get("autoattachGraphicsDevice") is not False
    saved_output = ((((vm.get("spec") or {}).get("template") or {}).get("metadata") or {}).get("annotations") or {}).get(BOOT_OUTPUT)
    return {
        "cpu": {"sockets": int(cpu.get("sockets") or 1), "cores": int(cpu.get("cores") or 1),
                "threads": int(cpu.get("threads") or 1), "model": str(cpu.get("model") or ""),
                "dedicated": bool(cpu.get("dedicatedCpuPlacement")),
                "isolate_emulator": bool(cpu.get("isolateEmulatorThread"))},
        "firmware": "uefi" if efi is not None else "bios",
        "secure_boot": bool(efi is not None and (efi or {}).get("secureBoot", True)),
        "efi_persistent": bool((efi or {}).get("persistent")),
        "tpm": "off" if tpm is None or tpm.get("enabled") is False else "persistent" if tpm.get("persistent") else "on",
        "machine": str((dom.get("machine") or {}).get("type") or ""),
        "hyperv": bool(features.get("hyperv")),
        "kvm_hidden": bool((features.get("kvm") or {}).get("hidden")),
        "timezone": str(clock.get("timezone") or ""),
        "graphics": graphics,
        "boot_output": "console" if graphics else "gpu" if saved_output == "gpu" or devices.get("gpus") else "serial",
        "serial": devices.get("autoattachSerialConsole") is not False,
        "tablet": any(i.get("type") == "tablet" for i in devices.get("inputs") or []),
        "rng": devices.get("rng") is not None,
        "balloon": devices.get("autoattachMemBalloon") is not False,
        "sound": devices.get("sound") is not None,
        "hugepages": str(((dom.get("memory") or {}).get("hugepages") or {}).get("pageSize") or ""),
        "eviction": str(tspec.get("evictionStrategy") or ""),
    }


def _bool(value):
    return value is True or str(value).lower() in ("true", "1", "yes", "on")


def _has_gpu(devices):
    if devices.get("gpus"):
        return True
    if not devices.get("hostDevices"):
        return False
    import homestead_passthrough as PASSTHROUGH
    verified = {row["resource"] for row in PASSTHROUGH.resources()["resources"] if row.get("gpu")}
    return any(device.get("deviceName") in verified for device in devices["hostDevices"])


def validate_boot_output(vm):
    """Validate the resulting explicit selection, including field-only edits."""
    template = vm["spec"]["template"]
    output = ((template.get("metadata") or {}).get("annotations") or {}).get(BOOT_OUTPUT)
    if not output:
        return  # Preserve older, independently configured display settings.
    settings = read(vm)
    devices = template["spec"]["domain"].get("devices") or {}
    if settings["graphics"] != (output == "console"):
        raise ValueError("Primary boot output conflicts with the virtual display setting")
    if output == "gpu":
        if not _has_gpu(devices):
            raise ValueError("GPU boot output needs an attached GPU verified in the host device inventory; inspect its host or choose web console output")
        if settings["firmware"] != "uefi":
            raise ValueError("GPU boot output needs UEFI firmware; choose web console output before switching to BIOS")
    if output == "serial" and not settings["serial"]:
        raise ValueError("Serial boot output needs the serial console enabled; choose another boot output before disabling it")


def apply(vm, cfg, locked_cpu=False):
    """Write the settings in cfg (the form's changed fields only) onto the
    VM. Returns True when anything changed; raises ValueError for a setting
    KubeVirt would refuse."""
    before = copy.deepcopy(vm)
    tspec = vm["spec"]["template"]["spec"]
    dom = tspec.setdefault("domain", {})
    devices = dom.setdefault("devices", {})
    now = read(vm)
    cfg = dict(cfg)
    if "boot_output" in cfg:
        output = cfg["boot_output"]
        if output not in ("console", "gpu", "serial"):
            raise ValueError("Primary boot output is web console, passed-through GPU, or serial console")
        if "graphics" in cfg and _bool(cfg["graphics"]) != (output == "console"):
            raise ValueError("Primary boot output conflicts with the virtual display setting")
        cfg["graphics"] = output == "console"
        if output == "gpu":
            if cfg.get("firmware", "uefi") != "uefi":
                raise ValueError("GPU boot output needs UEFI firmware")
            cfg["firmware"] = "uefi"
            if now["firmware"] != "uefi":
                cfg.setdefault("secure_boot", False)
        if output == "serial":
            if "serial" in cfg and not _bool(cfg["serial"]):
                raise ValueError("Serial boot output needs the serial console enabled")
            cfg["serial"] = True
        annotations = vm["spec"]["template"].setdefault("metadata", {}).setdefault("annotations", {})
        annotations[BOOT_OUTPUT] = output
    elif "graphics" in cfg:
        # Legacy callers and presets must not retain a stale GPU selection.
        annotations = (vm["spec"]["template"].get("metadata") or {}).get("annotations") or {}
        annotations.pop(BOOT_OUTPUT, None)

    if "cpu" in cfg:
        want = cfg["cpu"] or {}
        cpu = dom.setdefault("cpu", {})
        topology = {k: want[k] for k in ("sockets", "cores", "threads") if k in want}
        if topology and locked_cpu:
            raise ValueError("CPU topology is controlled by this VM's instance type")
        for key, value in topology.items():
            number = int(value)
            if not 1 <= number <= (8 if key != "cores" else 128):
                raise ValueError(f"CPU {key} must be between 1 and {8 if key != 'cores' else 128}")
            cpu[key] = number
        total = int(cpu.get("sockets") or 1) * int(cpu.get("cores") or 1) * int(cpu.get("threads") or 1)
        if total > 256:
            raise ValueError("at most 256 vCPUs")
        if topology:
            limits = (dom.get("resources") or {}).get("limits")
            if limits and "cpu" in limits:
                limits["cpu"] = str(total)
        if "model" in want:
            model = str(want["model"] or "").strip()
            if model and model not in MODELS and not CPU_MODEL.fullmatch(model):
                raise ValueError(f"{model} is not a CPU model name")
            if model:
                cpu["model"] = model
            else:
                cpu.pop("model", None)
        for key, field in (("dedicated", "dedicatedCpuPlacement"), ("isolate_emulator", "isolateEmulatorThread")):
            if key in want:
                if _bool(want[key]):
                    cpu[field] = True
                else:
                    cpu.pop(field, None)
        if cpu.get("isolateEmulatorThread") and not cpu.get("dedicatedCpuPlacement"):
            raise ValueError("an isolated emulator thread needs dedicated CPUs")
        if not cpu:
            dom.pop("cpu", None)

    if any(key in cfg for key in ("firmware", "secure_boot", "efi_persistent")):
        firmware_kind = cfg.get("firmware", now["firmware"])
        if firmware_kind not in FIRMWARE:
            raise ValueError("firmware is BIOS or UEFI")
        firmware = dom.setdefault("firmware", {})
        if firmware_kind == "bios":
            if _bool(cfg.get("secure_boot")):
                raise ValueError("Secure Boot needs UEFI firmware")
            firmware["bootloader"] = {"bios": {}}
        else:
            secure = _bool(cfg.get("secure_boot", now["secure_boot"]))
            efi = {"secureBoot": secure}
            if _bool(cfg.get("efi_persistent", now["efi_persistent"])):
                efi["persistent"] = True
            firmware["bootloader"] = {"efi": efi}
            if secure:
                # KubeVirt refuses Secure Boot without SMM.
                dom.setdefault("features", {})["smm"] = {"enabled": True}

    if "tpm" in cfg:
        if cfg["tpm"] not in TPM:
            raise ValueError("TPM is off, on, or on with persistent state")
        if cfg["tpm"] == "off":
            devices.pop("tpm", None)
        else:
            devices["tpm"] = {"persistent": True} if cfg["tpm"] == "persistent" else {}

    if "machine" in cfg:
        machine = str(cfg["machine"] or "").strip()
        if machine and machine not in MACHINES and not re.fullmatch(r"pc-q35-[a-z0-9.\-]{1,40}", machine):
            raise ValueError(f"{machine} is not a machine type KubeVirt offers")
        if machine:
            dom["machine"] = {"type": machine}
        else:
            dom.pop("machine", None)

    if "hyperv" in cfg or "kvm_hidden" in cfg:
        features = dom.setdefault("features", {})
        if "hyperv" in cfg:
            if _bool(cfg["hyperv"]):
                features["hyperv"] = copy.deepcopy(HYPERV)
                timer = dom.setdefault("clock", {}).setdefault("timer", {})
                timer.setdefault("hyperv", {})
            else:
                features.pop("hyperv", None)
                ((dom.get("clock") or {}).get("timer") or {}).pop("hyperv", None)
        if "kvm_hidden" in cfg:
            if _bool(cfg["kvm_hidden"]):
                features["kvm"] = {"hidden": True}
            else:
                features.pop("kvm", None)
        if not features:
            dom.pop("features", None)

    if "timezone" in cfg:
        zone = str(cfg["timezone"] or "").strip()
        clock = dom.setdefault("clock", {})
        if zone:
            if not TIMEZONE.fullmatch(zone):
                raise ValueError(f"{zone} is not a time zone name like Europe/London")
            clock.pop("utc", None)
            clock["timezone"] = zone
        else:
            clock.pop("timezone", None)
            clock.setdefault("utc", {})

    for key, field in (("graphics", "autoattachGraphicsDevice"), ("serial", "autoattachSerialConsole"),
                       ("balloon", "autoattachMemBalloon")):
        if key in cfg:
            if _bool(cfg[key]):
                devices.pop(field, None)
            else:
                devices[field] = False
    if "tablet" in cfg:
        inputs = [i for i in devices.get("inputs") or [] if i.get("type") != "tablet"]
        if _bool(cfg["tablet"]):
            inputs.append(dict(TABLET))
        if inputs:
            devices["inputs"] = inputs
        else:
            devices.pop("inputs", None)
    if "rng" in cfg:
        if _bool(cfg["rng"]):
            devices["rng"] = {}
        else:
            devices.pop("rng", None)
    if "sound" in cfg:
        if _bool(cfg["sound"]):
            devices["sound"] = {"name": "sound", "model": "ich9"}
        else:
            devices.pop("sound", None)

    if "hugepages" in cfg:
        if cfg["hugepages"] not in HUGEPAGES:
            raise ValueError("hugepages are 2Mi or 1Gi pages, or none")
        memory = dom.setdefault("memory", {})
        if cfg["hugepages"]:
            memory["hugepages"] = {"pageSize": cfg["hugepages"]}
        else:
            memory.pop("hugepages", None)

    if "eviction" in cfg:
        if cfg["eviction"] not in EVICTION:
            raise ValueError("on node drain a VM live-migrates, tries to, or stops")
        if cfg["eviction"]:
            tspec["evictionStrategy"] = cfg["eviction"]
        else:
            tspec.pop("evictionStrategy", None)
    validate_boot_output(vm)
    return vm != before


def requirements(settings, kubevirt=None, nodes=0):
    """What the cluster must provide for these settings, said before saving."""
    gates = set((((kubevirt or {}).get("spec") or {}).get("configuration") or {})
                .get("developerConfiguration", {}).get("featureGates") or [])
    notes = []
    if settings.get("boot_output") == "gpu":
        notes.append("GPU boot output uses UEFI and turns off the VNC display. Connect the monitor to the passed-through GPU; "
                     "it needs a UEFI-capable ROM and guest drivers. Changing firmware can require repairing the guest bootloader.")
    cpu = settings.get("cpu") or {}
    if cpu.get("dedicated"):
        notes.append("Dedicated CPUs need nodes whose kubelet runs the static CPU manager policy; "
                     "elsewhere the VM does not start.")
    if settings.get("hugepages"):
        notes.append(f"Hugepages of {settings['hugepages']} must be reserved on the node (a kernel "
                     "setting), or the VM does not start.")
    persistent = settings.get("efi_persistent") or settings.get("tpm") == "persistent"
    if persistent and "VMPersistentState" not in gates:
        notes.append("Persistent EFI variables or TPM state are kept in a small volume per VM; on "
                     "KubeVirt releases before 1.5 this needs its VMPersistentState feature.")
    if settings.get("secure_boot"):
        notes.append("Secure Boot needs a guest whose bootloader is signed - Windows 11, and most "
                     "current Linux installers.")
    if settings.get("eviction") in ("LiveMigrate",) and nodes < 2:
        notes.append("Live migration needs a second node; on one node a drain waits for the VM.")
    if cpu.get("model") == "host-passthrough":
        notes.append("host-passthrough exposes this node's exact CPU; the VM can then only live-"
                     "migrate to nodes with the same CPU.")
    return notes


def cpu_models(nodes):
    """CPU models every schedulable node supports, from KubeVirt's node labels."""
    sets = []
    for node in nodes:
        labels = (node.get("metadata") or {}).get("labels") or {}
        models = {key.split("/", 1)[1] for key, value in labels.items()
                  if key.startswith("cpu-model.node.kubevirt.io/") and value == "true"}
        if models:
            sets.append(models)
    common = set.intersection(*sets) if sets else set()
    return sorted(common)
