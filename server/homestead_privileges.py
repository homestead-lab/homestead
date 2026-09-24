"""What a container may do to the host it runs on, beyond the defaults.

VPN containers (transmission-openvpn, gluetun and friends) add routes and
open a tunnel device, which a container may not do by default:
"RTNETLINK answers: Operation not permitted". On Unraid the template ticks
Privileged, or passes --cap-add=NET_ADMIN --device=/dev/net/tun in its extra
parameters. Here the same needs are three settings:

  tun         /dev/net/tun mounted in, with NET_ADMIN - enough for a VPN,
              without giving the container the rest of the host;
  cap_add     further Linux capabilities, by name;
  privileged  everything, as Unraid's Privileged does - the last resort.

They are read from Docker (an import), from an Unraid template (the App
Store), and set by hand on Deploy and Edit.
"""
import re
import shlex

TUN = "/dev/net/tun"
TUN_VOLUME = "dev-net-tun"
CAP = re.compile(r"^[A-Z_]{2,40}$")


def from_docker(extra="", privileged=False, cap_add=(), devices=()):
    """Privileges from Docker's own settings and a template's extra parameters."""
    caps = {str(c).upper().replace("CAP_", "", 1) for c in cap_add or () if str(c).strip()}
    tun = any(TUN in str(d) for d in devices or ())
    try:
        words = shlex.split(str(extra or ""))
    except ValueError:
        words = str(extra or "").split()
    i = 0
    while i < len(words):
        word = words[i]
        value = ""
        for flag in ("--cap-add", "--device"):
            if word == flag and i + 1 < len(words):
                value, i = words[i + 1], i + 1
                word = flag
            elif word.startswith(flag + "="):
                value, word = word.split("=", 1)[1], flag
        if word == "--cap-add" and value:
            caps.add(value.upper().replace("CAP_", "", 1))
        elif word == "--device" and TUN in value:
            tun = True
        elif word == "--privileged" or word == "--privileged=true":
            privileged = True
        i += 1
    if tun:
        caps.add("NET_ADMIN")
    caps = {c for c in caps if CAP.match(c) and c != "ALL"}
    return {"privileged": bool(privileged), "cap_add": sorted(caps), "tun": tun}


def read(container, podspec):
    """The privileges a container has now, for the editor."""
    security = container.get("securityContext") or {}
    volumes = {v.get("name"): v for v in podspec.get("volumes") or []}
    tun = any(((volumes.get(m.get("name")) or {}).get("hostPath") or {}).get("path") == TUN
              for m in container.get("volumeMounts") or [])
    caps = sorted((security.get("capabilities") or {}).get("add") or [])
    return {"privileged": bool(security.get("privileged")), "tun": tun,
            "cap_add": [c for c in caps if not (tun and c == "NET_ADMIN")]}


def apply(container, podspec, wanted, hardware=False):
    """Make the container's privileges what is asked. hardware keeps it
    privileged whatever is asked: device passthrough needs it."""
    wanted = wanted or {}
    security = container.setdefault("securityContext", {})
    if wanted.get("privileged") or hardware:
        security["privileged"] = True
    else:
        security.pop("privileged", None)
    caps = {str(c).upper() for c in wanted.get("cap_add") or () if str(c).strip()}
    bad = [c for c in caps if not CAP.match(c)]
    if bad:
        raise ValueError(f"{', '.join(bad)} is not a capability name like NET_ADMIN")
    tun = bool(wanted.get("tun"))
    if tun:
        caps.add("NET_ADMIN")
    if caps:
        security.setdefault("capabilities", {})["add"] = sorted(caps)
    elif "capabilities" in security:
        security["capabilities"].pop("add", None)
        if not security["capabilities"]:
            security.pop("capabilities")
    if not security:
        container.pop("securityContext", None)
    # The tunnel device: one hostPath volume, mounted where VPN clients look.
    mounts = [m for m in container.get("volumeMounts") or [] if m.get("name") != TUN_VOLUME]
    if tun:
        mounts.append({"name": TUN_VOLUME, "mountPath": TUN})
        if not any(v.get("name") == TUN_VOLUME for v in podspec.get("volumes") or []):
            podspec.setdefault("volumes", []).append(
                {"name": TUN_VOLUME, "hostPath": {"path": TUN, "type": "CharDevice"}})
    if mounts:
        container["volumeMounts"] = mounts
    else:
        container.pop("volumeMounts", None)
    in_use = any(m.get("name") == TUN_VOLUME for c in podspec.get("containers") or [container]
                 for m in c.get("volumeMounts") or [])
    if not in_use and not tun:
        podspec["volumes"] = [v for v in podspec.get("volumes") or [] if v.get("name") != TUN_VOLUME]
        if not podspec["volumes"]:
            podspec.pop("volumes")
    return container
