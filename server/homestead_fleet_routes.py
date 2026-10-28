"""Linking clusters, picking which one a browser looks at, and the lists
gathered across them: the /api/fleet routes (#764). What a link is, and how
a request travels, is homestead_fleet; the relay itself stays in server.py,
which runs it before routing."""
import json
import urllib.parse

import homestead_cache as CACHE
import homestead_fleet as FLEET
import homestead_flow as FLOW
import homestead_move as MOVE
import homestead_nodes as NODES
import homestead_routes as MODULE_ROUTES
import homestead_storage as STORAGE
import homestead_vms as VMS
import homestead_workloads as WORKLOADS

Reply, Cookie = MODULE_ROUTES.Reply, MODULE_ROUTES.Cookie

# The resources the combined view gathers, and how this cluster answers locally.
FLEET_LISTS = {
    "workloads": lambda: CACHE.cached("wl", 5, WORKLOADS.get_workloads),
    "vms": lambda: CACHE.cached("vms", 5, VMS.list_vms),
    "nodes": lambda: CACHE.cached("nodes", 5, NODES.get_nodes),
    "volumes": lambda: CACHE.cached("vol", 8, STORAGE.get_volumes),
    "flow": lambda: FLOW.view(),
}


def summary(request):
    conn = MODULE_ROUTES.connected(request)
    conn.who()
    return FLEET.summary(via=conn.fleet)


def state(request):
    conn = MODULE_ROUTES.connected(request)
    conn.who()
    if not conn.fleet:
        return Reply(403, {"error": "only a linked Homestead asks for this"})
    return FLEET.shared_state()


def home(request):
    """Back to this cluster: the way out of one that does not answer."""
    return Reply(302, {"ok": True}, cookies=(Cookie(FLEET.COOKIE, "", max_age=0),), headers=(("Location", "/"),))


def gathered(what):
    """GET /api/fleet/all/<what>: the list from every linked cluster, and
    which of them did not answer."""
    def route(request):
        rows, missing = FLEET.gather(what, FLEET_LISTS[what], request.user, request.role)
        return Reply(200, rows, headers=(("X-Homestead-Fleet-Missing", urllib.parse.quote(json.dumps(missing))),)
                     if missing else ())
    return route


def linking(action):
    """A link action, with a refusal answered 409 and a cluster that does not
    answer 502."""
    def route(request):
        try:
            return action(request, request.body)
        except ValueError as error:
            return Reply(409, {"error": str(error)})
        except FLEET.Unreachable as error:
            return Reply(502, {"error": str(error)})
    return route


def switch(request, b):
    """Which cluster this browser looks at: a cookie, kept 30 days."""
    wanted = str(b.get("id") or "")
    if not wanted or wanted == FLEET.self_id():
        return Reply(200, {"ok": True, "id": FLEET.self_id()}, cookies=(Cookie(FLEET.COOKIE, "", max_age=0),))
    target = FLEET.member(wanted)
    if not target:
        return Reply(404, {"error": "no linked cluster by that name"})
    check = FLEET.check(target)
    if not check.get("reachable"):
        return Reply(502, {"error": f"{target.get('name')} is not answering this Homestead: "
                                    f"{check.get('error') or 'no answer'}"})
    return Reply(200, {"ok": True, "id": target["id"], "name": target.get("name")},
                 cookies=(Cookie(FLEET.COOKIE, target["id"], max_age=2592000),))


def sync(request, b):
    sender = MODULE_ROUTES.connected(request).fleet
    if not sender:
        return Reply(403, {"error": "only a linked Homestead sends this"})
    return FLEET.adopt(b, sender)


# The cookie only these routes set (homestead_routes.answer refuses it from any other).
OWN_COOKIES = (FLEET.COOKIE,)
# Its routes and who may use them (homestead_routes.py). Each combined list
# is its own route, so an unknown one is refused as undeclared, not a 404.
ROUTES = {
    ("GET", "/api/fleet"): ("viewer", summary),
    ("GET", "/api/fleet/state"): ("viewer", state),
    ("GET", "/api/fleet/home"): ("viewer", home),
    **{("GET", f"/api/fleet/all/{what}"): ("viewer", gathered(what)) for what in FLEET_LISTS},
    ("POST", "/api/fleet/switch"): ("viewer", linking(switch)),
    ("POST", "/api/fleet/join"): ("admin", linking(lambda request, b: FLEET.join(
        b.get("url"), b.get("username"), b.get("password"), b.get("own_url", ""), plain_ok=b.get("plain_ok") is True))),
    ("POST", "/api/fleet/accept"): ("admin", linking(lambda request, b: FLEET.accept(b))),
    ("POST", "/api/fleet/sync"): ("admin", linking(sync)),
    ("POST", "/api/fleet/remove"): ("admin", linking(lambda request, b: FLEET.remove(
        str(b.get("id") or ""), plain_ok=b.get("plain_ok") is True))),
    ("POST", "/api/fleet/leave"): ("admin", linking(lambda request, b: FLEET.leave(plain_ok=b.get("plain_ok") is True))),
    ("POST", "/api/fleet/address"): ("admin", linking(lambda request, b: FLEET.set_address(b.get("url")))),
    ("POST", "/api/fleet/link-legacy"): ("admin", linking(lambda request, b: MOVE.link_legacy(
        str(b.get("name") or ""), b.get("own_url", ""), plain_ok=b.get("plain_ok") is True))),
}
