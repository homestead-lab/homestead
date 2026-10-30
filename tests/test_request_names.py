"""Names a request gives are checked before they go into a Kubernetes path.

A namespace or workload name is put straight into the API path Homestead
asks as itself, so one holding "?" or "/" would point the request at a
different object. Each route that takes such a name refuses one that is not
a valid Kubernetes name, before anything is read or written."""
import sys
import unittest
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

    def test_the_target_check_refuses_a_name_that_is_not_a_kubernetes_name(self):
        import homestead_host_access as HOST
        self.addCleanup(HOST.set_role, None)
        HOST.set_role("operator")
        for ns, name in [(ns, "web") for ns in BAD_NAMESPACES] + [("lab", name) for name in BAD_NAMES]:
            with self.subTest(ns=ns, name=name), self.assertRaisesRegex(ValueError, "Kubernetes name"):
                server.KUBE_HELPERS.require_workload_target(ns, name)


class ApiPathCutTests(unittest.TestCase):
    """The shared path check refuses a path a name has cut short with "?"."""

    def test_a_second_query_start_is_refused(self):
        with self.assertRaises(ValueError):
            server.KUBE_HELPERS.api_path("/api/v1/namespaces/lab?x=/pods/web/log?tailLines=10")

    def test_a_query_that_starts_with_a_path_is_refused(self):
        for path in ("/api/v1/namespaces/kube-system/secrets/homestead-auth?/pods/web",
                     "/apis/apps/v1/namespaces/lab/deployments/web?/scale",
                     "/api/v1/namespaces/lab/pods?=x",
                     "/api/v1/namespaces/lab?limit=/pods/web",
                     "/api/v1/namespaces/lab?labelSelector=x/pods/web",
                     "/api/v1/namespaces/lab?labelSelector=/pods",
                     "/api/v1/namespaces/lab?container=a%2Fb"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                server.KUBE_HELPERS.api_path(path)

    def test_a_selector_value_with_a_slash_is_refused(self):
        for path in ("/api/v1/namespaces/lab?labelSelector=a=b/pods/web",
                     "/api/v1/namespaces/lab?labelSelector=app.kubernetes.io/name=x/log",
                     "/api/v1/namespaces/lab?labelSelector=app.kubernetes.io/name%3Dx%2Flog",
                     "/api/v1/namespaces/lab?labelSelector=x/log",
                     "/api/v1/namespaces/lab?labelSelector=app in (a,b/c)",
                     "/api/v1/namespaces/lab?fieldSelector=metadata.name=web/log",
                     "/api/v1/namespaces/lab?fieldSelector=spec/nodeName=node-a"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                server.KUBE_HELPERS.api_path(path)

    def test_an_encoded_question_mark_in_the_path_is_refused(self):
        for path in ("/api/v1/namespaces/lab%3f/pods/web", "/api/v1/namespaces/lab/pods/web%3F/log"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                server.KUBE_HELPERS.api_path(path)

    def test_selectors_in_every_form_kubernetes_takes_pass(self):
        for selector in ("app.kubernetes.io/name=x", "app.kubernetes.io/name%3Dx", "app==web", "tier!=low",
                         "homestead.example.com/managed", "!homestead.example.com/held",
                         "env in (prod,staging)", "env%20notin%20(dev)", "app=web,tier!=low,release",
                         "job-name%3Dbackup-1", "app="):
            path = "/api/v1/pods?labelSelector=" + selector
            with self.subTest(selector=selector):
                self.assertEqual(path, server.KUBE_HELPERS.api_path(path))
        for selector in ("spec.nodeName%3Dnode-a", "involvedObject.name=web,involvedObject.uid=0a1b-2c",
                         "status.phase!=Succeeded", "metadata.name==web.example.com"):
            path = "/api/v1/pods?fieldSelector=" + selector
            with self.subTest(selector=selector):
                self.assertEqual(path, server.KUBE_HELPERS.api_path(path))

    def test_ordinary_queries_pass(self):
        for path in ("/api/v1/pods?labelSelector=app.kubernetes.io/name%3Dweb&limit=10",
                     "/api/v1/namespaces/lab/pods/web/log?tailLines=300&timestamps=true&container=main",
                     "/api/v1/pods?labelSelector=homestead.example.com/managed=true",
                     "/api/v1/pods?labelSelector=homestead.example.com/managed%3Dtrue,app.kubernetes.io/name!=x",
                     "/api/v1/namespaces/lab/pods?fieldSelector=spec.nodeName%3Dnode-a",
                     "/apis/batch/v1/namespaces/lab/cronjobs/nightly?propagationPolicy=Background",
                     "/api/v1/namespaces/lab/pods/web", "/api/v1/nodes?"):
            with self.subTest(path=path):
                self.assertEqual(path, server.KUBE_HELPERS.api_path(path))


if __name__ == "__main__":
    unittest.main()
