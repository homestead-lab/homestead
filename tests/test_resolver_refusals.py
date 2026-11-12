"""The guard on refusals that never end: a ratchet that may only shrink.

The jobs loop asks each running job's resolver how it is doing. Anything a
resolver raises but a 404 is taken as a status read that failed for now
(homestead_operations._resolver_failed): the job keeps running, and only
after its kind's limit (homestead_job_errors) does it fail, or, for a kind
in KEEPS_GUARDS, read "Needs attention" and keep its guards for good. So a
resolver that raises ValueError for a reason that cannot clear (the host
changed, the plan no longer fits) never ends its job: it waits out its limit
and then, for most kinds, waits for ever.

A refusal that should end the job says so: the resolver returns
("failed", progress, why), or raises a ValueError subclass it catches and
turns into that answer (homestead_lhv2_cleanup.Refusal). Counted: a bare
`raise ValueError` in the direct body of a registered resolver, outside a
try that catches it there. "Registered resolver": every function
homestead_operations.RESOLVERS holds once server is imported, unwrapped
(homestead_routes.drops_when_ended, a functools.partial, a bound method),
and for a lambda, each server function it calls (the lambda's own body).
Then one level more: the functions of the resolver's own module that it
calls in its direct body, outside a try that catches a ValueError there.
Not further: a helper's helpers, functions of other modules a resolver
calls (homestead_storage_guard, homestead_operations.checkpoint) and defs
nested in a resolver are not read; a call graph is too much for a lint.
A handler catches only when it does not raise the ValueError on (a bare
raise, raise of the caught name, or a new ValueError; a raise under
`if isinstance(error, OtherError)` does not count).

ALLOWED is the sites on dev when the guard came in, per (file under server/,
function), each with why its refusal clears by itself or what ends the job,
read in the code. A count may go down (and then has to be lowered here, or
the entry removed), never up. A lambda whose call cannot be followed (a
method of what another call returns) is on NOT_FOLLOWED with what it calls.

It is a lint, not a proof: a ValueError raised by a helper the resolver
calls, or by a library, is not seen, and a subclass is taken at its word.
"""
import ast
import functools
import inspect
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import source_scan as SS

sys.path.insert(0, str(SS.SERVER))

WHAT_TO_DO = (
    "A ValueError a resolver raises leaves its job running: past its kind's limit it fails, or for a kind "
    "in KEEPS_GUARDS reads \"Needs attention\" and keeps its guards for good. When the refusal cannot "
    "clear by itself, end the job: return (\"failed\", progress, \"<why>\"), or raise a ValueError "
    "subclass that the resolver catches and turns into that answer (homestead_lhv2_cleanup.Refusal). "
    "When it clears by itself (a read that will answer, a step still to come), add the site to ALLOWED "
    "in tests/test_resolver_refusals.py with what clears it.")

# (file under server/, function) -> bare ValueError raises in its direct
# body, when the guard came in, and why each refusal ends.
ALLOWED = {
    # A paged or partial pod list: the next round's read answers whole, and
    # the job's own 20-minute limit (at the top of the function) fails it.
    ("homestead_shares.py", "removal_progress"): 1,
    # Helpers a resolver calls (one level, same module):
    # An incomplete engine list, read whole next round; resolve()'s TIMEOUT (1 h) fails it.
    ("homestead_snapshot_delete.py", "resolve"): 1,
    # The V2 clean-up's start checks, asked again every round by progress(): a
    # setting read that did not answer clears; the others (Harvester manages
    # the memory, V2 volumes or block disks back, other maintenance running)
    # clear only when a person acts, and until then the job keeps its guards
    # and hosts cordoned. Kept as found; a finding, not fixed here.
    ("homestead_lhv2_cleanup.py", "inventory"): 5,
    # CDI's upload proxy and its CA, read once CDI reported the disks ready to
    # receive: found unless CDI was removed in between, which does not clear by
    # itself (the job then needs attention). Kept as found; a finding, not fixed here.
    ("homestead_unraid_vms.py", "_upload_url"): 1,
    ("homestead_unraid_vms.py", "_ca"): 1,
}

# kind -> what its lambda calls and why that ends: registered resolvers the
# scan cannot follow (a method of what another call returns, a callable
# object), read by hand.
NOT_FOLLOWED = {
    "cluster-shutdown": "cluster_shutdown().progress is homestead_cluster_shutdown.Shutdown.progress: it raises "
                        "no ValueError, and answers failed once the shutdown's deadline has passed",
}

CAUGHT = {"ValueError", "Exception", "BaseException"}


def _first_line(node):
    return min([node.lineno, *(d.lineno for d in getattr(node, "decorator_list", []))])


def _def_of(fn):
    """The def or lambda node of a function object, and its file."""
    path = Path(inspect.getsourcefile(fn)).resolve()
    line, name = fn.__code__.co_firstlineno, fn.__name__
    found = []
    for node in ast.walk(SS.tree(path)):
        if name == "<lambda>" and isinstance(node, ast.Lambda) and node.lineno == line:
            found.append(node)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name \
                and _first_line(node) == line:
            found.append(node)
    if not found:
        raise LookupError(f"no def of {fn.__qualname__} at {path.name}:{line}")
    # The outermost lambda on its line is the one registered.
    return min(found, key=lambda node: node.col_offset), path


def _is_server(fn, path=None):
    try:
        path = path or Path(inspect.getsourcefile(fn)).resolve()
    except TypeError:
        return False
    return path.parent == SS.SERVER


def _lookup(dotted, fn):
    """The object a dotted name in fn stands for, through its globals and
    closure; None when it cannot be read."""
    head, *rest = dotted.split(".")
    scope = {**fn.__globals__, **inspect.getclosurevars(fn).nonlocals}
    if head not in scope:
        return None
    value = scope[head]
    for part in rest:
        value = getattr(value, part, None)
        if value is None:
            return None
    return value


def _readable(fn):
    """The plain function behind a registered callable: unwrapped, a
    partial's function, a bound method's function. None when there is none
    (a callable object)."""
    fn = inspect.unwrap(fn)
    if isinstance(fn, functools.partial):
        fn = inspect.unwrap(fn.func)
    if inspect.ismethod(fn):
        fn = fn.__func__
    return fn if inspect.isfunction(fn) else None


def followed(fn):
    """The server functions a registered resolver stands for: itself,
    unwrapped, or for a lambda the server functions it calls. Second: what
    cannot be followed (a call on a call's result, a callable object)."""
    registered = fn
    fn = _readable(fn)
    if fn is None:
        return [], [f"a {type(registered).__name__} object, not a function"]
    if fn.__name__ != "<lambda>":
        return [fn], []
    node, _ = _def_of(fn)
    params = {a.arg for a in node.args.args}
    found, lost = [], []
    for call in ast.walk(node.body):
        if not isinstance(call, ast.Call):
            continue
        name = SS.dotted(call.func)
        if name is None:
            if isinstance(call.func, ast.Attribute) and isinstance(call.func.value, ast.Call):
                lost.append(ast.unparse(call.func))
            continue
        if name.split(".")[0] in params:
            continue  # item.get(...), item["ref"]: the job's own data
        target = _lookup(name, fn)
        target = inspect.unwrap(target) if inspect.isfunction(target) else None
        if target is not None and _is_server(target) and target not in found:
            found.append(target)
    return found, lost


def _only_other_errors(test, name):
    """True for an if test that holds only for an error other than a
    ValueError: isinstance(error, HTTPError), alone or and-ed with more."""
    parts = test.values if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.And) else [test]
    for part in parts:
        if (isinstance(part, ast.Call) and SS.dotted(part.func) == "isinstance" and len(part.args) == 2
                and isinstance(part.args[0], ast.Name) and part.args[0].id == name):
            kinds = part.args[1].elts if isinstance(part.args[1], ast.Tuple) else [part.args[1]]
            if not any(SS.dotted(k) in CAUGHT for k in kinds):
                return True
    return False


def _raises_on(body, name):
    """Does this handler code raise a ValueError on: a bare raise, a raise of
    the caught name, or a new ValueError? A raise under an if that holds
    only for another error (isinstance(error, HTTPError)) does not."""
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(node, ast.Raise):
            exc = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
            if exc is None or SS.dotted(exc) == "ValueError" or (isinstance(exc, ast.Name) and exc.id == name):
                return True
        if isinstance(node, ast.If) and name and _only_other_errors(node.test, name):
            if _raises_on(node.orelse, name):
                return True
            continue
        for field in ("body", "orelse", "finalbody", "handlers"):
            inner = getattr(node, field, None)
            if isinstance(inner, list) and _raises_on(
                    [s for i in inner for s in (i.body if isinstance(i, ast.ExceptHandler) else [i])], name):
                return True
        if isinstance(node, ast.Match) and any(_raises_on(case.body, name) for case in node.cases):
            return True
    return False


def _catches(handler):
    """A handler that ends a ValueError there: it names ValueError (or a
    base of it, or nothing) and does not raise it on."""
    types = [] if handler.type is None else (
        handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type])
    if handler.type is not None and not any(SS.dotted(t) in CAUGHT for t in types):
        return False
    return not _raises_on(handler.body, handler.name)


def _scan(body, caught=False):
    """(bare ValueError raises, calls) in a list of statements, outside a try
    that catches a ValueError there; nested defs and classes are not read."""
    count, calls = 0, []

    def expression_calls(node):
        return [n for n in ast.walk(node) if isinstance(n, ast.Call)]

    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(node, (ast.Try, getattr(ast, "TryStar", ast.Try))):
            inner = _scan(node.body, caught or any(_catches(h) for h in node.handlers))
            parts = [inner, *(_scan(h.body, caught) for h in node.handlers),
                     _scan(node.orelse, caught), _scan(node.finalbody, caught)]
        elif isinstance(node, ast.Match):
            parts = [(0, expression_calls(node.subject)), *(_scan(case.body, caught) for case in node.cases)]
        elif any(isinstance(getattr(node, f, None), list) and getattr(node, f) and isinstance(getattr(node, f)[0], ast.stmt)
                 for f in ("body", "orelse")):  # if, for, while, with: the header, then the bodies
            head = [value for f, value in ast.iter_fields(node) if f not in ("body", "orelse")]
            parts = [(0, [c for value in head for item in (value if isinstance(value, list) else [value])
                          if isinstance(item, ast.AST) for c in expression_calls(item)]),
                     _scan(getattr(node, "body", []), caught), _scan(getattr(node, "orelse", []), caught)]
        else:
            parts = [(0, expression_calls(node))]
            if isinstance(node, ast.Raise):
                exc = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
                parts.append((int(exc is not None and SS.dotted(exc) == "ValueError"), []))
        for raised, called in parts:
            if not caught:
                count += raised
                calls += called
    return count, calls


def _raises(body, caught=False):
    """Bare ValueError raises in a list of statements, not counting nested
    defs and classes, nor a raise under a try that catches it there."""
    return _scan(body, caught)[0]


def helpers(fn):
    """The functions of fn's own module that fn calls in its direct body,
    outside a try that catches a ValueError: one level, not further."""
    node, path = _def_of(fn)
    found = []
    for call in _scan(node.body if isinstance(node.body, list) else [ast.Expr(node.body)])[1]:
        name = SS.dotted(call.func)
        target = _lookup(name, fn) if name else None
        target = _readable(target) if callable(target) else None
        if (target is not None and target is not fn and target not in found
                and _is_server(target) and Path(inspect.getsourcefile(target)).resolve() == path):
            found.append(target)
    return found


def refusals(fn):
    """Bare ValueError raises in the direct body of one function object."""
    node, _ = _def_of(fn)
    return _raises(node.body)


def resolver_sites(resolvers):
    """{(file, function): raises} for every function the resolvers stand
    for, and {kind: calls in its lambda not followed}."""
    found, lost = {}, {}
    for kind in sorted(resolvers):
        targets, unfollowed = followed(resolvers[kind])
        if unfollowed:
            lost[kind] = sorted(unfollowed)
        for fn in targets:
            for each in [fn, *helpers(fn)]:
                node, path = _def_of(each)
                count = _raises(node.body) if isinstance(node.body, list) else 0
                if count:
                    found[(path.name, each.__qualname__)] = count
    return found, lost


class ScanFindsRefusals(unittest.TestCase):
    """The scan itself, on small functions."""

    def count(self, source):
        tree = SS.tree(SS.code(source))
        return _raises(tree.body[0].body)

    def test_a_bare_value_error_is_counted(self):
        self.assertEqual(2, self.count(
            "def status(item):\n"
            "    if not item:\n"
            "        raise ValueError('the plan changed')\n"
            "    raise ValueError\n"))

    def test_a_subclass_or_another_error_is_not_counted(self):
        self.assertEqual(0, self.count(
            "def status(item):\n"
            "    if item:\n"
            "        raise Refusal('the host changed')\n"
            "    raise RuntimeError('not configured')\n"))

    def test_a_raise_caught_in_the_resolver_is_not_counted(self):
        self.assertEqual(0, self.count(
            "def status(item):\n"
            "    try:\n"
            "        raise ValueError('x')\n"
            "    except (KeyError, ValueError) as error:\n"
            "        return 'failed', 0, str(error)\n"))

    def test_a_raise_in_a_handler_or_a_branch_is_counted(self):
        self.assertEqual(3, self.count(
            "def status(item):\n"
            "    try:\n"
            "        read()\n"
            "    except KeyError:\n"
            "        raise ValueError('gone')\n"
            "    for row in item:\n"
            "        with lock:\n"
            "            if row:\n"
            "                raise ValueError('a')\n"
            "            else:\n"
            "                raise ValueError('b')\n"))

    def test_a_refusal_raised_on_from_its_handler_is_counted(self):
        self.assertEqual(3, self.count(
            "def status(item):\n"
            "    try:\n"
            "        raise ValueError('a')\n"
            "    except ValueError:\n"
            "        log()\n"
            "        raise\n"
            "    try:\n"
            "        raise ValueError('b')\n"
            "    except (ValueError, KeyError) as error:\n"
            "        raise error\n"
            "    try:\n"
            "        raise ValueError('c')\n"
            "    except Exception as error:\n"
            "        if retry:\n"
            "            raise\n"
            "        return 'failed', 0, str(error)\n"))

    def test_a_raise_on_for_another_error_only_still_catches(self):
        self.assertEqual(0, self.count(
            "def status(item):\n"
            "    try:\n"
            "        raise ValueError('a')\n"
            "    except (ValueError, HTTPError) as error:\n"
            "        if isinstance(error, HTTPError) and error.code != 404:\n"
            "            raise\n"
            "        return 'failed', 0, str(error)\n"))

    def test_a_helper_of_the_same_module_is_read_one_level(self):
        import homestead_snapshot_delete as SNAP
        names = [fn.__name__ for fn in helpers(SNAP.resume_resolve)]
        self.assertEqual(["resolve"], names)
        import homestead_lhv2_cleanup as LHV2
        self.assertIn("inventory", [fn.__name__ for fn in helpers(LHV2.progress)])

    def test_a_partial_or_a_bound_method_is_followed_and_an_object_is_said(self):
        import homestead_shares as SHARES

        class Runner:
            def progress(self, item):
                return "running", 0, ""

        self.assertEqual([SHARES.removal_progress], followed(functools.partial(SHARES.removal_progress))[0])
        self.assertEqual(["progress"], [fn.__name__ for fn in followed(Runner().progress)[0]])

        class Callable:
            def __call__(self, item):
                return "running", 0, ""

        self.assertEqual(([], ["a Callable object, not a function"]), followed(Callable()))

    def test_a_nested_def_is_not_read(self):
        self.assertEqual(0, self.count(
            "def status(item):\n"
            "    def check():\n"
            "        raise ValueError('inner')\n"
            "    return check()\n"))

    def test_a_lambda_is_followed_one_level(self):
        import server  # noqa: F401  (fills the registries)
        import homestead_operations as OPS
        targets, lost = followed(OPS.RESOLVERS["vm-power"])
        self.assertEqual(["status"], [fn.__name__ for fn in targets])
        self.assertEqual([], lost)

    def test_a_wrapped_resolver_is_unwrapped(self):
        import server  # noqa: F401  (fills the registries)
        import homestead_operations as OPS
        targets, _ = followed(OPS.RESOLVERS["disk-v2-convert"])
        self.assertEqual([("homestead_disk_v2", "progress")], [(fn.__module__, fn.__name__) for fn in targets])


class ResolverRefusalRatchet(unittest.TestCase):
    """Every registered resolver, against ALLOWED."""

    @classmethod
    def setUpClass(cls):
        import server  # noqa: F401  (fills the registries)
        import homestead_operations as OPS
        cls.resolvers = dict(OPS.RESOLVERS)
        cls.found, cls.lost = resolver_sites(cls.resolvers)

    def test_there_are_resolvers(self):
        self.assertGreater(len(self.resolvers), 50)

    def test_no_new_refusal_that_never_ends(self):
        grown = {site: (count, ALLOWED.get(site, 0)) for site, count in self.found.items()
                 if count > ALLOWED.get(site, 0)}
        self.assertEqual({}, grown, "(file, resolver): (bare ValueError raises, allowed). " + WHAT_TO_DO)

    def test_allowed_only_shrinks(self):
        stale = {site: (self.found.get(site, 0), allowed) for site, allowed in ALLOWED.items()
                 if self.found.get(site, 0) < allowed}
        self.assertEqual({}, stale,
                         "(file, resolver): (found, allowed). Fewer than allowed: lower the count in ALLOWED "
                         "in tests/test_resolver_refusals.py, or remove the entry, so it cannot grow back.")

    def test_every_lambda_is_followed(self):
        self.assertEqual(sorted(NOT_FOLLOWED), sorted(self.lost), f"not followed: {self.lost}. "
                         "A registered lambda calls a method of what another call returns, which this guard "
                         "cannot read. Register a named function (or a lambda calling one), or add the lambda "
                         "to NOT_FOLLOWED in tests/test_resolver_refusals.py with what it calls; remove an "
                         "entry that is gone.")


if __name__ == "__main__":
    unittest.main()
