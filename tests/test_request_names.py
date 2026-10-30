"""Names a request gives are checked before they go into a Kubernetes path.

A namespace or workload name is put straight into the API path Homestead
asks as itself, so one holding "?" or "/" would point the request at a
different object. Each route that takes such a name refuses one that is not
a valid Kubernetes name, before anything is read or written."""
import ast
import sys
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_routes as ROUTER
import server

BAD_NAMESPACES = ("lab?x=", "kube-system/secrets/homestead-auth?", "lab/x", "lab#x", "..", "lab%2Fx", "Lab", "")
BAD_NAMES = ("web?x=", "a/b", "web/../x", "web?", "web#x", "..", "web%2Fx", "Web", "")


def untouched(*_args, **_kwargs):
    raise AssertionError("nothing may be asked of the API server for a refused name")


class RequestNameTests(unittest.TestCase):
    def setUp(self):
        for name in ("kget", "ksend"):
            patcher = mock.patch.object(server.WORKLOADS, name, side_effect=untouched)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name in ("kget", "ksend", "_request", "transport"):
            if hasattr(server.KUBE_HELPERS, name):
                patcher = mock.patch.object(server.KUBE_HELPERS, name, side_effect=untouched)
                patcher.start()
                self.addCleanup(patcher.stop)

    def call(self, method, path, ns, name):
        role, handler = server.WORKLOADS.ROUTES[(method, path)]
        if method == "GET":
            request = ROUTER.Request(method, path, {"ns": [ns], "name": [name]}, {}, "someone", "admin")
        else:
            body = {"ns": ns, "name": name, "image": "example.com/app:1", "port": 8080, "priority": "low",
                    "starts_after": {"after": ["db"]}, "replicas": 1}
            request = ROUTER.Request(method, path, {}, body, "someone", "admin")
        return handler(request)

    def assert_refused(self, method, path):
        for ns, name in [(ns, "web") for ns in BAD_NAMESPACES] + [("lab", name) for name in BAD_NAMES]:
            with self.subTest(ns=ns, name=name):
                with self.assertRaisesRegex(ValueError, "Kubernetes name"):
                    self.call(method, path, ns, name)

    def test_the_edit_form_refuses_a_name_that_is_not_a_kubernetes_name(self):
        self.assert_refused("GET", "/api/workload")

    def test_saving_an_edit_refuses_a_name_that_is_not_a_kubernetes_name(self):
        self.assert_refused("POST", "/api/edit")

    def test_previewing_an_edit_refuses_a_name_that_is_not_a_kubernetes_name(self):
        self.assert_refused("POST", "/api/edit/preview")

    def test_setting_the_primary_port_refuses_a_namespace_that_is_not_a_kubernetes_name(self):
        for ns in BAD_NAMESPACES[:-1]:   # an empty namespace means the default one
            with self.subTest(ns=ns), self.assertRaisesRegex(ValueError, "Kubernetes name"):
                self.call("POST", "/api/workload/primary-port", ns, "web")

    def test_scaling_refuses_a_name_that_is_not_a_kubernetes_name(self):
        self.assert_refused("POST", "/api/scale")

    def test_restarting_refuses_a_name_that_is_not_a_kubernetes_name(self):
        self.assert_refused("POST", "/api/restart")

    def test_the_start_plan_refuses_a_name_that_is_not_a_kubernetes_name(self):
        self.assert_refused("GET", "/api/workloads/start-plan")

    def test_failover_settings_refuse_a_name_that_is_not_a_kubernetes_name(self):
        _, handler = server.WORKLOADS.ROUTES[("POST", "/api/workloads/failover")]
        for ns, name in [(ns, "web") for ns in BAD_NAMESPACES] + [("lab", name) for name in BAD_NAMES]:
            with self.subTest(ns=ns, name=name), self.assertRaisesRegex(ValueError, "Kubernetes name"):
                handler(ROUTER.Request("POST", "/api/workloads/failover", {},
                                       {"items": [{"ns": ns, "name": name, "mode": "fast"}]}, "someone", "admin"))
        import homestead_failover as FAILOVER
        with self.assertRaisesRegex(ValueError, "Kubernetes name"):
            FAILOVER.set_many([{"ns": "lab", "name": "web?x=", "mode": "fast"}])

    def test_the_target_check_keeps_an_admins_request_with_no_name(self):
        import homestead_host_access as HOST
        self.addCleanup(HOST.set_role, None)
        for role in ("admin", None):
            HOST.set_role(role)
            self.assertIsNone(server.KUBE_HELPERS.require_workload_target("lab", ""))

    def test_the_target_check_refuses_a_name_that_is_not_a_kubernetes_name(self):
        import homestead_host_access as HOST
        self.addCleanup(HOST.set_role, None)
        HOST.set_role("operator")
        for ns, name in [(ns, "web") for ns in BAD_NAMESPACES] + [("lab", name) for name in BAD_NAMES]:
            with self.subTest(ns=ns, name=name), self.assertRaisesRegex(ValueError, "Kubernetes name"):
                server.KUBE_HELPERS.require_workload_target(ns, name)


class ApiPathCutTests(unittest.TestCase):
    """The shared path check is a backstop on structure: one "?", no "#",
    neither (plain or encoded) inside a path segment, and query parameters
    with plain names. What a selector says is Kubernetes' to read."""

    def test_a_second_query_start_is_refused(self):
        with self.assertRaises(ValueError):
            server.KUBE_HELPERS.api_path("/api/v1/namespaces/lab?x=/pods/web/log?tailLines=10")

    def test_a_query_that_starts_with_a_path_is_refused(self):
        for path in ("/api/v1/namespaces/kube-system/secrets/homestead-auth?/pods/web",
                     "/apis/apps/v1/namespaces/lab/deployments/web?/scale",
                     "/api/v1/namespaces/lab/pods?=x", "/api/v1/namespaces/lab/pods?limit=1#x"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                server.KUBE_HELPERS.api_path(path)

    def test_an_encoded_question_mark_or_hash_in_a_segment_is_refused(self):
        for path in ("/api/v1/namespaces/lab%3f/pods/web", "/api/v1/namespaces/lab/pods/web%3F/log",
                     "/api/v1/namespaces/lab/pods/web%23x"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                server.KUBE_HELPERS.api_path(path)

    def test_selectors_in_every_form_kubernetes_takes_pass(self):
        for selector in ("app.kubernetes.io/name=x", "app.kubernetes.io/name%3Dx", "example/app=web",
                         "x/log", "app==web", "tier!=low", "!example.com/held", "env in (prod,staging)",
                         "env+notin+(dev)", "app=", "app=web,tier!=low,release", "job-name%3Dbackup-1",
                         "replicas>1"):
            path = "/api/v1/pods?labelSelector=" + selector
            with self.subTest(selector=selector):
                self.assertEqual(path, server.KUBE_HELPERS.api_path(path))
        for selector in ("type%3Dkubernetes.io%2Ftls", "type=kubernetes.io/tls", "spec.nodeName%3Dnode-a",
                         "involvedObject.fieldPath=spec.containers{web}", "status.phase!=Succeeded"):
            path = "/api/v1/secrets?fieldSelector=" + selector
            with self.subTest(selector=selector):
                self.assertEqual(path, server.KUBE_HELPERS.api_path(path))

    def test_ordinary_queries_pass(self):
        for path in ("/api/v1/pods?labelSelector=app.kubernetes.io/name%3Dweb&limit=10",
                     "/api/v1/namespaces/lab/pods/web/log?tailLines=300&timestamps=true&container=main",
                     "/apis/batch/v1/namespaces/lab/cronjobs/nightly?propagationPolicy=Background",
                     "/api/v1/pods?limit=50&continue=eyJ2IjoibWV0YS5rOHMuaW8vdjEi",
                     "/api/v1/namespaces/lab/pods/web", "/api/v1/nodes?"):
            with self.subTest(path=path):
                self.assertEqual(path, server.KUBE_HELPERS.api_path(path))


class HomesteadsOwnQueriesTests(unittest.TestCase):
    """Every selector and query Homestead itself builds passes the check:
    each f-string or string in server/ holding labelSelector= or
    fieldSelector= is built with sample names and checked."""

    SAMPLES = (("selector", "example/app=web,app.kubernetes.io/name=web"), ("label", "example.com/managed"),
               ("query", "example/app=web"), ("key", "example.com/role"), ("token", "eyJ2IjoxfQ"))

    def sample(self, source):
        low = source.lower()
        value = next((v for k, v in self.SAMPLES if k in low), "web")
        if "quote(" in source:
            value = urllib.parse.quote(value, safe="")
        return value

    def built(self):
        out, seen = [], set()
        for path in sorted((Path(__file__).resolve().parents[1] / "server").glob("*.py")):
            source = path.read_text()
            tree = ast.parse(source)
            parts = {id(part) for node in ast.walk(tree) if isinstance(node, ast.JoinedStr) for part in node.values}
            for node in ast.walk(tree):
                if id(node) in parts:
                    continue
                if isinstance(node, ast.JoinedStr):
                    text = "".join(part.value if isinstance(part, ast.Constant) else
                                   self.sample(ast.get_source_segment(source, part.value) or "")
                                   for part in node.values)
                elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                    text = node.value
                else:
                    continue
                if "labelSelector=" not in text and "fieldSelector=" not in text:
                    continue
                text = text[min(i for i in (text.find("/api"), text.find("?")) if i >= 0):]
                out.append((f"{path.name}:{node.lineno}", text if text.startswith("/") else "/api/v1/pods" + text))
        return out

    def test_every_query_homestead_builds_passes_the_path_check(self):
        built = self.built()
        self.assertGreater(len(built), 40, "the scan found Homestead's selectors")
        for where, path in built:
            with self.subTest(where=where, path=path):
                self.assertEqual(path, server.KUBE_HELPERS.api_path(path))


if __name__ == "__main__":
    unittest.main()
