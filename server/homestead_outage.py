"""What to do when an app or VM is down: nothing, unless asked.

Each app and VM may have actions for an outage, kept as an annotation on it
(homestead.io/outage-actions), so setting them restarts nothing:

* restart it once it has been down for after_min minutes, then again every
  after_min minutes it stays down, at most max times. The count starts again
  once it has answered for UP_RESET seconds. Out of tries, it is left alone and
  an alert says so. A VM is rebooted cleanly, and only when it is monitored on
  a port of its own choosing: automatically, Homestead cannot tell a VM that is
  down from one that does not answer on the usual ports.
* call a webhook - an HTTP POST - when it goes down, when it comes back, and
  when an automatic update of it finishes or is rolled back. Its format is
  generic JSON (the default), ntfy (the sentence as text, with Title, Priority
  and Tags headers) or Gotify (JSON title, message and priority) (#741).

A webhook's address can carry secrets anywhere after its host (a password
before the host, a token in the path, the query or the fragment), so it is
only ever shown as its scheme, host and port, with everything after the host
as HIDDEN: in the app and VM lists, the Resources page, errors and the
leader's record of its last call. Saving or testing the address as shown
uses the stored one, and only while the scheme, host and port are unchanged,
so a hidden part never moves to another host. The admin-only reveal on the
Resources page shows it all.

Where it is kept (#931): an address with anything after its host is kept in
a Secret of Homestead's own per app or VM (homestead_outage_secret); the
annotation holds only what anyone may see, and the Secret's name ("secret").
An annotation that still holds a whole address - saved before, or typed on
the Resources page - is used as it is, and the leader moves it over after
its next monitoring round (migrate).

Where a webhook may go: every address its name resolves to is checked, and
the call is made to the addresses checked (no second lookup): each in turn,
until one answers, within WEBHOOK_TIMEOUT. Loopback,
link-local, multicast and cloud metadata addresses are never reached; an
IPv6 address that carries an IPv4 one (NAT64 64:ff9b::/96, 6to4) is checked
as the IPv4 inside, and local-use NAT64 and Teredo are never reached. A LAN
or cluster address (private, carrier-grade NAT, unique local) - a
self-hosted ntfy or Gotify, say - only when an admin ticked "Allow a LAN
address" for that webhook (lan); an operator's save keeps an admin's
allowance only while the address is unchanged. No redirect is followed.
Send a test is admin-only, may reach the LAN, and goes out at most
TEST_LIMIT times a minute.

Nothing is restarted while a job is working on the app (an update, a
rollback, a move, a restore test, a scheduled stop or start): an automatic
update watches the app itself and rolls the image back if it stays down, and a
restart in the middle would read as the update failing.

The leader runs tick() after each monitoring round. Its state - restarts done,
what each webhook was last told - is a small file, written when it changes.
"""
import base64
import http.client
import ipaddress
import json
import os
import re
import socket
import ssl
import threading
import time
import urllib.parse

import homestead_kube as KUBE
import homestead_leader_state as LEADER_STATE
import homestead_names as NAMES
import homestead_outage_secret as SECRET
import homestead_routes as ROUTER
import homestead_shared as SHARED
import homestead_uptime as UPTIME

KEY = "outage-actions"
UP_RESET = 30 * 60
MAX_RESTARTS = 10
WEBHOOK_TIMEOUT = 10
DATA_DIR = "/data"
FORMATS = ("generic", "ntfy", "gotify")
HIDDEN = "(hidden)"
TEST_LIMIT, TEST_WINDOW = 5, 60
# Never reached, even on an admin's say: cloud metadata services.
METADATA = [ipaddress.ip_network(n) for n in ("169.254.169.254/32", "fd00:ec2::254/128", "100.100.100.200/32")]
# IPv6 prefixes that carry an IPv4 address: checked as the address inside.
NAT64 = ipaddress.ip_network("64:ff9b::/96")
# Local-use NAT64 (its IPv4 sits at a place the network chooses) and Teredo: never.
NEVER_V6 = [ipaddress.ip_network(n) for n in ("64:ff9b:1::/48", "2001::/32")]
_lock = threading.Lock()
_tests = []


# What server.py lends to read and write the app or VM, and to check its
# name: the same functions its own routes use (bind).
_bound = {"kget": None, "ksend": None, "dns_name": None, "guard": lambda ns, name: None, "ns": "lab"}


def bind(data_dir="/data", kget=None, ksend=None, dns_name=None, guard=None, ns=None):
    global DATA_DIR
    DATA_DIR = data_dir
    for key, value in (("kget", kget), ("ksend", ksend), ("dns_name", dns_name), ("guard", guard), ("ns", ns)):
        if value:
            _bound[key] = value


def _path():
    return os.path.join(DATA_DIR, "outage-actions.json")


def _load():
    try:
        with open(_path(), encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def webhook_url(value):
    """A webhook address, checked; "" for none."""
    url = str(value or "").strip()
    if not url:
        return ""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or len(url) > 500 or any(c in url for c in "\r\n "):
        raise ValueError("the webhook is an http:// or https:// address, up to 500 characters")
    return url


def _parts(url):
    """(scheme, host and port as typed, everything else) of an address."""
    parsed = urllib.parse.urlsplit(str(url or ""))
    userinfo, at, hostport = parsed.netloc.rpartition("@")
    tail = (userinfo + at) + urllib.parse.urlunsplit(("", "", parsed.path, parsed.query, parsed.fragment))
    return parsed.scheme, hostport, "" if tail in ("", "/") else tail


def hide(url):
    """The address as anyone may see it: scheme, host and port, the rest HIDDEN."""
    scheme, hostport, tail = _parts(url)
    return f"{scheme}://{hostport}" + (f"/{HIDDEN}" if tail else "")


def scrub(text, url):
    """text with the address, and every piece after its host, hidden - raw or quoted."""
    text = str(text or "")
    if not url:
        return text
    text = text.replace(str(url), hide(url))
    tail = _parts(url)[2]
    pieces = {tail} | set(re.split(r"[/?#&=@:;,]", tail))
    for piece in sorted((p for p in pieces if len(p) >= 4 and p != HIDDEN), key=len, reverse=True):
        for form in {piece, urllib.parse.quote(piece, safe=""), urllib.parse.quote_plus(piece), urllib.parse.unquote(piece)}:
            text = text.replace(form, HIDDEN)
    return text


def restore(given, stored):
    """The address to keep or use: given as typed, or - when it is shown
    hidden - the stored one, only if its scheme, host and port are unchanged."""
    if HIDDEN not in (given or ""):
        return given
    if stored and hide(stored) == given:
        return stored
    raise ValueError("the address changed, so its hidden part is not used: type the whole address again")


def kept_apart(actions):
    """Whether the annotation keeps the address in a Secret: it holds the
    address hidden, and the Secret's name."""
    hook = (actions or {}).get("webhook")
    return isinstance(hook, str) and HIDDEN in hook and bool(actions.get("secret"))


def public(actions):
    """Actions as the browser may read them: the address hidden after its
    host. The Secret's name only while the address is kept there: the
    leader moves one without it (migrate)."""
    if not actions or not actions.get("webhook"):
        return actions
    shown = {**actions, "webhook": hide(actions["webhook"])}
    if not kept_apart(actions):
        shown.pop("secret", None)
    return shown


def mask_annotations(annotations):
    """Annotations as a reader other than an admin's reveal sees them (Resources page)."""
    raw = (annotations or {}).get(NAMES.key(KEY))
    if raw is None:
        return annotations
    try:
        value = json.loads(raw)
        masked = json.dumps(public(value), separators=(",", ":")) if isinstance(value, dict) else HIDDEN
    except ValueError:
        masked = HIDDEN
    return {**annotations, NAMES.key(KEY): masked}


def unmask_annotations(annotations, current):
    """Annotations edited from a masked copy, with the stored secrets put
    back - only where the rest of the address is unchanged."""
    raw = (annotations or {}).get(NAMES.key(KEY))
    if raw is None or HIDDEN not in raw:
        return annotations
    before = stored(current, NAMES) or {}
    try:
        value = json.loads(raw)
    except ValueError:
        value = None
    if not isinstance(value, dict):
        raise ValueError("the outage actions annotation is hidden: reveal it before editing it")
    hook = restore(str(value.get("webhook") or ""), before.get("webhook"))
    if hook != before.get("webhook"):
        raise ValueError("the outage actions annotation is hidden: reveal it before editing it")
    value = {**value, "webhook": hook}
    # The Secret it is kept in stays the one saved: the YAML cannot point it at another.
    if kept_apart(before):
        value["secret"] = before["secret"]
    else:
        value.pop("secret", None)
    return {**annotations, NAMES.key(KEY): json.dumps(value, separators=(",", ":"))}


def clean(value, kind="app", monitored_on_port=True, before=None, role="operator"):
    """Actions as given, checked: {"restart": {...}?, "webhook": url?, "format": f?, "lan": True?},
    or None for none. before: the actions saved now, whose hidden parts an
    address showing HIDDEN keeps, and whose LAN allowance an operator keeps
    only while the address is unchanged."""
    before = before or {}
    value = value or {}
    if not isinstance(value, dict):
        raise ValueError("outage actions are a restart and a webhook")
    out = {}
    restart = value.get("restart")
    if restart:
        try:
            after, most = int(restart.get("after_min")), int(restart.get("max"))
        except (TypeError, ValueError, AttributeError):
            raise ValueError("say after how many minutes down to restart, and at most how many times") from None
        if not 1 <= after <= 1440:
            raise ValueError("restart after 1 to 1440 minutes down")
        if not 1 <= most <= MAX_RESTARTS:
            raise ValueError(f"restart at most 1 to {MAX_RESTARTS} times")
        if kind == "vm" and not monitored_on_port:
            raise ValueError("a VM is restarted only when it is monitored on a port you chose: "
                             "automatically, Homestead cannot tell it is down")
        out["restart"] = {"after_min": after, "max": most}
    url = webhook_url(restore(webhook_url(value.get("webhook")), before.get("webhook")))
    fmt = str(value.get("format") or "generic")
    if fmt not in FORMATS:
        raise ValueError("the webhook format is generic, ntfy or gotify")
    lan = bool(value.get("lan")) and bool(url)
    if lan and role != "admin" and not (before.get("lan") and url == before.get("webhook")):
        raise PermissionError("only an admin can allow a webhook to reach a LAN address")
    if url:
        out["webhook"] = url
        out["format"] = fmt     # always: a save since #741 is told from one before it (homestead_outage_carry)
        if lan:
            out["lan"] = True
    return out or None


def stored(annotations, names):
    """The actions as kept on the annotation, tokens and all: never for the browser."""
    raw = names.read(annotations or {}, KEY)
    if not raw:
        return None
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) and (value.get("restart") or value.get("webhook")) else None
    except ValueError:
        return None


def read(annotations, names):
    """The actions as the app and VM lists show them: tokens hidden."""
    return public(stored(annotations, names))


def resolved(kind, ns, annotations):
    """The actions with the webhook's whole address, from its Secret when it
    is kept there: never for the browser. The Secret is used only when it is
    Homestead's for this webhook and its address has the scheme, host and
    port the annotation shows; else there is no address, and a call says so."""
    actions = dict(stored(annotations, NAMES) or {})
    apart, ref, hook = kept_apart(actions), actions.pop("secret", None), actions.get("webhook")
    whole = SECRET.load(kind, ns, ref, _bound["kget"]) if apart else None
    if whole and hide(whole) == hook:
        actions["webhook"] = whole
    elif isinstance(hook, str) and HIDDEN in hook:
        actions.pop("webhook")      # the mask alone is no address to call
    return actions


def items(workloads, vms):
    """Every app and VM with actions: kind, ns, name, key (as monitoring keys it), actions."""
    out = []
    for w in workloads or []:
        if w.get("outage_actions"):
            out.append({"kind": "app", "ns": w["ns"], "name": w["name"], "key": f"{w['ns']}/{w['name']}",
                        "actions": w["outage_actions"]})
    for v in vms or []:
        if v.get("outage_actions"):
            out.append({"kind": "vm", "ns": v["ns"], "name": v["name"], "key": f"vm:{v['ns']}/{v['name']}",
                        "actions": v["outage_actions"]})
    return out


def payload(item, event, state=None, detail="", now=None):
    """What a webhook is sent: plain fields, and a sentence as text/content/message
    so chat services (Slack, Discord, Home Assistant) show it as it is.
    ntfy and Gotify get it in their own form (request_for)."""
    state = state or {}
    error = (state.get("last") or {}).get("error") or ""
    what = "VM " if item["kind"] == "vm" else ""
    sentence = {"down": f"{what}{item['name']} is down" + (f": {error}" if error else ""),
                "up": f"{what}{item['name']} is up again",
                "restarted": f"Restarted {what}{item['name']}: {detail}",
                "gave-up": f"{what}{item['name']} is still down after {detail}",
                "update": f"{item['name']}: {detail}",
                "test": f"A test from Homestead for {what}{item['name']}"}.get(event, f"{item['name']}: {event}")
    return {"event": event, "kind": item["kind"], "namespace": item["ns"], "name": item["name"],
            "state": state.get("state", ""), "since": state.get("since"), "error": error, "detail": detail,
            "at": round(now or time.time()), "text": sentence, "content": sentence, "message": sentence}


URGENT = ("down", "gave-up")
NTFY_TAGS = {"down": "rotating_light", "gave-up": "rotating_light", "up": "white_check_mark",
             "restarted": "arrows_counterclockwise", "update": "package", "test": "test_tube"}


# A notice's level as ntfy and Gotify say it.
LEVELS = {"high": ("high", 8), "default": ("default", 5), "low": ("low", 2)}


def _header(text):
    """One line of header text; RFC 2047 (which ntfy reads) when it is not plain ASCII."""
    text = " ".join(str(text or "").split())[:200]
    try:
        text.encode("ascii")
        return text
    except UnicodeEncodeError:
        return "=?utf-8?b?" + base64.b64encode(text.encode()).decode() + "?="


def request_for(url, body, fmt="generic", notice=None):
    """(url, data, headers) for one call in the webhook's format. notice -
    title, message, level (LEVELS) and tags - is what ntfy and Gotify show;
    an outage's is Homestead, its sentence, high when urgent, and its tag.
    Its agent, if any, is the User-Agent (alerts say so; outages as before)."""
    headers = {"User-Agent": (notice or {}).get("agent") or "Homestead outage actions"}
    urgent = body.get("event") in URGENT
    notice = notice or {"title": "Homestead", "message": body.get("message") or "",
                        "level": "high" if urgent else "default", "tags": NTFY_TAGS.get(body.get("event"), "house")}
    ntfy_level, gotify_level = LEVELS.get(notice.get("level"), LEVELS["default"])
    if fmt == "ntfy":
        # ntfy publishes the body of a POST to a topic as the message itself.
        headers.update({"Content-Type": "text/plain; charset=utf-8", "Title": _header(notice["title"]),
                        "Priority": ntfy_level, "Tags": _header(notice["tags"])})
        return url, str(notice["message"] or "").encode(), headers
    if fmt == "gotify":
        data = {"title": notice["title"], "message": notice["message"] or "", "priority": gotify_level}
    else:
        data = body
    headers["Content-Type"] = "application/json"
    return url, json.dumps(data).encode(), headers


class Refused(OSError):
    """An address a webhook may not reach."""


def _resolve(host, port):
    return sorted({info[4][0] for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})


def _kind(ip):
    """public, lan (private, carrier-grade NAT, unique local: a home's own
    services and the cluster's), or never (loopback, link-local, multicast,
    unspecified, reserved, cloud metadata)."""
    address = ipaddress.ip_address(str(ip).split("%")[0])
    if address.version == 6:
        if any(address in net for net in NEVER_V6):
            return "never"
        if address.ipv4_mapped:
            address = address.ipv4_mapped
        elif address.sixtofour:
            address = address.sixtofour
        elif address in NAT64:
            address = ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
    if (any(address in net for net in METADATA if net.version == address.version) or address.is_loopback
            or address.is_link_local or address.is_multicast or address.is_unspecified or address.is_reserved):
        return "never"
    return "public" if address.is_global else "lan"


def _addresses(url):
    """(parsed, every address its host resolves to, port); OSError when none."""
    parsed = urllib.parse.urlsplit(url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        found = _resolve(parsed.hostname, port)
    except (OSError, UnicodeError):
        found = []
    if not found:
        raise OSError(f"{parsed.hostname} could not be found")
    return parsed, found, port


def destinations(url, allow_lan=False):
    """([addresses], port) to connect to: every address the host resolves to checked."""
    parsed, found, port = _addresses(url)
    kinds = {_kind(ip) for ip in found}
    if "never" in kinds:
        raise Refused(f"{parsed.hostname} is this host, a link-local or a cloud metadata address, which a webhook never reaches")
    if "lan" in kinds and not allow_lan:
        raise Refused(f"{parsed.hostname} is a LAN address: an admin must tick Allow a LAN address for this webhook")
    return found, port


def destination(url, allow_lan=False):
    """(first address, port): every address the host resolves to checked."""
    found, port = destinations(url, allow_lan)
    return found[0], port


def _connect(addresses, port, timeout, clock=time.monotonic):
    """A socket to the first of the checked addresses that answers (#931): a
    host with an IPv6 and an IPv4 address, one of them unreachable, still
    gets its call. The tries share timeout, each given an equal part of what
    is left; once one answers it has timeout for each read, as before."""
    deadline = clock() + timeout
    error = None
    for left, address in zip(range(len(addresses), 0, -1), addresses):
        remaining = deadline - clock()
        if remaining <= 0:
            break
        try:
            sock = socket.create_connection((address, port), remaining / left)
        except OSError as failed:
            error = failed
            continue
        sock.settimeout(timeout)
        return sock
    raise error or TimeoutError("timed out")


class _Pinned(http.client.HTTPConnection):
    """A connection to an address checked, whatever the name resolves to now."""

    def __init__(self, host, port, addresses, timeout):
        super().__init__(host, port, timeout=timeout)
        self.addresses = addresses

    def connect(self):
        self.sock = _connect(self.addresses, self.port, self.timeout)


class _PinnedTLS(http.client.HTTPSConnection):
    def __init__(self, host, port, addresses, timeout):
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self.addresses = addresses

    def connect(self):
        self.sock = self._context.wrap_socket(_connect(self.addresses, self.port, self.timeout), server_hostname=self.host)


def post(url, body, fmt="generic", allow_lan=False, notice=None):
    """Send one webhook; raises on failure, in words that never hold its secrets.
    No redirect is followed."""
    url, data, headers = request_for(url, body, fmt, notice)
    parsed = urllib.parse.urlsplit(url)
    addresses, port = destinations(url, allow_lan)
    if "@" in parsed.netloc:
        user, _, password = parsed.netloc.rpartition("@")[0].partition(":")
        pair = f"{urllib.parse.unquote(user)}:{urllib.parse.unquote(password)}".encode()
        headers["Authorization"] = "Basic " + base64.b64encode(pair).decode()
    connection = (_PinnedTLS if parsed.scheme == "https" else _Pinned)(parsed.hostname, port, addresses, WEBHOOK_TIMEOUT)
    try:
        connection.request("POST", (parsed.path or "/") + (f"?{parsed.query}" if parsed.query else ""), body=data, headers=headers)
        status = connection.getresponse().status
    except Exception as error:
        raise OSError(scrub(str(error), url)[:200]) from None
    finally:
        connection.close()
    if 300 <= status < 400:
        raise OSError(f"HTTP {status}: it redirects, and a webhook never follows a redirect")
    if status >= 400:
        raise OSError(f"HTTP {status}")


def tick(scheduled, report, restart, busy, operations=(), send=None, now=None):
    """One look after a monitoring round. restart(item) restarts it (raises
    to say why it could not); busy(item) is whether a job is working on it;
    send(url, body, actions) sends a webhook (url and actions as the lists
    show them, secrets hidden). Returns what was done."""
    now = now or time.time()
    send = send or _send_later
    did = []
    with _lock:
        data = _load()
        state_by_key = data.setdefault("items", {})
        changed = False
        keys = set()
        for item in scheduled:
            key = f"{item['kind']}:{item['ns']}/{item['name']}"
            keys.add(key)
            mine = state_by_key.setdefault(key, {"restarts": 0})
            seen = report.get(item["key"]) or {}
            now_state = seen.get("state")
            hook = (item["actions"] or {}).get("webhook")
            plan = (item["actions"] or {}).get("restart")

            def tell(event, detail=""):
                if hook:
                    send(hook, payload(item, event, seen, detail, now), item["actions"])

            if now_state == "down":
                if mine.get("told") != "down":
                    mine["told"], changed = "down", True
                    tell("down")
                if mine.pop("up_since", None) is not None:
                    changed = True
                if plan and not busy(item):
                    since = seen.get("since") or now
                    wait = plan["after_min"] * 60
                    due = now - since >= wait and now - (mine.get("last_restart") or 0) >= wait
                    if due and mine.get("restarts", 0) < plan["max"]:
                        try:
                            restart(item)
                            mine["restarts"] = mine.get("restarts", 0) + 1
                            mine["last_restart"], mine["last_error"] = round(now), ""
                            detail = f"try {mine['restarts']} of {plan['max']}"
                            did.append({"key": key, "action": "restart", "detail": detail})
                            tell("restarted", detail)
                        except Exception as error:
                            mine["last_restart"], mine["last_error"] = round(now), str(error)[:200]
                            did.append({"key": key, "action": "restart", "error": mine["last_error"]})
                        changed = True
                    elif due and not mine.get("gave_up") and mine.get("restarts", 0) >= plan["max"]:
                        mine["gave_up"], changed = True, True
                        tell("gave-up", f"{plan['max']} restart{'s' if plan['max'] != 1 else ''}")
            elif now_state in ("up", "slow"):
                if mine.get("told") == "down":
                    mine["told"], changed = "up", True
                    tell("up")
                if mine.get("gave_up") or mine.get("last_error"):
                    # Answering again: its alert resolves; the count waits for UP_RESET.
                    mine.update(gave_up=False, last_error="")
                    changed = True
                if mine.get("up_since") is None:
                    mine["up_since"], changed = round(now), True
                elif mine.get("restarts") and now - mine["up_since"] >= UP_RESET:
                    mine["restarts"], changed = 0, True
            # An automatic update of it finished: say how, once.
            if hook:
                told = set(mine.get("told_jobs") or [])
                for op in operations or []:
                    res = op.get("resource") or {}
                    if op.get("kind") != "auto-update" or op.get("status") not in ("succeeded", "failed") \
                            or (res.get("namespace"), res.get("name")) != (item["ns"], item["name"]) or op.get("id") in told:
                        continue
                    told.add(op.get("id"))
                    tell("update", op.get("message") or op.get("status"))
                    changed = True
                if told != set(mine.get("told_jobs") or []):
                    mine["told_jobs"] = sorted(told)[-20:]
        for key in [k for k in state_by_key if k not in keys]:
            state_by_key.pop(key)
            changed = True
        if changed:
            SHARED.write_json(_path(), data, separators=(",", ":"))
    return did


def _path_of(kind, ns, name):
    return (f"/apis/kubevirt.io/v1/namespaces/{ns}/virtualmachines/{name}" if kind == "vm"
            else f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")


def _annotations(kind, ns, name):
    current = _bound["kget"](_path_of(kind, ns, name)) or {}
    return (current.get("metadata") or {}).get("annotations") or {}


def _stored_actions(kind, ns, name):
    """The actions saved on the app or VM now, secrets included."""
    return resolved(kind, ns, _annotations(kind, ns, name))


def _gone(owner, ns):
    """Whether the app or VM a Secret was written for (kind/name) is no longer there."""
    kind, _, name = str(owner or "").partition("/")
    if kind not in ("app", "vm") or not name:
        return False
    return KUBE.get_optional(_path_of(kind, ns, name), read=_bound["kget"]) is None


def _undo(ns, saved):
    if not saved:
        return
    try:
        SECRET.undo(ns, saved, _bound["ksend"])
    except Exception as error:
        print(f"outage actions: Secret {ns}/{saved['ref']} not put back ({type(error).__name__})", flush=True)


def _drop_secret(kind, ns, name, refs, keep=None):
    """Delete the Secrets in refs that nothing names now (the address is
    gone, needs no Secret, or moved to the app's own name): only one
    Homestead keeps for this kind's webhook, written for this app or VM, or
    for one no longer there (the name before a rename). Never another's."""
    for ref in sorted(set(refs) - {keep, None, ""}):
        try:
            secret = SECRET.read_secret(kind, ns, ref, _bound["kget"])
            owner = SECRET.owner_of(secret)
            if secret and (owner == f"{kind}/{name}" or _gone(owner, ns)):
                SECRET.remove(kind, ns, ref, _bound["kget"], _bound["ksend"], owner)
        except Exception as error:
            print(f"outage actions: Secret {ns}/{ref} not deleted ({type(error).__name__}); "
                  "nothing names it now, so delete it by hand", flush=True)


def forget(kind, ns, obj):
    """When an app or VM is deleted: the Secret its webhook address was kept in goes too."""
    meta = (obj or {}).get("metadata") or {}
    name = meta.get("name") or ""
    _drop_secret(kind, ns, name, {(stored(meta.get("annotations") or {}, NAMES) or {}).get("secret"),
                                  SECRET.secret_name(kind, name) if name else None})


MOVE_LIMIT = 20
_unmovable = {}


def _to_move(item):
    """"whole" when the lists show an address kept whole on the annotation,
    "renamed" when it is kept in a Secret of another name (the name before a
    rename), else None."""
    shown = item.get("actions") or {}
    hook = shown.get("webhook")
    if not isinstance(hook, str) or HIDDEN not in hook:
        return None
    if not shown.get("secret"):
        return "whole"
    return "renamed" if shown["secret"] != SECRET.secret_name(item["kind"], item["name"]) else None


def migrate(found):
    """On the leader, after each monitoring round, every address is moved
    into the app's or VM's own Secret, every other key kept as it was:
    one still whole on its annotation (saved before #931, or typed on the
    Resources page), and one in the Secret of the name it had before a
    rename (whose Secret then goes, unless that name is in use again). The
    lists show which, so one in place costs no read. Each write names what
    it read: a save meanwhile wins, a Secret holding another address is
    never overwritten, and a Secret written for an annotation that could
    not be written is put back. One that fails is tried again next round,
    at most MOVE_LIMIT a round. Returns the keys moved."""
    moved, tries = [], 0
    for item in found or []:
        why, hook = _to_move(item), (item.get("actions") or {}).get("webhook")
        kind, ns, name = item["kind"], item["ns"], item["name"]
        key = f"{kind}:{ns}/{name}"
        if not why or _unmovable.get(key) == hook:
            continue
        if tries >= MOVE_LIMIT:
            break
        tries += 1
        path, url, saved = _path_of(kind, ns, name), "", None
        try:
            # The Secret first, then the app or VM: each write names what was
            # read, so a save made after either read makes the move fail, never
            # overwrites (a save between the two shows on the annotation).
            secret_now = SECRET.current(kind, ns, name, _bound["kget"])
            meta = (_bound["kget"](path) or {}).get("metadata") or {}
            actions = stored(meta.get("annotations") or {}, NAMES) or {}
            old = actions.pop("secret", None)
            if why == "renamed":
                url = SECRET.load(kind, ns, old, _bound["kget"]) if old and old != SECRET.secret_name(kind, name) else None
                if not url or hide(url) != actions.get("webhook"):
                    continue                # moved or saved meanwhile, or nothing there to move
            else:
                url = actions.get("webhook")
                if not isinstance(url, str) or HIDDEN in url or hide(url) == url:
                    continue
                try:
                    webhook_url(url)
                except ValueError:
                    _unmovable[key] = hook  # no call could use it either; left as it is
                    continue
            try:
                saved = SECRET.save(kind, ns, name, url, _bound["kget"], _bound["ksend"], secret_now)
            except ValueError as error:     # a Secret of that name Homestead did not make: said once, left
                _unmovable[key] = hook
                print(f"outage actions: {key}'s webhook stays where it is: {error}", flush=True)
                continue
            patch = {"annotations": {NAMES.key(KEY): json.dumps({**actions, "webhook": hide(url), "secret": saved["ref"]},
                                                                separators=(",", ":"))}}
            if meta.get("resourceVersion"):
                patch["resourceVersion"] = meta["resourceVersion"]
            try:
                _bound["ksend"]("PATCH", path, {"metadata": patch}, ctype="application/merge-patch+json")
            except Exception:
                _undo(ns, saved)            # nothing names it: no token is left behind
                raise
            moved.append(key)
            _drop_secret(kind, ns, name, {old}, keep=saved["ref"])
        except Exception as error:
            print(f"outage actions: {key}'s webhook not moved into its Secret yet "
                  f"({scrub(str(error), url)[:120]}); tried again next round", flush=True)
    if moved:
        ROUTER.forget("wl", "vms")
        print(f"outage actions: {len(moved)} webhook address(es) moved from annotations into Secrets", flush=True)
    return moved


def _send_later(url, body, actions=None):
    """Webhooks go out on their own thread, so a slow one holds nothing up;
    how the last one went is kept for the dialog. url is as the lists show
    it: the address, format and LAN allowance used are the ones saved now."""
    def run():
        real = url
        try:
            annotations = _annotations(body["kind"], body["namespace"], body["name"])
            now = resolved(body["kind"], body["namespace"], annotations)
            real, fmt = now.get("webhook") or "", now.get("format") or "generic"
            if not real and (stored(annotations, NAMES) or {}).get("webhook"):
                raise OSError("its address could not be read from its Secret: save the webhook again")
            if not real or hide(real) != url:
                raise OSError("its webhook changed meanwhile; the next call uses the new one")
            post(real, body, fmt, bool(now.get("lan")))
            result = {"at": round(time.time()), "ok": True, "event": body.get("event")}
        except Exception as error:
            result = {"at": round(time.time()), "ok": False, "event": body.get("event"),
                      "error": scrub(str(error), real)[:200]}
        _last_webhook[f"{body['kind']}:{body['namespace']}/{body['name']}"] = result
        LEADER_STATE.put("outage-webhooks", dict(_last_webhook))
    threading.Thread(target=run, name="outage-webhook", daemon=True).start()


_last_webhook = {}


def report():
    """Each item's restarts and last webhook, for the Monitoring dialogs.
    Webhooks go out from the leader: the newer of its shared calls and this
    replica's own."""
    with _lock:
        data = _load().get("items") or {}
    shared, _ = LEADER_STATE.get("outage-webhooks")
    hooks = dict(shared) if isinstance(shared, dict) else {}
    for key, hook in list(_last_webhook.items()):
        if (hook or {}).get("at", 0) >= ((hooks.get(key) or {}).get("at", 0)):
            hooks[key] = hook
    return {key: {"restarts": s.get("restarts", 0), "gave_up": bool(s.get("gave_up")), "last_restart": s.get("last_restart"),
                  "last_error": s.get("last_error", ""), "webhook": hooks.get(key)}
            for key, s in data.items()} | {key: {"restarts": 0, "webhook": hook}
                                            for key, hook in hooks.items() if key not in data}


def alert_facts(scheduled_report=None):
    """Out of restarts and still down, or a restart refused."""
    facts = []
    with _lock:
        data = _load().get("items") or {}
    for key, s in sorted(data.items()):
        kind, ref = key.split(":", 1)
        ns, name = ref.split("/", 1)
        href = f"/vms?panel=monitoring&ns={ns}&vm={name}" if kind == "vm" else f"/containers?panel=monitoring&ns={ns}&workload={name}"
        what = "VM " if kind == "vm" else ""
        if s.get("gave_up"):
            facts.append({"key": f"outage:{key}", "category": "outage", "severity": "critical",
                          "title": f"{what}{name} is still down after {s.get('restarts')} restart{'s' if s.get('restarts') != 1 else ''}",
                          "body": f"Homestead restarted {what}{name} as many times as it was asked to, and it still does not answer. "
                                  "It is left alone now; look at its logs.",
                          "resolved": f"{what}{name} is answering again", "href": href, "signals": {"restarts": s.get("restarts")}})
        elif s.get("last_error"):
            facts.append({"key": f"outage:{key}", "category": "degraded", "severity": "degraded",
                          "title": f"{what}{name} is down and could not be restarted",
                          "body": s["last_error"], "resolved": f"{what}{name} could be restarted", "href": href,
                          "signals": {"error": s["last_error"]}})
    return facts


# Its routes and who may use them (homestead_routes.py).
def target(b):
    """(kind, ns, name, path) of the app or VM outage actions are for."""
    kind = b.get("kind")
    ns = _bound["dns_name"](b.get("ns"), "namespace")
    if kind == "app":
        name = _bound["dns_name"](b.get("name"), "workload name")
        _bound["guard"](ns, name)
        return kind, ns, name, f"/apis/apps/v1/namespaces/{ns}/deployments/{name}"
    if kind == "vm":
        name = _bound["dns_name"](b.get("name"), "VM name")
        return kind, ns, name, f"/apis/kubevirt.io/v1/namespaces/{ns}/virtualmachines/{name}"
    raise ValueError("outage actions are for an app or a VM")


def set_actions(b, role="operator"):
    """What to do when an app or VM is down - nothing unless asked. An annotation, so nothing restarts."""
    kind, ns, name, path = target(b)
    annotations = _annotations(kind, ns, name)
    on_port = True
    if kind == "vm":
        chosen = UPTIME.setting(NAMES.read(annotations, "uptime"))
        on_port = chosen["mode"] in ("tcp", "http") and bool(chosen.get("port"))
    given = b.get("actions") if isinstance(b.get("actions"), dict) else {}
    before = resolved(kind, ns, annotations) if given.get("webhook") else {}
    actions = clean(b.get("actions"), kind, on_port, before, role)
    if actions and actions.get("webhook"):
        try:
            destination(actions["webhook"], bool(actions.get("lan")))
        except Refused as error:
            raise ValueError(str(error)) from None
        except OSError:
            pass    # not found now: checked again at each call
    kept, saved = actions, None
    if actions and actions.get("webhook") and hide(actions["webhook"]) != actions["webhook"]:
        # The whole address goes into the Secret first; the annotation then names it (#931).
        saved = SECRET.save(kind, ns, name, actions["webhook"], _bound["kget"], _bound["ksend"])
        kept = {**actions, "webhook": hide(actions["webhook"]), "secret": saved["ref"]}
    try:
        _bound["ksend"]("PATCH", path, {"metadata": {"annotations": {
            NAMES.key(KEY): json.dumps(kept, separators=(",", ":")) if kept else None}}},
            ctype="application/merge-patch+json")
    except Exception:
        _undo(ns, saved)    # a save that failed leaves the Secret as it was
        raise
    _drop_secret(kind, ns, name, {(stored(annotations, NAMES) or {}).get("secret"), SECRET.secret_name(kind, name)},
                 keep=(saved or {}).get("ref"))
    ROUTER.forget("wl" if kind == "app" else "vms")
    said = []
    if actions and actions.get("restart"):
        r = actions["restart"]
        said.append(f"restarted after {r['after_min']} min down, at most {r['max']} time{'s' if r['max'] != 1 else ''}")
    if actions and actions.get("webhook"):
        said.append("its webhook called")
    return {"ok": True, "actions": public(actions),
            "detail": f"When {name} is down: " + (" and ".join(said) if said else "nothing is done")}


def _allow_test(now=None):
    """At most TEST_LIMIT tests a minute from this copy of Homestead."""
    now = now or time.time()
    with _lock:
        _tests[:] = [t for t in _tests if now - t < TEST_WINDOW]
        if len(_tests) >= TEST_LIMIT:
            return False
        _tests.append(now)
        return True


def test_webhook(b):
    """Send a test now, in the format chosen in the dialog (saved or not)."""
    kind, ns, name, _ = target(b)
    url = webhook_url(b.get("webhook"))
    if not url:
        raise ValueError("type the webhook address to test")
    fmt = str(b.get("format") or "generic")
    if fmt not in FORMATS:
        raise ValueError("the webhook format is generic, ntfy or gotify")
    before = _stored_actions(kind, ns, name) if HIDDEN in url else {}
    url = webhook_url(restore(url, before.get("webhook")))
    if not _allow_test():
        return ROUTER.Reply(429, {"error": f"at most {TEST_LIMIT} tests a minute; wait a little and send it again"})
    item = {"kind": kind, "ns": ns, "name": name}
    what = {"ntfy": "ntfy", "gotify": "Gotify"}.get(fmt, "The webhook")
    try:
        post(url, payload(item, "test"), fmt, allow_lan=True)    # an admin's test may reach the LAN
    except Exception as error:
        return {"ok": False, "detail": f"{what} did not take it: {scrub(str(error), url)[:200]}"}
    return {"ok": True, "detail": f"{what} took the test"}


ROUTES = {
    ("POST", "/api/monitoring/actions"): ("operator", lambda request: set_actions(request.body, request.role)),
    # Admin-only: it sends a request from inside the cluster to any address typed.
    ("POST", "/api/monitoring/actions/test"): ("admin", lambda request: test_webhook(request.body)),
}
