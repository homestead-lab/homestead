"""A request leaves nothing bound on its thread when it ends.

The request role (host_access) and the person a change is signed as
(changes.REQUEST.user) are thread locals. The handler bound them for each
request and cleared them only when the next request began, so after a request
they still named its caller. Tests that drive the handler in their own thread
left them bound for every later test (test_self_guard then failed in some
shard orders), and any later work on a handler's thread read the last caller."""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import server
import homestead_changes as CHANGES
import homestead_host_access as HOSTACCESS
import homestead_metrics as METRICS
import homestead_readyz as READYZ
import homestead_request_scope as SCOPE
import homestead_routes as MODULE_ROUTES


def handler(path, method="GET"):
    h = object.__new__(server.H)
    h.command, h.path, h.headers = method, path, {}
    h._send = mock.Mock()
    h._who = mock.Mock(return_value={"user": "viewer1", "role": "viewer", "remember": False, "started": 0})
    h.client_address = ("192.0.2.10", 40000)
    return h


class RequestScopeTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(SCOPE.unbind)

    def bound(self):
        return HOSTACCESS.role(), getattr(CHANGES.REQUEST, "user", None)

    def test_a_signed_in_request_leaves_no_role_or_user_bound(self):
        MODULE_ROUTES.forget("metrics"); self.addCleanup(MODULE_ROUTES.forget, "metrics")
        seen = []
        real = HOSTACCESS.set_role
        with mock.patch.object(server, "_self_data_boot_pending", False), \
             mock.patch.object(METRICS.SETTINGS_HELPERS, "get_app_settings", return_value={"metrics": {"enabled": True}}), \
             mock.patch.object(READYZ.KUBE, "raw_get", return_value="{}"), \
             mock.patch.object(HOSTACCESS, "set_role", side_effect=lambda role: seen.append(role) or real(role)):
            handler("/metrics").do_GET()
        self.assertIn("viewer", seen)   # the request did run as its caller
        self.assertEqual((None, None), self.bound())

    def test_each_method_unbinds_when_it_ends(self):
        for name in ("do_GET", "do_POST", "do_DELETE"):
            with self.subTest(name=name):
                h = handler("/api/nothing-here", name[3:])
                def bind_and_stop(*_):
                    HOSTACCESS.set_role("admin"); CHANGES.REQUEST.user = "ada"
                    raise RuntimeError("stop here")
                with mock.patch.object(server, "_self_data_boot_pending", True), \
                     mock.patch.object(server.H, "_begin", bind_and_stop):
                    with self.assertRaises(RuntimeError):
                        getattr(h, name)()
                self.assertEqual((None, None), self.bound())

    def test_unbind_clears_both(self):
        HOSTACCESS.set_role("operator"); CHANGES.REQUEST.user = "robin"
        SCOPE.unbind()
        self.assertEqual((None, None), self.bound())


if __name__ == "__main__":
    unittest.main()
