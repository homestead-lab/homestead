"""The last test file of a full run: no test has left guard state bound (#934).

These bindings decide what a write is allowed to do, who it is signed as and
which cluster client the lifecycle code uses: the request role
(host_access.set_role), changes._author_key, changes.REQUEST.user (a thread
local), and what server.py gives homestead_lifecycle through its bind():
kget, ksend, SYS_NS, _cache, hardware_features and create_pvc
(a test that calls lifecycle.bind() restores the module with addCleanup). A test that binds one and does not restore it changes every test after
it, so a file passes alone and fails in the suite, or the other way round.
A test that sets them restores them itself (mock.patch, addCleanup); this
file says so when one does not. It sees only the tests of its own process, so
scripts/run_tests.py ends every share (and every --changed run) with it.

The files are loaded in name order and all of them are imported before any
test runs, so what is read here at import is the state a fresh run starts
from, and the tests then read it after every other file has run. Run alone it
passes. When it fails, find the test that binds the name with a probe: run the
suite in one process and compare them after each test (a TextTestResult
whose stopTest does it names the first test that changes one)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_changes as CH
import homestead_host_access as HOSTACCESS
import homestead_lifecycle as LIFECYCLE
import homestead_platform as PLATFORM
import server  # noqa: F401  (binds lifecycle, so the baseline is server's client even if no file imported it yet)

UNSET = object()
# Everything LIFECYCLE.bind sets, not only its client.
LIFECYCLE_BOUND = {"lifecycle reads": "kget", "lifecycle writes": "ksend", "lifecycle system namespaces": "SYS_NS",
                   "lifecycle cache": "_cache", "lifecycle hardware features": "hardware_features",
                   "lifecycle create pvc": "create_pvc"}


def bound_now():
    return {"role": getattr(HOSTACCESS._request, "role", None), "author key": CH._author_key, "request user": getattr(CH.REQUEST, "user", UNSET),
            "platform reads": PLATFORM.kget,
            **{label: getattr(LIFECYCLE, name) for label, name in LIFECYCLE_BOUND.items()}}


AT_IMPORT = bound_now()


class GuardState(unittest.TestCase):
    def test_no_test_left_the_guards_bound(self):
        now = bound_now()
        left = {name: now[name] for name in now if now[name] is not AT_IMPORT[name] and now[name] != AT_IMPORT[name]}
        if left.get("role", 1) is None:
            del left["role"]
        # A request through the server leaves the thread's user as None, which is as good as unset.
        if left.get("request user", UNSET) is None and AT_IMPORT["request user"] is UNSET:
            del left["request user"]
        self.assertEqual(left, {}, "a test bound these and did not put them back")

    def test_no_test_left_longhorn_bound_to_its_own_fake(self):
        # server.py binds homestead_longhorn once, when it is first imported. A
        # test that rebinds it and does not put it back makes every later test
        # that reads Longhorn through the server read its fake instead.
        server = sys.modules.get("server")
        if server is None:
            self.skipTest("no test in this shard imported server")
        # Everything LH.bind sets, homestead_csi_restore's binding included.
        LH = sys.modules["homestead_longhorn"]
        bound = {"kget": LH.kget is server.kget, "ksend": LH.ksend is server.ksend, "cache": LH._cache is server._cache,
                 "namespace": LH.DEFAULT_NS == server.DEFAULT_NS,
                 "csi kget": LH.CSI_RESTORE.kget is server.kget, "csi ksend": LH.CSI_RESTORE.ksend is server.ksend}
        self.assertEqual({name: True for name in bound}, bound,
                         "a test bound homestead_longhorn to its own fake and did not put the server's binding back")


if __name__ == "__main__":
    unittest.main()
