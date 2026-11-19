"""Host kernels on validated versions only (issue #1348, section 5.3).

On a host of the validated tier (Ubuntu 26.04, homestead_validated) with
Settings > Updates > Host kernels on validated versions only:

- the packages that bring new kernels in - Ubuntu's linux-generic and
  linux-virtual meta packages, with their image and headers - and the ones
  that change confinement, apparmor and linux-firmware, are held with
  apt-mark, so that nothing else installs them: not unattended-upgrades, not
  a plain upgrade, not Homestead's own (upgrade --with-new-pkgs keeps holds);
- an update Homestead runs on the host first installs the newest validated
  kernel by its exact version (install_command), so with the meta packages
  held the newest kernel installed is always a validated one, and GRUB boots
  it. Earlier kernels stay installed as the fallback: Homestead never removes
  a package;
- a host running a kernel the list marks bad is offered Boot the last
  validated kernel (boot_validated): GRUB's default is set to the newest
  installed validated kernel, and the host is restarted through the normal
  restart review.

Until the list has validated a kernel for the host's distribution nothing is
held: holding would stop every kernel update, security fixes included.
Switching back to any version lets go of the holds Homestead made, and only
those: a hold a person made stays. Each host is checked from what its last
read saw (homestead_host_os), so a host command runs only when something is
to change.
"""
import os
import re
import time

import homestead_hostrun as HOSTRUN
import homestead_shared as SHARED
import homestead_validated as VALIDATED

hostrun = None
DATA_DIR = None     # None: homestead_shared's
HELD = re.compile(r"linux-(generic|virtual|image-generic|image-virtual|headers-generic|headers-virtual)(-hwe-[0-9.]+)?|apparmor|linux-firmware")
GRUB_FILE = "/etc/default/grub.d/90-homestead-kernel.cfg"
ABI = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+-[0-9]+-[a-z0-9]+")


def bind(_hostrun, data_dir=None):
    global hostrun, DATA_DIR
    hostrun, DATA_DIR = _hostrun, data_dir


def _path():
    return os.path.join(DATA_DIR or SHARED.DIR, "kernel-holds.json")


def _load():
    try:
        return SHARED.read_json(_path(), {})
    except SHARED.Unreadable:
        return None


def applies(facts):
    return (facts or {}).get("package_manager") == "apt" and VALIDATED.validated_tier(VALIDATED.host_key(facts))


def _version_key(version):
    """A Debian version like 7.0.0-43.43 as numbers, for comparing kernels."""
    return tuple(int(part) for part in re.findall(r"\d+", str(version or "")))


def _validated(facts):
    """The validated kernel entries for this host's distribution, newest first."""
    document, _ = VALIDATED.current()
    entries = (document.get("host") or {}).get(VALIDATED.host_key(facts)) or []
    # A kernel the list marks bad that this release copes with counts as
    # validated: Homestead sets its workaround on the host by itself.
    rows = [e for e in entries if (e.get("status") == "validated" or VALIDATED.fixed(e))
            and str(e.get("package", "")).startswith("linux-image-")]
    return sorted(rows, key=lambda e: _version_key(e["version"]), reverse=True)


def installed(facts):
    """{package: version} of the kernel images installed on the host."""
    return dict(item.split("=", 1) for item in (facts or {}).get("kernels") or []
                if "=" in item and item.startswith("linux-image-"))


def fallback(facts):
    """The newest validated kernel installed on the host, as its uname -r, or ""."""
    have = installed(facts)
    for entry in _validated(facts):
        if have.get(entry["package"]) == entry["version"]:
            return entry["package"][len("linux-image-"):]
    return ""


def install_command(facts):
    """The apt command that installs the newest validated kernel by its exact
    version, or "" when that one is installed or none is validated, or the
    host is not on validated versions only."""
    if not applies(facts) or VALIDATED.settings()["host"] != "validated":
        return ""
    newest = next(iter(_validated(facts)), None)
    if not newest or installed(facts).get(newest["package"]) == newest["version"]:
        return ""
    abi = newest["package"][len("linux-image-"):]
    if not ABI.fullmatch(abi) or not re.fullmatch(r"[0-9A-Za-z.+~:-]+", newest["version"]):
        return ""
    packages = [f"{newest['package']}={newest['version']}", f"linux-modules-{abi}={newest['version']}"]
    if any(item.startswith("linux-headers-") for item in (facts or {}).get("kernels") or []):
        packages.append(f"linux-headers-{abi}={newest['version']}")
    return ("DEBIAN_FRONTEND=noninteractive apt-get -y -o DPkg::Lock::Timeout=300 "
            "-o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold install " + " ".join(packages))


def _hold_script():
    return (f"P=$(dpkg-query -W -f='${{Package}} ${{db:Status-Status}}\\n' 2>/dev/null | awk '$2==\"installed\" {{print $1}}' "
            f"| grep -Ex '{HELD.pattern}'); [ -n \"$P\" ] && apt-mark hold $P >/dev/null; echo \"HELD $P\"")


def _release_script(packages):
    names = " ".join(p for p in packages if HELD.fullmatch(p))
    return f"[ -n '{names}' ] && apt-mark unhold {names} >/dev/null; echo RELEASED" if names else "echo RELEASED"


def _run(node, script, change):
    import homestead_host_os as HOST_OS
    return (hostrun or HOST_OS.hostrun).run(node, script, timeout=60, change=change)


def tick(hosts, now=None):
    """Hosts, by name, with their last read facts: hold or let go as the
    setting says. A host is acted on again only once a read newer than the
    last action shows a hold missing. Returns {node: what changed}."""
    now = time.time() if now is None else now
    state = _load()
    if state is None:
        return {}
    mode, changed = VALIDATED.settings()["host"], {}
    for node, facts in sorted((hosts or {}).items()):
        if not applies(facts):
            continue
        # Until the list has validated a kernel here, holding would stop every
        # kernel update, security fixes included: kernels then update as before.
        want = mode == "validated" and bool(_validated(facts))
        record = state.get(node) or {}
        ours, held = record.get("packages") or [], set((facts or {}).get("held") or [])
        read_since = float((facts or {}).get("at") or 0) > float(record.get("at") or 0)
        try:
            if want:
                if node in state and (not read_since or set(ours) <= held):
                    continue
                out, _ = _run(node, _hold_script(), f"Hold kernel packages on {node} to validated versions")
                words = out.split()
                if not words or words[0] != "HELD":
                    raise ValueError(f"Homestead could not hold {node}'s kernel packages: {out[-160:]}")
                made = [p for p in words[1:] if p not in held or p in ours]
                state[node] = {"packages": sorted(set(ours) | set(made)), "at": now}
                changed[node] = f"held {', '.join(words[1:]) or 'nothing (no kernel packages installed)'}"
            elif node in state:
                if ours:
                    out, _ = _run(node, _release_script(ours), f"Let go of the kernel package holds on {node}")
                    if "RELEASED" not in out:
                        raise ValueError(f"Homestead could not release {node}'s kernel packages: {out[-160:]}")
                    changed[node] = f"released {', '.join(ours)}"
                state.pop(node, None)
            else:
                continue
        except HOSTRUN.Busy:
            continue      # another change has the host: next round
        SHARED.write_json(_path(), state, indent=1, sort_keys=True)
    return changed


def boot_script(abi):
    """GRUB's default set to this kernel's entry under Advanced options, by the
    entry ids grub.cfg gives them."""
    if not ABI.fullmatch(abi):
        raise ValueError(f"{abi} is not a kernel release like 7.0.0-38-generic.")
    return f"""G=/boot/grub/grub.cfg
[ -r "$G" ] && command -v update-grub >/dev/null || {{ echo "NOGRUB"; exit 0; }}
SUB=$(grep -o "^submenu .*\\$menuentry_id_option '[^']*'" "$G" | head -n 1 | sed -E "s/.*'([^']*)'$/\\1/")
ENT=$(grep -o "\\$menuentry_id_option 'gnulinux-{abi}-advanced-[^']*'" "$G" | head -n 1 | sed -E "s/.*'([^']*)'$/\\1/")
[ -n "$SUB" ] && [ -n "$ENT" ] || {{ echo "NOENTRY"; exit 0; }}
mkdir -p /etc/default/grub.d
printf '# Homestead: boot the last validated kernel ({abi}).\\nGRUB_DEFAULT="%s>%s"\\n' "$SUB" "$ENT" > {GRUB_FILE}
update-grub >/dev/null 2>&1 && echo "SET $ENT" || echo "FAILED"
"""


def boot_validated(body):
    """Point the host's boot at the newest validated kernel installed on it.
    The restart itself is the normal one, reviewed by the person."""
    import homestead_host_os as HOST_OS
    node = str((body or {}).get("node") or "")
    facts = HOST_OS.stored(node)
    if not facts:
        raise ValueError(f"Homestead has not read {node}'s operating system yet.")
    abi = fallback(facts)
    if not abi:
        raise ValueError(f"{node} has no validated kernel installed to boot.")
    if abi == facts.get("kernel"):
        raise ValueError(f"{node} already runs {abi}, the newest validated kernel it has.")
    out, err = (hostrun or HOST_OS.hostrun).run(node, boot_script(abi), timeout=120, change=f"Boot {node} on kernel {abi} next")
    word = (out.split() or [""])[0]
    if word == "NOGRUB":
        raise ValueError(f"{node} does not boot with GRUB, so Homestead cannot choose its kernel. Choose it at the host's boot menu.")
    if word == "NOENTRY":
        raise ValueError(f"GRUB on {node} lists no entry for {abi}.")
    if word != "SET":
        raise ValueError(f"Homestead could not set {node}'s boot kernel: {(err or out)[-200:]}")
    return {"ok": True, "node": node, "kernel": abi, "at": int(time.time()),
            "detail": f"{node} boots on {abi} at its next restart. Restart it now to switch."}


ROUTES = {
    ("POST", "/api/node/kernel/boot-validated"): ("admin", lambda request: boot_validated(request.body)),
}
