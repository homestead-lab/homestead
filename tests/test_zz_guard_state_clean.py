"""The last test file of a full run: no test has left guard state bound (#934).

Three things decide what a write is allowed to do and who it is signed as:
the request role (host_access.set_role), changes._author_key and changes.REQUEST.user (a thread
local). A test that binds one and does not restore it changes every test after
it, so a file passes alone and fails in the suite, or the other way round.
A test that sets them restores them itself (mock.patch, addCleanup); this
file says so when one does not. CI runs four shards (scripts/run_tests.py), so it
checks the files of its own shard only.

The files are loaded in name order and all of them are imported before any
test runs, so what is read here at import is the state a fresh run starts
from, and the tests then read it after every other file has run. Run alone it
passes. When it fails, find the test that binds the name with a probe: run the
suite in one process and compare the three after each test (a TextTestResult
whose stopTest does it names the first test that changes one)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_changes as CH
import homestead_host_access as HOSTACCESS

UNSET = object()
AT_IMPORT = {"role": getattr(HOSTACCESS._request, "role", None), "author key": CH._author_key, "request user": getattr(CH.REQUEST, "user", UNSET)}


class GuardState(unittest.TestCase):
    def test_no_test_left_the_guards_bound(self):
        now = {"role": getattr(HOSTACCESS._request, "role", None), "author key": CH._author_key, "request user": getattr(CH.REQUEST, "user", UNSET)}
        left = {name: now[name] for name in now if now[name] is not AT_IMPORT[name] and now[name] != AT_IMPORT[name]}
        if left.get("role", 1) is None:
            del left["role"]
        # A request through the server leaves the thread's user as None, which is as good as unset.
        if left.get("request user", UNSET) is None and AT_IMPORT["request user"] is UNSET:
            del left["request user"]
        self.assertEqual(left, {}, "a test bound these and did not put them back")


if __name__ == "__main__":
    unittest.main()
