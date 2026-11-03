"""Which test left which module's bound names changed (tests/test_zz_bound_state_clean.py).

Many modules are wired by bind(): the cluster client, the data folder, callbacks,
an entry in another module's table. server.py binds each one once, at import. A
test that binds a module to its own fake and does not put it back changes every
test after it in the same process: a file passes alone and fails in a shard
(#1073, #1092).

install() wraps unittest.TestCase.run. Around each test it compares everything the
loaded modules' bind() sets (tests/rebind.py's bound_targets: their globals, and
the attributes and entries they set on other objects) before and after, and records
(name, test file) for each one the test left changed. It changes nothing: a test
still sees what the tests before it left, as without the guard. Most leaks leave a
fresh fake, which reads as a change whatever ran before, so a file's pairs are
nearly the same in any order; that is what lets bound_leaks.txt be checked both
ways (a pair not on it, and an entry whose file no longer leaks it).

A test that imports server for the first time rebinds every module itself; its
changes are not counted, and its file is noted as not checked.

Values compare by identity, immutable ones by equality: an equal but new mutable
value (a fresh dict or set a bind() builds) reads as a change. That is cautious on
purpose; such a name is either restored or on the list.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rebind as REBIND

LEAKS_FILE = Path(__file__).resolve().parent / "bound_leaks.txt"
IMMUTABLE = (str, int, float, bool, type(None), tuple, frozenset)

left = {}          # (name, test file) -> first test: a FAKE left there, credited to the file that made it
others = {}        # (name, test file) -> first test: any other change (a default, a server value), printed only
held = set()       # (name, test file): the name held some fake when one of that file's tests ended
watch = set()      # names to note in held (the listed fakes), set by the guard
ran = set()        # test files whose tests ran with the recorder
unchecked = set()  # test files where server was first imported, so nothing was counted
covered = set()    # modules whose names were watched
_installed = {}


_targets = {}


def state():
    """{"module.target": value} for every loaded homestead_* module with a bind()."""
    out = {}
    for name, module in list(sys.modules.items()):
        if name.startswith("homestead_") and module is not None:
            if name not in _targets:
                _targets[name] = REBIND.bound_targets(module)
            for target in _targets[name]:
                out[f"{name}.{target}"] = REBIND.get(module, target)
    return out


TESTS_DIR = Path(__file__).resolve().parent
_TEMP = __import__("tempfile").gettempdir().rstrip("/") + "/"


def _code_file(value):
    function = getattr(value, "__func__", value)
    if isinstance(function, __import__("functools").partial):
        function = function.func
    code = getattr(function, "__code__", None)
    if code is not None:
        return Path(code.co_filename)
    module = sys.modules.get(getattr(type(value), "__module__", ""), None)
    path = getattr(module, "__file__", None)
    return Path(path) if path else None


def fake_origin(value, running_file):
    """The test file to credit when value is a fake, else None: always the test
    file that was running when it was left, whichever file defines the helper.

    A fake: a unittest.mock object; a function, method or object whose code is
    under tests/; a path under the temp folder (a test's own data folder)."""
    from unittest import mock
    if isinstance(value, (mock.NonCallableMock, mock.Mock)) or type(value).__module__ == "unittest.mock":
        return running_file
    if isinstance(value, str):
        return running_file if value.startswith(_TEMP) else None
    if isinstance(value, (list, tuple)):
        return running_file if any(fake_origin(item, running_file) for item in value) else None
    if isinstance(value, IMMUTABLE + (dict, set)):
        return None
    path = _code_file(value)
    if path is None or TESTS_DIR not in path.resolve().parents:
        return None
    return running_file


def files_before(all_files, me):
    """The test files that run before this guard file (discover and run_tests both
    put the guards last): what "the whole suite" means when it checks."""
    return {name for name in all_files if name < me}


def full_run(ran_files, all_files):
    """Every test file ran in this process: the order the list was measured in."""
    return set(all_files) <= set(ran_files)


def same(before, now):
    return before is now or (isinstance(before, IMMUTABLE) and type(before) is type(now) and before == now)


def _run(self, result=None):
    test_id = unittest.TestCase.id(self)
    test_file = test_id.split(".", 1)[0]
    server_before = "server" in sys.modules
    before = state()
    try:
        return _installed["run"](self, result)
    finally:
        ran.add(test_file)
        if not server_before and "server" in sys.modules:
            unchecked.add(test_file)
        else:
            now = state()
            before_ids = {id(v) for v in before.values()}
            for name, value in before.items():
                covered.add(name.split(".", 1)[0])
                after = now.get(name, REBIND.MISSING)
                if not same(value, after):
                    origin = fake_origin(after, test_file)
                    # Enforced only when the test made the fake: an object no bound name held
                    # before it. One that was bound already (a restore that put back another
                    # file's leftover, a shared fake) depends on what ran before: reported.
                    if origin and id(after) not in before_ids:
                        left.setdefault((name, origin), test_id)
                    else:
                        others.setdefault((name, test_file), test_id)
                if name in watch and fake_origin(now.get(name), test_file):
                    held.add((name, test_file))


def install():
    if not _installed:
        _installed["run"] = unittest.TestCase.run
        unittest.TestCase.run = _run


def allowed(path=LEAKS_FILE):
    """{(name, test file, kind)} from bound_leaks.txt; kind is "fake" or "other"."""
    lines = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#"):
            fields = [field.strip() for field in line.split("\t")]
            lines.add((fields[0], fields[1], fields[2] if len(fields) > 2 else "other"))
    return lines


def new_leaks(found, listed):
    """Fakes a test left that the list does not hold: a new leak, or a listed name with another file's fake."""
    return sorted(pair for pair in found if pair not in listed)


def stale(found, listed, ran_files, unchecked_files, covered_modules, held_pairs=()):
    """Listed pairs whose file ran, checked, and no longer leaks the name: take them off
    the list. A name that held a fake when one of that file's tests ended still counts
    as leaked there (another file may have left the same shared fake first)."""
    return sorted((name, test_file) for name, test_file in listed
                  if test_file in ran_files and test_file not in unchecked_files
                  and name.split(".", 1)[0] in covered_modules and (name, test_file) not in found
                  and (name, test_file) not in held_pairs)


def unknown(listed, bound_now, loaded_modules):
    """Listed names of a module this run loaded that its bind() no longer sets."""
    return sorted({entry[0] for entry in listed
                   if entry[0].split(".", 1)[0] in loaded_modules and entry[0] not in bound_now})
