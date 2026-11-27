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

Two caches no bind() sets are emptied after every file too, server or not (CACHES): the
account store as last read (homestead_auth._store_cache) and the change-author key and
names copied from it (homestead_changes._known). A copy one file leaves answers the next
file's reads for up to 10 seconds, and a signing key in it marks every Deployment write the
next file makes with the wall-clock second (test_revert_fenced failed on CI that way, 23 Nov).
The file that left one set is recorded in caches_left (its name and the caches), so
tests/test_zz_bound_state_clean.py can name it; cache_files holds every file checked.
"""
import sys
import unittest

import rebind as REBIND

_installed = {}
_SNAPSHOT = "_bound_restore_snapshot"


def _store_set(cache):
    return cache.get("data") is not None


def _store_empty(cache):
    cache.clear()
    cache.update(at=0, data=None)


# "module.name": (whether a file left it set, how to empty it).
CACHES = {"homestead_auth._store_cache": (_store_set, _store_empty),
          "homestead_changes._known": (bool, dict.clear)}
caches_left = {}    # test file -> the CACHES names it left set
cache_files = set()  # test files whose caches were checked


def empty_caches(test_file):
    """Empty the CACHES the file left set, and record it in caches_left by name."""
    cache_files.add(test_file)
    for name, (is_set, empty) in CACHES.items():
        module, attribute = name.rsplit(".", 1)
        cache = getattr(sys.modules.get(module), attribute, None)
        if cache is not None and is_set(cache):
            caches_left.setdefault(test_file, set()).add(name)
            empty(cache)


def new_cache_leaks(left, listed):
    """"file: cache, ..." for each file that left a cache set and is not listed."""
    return [f"{test_file}: {', '.join(sorted(names))}" for test_file, names in sorted(left.items())
            if test_file not in listed]


def stale_cache_leaks(left, checked, listed, pending=frozenset()):
    """Listed files that were checked and left no cache set: take them off the list.
    A file that is not in this tree, or did not run here, was not checked, so it is never
    stale (one an open PR of ours adds). A pending file (one an open PR of ours fixes) is
    not stale while it is pending."""
    return sorted(test_file for test_file in listed
                  if test_file in checked and test_file not in left and test_file not in pending)


def cache_ceiling_faults(listed, pending, ceiling, pending_ceiling):
    """What breaks the list's ceilings. Every listed file counts against ceiling, whether
    its file is in this tree yet or not: one an open PR of ours adds is listed before it
    arrives, so a merge in any order changes no count. The entries that come with an open
    PR (pending: fixed by one, or added by one) count again against pending_ceiling. Each
    count must match its ceiling (lower it as the list shrinks), and a pending file must
    be listed."""
    faults = []
    for what, count, cap in (("CACHE_LEAKS", len(listed), ceiling), ("pending entries", len(pending), pending_ceiling)):
        if count > cap:
            faults.append(f"{what} has {count}, ceiling {cap}: it may only shrink, fix the leak instead")
        elif count < cap:
            faults.append(f"{what} has {count}, ceiling {cap}: lower the ceiling to match")
    faults += [f"{test_file} is pending but not on CACHE_LEAKS" for test_file in sorted(set(pending) - set(listed))]
    return faults


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
        previous = self._get_previous_module(result)
        if previous is not None:
            empty_caches(previous)
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
