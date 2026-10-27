"""What the networking modules share: the clients and settings
homestead_networking.bind() sets, and the small readers and checks the
inventory, the Services, the VIP registry and the VM networks all use (#623).

Replace kget, ksend, SYSTEM_NAMESPACES, DEFAULT_NAMESPACE or SHARED_VIP here:
they live only in this module.
"""
import ipaddress
import re
import time
import urllib.error

import homestead_kube as KUBE


kget = ksend = None
SYSTEM_NAMESPACES = set()
DEFAULT_NAMESPACE = "lab"
SHARED_VIP = ""
DNS_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
VIP_ANNOTATION = "kube-vip.io/loadbalancerIPs"


def _items(path, strict=False):
    """A list read. An optional one (strict=False) is [] when it fails: an
    API the cluster may not have, such as Harvester's IP pools. A strict one
    raises: Services, EndpointSlices, Deployments and Nodes decide which
    address and port are free, and none seen is not none in use (#615)."""
    for attempt in range(2):
        try:
            listing = kget(path) if strict else KUBE.get_optional(path, read=kget, missing=KUBE.GONE)
            return [] if listing is KUBE.GONE else listing.get("items", [])
        except urllib.error.HTTPError as error:
            if error.code == 403 and not strict:
                return []
            if error.code == 429 and attempt == 0:
                try:
                    delay = float((error.headers or {}).get("Retry-After", "0.5"))
                except (TypeError, ValueError):
                    delay = 0.5
                time.sleep(max(0.0, min(2.0, delay)))
                continue
            raise
        except Exception:
            if strict:
                raise
            return []
    return []


def _unread(state):
    """What could not be read for this state, in words, or ""."""
    unread = state.get("unread") or []
    return f"could not read the cluster's {' and '.join(unread)}" if unread else ""


def _complete(state, outcome):
    """state, or a refusal naming what could not be read and what did not happen."""
    if _unread(state):
        raise ValueError(f"{_unread(state)}; {outcome}. Try again in a moment")
    return state


def _read(path, what):
    """A strict list read whose failure says what could not be read."""
    try:
        return _items(path, strict=True)
    except Exception as error:
        print(f"Networking: could not read {what}: {str(error)[:160]}", flush=True)
        raise ValueError(f"could not read the cluster's {what}; nothing was changed. Try again in a moment") from error


def _name(value, label):
    value = str(value or "").strip().lower()
    if not DNS_NAME.fullmatch(value):
        raise ValueError(f"{label} must be a Kubernetes name using lowercase letters, numbers and dashes")
    return value


def _ipv4(value, label="VIP"):
    try:
        address = ipaddress.ip_address(str(value or "").strip())
    except ValueError as error:
        raise ValueError(f"{label} must be a valid IPv4 address") from error
    if address.version != 4 or address.is_multicast or address.is_unspecified or address.is_loopback:
        raise ValueError(f"{label} must be a usable unicast IPv4 address")
    return str(address)
