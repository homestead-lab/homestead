"""Starts after (#746): an app names the apps it starts after, and every new
pod of it waits for their Services before anything of it runs.

The setting lives on the Deployment, as the annotation KEY. From it one
function renders the start gate: a small init container, always first, that
waits until each dependency's Service answers on its TCP port (or until the
timeout) and then exits. It reads no Kubernetes API, so it works on a cold
boot with Homestead still down: a ClusterIP Service only routes to Ready
pods, so a Service that answers is a dependency that is ready.

Homestead and system apps are never gated, so this can't hold up the
platform's own recovery. An app Flux or Argo CD applies is never written:
Homestead shows the lines to commit instead, as it does for tiers.

Nothing here is bound by server.py: it reads through the helpers
homestead_workloads and homestead_lifecycle already have.
"""
import json
import re

import homestead_names as NAMES
import homestead_namespaces as NAMESPACES

KEY = NAMES.key("starts-after")
GATE = f"{NAMES.BRAND}-start-gate"
TIMEOUT = (30, 300, 3600)        # seconds: least, default, most
MAX_AFTER = 10
ON_TIMEOUT = ("start", "wait")   # the first is the default
NO_SERVICE = "{app} has no Service with a TCP port, so nothing can tell when it is ready"
DNS_LABEL = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")


def _workloads():
    import homestead_workloads as WORKLOADS
    return WORKLOADS


def _meta(obj):
    return obj.get("metadata") or {}


def _ref(dep):
    meta = _meta(dep)
    return meta.get("namespace", ""), meta.get("name", "")


def _pod_spec(dep):
    return ((dep.get("spec") or {}).get("template") or {}).get("spec") or {}


def _dns_label(value, label):
    import homestead_lifecycle as LC
    return LC.dns_label(value if isinstance(value, str) else "", label)


def never_gated(ns, name):
    """Homestead itself, the helpers it runs (its SMB and NFS servers, the
    object store), and anything in a system namespace: the platform's own
    recovery must never wait for an app."""
    part = getattr(_workloads(), "homestead_part", None)
    return (_is_self(ns, name) or bool(part and part(ns, name)) or ns in (_workloads().SYS_NS or ())
            or NAMESPACES.is_system({"metadata": {"name": ns}}))


def _int(value, low, high):
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def _well_formed(value):
    """The shape clean() writes, and nothing else: an annotation edited by
    hand that is anything other than that is never acted on."""
    if not isinstance(value, dict) or not isinstance(value.get("after"), list):
        return False
    for item in value["after"]:
        if not isinstance(item, dict) or not _int(item.get("port"), 1, 65535):
            return False
        if not all(isinstance(item.get(k), str) and DNS_LABEL.fullmatch(item[k]) for k in ("ns", "app", "service")):
            return False
    return _int(value.get("timeout"), TIMEOUT[0], TIMEOUT[2]) and value.get("on_timeout") in ON_TIMEOUT


def read(dep):
    """The stored setting; None when there is none, {"unreadable": True}
    when it isn't what Homestead writes, so a row shows it and never fails."""
    raw = (_meta(dep).get("annotations") or {}).get(KEY)
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {"unreadable": True}
    return value if _well_formed(value) else {"unreadable": True}


def after_of(dep):
    """(ns, app) of each app a Deployment's setting names, in its order."""
    setting = read(dep) or {}
    return [(str(a.get("ns") or ""), str(a.get("app") or "")) for a in setting.get("after") or []
            if isinstance(a, dict)]


def services_for(ns, app, deployments, services):
    """The Services that select the app's pods and can show it is ready:
    [{"name", "ports": [TCP ports]}]. An ExternalName Service, one with no
    selector, or one that publishes pods before they are ready would answer
    at once, so they don't count. A headless Service resolves to the pods
    themselves, so only a port that is its own target counts there."""
    dep = next((d for d in deployments if _ref(d) == (ns, app)), None)
    if dep is None:
        return []
    labels = ((((dep.get("spec") or {}).get("template") or {}).get("metadata") or {}).get("labels") or {})
    found = []
    for svc in services:
        if _meta(svc).get("namespace") != ns:
            continue
        spec = svc.get("spec") or {}
        selector = spec.get("selector") or {}
        if (spec.get("type") == "ExternalName" or not selector or spec.get("publishNotReadyAddresses")
                or any(labels.get(k) != v for k, v in selector.items())):
            continue
        headless = spec.get("clusterIP") == "None"
        ports = [p["port"] for p in spec.get("ports") or []
                 if isinstance(p.get("port"), int) and str(p.get("protocol") or "TCP").upper() == "TCP"
                 and (not headless or p.get("targetPort") in (None, p["port"]))]
        if ports:
            found.append({"name": _meta(svc).get("name", ""), "ports": ports})
    return found


def graph(deployments):
    """(ns, app) -> [(ns, app), ...] from every Deployment's setting."""
    edges = {}
    for dep in deployments:
        after = after_of(dep)
        if after:
            edges[_ref(dep)] = after
    return edges


def find_cycle(edges, start):
    """The app names from start round to start, or None."""
    path, on_path, seen = [start], {start}, set()

    def walk(node):
        for nxt in edges.get(node, ()):
            if nxt == start:
                return path + [start]
            if nxt in on_path or nxt in seen:
                continue
            path.append(nxt)
            on_path.add(nxt)
            found = walk(nxt)
            if found:
                return found
            path.pop()
            on_path.discard(nxt)
            seen.add(nxt)
        return None

    found = walk(start)
    return [name for _, name in found] if found else None


def clean(dep, value, deployments, services):
    """The setting as it is stored, or None to remove it. Refused (ValueError)
    when it can't work: a loop, Homestead or a system app as the one waiting,
    a dependency nothing can see is ready, limits out of range."""
    if value is None:
        return None
    ns, name = _ref(dep)
    if isinstance(value, dict) and value.get("unreadable"):
        raise ValueError(f"Starts after can't be read on {name}; set it again or clear it")
    if not isinstance(value, dict) or not isinstance(value.get("after", []), list):
        raise ValueError("Starts after must be a list of apps")
    timeout = value.get("timeout", TIMEOUT[1])
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not TIMEOUT[0] <= timeout <= TIMEOUT[2]:
        raise ValueError("wait up to must be between 30 s and 60 min")
    on_timeout = value.get("on_timeout", ON_TIMEOUT[0])
    if on_timeout not in ON_TIMEOUT:
        raise ValueError("if it never comes up: start or wait")
    wanted, seen = [], set()
    for item in value.get("after") or []:
        if not isinstance(item, dict):
            raise ValueError("Starts after must be a list of apps")
        ref = (_dns_label(item.get("ns") or ns, "namespace"), _dns_label(item.get("app"), "app name"))
        if ref not in seen:
            seen.add(ref)
            wanted.append((ref, item))
    if not wanted:
        return None
    if len(wanted) > MAX_AFTER:
        raise ValueError(f"Starts after takes at most {MAX_AFTER} apps")
    if never_gated(ns, name):
        raise ValueError("Homestead and system apps can't wait for other apps")
    # Only what changed is checked: an entry kept exactly as stored stays,
    # even when its app has since gone (the alerts say so), so an unrelated
    # edit never fails on it. A new or changed entry must work now.
    kept = (read(dep) or {}).get("after") or []
    after = []
    for (dep_ns, app), item in wanted:
        if (dep_ns, app) == (ns, name):
            raise ValueError("an app can't start after itself")
        as_stored = {"ns": dep_ns, "app": app, "service": item.get("service"), "port": item.get("port")}
        if as_stored in kept:
            after.append(as_stored)
            continue
        found = services_for(dep_ns, app, deployments, services)
        if item.get("service") not in (None, ""):
            chosen = _dns_label(item["service"], "Service name")
            found = [s for s in found if s["name"] == chosen]
        if not found:
            raise ValueError(NO_SERVICE.format(app=app))
        svc = found[0]
        port = item.get("port")
        if port in (None, ""):
            port = svc["ports"][0]
        elif isinstance(port, bool) or not isinstance(port, int) or port not in svc["ports"]:
            raise ValueError(f"{app}'s Service has no port {port}")
        after.append({"ns": dep_ns, "app": app, "service": svc["name"], "port": port})
    spec = _pod_spec(dep)
    if spec.get("dnsPolicy") in ("Default", "None"):
        raise ValueError(f"{name} does not use cluster DNS (dnsPolicy {spec['dnsPolicy']}), so it can't find "
                         f"{after[0]['app']}; set dnsPolicy to ClusterFirst")
    if spec.get("hostNetwork") and spec.get("dnsPolicy") != "ClusterFirstWithHostNet":
        raise ValueError(f"{name} uses the host's network without cluster DNS; set dnsPolicy to "
                         f"ClusterFirstWithHostNet so it can find {after[0]['app']}")
    edges = graph(deployments)
    edges[(ns, name)] = [(a["ns"], a["app"]) for a in after]
    loop = find_cycle(edges, (ns, name))
    if loop:
        raise ValueError("a cycle: " + " → ".join(loop))
    return {"after": after, "timeout": timeout, "on_timeout": on_timeout}


# ---- the start gate ---------------------------------------------------------
# alpine 3.24.3 (busybox 1.37.0 nc and sh), by its multi-arch index digest,
# resolved 2026-10-22. The same repository Homestead already uses
# (homestead_volowner.IMAGE), so an air-gapped mirror has it. Changing it
# changes every gate, and so restarts every gated app once: bump it only
# with a reason (a fix to busybox nc or sh).
GATE_IMAGE = ("docker.io/library/alpine:3.24"
              "@sha256:71e9b7bae160ba167fa17414401b94cbcdaf3c32feeab9298bd794a2a0614c35")
# Fixed text: names reach it only through env (HS_AFTER, HS_TIMEOUT,
# HS_ON_TIMEOUT), so nothing a person types becomes shell. It never exits
# non-zero, which would put the pod in Init:CrashLoopBackOff and delay its
# start once the dependency is ready. HS_MESSAGE exists for the tests.
SCRIPT = """start=$(date +%s); last=; said=0
while :; do
  missing=
  for t in $HS_AFTER; do nc -z -w 2 "${t%:*}" "${t##*:}" >/dev/null 2>&1 || missing="$missing $t"; done
  now=$(date +%s); took=$((now - start))
  if [ -z "$missing" ]; then echo "ready after $took s"; exit 0; fi
  if [ "$missing" != "$last" ] || [ $((now - said)) -ge 60 ]; then
    echo "waiting for$missing ($took s of $HS_TIMEOUT s)"; last=$missing; said=$now; fi
  if [ "$HS_ON_TIMEOUT" = start ] && [ "$took" -ge "$HS_TIMEOUT" ]; then
    echo "timeout$missing" > "${HS_MESSAGE:-/dev/termination-log}"
    echo "not ready after $took s: starting anyway"; exit 0; fi
  sleep 2
done
"""


def target(item):
    """Where the gate knocks for one dependency: <service>.<ns>.svc:<port>."""
    return f"{item['service']}.{item['ns']}.svc:{item['port']}"


def render(setting):
    """The gate container for a stored setting."""
    return {"name": GATE, "image": GATE_IMAGE, "imagePullPolicy": "IfNotPresent",
            "command": ["sh", "-c", SCRIPT],
            "env": [{"name": "HS_AFTER", "value": " ".join(target(a) for a in setting["after"])},
                    {"name": "HS_TIMEOUT", "value": str(setting["timeout"])},
                    {"name": "HS_ON_TIMEOUT", "value": setting["on_timeout"]}],
            "resources": {"requests": {"cpu": "5m", "memory": "8Mi"}, "limits": {"cpu": "50m", "memory": "16Mi"}},
            "securityContext": {"runAsNonRoot": True, "runAsUser": 65534, "runAsGroup": 65534,
                                "allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]}, "seccompProfile": {"type": "RuntimeDefault"}},
            "terminationMessagePolicy": "File"}


def _matches(have, want):
    """have is want, give or take what the API server fills in when it
    stores a template (terminationMessagePath and the like)."""
    return all(have.get(key) == value for key, value in want.items())


def _stored_text(setting):
    return json.dumps(setting, sort_keys=True, separators=(",", ":"))


def apply_to(dep, setting):
    """Write the setting and its gate, first in initContainers; None takes
    both away and touches nothing else. A gate that already matches is kept
    as it is, so saving the same setting again restarts nothing."""
    spec = dep["spec"]["template"]["spec"]
    current = spec.get("initContainers") or []
    others = [c for c in current if c.get("name") != GATE]
    if setting is None:
        annotations = _meta(dep).get("annotations")
        if annotations:
            annotations.pop(KEY, None)
        if others:
            spec["initContainers"] = others
        elif "initContainers" in spec and len(others) != len(current):
            spec.pop("initContainers")
        return
    dep["metadata"].setdefault("annotations", {})[KEY] = _stored_text(setting)
    want = render(setting)
    gate = current[0] if current and current[0].get("name") == GATE and _matches(current[0], want) else want
    spec["initContainers"] = [gate] + others


def gate_state(dep):
    """"none", "rendered", "missing" (set, no gate) or "differs" (a gate
    changed, moved or left with no setting)."""
    setting = read(dep)
    current = _pod_spec(dep).get("initContainers") or []
    gates = [i for i, c in enumerate(current) if c.get("name") == GATE]
    if setting is None:
        return "differs" if gates else "none"
    if setting.get("unreadable"):
        return "differs"
    if not gates:
        return "missing"
    try:
        want = render(setting)
    except (KeyError, TypeError):
        return "differs"
    return "rendered" if gates == [0] and _matches(current[0], want) else "differs"


# ---- saving it ---------------------------------------------------------------

def lists(get):
    """Every Deployment and Service, which the checks need."""
    return (get("/apis/apps/v1/deployments").get("items", []) or [],
            get("/api/v1/services").get("items", []) or [])


def _managed(dep):
    import homestead_updates as UPDATES
    return UPDATES.managed_by(dep)


def managed_message(name, owner):
    return f"{name}'s start order is set where {owner['by']} applies it; commit the lines Homestead shows"


def _same(dep, setting):
    stored = read(dep)
    if stored is not None and stored.get("unreadable"):
        return False
    return stored == setting


def unchanged(dep, value):
    """Whether value is what Edit's form showed for dep and nothing more: the
    stored setting itself, or the marker of one that can't be read. Edit sends
    the form back whole, so that is no change, and nothing of it is checked."""
    if isinstance(value, dict) and value.get("unreadable"):
        return True
    stored = read(dep)
    return not (stored or {}).get("unreadable") and value == stored


def apply(dep, value, deployments, services):
    """The setting checked and written onto dep (a copy being edited). An app
    Flux or Argo CD applies is never written: a change is refused, and the
    same setting again is left alone."""
    if unchanged(dep, value):
        return
    setting = clean(dep, value, deployments, services)
    owner = _managed(dep)
    if owner:
        if not _same(dep, setting):
            raise ValueError(managed_message(_ref(dep)[1], owner))
        return
    apply_to(dep, setting)


def changes(dep, value, get):
    """Whether saving value would change dep's setting (checked first)."""
    return not _same(dep, clean(dep, value, *lists(get)))


def _yaml(value):
    # JSON is YAML: every value is written as a JSON scalar or list.
    return json.dumps(value)


def git_lines(dep, setting):
    """What to commit for an app Flux or Argo CD applies: the annotation and
    the gate, first in initContainers. For a HelmRelease, the same as chart
    values."""
    helm = str((_managed(dep) or {}).get("source") or "").startswith("HelmRelease")
    head = ("# chart values, if the chart takes extra initContainers and podAnnotations;\n"
            "# if it doesn't, add them with a Flux post-renderer patch\n") if helm else ""
    if setting is None:
        return head + (f"# remove {KEY} from metadata.annotations and the {GATE} entry from "
                       "spec.template.spec.initContainers\n")
    gate = render(setting)
    item = [f"- name: {gate['name']}"] + [f"  {key}: {_yaml(value)}" for key, value in gate.items() if key != "name"]
    annotation = f"{KEY}: {_yaml(_stored_text(setting))}"
    if helm:
        return head + f"podAnnotations:\n  {annotation}\ninitContainers:   # first\n" + \
            "".join(f"  {line}\n" for line in item)
    return (f"metadata:\n  annotations:\n    {annotation}\n"
            "spec:\n  template:\n    spec:\n      initContainers:   # first, before any others\n"
            + "".join(f"        {line}\n" for line in item))


def _image_wanted(deployments, get):
    """True until a first gate is written: then no node has the image yet,
    and a cold boot with the internet down could not pull it."""
    if any(c.get("name") == GATE for d in deployments for c in _pod_spec(d).get("initContainers") or []):
        return False
    digest = GATE_IMAGE.split("@", 1)[1]
    for node in get("/api/v1/nodes").get("items", []) or []:
        for image in (node.get("status") or {}).get("images") or []:
            if any(name.endswith("@" + digest) for name in image.get("names") or []):
                return False
    return True


def ensure_image(deployments, get):
    """Pre-pull the gate's image to every node the first time a gate is
    written (deployments: as they were before). A note when that fails;
    the save itself stands."""
    import homestead_image_cache as IMAGES
    try:
        if _image_wanted(deployments, get):
            IMAGES.prepull(GATE_IMAGE)
    except Exception as error:
        return f"the start gate's image could not be pre-pulled: {error}"
    return None


def _setting_of(dep):
    setting = read(dep)
    return None if setting is None or setting.get("unreadable") else setting


def form(ns, name):
    """Edit's Starts after: the setting, who applies the app, the gate as it
    is, the apps it may start after, and for a managed app the lines to
    commit."""
    import homestead_lifecycle as LC
    ns, name = LC.dns_label(ns, "namespace"), LC.dns_label(name, "app name")
    dep = LC.kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")
    deployments, services = lists(LC.kget)
    setting = _setting_of(dep)
    owner = _managed(dep)
    gate = gate_state(dep)
    if owner and gate == "missing":
        gate = "not-committed"
    candidates = [{"ns": d_ns, "app": d_name, "services": services_for(d_ns, d_name, deployments, services)}
                  for d_ns, d_name in sorted(_ref(d) for d in deployments)
                  if (d_ns, d_name) != (ns, name) and not (never_gated(d_ns, d_name) and not _is_self(d_ns, d_name))]
    return {"after": (setting or {}).get("after", []),
            "timeout": (setting or {}).get("timeout", TIMEOUT[1]),
            "on_timeout": (setting or {}).get("on_timeout", ON_TIMEOUT[0]),
            "unreadable": bool((read(dep) or {}).get("unreadable")),
            "managed_by": (owner or {}).get("by"), "gate": gate, "candidates": candidates,
            "git": git_lines(dep, setting) if owner else ""}


def _is_self(ns, name):
    is_self = getattr(_workloads(), "is_self", None)
    return bool(is_self and is_self(ns, name))


def save(body):
    """Starts after saved on its own: {"ok", "restarts"}, and a note when
    the gate's image could not be pre-pulled. A managed app gets 409 with
    the lines to commit, and nothing is written. With "lines": true it only
    answers {"git"}, the lines for that setting, and never writes."""
    import homestead_lifecycle as LC
    import homestead_routes as ROUTER
    ns, name = LC.dns_label(body.get("ns"), "namespace"), LC.dns_label(body.get("name"), "app name")
    # The checks Edit's save makes first (homestead_workloads.edit): never
    # Homestead's SMB or NFS server, and Homestead itself only as Edit allows.
    WORKLOADS = _workloads()
    WORKLOADS.guard_managed_smb(ns, name)
    WORKLOADS.guard_self(ns, name)
    dep = LC.kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")
    deployments, services = lists(LC.kget)
    value = {key: body[key] for key in ("after", "timeout", "on_timeout") if key in body}
    if body.get("restore_gate") is True:
        # As Edit takes it: the setting is not "unchanged", so a gate changed
        # outside Homestead is rendered again (the flag itself is not stored).
        value["restore_gate"] = True
    setting = clean(dep, value, deployments, services)
    if body.get("lines") is True:
        # Only the lines to commit, for Edit's "Show the lines to commit":
        # never a write, even if the app is no longer applied from git.
        return {"git": git_lines(dep, setting)}
    owner = _managed(dep)
    if owner:
        return ROUTER.Reply(409, {"error": managed_message(name, owner), "git": git_lines(dep, setting)})
    cfg = {"ns": ns, "name": name, "starts_after": value}
    prepared = LC.prepare_edit(cfg, dep)
    restarts = LC.restarts(dep, prepared["deployment"])
    if prepared["deployment"] != dep:
        LC.edit_workload(cfg, prepared=prepared)
    note = ensure_image(deployments, LC.kget) if setting else None
    return {"ok": True, "restarts": restarts, **({"note": note} if note else {})}


def _arg(request, key):
    return (request.query.get(key) or [""])[0]


# Its routes and who may use them (homestead_routes.py).
ROUTES = {
    ("GET", "/api/workloads/starts-after"): ("viewer", lambda request: form(_arg(request, "ns"), _arg(request, "name"))),
    ("POST", "/api/workloads/starts-after"): ("operator", lambda request: save(request.body)),
}


# ---- ordered starts ----------------------------------------------------------
# When Homestead starts several apps at once (a schedule's tick), the ones
# others wait for go first. Nothing waits here: the gate does the waiting.

def order(items, deployments):
    """items ({"ns", "name", ...}) in layers, dependencies first. Only edges
    between the items count; independent ones share a layer, in the order
    given. A loop made outside Homestead (two Flux commits) is one layer."""
    keys = [(i.get("ns"), i.get("name")) if i.get("kind", "app") == "app" else None for i in items]
    index = {key: n for n, key in enumerate(keys) if key is not None}
    by_ref = {_ref(d): d for d in deployments}
    edges = {n: [index[a] for a in after_of(by_ref[key]) if a in index and index[a] != n]
             if key in by_ref else [] for key, n in index.items()}
    comp = _components(len(items), edges)
    depth = {}

    def level(c):
        if c not in depth:
            depth[c] = 0   # a member's edge back into its own unit adds nothing
            members = [v for v in range(len(items)) if comp[v] == c]
            depth[c] = max([level(comp[w]) + 1 for v in members for w in edges.get(v, ()) if comp[w] != c],
                           default=0)
        return depth[c]

    layers = {}
    for v in range(len(items)):
        layers.setdefault(level(comp[v]), []).append(items[v])
    return [layers[k] for k in sorted(layers)]


def _components(count, edges):
    """Tarjan: node -> the root of its strongly connected component, for
    nodes 0..count-1 and edges {node: [node, ...]}."""
    comp, low, num, stack, on, counter = {}, {}, {}, [], set(), [0]

    def visit(v):
        num[v] = low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on.add(v)
        for w in edges.get(v, ()):
            if w not in num:
                visit(w)
                low[v] = min(low[v], low[w])
            elif w in on:
                low[v] = min(low[v], num[w])
        if low[v] == num[v]:
            while True:
                w = stack.pop()
                on.discard(w)
                comp[w] = v
                if w == v:
                    break

    for v in range(count):
        if v not in num:
            visit(v)
    return comp


def arrange(items):
    """order(), flattened, read through the bound kget."""
    import homestead_lifecycle as LC
    return [i for layer in order(items, LC.kget("/apis/apps/v1/deployments").get("items", []) or []) for i in layer]


def stopped_dependencies(dep, deployments):
    """The apps dep starts after that are stopped (no replicas) or gone."""
    by_ref = {_ref(d): d for d in deployments}
    return [app for ref in after_of(dep) for app in [ref[1]]
            if ref not in by_ref or not ((by_ref[ref].get("spec") or {}).get("replicas") or 0)]


def waits_for(ns, name, get):
    """What a start of ns/name would wait for: its stopped dependencies."""
    dep = get(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")
    if not after_of(dep):
        return []
    return stopped_dependencies(dep, get("/apis/apps/v1/deployments").get("items", []) or [])


# ---- what the person is told: rows, alerts, flow ------------------------------

def _epoch(stamp):
    import calendar
    import time
    try:
        return calendar.timegm(time.strptime(str(stamp), "%Y-%m-%dT%H:%M:%SZ"))
    except ValueError:
        return 0


def _gate_status(pod):
    return next((s for s in (pod.get("status") or {}).get("initContainerStatuses") or []
                 if s.get("name") == GATE), None)


def pod_wait(dep, pods, deployments):
    """From the pods the Apps list has already read: {"waiting_for", "since"}
    while a gate waits, {"started_without", "at"} when the newest pod's gate
    gave up waiting, else None."""
    setting = _setting_of(dep)
    if not setting:
        return None
    by_ref = {_ref(d): d for d in deployments}
    running = [_epoch(((_gate_status(p) or {}).get("state") or {}).get("running", {}).get("startedAt"))
               for p in pods if ((_gate_status(p) or {}).get("state") or {}).get("running")]
    if running:
        down = [a["app"] for a in setting["after"] if (a["ns"], a["app"]) not in by_ref
                or not ((by_ref[(a["ns"], a["app"])].get("status") or {}).get("readyReplicas") or 0)]
        return {"waiting_for": down or [a["app"] for a in setting["after"]], "since": min(running)}
    # Every pod of the template the newest one runs: with several copies,
    # any one that gave up started without its apps.
    newest = max(pods, key=lambda p: _epoch(_meta(p).get("creationTimestamp")), default=None)
    template = (_meta(newest).get("labels") or {}).get("pod-template-hash") if newest else None
    apps = {target(a): a["app"] for a in setting["after"]}
    without, at = [], 0
    for pod in pods:
        if (_meta(pod).get("labels") or {}).get("pod-template-hash") != template:
            continue
        ended = ((_gate_status(pod) or {}).get("state") or {}).get("terminated") or {}
        message = str(ended.get("message") or "").split()
        if message[:1] == ["timeout"]:
            without += [apps.get(t, t.split(".", 1)[0]) for t in message[1:]]
            at = max(at, _epoch(ended.get("finishedAt")))
    if without:
        return {"started_without": list(dict.fromkeys(without)), "at": at}
    return None


def waiting_in_gate(dep, get):
    """(the apps it waits for, the gate's timeout in seconds) while a pod of
    dep is in its start gate, else None: a rollout that is waiting there is
    waiting, not broken (the auto-update job reads this)."""
    import urllib.parse
    setting = _setting_of(dep)
    labels = ((dep.get("spec") or {}).get("selector") or {}).get("matchLabels") or {}
    if not setting or not labels:
        return None
    ns = _ref(dep)[0]
    selector = urllib.parse.quote(",".join(f"{k}={v}" for k, v in sorted(labels.items())))
    pods = get(f"/api/v1/namespaces/{ns}/pods?labelSelector={selector}").get("items", []) or []
    if not any(((_gate_status(p) or {}).get("state") or {}).get("running") for p in pods):
        return None
    return [a["app"] for a in setting["after"]], setting["timeout"]


def row_fields(dep, pods, deployments):
    """What the Apps list shows of it: the apps it starts after, its gate,
    whether a pod is waiting, and the setting the alerts read."""
    setting = _setting_of(dep)
    existing = {_ref(d) for d in deployments}
    return {"starts_after": [a["app"] for a in (setting or {}).get("after", [])],
            "start_unreadable": bool((read(dep) or {}).get("unreadable")),
            "start_gate": gate_state(dep),
            "start_wait": pod_wait(dep, pods, deployments),
            "start_setting": None if not setting else {
                "after": [{"ns": a["ns"], "app": a["app"], "gone": (a["ns"], a["app"]) not in existing}
                          for a in setting["after"]],
                "timeout": setting["timeout"], "on_timeout": setting["on_timeout"], "managed": bool(_managed(dep))}}


def _words(names):
    names = list(names)
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _span(seconds):
    return f"{seconds} s" if seconds < 60 else f"{seconds / 60:g} min"


def alert_facts(workloads, now=None):
    """Facts for the alert collector, from the Apps list's rows."""
    import time
    now = now or time.time()
    facts, rows = [], [r for r in workloads or [] if isinstance(r, dict) and r.get("ns") and r.get("name")]
    for row in rows:
        ns, name = row["ns"], row["name"]
        href = f"/containers?ns={ns}&workload={name}"
        setting = row.get("start_setting") if isinstance(row.get("start_setting"), dict) else None
        wait = row.get("start_wait") if isinstance(row.get("start_wait"), dict) else {}
        if setting and wait.get("started_without"):
            facts.append({"key": f"starts-after:started-without:{ns}/{name}", "category": "degraded", "severity": "degraded",
                          "title": f"{name} started without {_words(wait['started_without'])}",
                          "body": f"{_words(wait['started_without'])} was not ready after {_span(setting['timeout'])}, "
                                  f"so {name} started anyway and may not work until it is restarted.",
                          "resolved": f"{name} started with what it needs", "href": href,
                          "signals": {"at": wait.get("at")}})
        if (setting and wait.get("waiting_for") and setting.get("on_timeout") == "wait"
                and now - (wait.get("since") or now) >= setting["timeout"]):
            facts.append({"key": f"starts-after:waiting:{ns}/{name}", "category": "degraded", "severity": "degraded",
                          "title": f"{name} is waiting for {_words(wait['waiting_for'])}",
                          "body": f"{name} is set to keep waiting, and {_words(wait['waiting_for'])} has not been ready "
                                  f"for {_span(setting['timeout'])}. Start it, or set {name} to Start anyway.",
                          "resolved": f"{name} started", "href": href, "signals": {}})
        gone = [a["app"] for a in (setting or {}).get("after", []) if a.get("gone")]
        if gone:
            facts.append({"key": f"starts-after:gone:{ns}/{name}", "category": "degraded", "severity": "degraded",
                          "title": f"{name} waits for {_words(gone)}, which no longer exists",
                          "body": f"Remove it from Starts after in {name}'s Edit; until then each start of {name} "
                                  "waits for it.", "resolved": f"{name} waits only for apps that exist", "href": href,
                          "signals": {}})
        gate = row.get("start_gate")
        if gate == "differs" or (gate == "missing" and (setting or {}).get("managed")):
            # The same setting saved again is no change: Edit's button sends restore_gate.
            fix = ("commit the lines Edit → Placement shows" if (setting or {}).get("managed")
                   else "use Put the start gate back in Edit → Placement; that restarts it once")
            facts.append({"key": f"starts-after:differs:{ns}/{name}", "category": "degraded", "severity": "info",
                          "title": f"{name}'s start gate does not match its setting",
                          "body": f"Its pods don't wait as Starts after says: {fix}.",
                          "resolved": f"{name}'s start gate matches its setting", "href": href, "signals": {}})
    # A loop made outside Homestead: with Keep waiting it never starts.
    refs = [(r["ns"], r["name"]) for r in rows]
    index = {ref: n for n, ref in enumerate(refs)}
    edges = {index[(r["ns"], r["name"])]: [index[(a["ns"], a["app"])] for a in r["start_setting"]["after"]
                                          if (a.get("ns"), a.get("app")) in index]
             for r in rows if isinstance(r.get("start_setting"), dict)}
    comp = _components(len(refs), edges)
    loops = {}
    for v, root in comp.items():
        loops.setdefault(root, []).append(refs[v])
    for members in loops.values():
        if len(members) < 2:
            continue
        members.sort()
        facts.append({"key": "starts-after:cycle:" + ",".join(f"{ns}/{n}" for ns, n in members),
                      "category": "degraded", "severity": "degraded",
                      "title": f"{_words(n for _, n in members)} wait for each other",
                      "body": "Each starts only after the other: with Keep waiting neither starts; "
                              "set one of them to Start anyway, or remove one of the links.",
                      "resolved": "no apps wait for each other", "href": "/containers", "signals": {}})
    return facts


def row_unread():
    """The row's fields when they could not be worked out: empty, and said."""
    return {"starts_after": [], "start_unreadable": True, "start_gate": "differs", "start_wait": None,
            "start_setting": None, "start_note": "its Starts after setting could not be read"}


def flow_after(dep):
    """The Architecture page's arrows: the apps dep starts after, dashed
    while its gate is not in the template (a Flux app not committed yet)."""
    if not dep:
        return []
    committed = gate_state(dep) == "rendered"
    return [{"id": f"w:{ns}/{app}", "committed": committed} for ns, app in after_of(dep)]
