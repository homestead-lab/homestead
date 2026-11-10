"""Put back, after each test file, everything the loaded modules' bind() sets.

A test that binds a server module (bind(), or a name bind() sets) and does not put it
back changes every test file after it in the same process: a file passes alone and
fails in a share (#1073, #1092). tests/rebind.py's keep_bound puts back what one test
names; this does it for every file, whether or not its tests remember.

install() wraps unittest's TestSuite._handleModuleTearDown, which runs when the tests
of one file are done (after its tearDownModule and module cleanups) and before the
next file's setUpModule. There it puts back what the file left changed, then takes a
new snapshot for the next file. The same TestSuite runs the tests under
`python -m unittest discover -s tests` and `scripts/run_tests.py`, so both get it;
tests/test_zz_bound_state_clean.py installs it when it loads, before any test runs,
as it installs the recorder (tests/bound_guard.py).

What a snapshot holds, and how it is put back, is keep_bound's (tests/rebind.py):
for each loaded homestead_* module, bound_targets(module), read with get() and put
back with put(). Within a file nothing changes: a test still sees what the tests
before it in that file left, and the bound-state guard still fails a fake a test
leaves, so keep_bound stays required within a file. Not put back:
- a module first loaded during the file (there was nothing to keep);
- anything, when the file imported server for the first time: server binds every
  module then, and that wiring is what later files expect.
"""
import sys
import unittest

import rebind as REBIND

_installed = {}
_SNAPSHOT = "_bound_restore_snapshot"


def _modules():
    return {name: module for name, module in list(sys.modules.items())
            if name.startswith("homestead_") and module is not None}


def snapshot():
    modules = _modules()
    kept = {name: {target: REBIND.get(module, target) for target in REBIND.bound_targets(module)}
            for name, module in modules.items()}
    return {"server": "server" in sys.modules, "modules": modules, "kept": kept}


def restore(saved):
    """Put back what saved holds, unless server was first imported since it was taken."""
    if not saved["server"] and "server" in sys.modules:
        return
    for name, module in saved["modules"].items():
        for target, value in saved["kept"][name].items():
            REBIND.put(module, target, value)


def _handle_module_tear_down(self, result):
    try:
        return _installed["teardown"](self, result)
    finally:
        saved = getattr(result, _SNAPSHOT, None)
        if saved is not None:
            restore(saved)
        setattr(result, _SNAPSHOT, snapshot())


def installed():
    return bool(_installed)


def install():
    if not _installed:
        _installed["teardown"] = unittest.TestSuite._handleModuleTearDown
        unittest.TestSuite._handleModuleTearDown = _handle_module_tear_down


def uninstall():
    if _installed:
        unittest.TestSuite._handleModuleTearDown = _installed.pop("teardown")
