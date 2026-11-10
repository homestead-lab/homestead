"""Every API call a page makes has a route on the server and an answer in the
demo, and every demo answer has a route (#733).

The three sides of a call were each tested alone: the server's routes
(test_module_routes.py), the demo's paths (demo-routes.test.js), and nothing
for the 600-odd api(...) calls in web/js. A typo in a page's path, a route
renamed on the server but not in the page, or a demo answer left behind for
a route that is gone passed every unit test.

The page side is read as text (source_scan.js_calls). A path written as a
string or a template is checked, and the query string is ignored. A path
built entirely at run time (a variable, a ternary) cannot be read and is
counted instead. The server side is homestead_routes' table, the routes
server.py answers itself (test_module_routes.INLINE_ROUTES) and the one it
matches by its parts (PART_ROUTES). The demo side is the paths demo.js
answers; it answers a path whatever the method.

The reading is loose where it can't be exact, so it misses some mistakes
rather than inventing them: a ${...} part matches any text without a / (an
empty one too), and a string added to ("/api/x/" + a + "/y") is read as its
first piece followed by one such part. Direct fetch(), EventSource and
WebSocket paths are not read.

What fails today is listed below with its reason. The lists may only shrink:
an entry that passes now must be removed, and a new call needs its route and
its demo answer rather than a new entry.
"""
import re
import sys
import unittest
from collections import namedtuple
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
import homestead_routes as ROUTES
import source_scan as SS
from test_module_routes import INLINE_ROUTES

WEB = SS.ROOT / "web" / "js"
DEMO = WEB / "demo.js"

# Functions that hand a path on to api(): (where the path is, the method or
# where the options are, how the path is written into the one api() gets).
# api() and mustRead() are global; the others belong to the file they are
# declared in.
Wrapper = namedtuple("Wrapper", "path method template", defaults=("{}",))
GLOBAL_WRAPPERS = {"api": Wrapper(0, 1), "mustRead": Wrapper(0, 2)}
WRAPPERS = {
    "app.js": {"cached": Wrapper(1, "GET")},
    "core-privileges.js": {"post": Wrapper(0, "POST")},
    "filemanager.js": {"fmPost": Wrapper(0, "POST"), "fmGet": Wrapper(0, "GET")},
    "pwa.js": {"pwaPost": Wrapper(0, "POST")},
    "troubleshooting.js": {"post": Wrapper(0, "POST", "/api/diagnostics/{}")},
    "views-agents.js": {"agentPost": Wrapper(0, "POST")},
    "views-helm.js": {"helmPost": Wrapper(0, "POST")},
    "views-ipam.js": {"ipamPost": Wrapper(0, "POST")},
    "views-lifecycle-import.js": {"part": Wrapper(0, "GET")},
    "views-lifecycle-vms.js": {"partly": Wrapper(0, "GET")},
    "views-resources.js": {"resPost": Wrapper(0, "POST")},
    "views-settings.js": {"selfDataPost": Wrapper(0, "POST")},
    "views-stats.js": {"mqttPost": Wrapper(0, "POST")},
    "views-storage-disks.js": {"tagsSave": Wrapper(0, "POST")},
    "views-storage-volumes.js": {"part": Wrapper(0, "GET")},
    "views-vms.js": {"vmStorePost": Wrapper(0, "POST"), "uvmPost": Wrapper(0, "POST"), "part": Wrapper(0, "GET")},
}

# Calls whose path is built entirely at run time, so this test cannot read
# them. Lower it as they become readable; never raise it.
MAX_UNREAD = 33

# Page calls with no server route: "METHOD /path" (a ${...} part as {}).
NO_ROUTE = {}

CHANGE = "a change the demo has no answer for: the live demo takes it and keeps nothing; ?demo=1 shows an error"
# Page calls the demo does not answer.
NO_DEMO_ANSWER = {
    "DELETE /api/workload/{}/{}": "Delete workload: the demo matches paths whole and has no answer for one with a name in it",
    "GET /api/lh/snapshot/delete-plan": "Delete snapshot: the demo shows the dialog's read error instead of the plan",
    "GET /api/node/probe/allocation/check": "host card's allocation check (admin): the demo shows its read error",
    "GET /api/push/key": "notifications setup: the demo has no push key to give",
    "POST /api/addons/kubevirt/emulation": CHANGE,
    "POST /api/addons/multus/repair": CHANGE,
    "POST /api/addons/smb/remove": CHANGE,
    "POST /api/auth/password": CHANGE,
    "POST /api/auth/users/delete": CHANGE,
    "POST /api/deploy": CHANGE,
    "POST /api/disks/name": CHANGE,
    "POST /api/hardware/rescan": CHANGE,
    "POST /api/helm/uninstall": CHANGE,
    "POST /api/helm/upgrade": CHANGE,
    "POST /api/images/cleanup": CHANGE,
    "POST /api/ipam/subnets": CHANGE,
    "POST /api/lh/assign": CHANGE,
    "POST /api/lh/backup": CHANGE,
    "POST /api/lh/job": CHANGE,
    "POST /api/lh/job/delete": CHANGE,
    "POST /api/lh/snapshot": CHANGE,
    "POST /api/lh/snapshot/delete": CHANGE,
    "POST /api/lh/target": CHANGE,
    "POST /api/lh/trim": CHANGE,
    "POST /api/logo-fit/resized": CHANGE,
    "POST /api/move/moves/dismiss": CHANGE,
    "POST /api/namespaces/create": CHANGE,
    "POST /api/namespaces/delete": CHANGE,
    "POST /api/network/vips/default": CHANGE,
    "POST /api/node/cordon": CHANGE,
    "POST /api/node/drain": CHANGE,
    "POST /api/node/hardware": CHANGE,
    "POST /api/node/os/check": CHANGE,
    "POST /api/node/power/release": CHANGE,
    "POST /api/node/shell/prepare": CHANGE,
    "POST /api/node/smart/test": CHANGE,
    "POST /api/operations/resume": CHANGE,
    "POST /api/os-updates/settings": CHANGE,
    "POST /api/os-updates/stop": CHANGE,
    "POST /api/passthrough/iommu": CHANGE,
    "POST /api/passthrough/pci": CHANGE,
    "POST /api/passthrough/usb": CHANGE,
    "POST /api/platform/baseline/install": CHANGE,
    "POST /api/push/status": CHANGE,
    "POST /api/push/subscribe": CHANGE,
    "POST /api/push/test": CHANGE,
    "POST /api/push/unsubscribe": CHANGE,
    "POST /api/resources/create": CHANGE,
    "POST /api/resources/delete": CHANGE,
    "POST /api/resources/save": CHANGE,
    "POST /api/restart": CHANGE,
    "POST /api/scale": CHANGE,
    "POST /api/schedules/delete": CHANGE,
    "POST /api/schedules/run": CHANGE,
    "POST /api/self/data/handoff/close": CHANGE,
    "POST /api/self/data/handoff/close/preview": CHANGE,
    "POST /api/self/permissions": CHANGE,
    "POST /api/shares/delete": CHANGE,
    "POST /api/shares/repair": CHANGE,
    "POST /api/sources/delete": CHANGE,
    "POST /api/storage/classes/cleanup": CHANGE,
    "POST /api/storage/classes/default": CHANGE,
    "POST /api/storage/classes/delete": CHANGE,
    "POST /api/vm/migrate": CHANGE,
    "POST /api/vm/store/auto": CHANGE,
    "POST /api/vm/store/forget": CHANGE,
    "POST /api/volumes/create": CHANGE,
    "POST /api/volumes/delete": CHANGE,
    "POST /api/volumes/edit": CHANGE,
    "POST /api/volumes/repair-class": CHANGE,
    "POST /api/workload/primary-port": CHANGE,
}
# Demo answers for a path the server has no route for.
DEMO_WITHOUT_ROUTE = {}

ANY = object()
Call = namedtuple("Call", "file line method parts")
Route = namedtuple("Route", "method path kind")


def _template(arg, blanked):
    """The parts of a template literal that is the whole argument, or None."""
    parts, start, i = [], 1, 1
    while i < len(arg):
        if blanked.startswith("${", i):
            parts.append(arg[start:i])
            depth, i = 0, i + 2
            while depth or blanked[i] != "}":
                depth += {"{": 1, "}": -1}.get(blanked[i], 0)
                i += 1
            parts.append(ANY)
            start = i = i + 1
        elif blanked[i] == "`":
            parts.append(arg[start:i])
            return parts, arg[i + 1:].strip()
        else:
            i += 1
    return None


def path_parts(arg, template="{}"):
    """How a path argument is written: a list of text and ANY, with the query
    string cut off; None when it is built at run time."""
    if not arg:
        return None
    blanked = SS.js_code(SS.code(arg))
    if arg[:1] in "\"'":
        end = blanked.index(arg[0], 1)
        parts, rest = [arg[1:end]], arg[end + 1:].strip()
    elif arg[:1] == "`":
        parts, rest = _template(arg, blanked)
    else:
        return None
    if rest:
        if not rest.startswith("+"):
            return None
        parts.append(ANY)
    before, _, after = template.partition("{}")
    parts = [before, *parts, after]
    cut = []
    for part in parts:
        if part is not ANY and "?" in part:
            cut.append(part.split("?", 1)[0])
            break
        cut.append(part)
    merged = []
    for part in cut:
        if part is not ANY and merged and merged[-1] is not ANY:
            merged[-1] += part
        elif part != "":
            merged.append(part)
    cut = tuple(merged)
    if not cut or cut[0] is ANY or not cut[0].startswith("/api/"):
        return None
    return cut


def method_of(args, where):
    if isinstance(where, str):
        return where
    if len(args) <= where:
        return "GET"
    found = re.search(r"\bmethod\s*:\s*[\"'`](\w+)", args[where])
    if found:
        return found.group(1).upper()
    return "GET" if args[where].startswith("{") else None


def shown(parts):
    return "".join("{}" if part is ANY else part for part in parts)


def pattern(parts):
    return re.compile("".join("[^/]*" if part is ANY else re.escape(part) for part in parts))


def web_calls():
    """Every page call: (readable calls, how many could not be read)."""
    found, unread = [], 0
    for path in sorted(WEB.glob("*.js")):
        if path == DEMO:
            continue
        wrappers = {**GLOBAL_WRAPPERS, **WRAPPERS.get(path.name, {})}
        for name, wrapper in wrappers.items():
            for call in SS.js_calls(path, name):
                if len(call.args) <= wrapper.path:
                    continue
                parts = path_parts(call.args[wrapper.path], wrapper.template)
                if parts is None:
                    unread += 1
                    continue
                found.append(Call(path.name, call.line, method_of(call.args, wrapper.method), parts))
    return found, unread


# Routes a module's prefix route matches by the parts of the path: each {} is
# one segment. A test below checks homestead_deploy_server still does.
PART_ROUTES = (Route("DELETE", "/api/workload/{}/{}", "parts"),)


def server_routes():
    """Every route: the table, server.py's own (a path ending in / is a
    prefix), and PART_ROUTES."""
    routes = [Route(method, path, "exact") for method, path in ROUTES.table()]
    routes += [Route(method, path, "prefix" if path.endswith("/") else "exact") for method, path, why in INLINE_ROUTES
               if not why.startswith("not a route")]
    return routes + list(PART_ROUTES)


LOOP_KEYS = re.compile(r"for \(const (\w+) of \[([^\]]*)\]\) responses\[`(/api/[^`$]*)\$\{\1\}([^`$]*)`\]\s*=(?!=)")


def demo_paths(source=DEMO):
    """The paths demo.js answers: its keys, the keys it sets later, the paths
    it tests for itself, and the keys it sets in a loop over a list."""
    source = SS.text(source) if not isinstance(source, SS.Code) else source
    keys = re.findall(r"(?:^|[{,])\s*\"(/api/[^\"]+)\"\s*:", source, re.M)
    keys += re.findall(r"responses\[\"(/api/[^\"]+)\"\]\s*=(?!=)", source)
    keys += re.findall(r"\bkey\s*===\s*\"(/api/[^\"]+)\"", source)
    for loop in LOOP_KEYS.finditer(source):
        keys += [loop[3] + item + loop[4] for item in re.findall(r"[\"']([^\"']*)[\"']", loop[2])]
    return set(keys)


def _answers(route, path):
    """Whether a route answers a path written out in full."""
    if route.kind == "prefix":
        return path.startswith(route.path)
    if route.kind == "parts":
        return bool(re.fullmatch(re.escape(route.path).replace(r"\{\}", "[^/]+"), path))
    return path == route.path


def routed(call, routes):
    """Whether a route answers a call. A ${...} part of the call is one path
    segment; a call whose method can't be read matches any method."""
    whole = pattern(call.parts)
    sample = "".join("x" if part is ANY else part for part in call.parts)
    for route in routes:
        if call.method not in (None, route.method) and route.method != "ANY":
            continue
        if route.kind == "exact" and whole.fullmatch(route.path) or route.kind != "exact" and _answers(route, sample):
            return True
    return False


def where(calls):
    return ", ".join(f"{call.file}:{call.line}" for call in calls)


def name(call):
    return f"{call.method or 'ANY'} {shown(call.parts)}"


def gaps():
    calls, unread = web_calls()
    routes = server_routes()
    demo = demo_paths()
    no_route, no_demo = {}, {}
    for call in calls:
        if not routed(call, routes):
            no_route.setdefault(name(call), []).append(call)
        if not any(map(pattern(call.parts).fullmatch, demo)):
            no_demo.setdefault(name(call), []).append(call)
    orphans = sorted(key for key in demo if not any(_answers(route, key) for route in routes))
    return calls, unread, no_route, no_demo, orphans


class Reader(unittest.TestCase):
    """How a call's path is read."""

    def test_a_string_or_a_template_is_read_and_its_query_cut_off(self):
        self.assertEqual(("/api/vms",), path_parts('"/api/vms"'))
        self.assertEqual(("/api/vms",), path_parts("'/api/vms?all=1'"))
        self.assertEqual(("/api/vm/", ANY, "/logo"), path_parts('`/api/vm/${encodeURIComponent(x + "/")}/logo?a=${b}`'))
        self.assertEqual(("/api/diagnostics/events",), path_parts('"events"', "/api/diagnostics/{}"))

    def test_a_string_added_to_is_read_up_to_its_query(self):
        self.assertEqual(("/api/logs",), path_parts('"/api/logs?ns=" + ns'))
        self.assertEqual(("/api/vm/", ANY), path_parts('"/api/vm/" + name'))

    def test_a_path_built_at_run_time_is_not_read(self):
        for arg in ("path", 'kind === "vm" ? "/api/vms" : "/api/workloads"', "`${base}/logo`", "endpoint + \"/x\""):
            self.assertIsNone(path_parts(arg), arg)

    def test_the_method_comes_from_the_options_when_it_can_be_read(self):
        self.assertEqual("GET", method_of(['"/api/x"'], 1))
        self.assertEqual("POST", method_of(['"/api/x"', '{ method: "POST", body }'], 1))
        self.assertEqual("GET", method_of(['"/api/x"', "{ keep: true }"], 1))
        self.assertIsNone(method_of(['"/api/x"', "opts"], 1))
        self.assertEqual("POST", method_of(['"/api/x"'], "POST"))

    def test_the_demo_s_paths_include_those_it_answers_in_a_loop(self):
        paths = demo_paths(SS.code('for (const path of ["a", "b"]) responses[`/api/x/${path}`] = () => 1;\n'))
        self.assertEqual({"/api/x/a", "/api/x/b"}, paths)
        self.assertLessEqual({f"/api/diagnostics/{name}" for name in ("events", "stop", "draft", "prepare", "delete")},
                             demo_paths())

    def test_the_demo_s_paths_are_its_keys_not_its_values(self):
        paths = demo_paths()
        self.assertIn("/api/cluster", paths)
        self.assertIn("/api/firewall/save", paths)
        self.assertIn("/api/diagnostics/download", paths)
        self.assertGreater(len(paths), 300)

    def test_every_listed_wrapper_hands_its_path_to_api(self):
        for file, wrappers in WRAPPERS.items():
            for wrapper in wrappers:
                declared = re.search(r"\b(?:function\s+|const\s+)" + re.escape(wrapper) + r"\b", SS.text(WEB / file))
                self.assertTrue(declared, f"{wrapper} is no longer declared in web/js/{file}: remove it from WRAPPERS")
                self.assertTrue(SS.js_calls(WEB / file, wrapper), f"{wrapper} is no longer called in web/js/{file}")
                body = SS.js_code(WEB / file)[declared.end():declared.end() + 400]
                self.assertRegex(body, r"(?<![\w$.])(api|mustRead)\s*\(", f"{wrapper} no longer calls api()")


class Routing(unittest.TestCase):
    """How a call is matched to a route."""

    ROUTES = [Route("POST", "/api/save", "exact"), Route("GET", "/api/fleet/all/", "prefix"),
              Route("ANY", "/api/anything", "exact"), Route("DELETE", "/api/workload/{}/{}", "parts")]

    def call(self, method, *parts):
        return Call("x.js", 1, method, parts)

    def test_a_call_with_another_method_is_a_gap(self):
        self.assertFalse(routed(self.call("GET", "/api/save"), self.ROUTES))
        self.assertTrue(routed(self.call("POST", "/api/save"), self.ROUTES))

    def test_a_prefix_route_answers_every_path_under_it(self):
        self.assertTrue(routed(self.call("GET", "/api/fleet/all/flow"), self.ROUTES))
        self.assertTrue(routed(self.call("GET", "/api/fleet/all/", ANY), self.ROUTES))
        self.assertFalse(routed(self.call("GET", "/api/fleet/allx"), self.ROUTES))

    def test_a_call_whose_method_cannot_be_read_matches_any_method(self):
        self.assertTrue(routed(self.call(None, "/api/save"), self.ROUTES))

    def test_an_any_route_answers_every_method(self):
        self.assertTrue(routed(self.call("PUT", "/api/anything"), self.ROUTES))

    def test_a_route_matched_by_its_parts_takes_one_segment_per_part(self):
        self.assertTrue(routed(self.call("DELETE", "/api/workload/", ANY, "/", ANY), self.ROUTES))
        self.assertFalse(routed(self.call("DELETE", "/api/workload/", ANY), self.ROUTES))
        self.assertFalse(routed(self.call("GET", "/api/workload/", ANY, "/", ANY), self.ROUTES))

    def test_the_workload_delete_is_still_matched_by_its_parts(self):
        # #764 moved it out of server.py's do_DELETE into a module prefix route.
        self.assertIn(("DELETE", "/api/workload/*"), ROUTES.table())
        delete = SS.functions(SS.SERVER / "homestead_deploy_server.py")["_delete_workload"].text
        self.assertIn('re.fullmatch(r"/api/workload/([a-z0-9.-]+)/([a-z0-9.-]+)", request.path)', delete,
                      "_delete_workload changed: update PART_ROUTES in this test")


class Contract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.calls, cls.unread, cls.no_route, cls.no_demo, cls.orphans = gaps()

    def test_the_page_calls_were_found(self):
        self.assertGreater(len(self.calls), 400)

    def test_every_page_call_has_a_server_route(self):
        new = sorted(set(self.no_route) - set(NO_ROUTE))
        self.assertEqual([], [f"{gap} ({where(self.no_route[gap])}): the server has no such route. Check the path "
                              "against the module's ROUTES; if server.py answers it itself, list it in INLINE_ROUTES "
                              "(tests/test_module_routes.py)." for gap in new])

    def test_every_page_call_has_a_demo_answer(self):
        new = sorted(set(self.no_demo) - set(NO_DEMO_ANSWER))
        self.assertEqual([], [f"{gap} ({where(self.no_demo[gap])}): add a demo answer for it in web/js/demo.js, "
                              "as a literal key of responses." for gap in new])

    def test_every_demo_answer_has_a_server_route(self):
        new = sorted(set(self.orphans) - set(DEMO_WITHOUT_ROUTE))
        self.assertEqual([], [f"{path}: web/js/demo.js answers it, but the server has no such route. "
                              "Remove the answer, or fix its path." for path in new])

    def test_the_lists_only_shrink(self):
        stale = sorted({*(set(NO_ROUTE) - set(self.no_route)), *(set(NO_DEMO_ANSWER) - set(self.no_demo)),
                        *(set(DEMO_WITHOUT_ROUTE) - set(self.orphans))})
        self.assertEqual([], [f"{gap} passes now: remove it from this test's lists." for gap in stale])

    def test_unread_calls_only_go_down(self):
        self.assertLessEqual(self.unread, MAX_UNREAD,
                             "a new api() call builds its whole path at run time: write it as a string or a "
                             "template, or list the function that passes it on in WRAPPERS.")
        self.assertGreaterEqual(self.unread, MAX_UNREAD - 5, f"only {self.unread} calls are unread now: lower MAX_UNREAD")


if __name__ == "__main__":
    unittest.main()
