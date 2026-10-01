"""Homestead's public API, version 1: stable, scoped, and self-describing.

The web app talks to Homestead through routes that change whenever the app
does. This is the other door - for Home Assistant, scripts and AI agents -
and it promises more: every endpoint here is declared once, below, with the
scope it needs, its parameters and the shape of its answer, and that one
declaration is both what the dispatcher enforces and what /api/v1/openapi.json
describes (and docs/api/openapi.json, kept in step by a test). Nothing
reaches an endpoint whose declaration does not grant its scope.

Shapes here are this module's own, built from Homestead's internal ones and
kept stable across releases: a field may be added, never renamed or removed
within v1.
"""
import re
import urllib.error

VERSION = "1"
NAME = re.compile(r"[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?")
ctx = {}


def bind(**functions):
    """nodes, workloads, vms, alerts, jobs, scale, restart, vm_power, version."""
    ctx.update(functions)


class ApiError(Exception):
    def __init__(self, code, message, **extra):
        super().__init__(message)
        self.code, self.message, self.extra = code, message, extra


# ---------------------------------------------------------------- shapes

def _container(w):
    desired, ready = int(w.get("desired") or 0), int(w.get("ready") or 0)
    problems = [str(p) for p in w.get("problems") or []][:10]
    state = ("stopped" if desired == 0 else "running" if ready >= desired
             else "failing" if problems else "starting")
    return {"namespace": w["ns"], "name": w["name"], "state": state, "ready": ready, "desired": desired,
            "images": list(w.get("images") or []), "nodes": list(w.get("nodes") or []),
            "cpu_cores": w.get("cpu"), "memory_mb": w.get("mem_mb"), "uptime_seconds": w.get("uptime"),
            "problems": problems, "homestead": bool(w.get("homestead") or w.get("self"))}


def _vm(v):
    return {"namespace": v["ns"], "name": v["name"], "state": str(v.get("status") or ""),
            "running": bool(v.get("running")), "node": v.get("node") or "", "ip": v.get("ip") or "",
            "cores": v.get("cores"), "memory": v.get("memory") or "", "os": v.get("os") or ""}


def _node(n):
    return {"name": n["name"], "ready": n.get("status") == "Ready", "schedulable": bool(n.get("schedulable", True)),
            "roles": list(n.get("roles") or []), "cpu_percent": n.get("cpu_pct"), "memory_percent": n.get("mem_pct"),
            "memory_used_gb": n.get("mem_used_gb"), "memory_total_gb": n.get("mem_cap_gb"),
            "pods": n.get("pods"), "vms": n.get("vms"), "os": n.get("os") or "", "kernel": n.get("kernel") or ""}


def _alert(a):
    return {"key": str(a.get("key") or ""), "severity": str(a.get("severity") or ""),
            "category": str(a.get("category") or ""), "title": str(a.get("title") or ""),
            "detail": str(a.get("body") or ""), "since": a.get("first") or a.get("since") or a.get("at")}


def _job(j):
    return {"id": j["id"], "kind": j.get("kind", ""), "title": j.get("title", ""), "status": j.get("status", ""),
            "progress": j.get("progress", 0), "message": j.get("message", ""),
            "started_at": j.get("started_at", ""), "finished_at": j.get("finished_at", "")}


# ---------------------------------------------------------------- handlers

def _find(rows, ns, name, what):
    found = next((r for r in rows if r.get("ns") == ns and r.get("name") == name), None)
    if not found:
        raise ApiError(404, f"there is no {what} {ns}/{name}")
    return found


def whoami(auth, **_):
    return {"name": auth["name"], "kind": auth["kind"], "scopes": auth["scopes"], "expires": auth.get("expires")}


def status(**_):
    nodes, workloads, vms, alerts = ctx["nodes"](), ctx["workloads"](), ctx["vms"](), ctx["alerts"]()
    containers = [_container(w) for w in workloads]
    return {"version": ctx["version"](),
            "nodes": {"total": len(nodes), "ready": sum(1 for n in nodes if n.get("status") == "Ready")},
            "containers": {"total": len(containers), "running": sum(1 for c in containers if c["state"] == "running"),
                           "stopped": sum(1 for c in containers if c["state"] == "stopped"),
                           "failing": sum(1 for c in containers if c["state"] == "failing")},
            "vms": {"total": len(vms), "running": sum(1 for v in vms if v.get("running"))},
            "alerts": {"active": len(alerts), "critical": sum(1 for a in alerts if a.get("severity") == "critical")}}


def nodes(**_):
    return [_node(n) for n in ctx["nodes"]()]


def containers(query, **_):
    ns = (query.get("namespace") or [""])[0]
    return [_container(w) for w in ctx["workloads"]() if not ns or w["ns"] == ns]


def container(params, **_):
    return _container(_find(ctx["workloads"](), params["namespace"], params["name"], "container"))


def container_action(params, action, body, **_):
    ns, name = params["namespace"], params["name"]
    _find(ctx["workloads"](), ns, name, "container")
    if action == "restart":
        ctx["restart"](ns, name)
        return {"ok": True, "action": action, "warnings": []}
    return {"ok": True, "action": action, **ctx["scale"](ns, name, 1 if action == "start" else 0)}


def vms(query, **_):
    ns = (query.get("namespace") or [""])[0]
    return [_vm(v) for v in ctx["vms"]() if not ns or v["ns"] == ns]


def vm(params, **_):
    return _vm(_find(ctx["vms"](), params["namespace"], params["name"], "virtual machine"))


def vm_action(params, action, **_):
    ns, name = params["namespace"], params["name"]
    _find(ctx["vms"](), ns, name, "virtual machine")
    return {"ok": True, "action": action, **ctx["vm_power"](ns, name, action)}


def alerts(**_):
    return [_alert(a) for a in ctx["alerts"]()]


def job(params, **_):
    found = next((j for j in ctx["jobs"]() if j.get("id") == params["id"]), None)
    if not found:
        raise ApiError(404, "there is no such job")
    return _job(found)


# ---------------------------------------------------------------- the registry

def _obj(properties, required=None):
    return {"type": "object", "properties": properties, "required": list(required or properties)}


S, I, B, N = {"type": "string"}, {"type": "integer"}, {"type": "boolean"}, {"type": "number"}
NULLABLE = lambda schema: {**schema, "type": [schema["type"], "null"]}
SCHEMAS = {
    "Error": _obj({"error": S}),
    "WhoAmI": _obj({"name": S, "kind": {"type": "string", "enum": ["key", "session"]},
                    "scopes": {"type": "array", "items": S}, "expires": NULLABLE(I)}),
    "Status": _obj({"version": S,
                    "nodes": _obj({"total": I, "ready": I}),
                    "containers": _obj({"total": I, "running": I, "stopped": I, "failing": I}),
                    "vms": _obj({"total": I, "running": I}),
                    "alerts": _obj({"active": I, "critical": I})}),
    "Node": _obj({"name": S, "ready": B, "schedulable": B, "roles": {"type": "array", "items": S},
                  "cpu_percent": NULLABLE(N), "memory_percent": NULLABLE(N), "memory_used_gb": NULLABLE(N),
                  "memory_total_gb": NULLABLE(N), "pods": NULLABLE(I), "vms": NULLABLE(I), "os": S, "kernel": S}),
    "Container": _obj({"namespace": S, "name": S,
                       "state": {"type": "string", "enum": ["running", "starting", "failing", "stopped"]},
                       "ready": I, "desired": I, "images": {"type": "array", "items": S},
                       "nodes": {"type": "array", "items": S}, "cpu_cores": NULLABLE(N), "memory_mb": NULLABLE(N),
                       "uptime_seconds": NULLABLE(N), "problems": {"type": "array", "items": S},
                       "homestead": {"type": "boolean", "description": "Part of Homestead itself"}}),
    "VM": _obj({"namespace": S, "name": S, "state": S, "running": B, "node": S, "ip": S,
                "cores": NULLABLE(I), "memory": S, "os": S}),
    "Alert": _obj({"key": S, "severity": S, "category": S, "title": S, "detail": S, "since": NULLABLE(N)}),
    "Job": _obj({"id": S, "kind": S, "title": S,
                 "status": {"type": "string", "description": "queued, running, succeeded, failed or cancelled"},
                 "progress": N, "message": S, "started_at": S, "finished_at": S}),
    "ActionResult": _obj({"ok": B, "action": S, "warnings": {"type": "array", "items": S},
                          "job": NULLABLE(S)}, ["ok", "action", "warnings"]),
}
REF = lambda name: {"$ref": f"#/components/schemas/{name}"}
LIST = lambda name: {"type": "array", "items": REF(name)}
NS_QUERY = {"name": "namespace", "in": "query", "required": False, "schema": S,
            "description": "Only those in this namespace"}


class Endpoint:
    def __init__(self, method, path, scope, summary, handler, response, *, tag, description="",
                 query=(), action=None, operation=None):
        self.method, self.path, self.scope, self.summary = method, path, scope, summary
        self.handler, self.response, self.tag, self.description = handler, response, tag, description
        self.query, self.action = list(query), action
        self.operation = operation or re.sub(r"\W+", "_", f"{method}_{path}").strip("_").lower()
        names = re.findall(r"\{(\w+)\}", path)
        self.params = names
        self.pattern = re.compile("^" + re.sub(r"\\\{(\w+)\\\}", r"(?P<\1>[^/]+)", re.escape(path)) + "$")


START_NOTE = ("Homestead checks there is room first, as the app does. A start that cannot fit is refused (409) "
              "with the reasons; one that fits with warnings goes ahead, the warnings in the answer - the key's "
              "control scope is the acknowledgement the app asks a person for.")
ENDPOINTS = [
    Endpoint("GET", "/api/v1/whoami", None, "Who is asking", whoami, REF("WhoAmI"), tag="Access",
             description="The key (or signed-in person) making the request, its scopes and when it expires."),
    Endpoint("GET", "/api/v1/status", "read", "The cluster at a glance", status, REF("Status"), tag="Status",
             description="Counts of nodes, containers, VMs and alerts: one call for a dashboard or a health check."),
    Endpoint("GET", "/api/v1/nodes", "read", "Nodes", nodes, LIST("Node"), tag="Status"),
    Endpoint("GET", "/api/v1/alerts", "read", "Active alerts", alerts, LIST("Alert"), tag="Status",
             description="What is wrong right now, as the app's bell shows it."),
    Endpoint("GET", "/api/v1/containers", "read", "Containers", containers, LIST("Container"), tag="Containers",
             query=[NS_QUERY]),
    Endpoint("GET", "/api/v1/containers/{namespace}/{name}", "read", "One container", container, REF("Container"),
             tag="Containers"),
    *[Endpoint("POST", f"/api/v1/containers/{{namespace}}/{{name}}/{action}", "containers:control",
               f"{action.capitalize()} a container", container_action, REF("ActionResult"), tag="Containers",
               action=action, operation=f"container_{action}",
               description={"start": "Starts the container with one replica. " + START_NOTE,
                            "stop": "Scales the container to zero. Homestead itself cannot be stopped this way.",
                            "restart": "Restarts its pods, one rollout."}[action])
      for action in ("start", "stop", "restart")],
    Endpoint("GET", "/api/v1/vms", "read", "Virtual machines", vms, LIST("VM"), tag="Virtual machines",
             query=[NS_QUERY]),
    Endpoint("GET", "/api/v1/vms/{namespace}/{name}", "read", "One virtual machine", vm, REF("VM"),
             tag="Virtual machines"),
    *[Endpoint("POST", f"/api/v1/vms/{{namespace}}/{{name}}/{action}", "vms:control",
               f"{action.capitalize()} a virtual machine", vm_action, REF("ActionResult"), tag="Virtual machines",
               action=action, operation=f"vm_{action}",
               description={"start": START_NOTE + " A start that needs a person - setting up missing TPM or "
                                                  "EFI state - is refused; do it in the app.",
                            "stop": "Asks the guest to shut down, as its power button would.",
                            "restart": "Restarts the guest. " + START_NOTE}[action])
      for action in ("start", "stop", "restart")],
    Endpoint("GET", "/api/v1/jobs/{id}", "read", "A job", job, REF("Job"), tag="Status",
             description="A background job by the id an action answered with: follow it until it ends."),
]


def match(method, path):
    """(endpoint, path parameters), or raise ApiError 404/405."""
    allowed = []
    for endpoint in ENDPOINTS:
        found = endpoint.pattern.match(path)
        if not found:
            continue
        if endpoint.method != method:
            allowed.append(endpoint.method)
            continue
        params = found.groupdict()
        for key, value in params.items():
            if key in ("namespace", "name") and not NAME.fullmatch(value):
                raise ApiError(400, f"{value} is not a valid {key}")
            if key == "id" and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
                raise ApiError(400, "that is not a job id")
        return endpoint, params
    if allowed:
        raise ApiError(405, f"use {' or '.join(sorted(set(allowed)))} here")
    raise ApiError(404, "no such endpoint; GET /api/v1/openapi.json lists them")


def handle(method, path, query, body, auth):
    """Run one request for an authenticated caller: (status, answer)."""
    try:
        endpoint, params = match(method, path)
        if endpoint.scope and endpoint.scope not in auth["scopes"]:
            raise ApiError(403, f"this needs the {endpoint.scope} scope")
        return 200, endpoint.handler(params=params, query=query, body=body or {}, auth=auth, action=endpoint.action)
    except ApiError as error:
        return error.code, {"error": error.message, **error.extra}
    except urllib.error.HTTPError as error:
        return (404, {"error": "it no longer exists"}) if error.code == 404 else (502, {"error": "the cluster did not answer"})
    except (ValueError, PermissionError) as error:
        return 409, {"error": str(error)[:400]}


# ---------------------------------------------------------------- the description

ERRORS = {"400": "The request is malformed", "401": "No API key, or one that is not valid, has expired, or is used from a network it is not allowed",
          "403": "The key does not have the scope this needs", "404": "No such thing",
          "409": "Refused: it cannot be done now (not enough room, Homestead itself, needs a person)",
          "429": "Too many refused keys from this address (a valid key is never refused this way)"}


def openapi(version, scopes):
    paths = {}
    for e in ENDPOINTS:
        op = {"operationId": e.operation, "summary": e.summary, "tags": [e.tag],
              "responses": {"200": {"description": "OK", "content": {"application/json": {"schema": e.response}}},
                            **{code: {"description": text, "content": {"application/json": {"schema": REF("Error")}}}
                               for code, text in ERRORS.items()
                               if code != "409" or e.method == "POST"}}}
        if e.description:
            op["description"] = e.description
        if e.scope:
            op["security"] = [{"apiKey": [e.scope]}]
            op["x-homestead-scope"] = e.scope
        params = [{"name": p, "in": "path", "required": True, "schema": S} for p in e.params] + list(e.query)
        if params:
            op["parameters"] = params
        paths.setdefault(e.path, {})[e.method.lower()] = op
    paths["/api/v1/openapi.json"] = {"get": {"operationId": "openapi", "summary": "This description", "tags": ["Access"],
                                             "responses": {"200": {"description": "OpenAPI 3.1",
                                                                   "content": {"application/json": {"schema": {"type": "object"}}}}}}}
    return {
        "openapi": "3.1.0",
        "info": {"title": "Homestead API", "version": f"{VERSION}.0",
                 **({"x-homestead-version": version} if version else {}),
                 "description": ("Read Homestead's status and start, stop and restart containers and virtual machines, "
                                 "with an API key made by an administrator under Settings > Users and access > API keys. "
                                 "Send it as `Authorization: Bearer hsk_...`. Every key expires, holds only the scopes "
                                 "it was given and works only on /api/v1."),
                 "license": {"name": "See the repository"}},
        "servers": [{"url": "/"}],
        "security": [{"apiKey": []}],
        "tags": [{"name": t} for t in ("Access", "Status", "Containers", "Virtual machines")],
        "components": {
            "securitySchemes": {"apiKey": {
                "type": "http", "scheme": "bearer", "bearerFormat": "hsk_<id>_<secret>",
                "description": "Scopes: " + "; ".join(f"`{s}` - {text}" for s, text in scopes.items())}},
            "schemas": SCHEMAS},
        "paths": dict(sorted(paths.items())),
    }
