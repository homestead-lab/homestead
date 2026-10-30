"""Finds unbounded work in the server's source, for tests/test_capped_reads.py.

reads(source), caches(source) and joins(source) each give {key: count} for
one file; every(scan) runs one of them over every server file and keys the
counts by (file under server/, key). Standard library only; see the test's
docstring for what is counted and what is not seen.
"""
import ast
import functools

import source_scan as SS

# Calls whose result is an answer from the network.
OPENERS = {"urlopen", "getresponse", "_open"}


def _is_opener(node):
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
    if name in OPENERS:
        return True
    # opener.open(...), self.opener.open(...): urllib's OpenerDirector.
    return (name == "open" and isinstance(func, ast.Attribute)
            and "opener" in (SS.dotted(func.value) or "").lower())


def _is_http_error(node):
    names = node.elts if isinstance(node, ast.Tuple) else [node]
    return any((SS.dotted(n) or "").endswith("HTTPError") for n in names)


def _own_nodes(body):
    """Every node under the statements, not going into nested defs, classes
    or lambdas (they are scanned as their own functions)."""
    nested = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
    stack = [node for node in body if not isinstance(node, nested)]
    while stack:
        node = stack.pop()
        yield node
        stack += [child for child in ast.iter_child_nodes(node) if not isinstance(child, nested)]


def _scopes(source):
    """(qualified name, statements, parameter names) for the module ("<module>") and every def."""
    module = SS.tree(source)
    yield "<module>", module.body, set()
    found = []

    def visit(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = prefix + child.name
                if not isinstance(child, ast.ClassDef):
                    a = child.args
                    params = {p.arg for p in [*a.posonlyargs, *a.args, *a.kwonlyargs, a.vararg, a.kwarg] if p}
                    found.append((name, child.body, params))
                visit(child, name + ".")
            else:
                visit(child, prefix)

    visit(module, "")
    yield from found


@functools.lru_cache(maxsize=None)
def _walked(source):
    """_scopes() with each scope's own nodes listed, walked once per file for all three scans."""
    return [(scope, list(_own_nodes(body)), params) for scope, body, params in _scopes(source)]


def _answers(nodes):
    """Names bound to a network answer in one scope."""
    names = set()
    for node in nodes:
        if isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if _is_opener(item.context_expr) and isinstance(item.optional_vars, ast.Name):
                    names.add(item.optional_vars.id)
        elif isinstance(node, ast.Assign) and _is_opener(node.value):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.ExceptHandler) and node.name and node.type is not None \
                and _is_http_error(node.type):
            names.add(node.name)
    return names


def _no_size(call):
    """read()/readlines() with no size, or with -1 or None (which mean the same)."""
    if call.keywords or len(call.args) > 1:
        return False
    if not call.args:
        return True
    arg = call.args[0]
    if isinstance(arg, ast.UnaryOp) and isinstance(arg.op, ast.USub):
        arg = arg.operand
        return isinstance(arg, ast.Constant) and arg.value == 1
    return isinstance(arg, ast.Constant) and arg.value is None


def _json_loads_names(module):
    """Names json.load is bound to in a module: "json.load", plus `from json import load [as x]`."""
    names = {"json.load"}
    for node in ast.walk(module):
        if isinstance(node, ast.ImportFrom) and node.module == "json":
            names.update(a.asname or a.name for a in node.names if a.name == "load")
    return names


def reads(source):
    """{function: count} of answers read whole: .read()/.readlines() with no size,
    iterating the answer, "".join(answer), shutil.copyfileobj(answer, ...), json.load(answer)."""
    found = {}
    loaders = _json_loads_names(SS.tree(source))
    for scope, nodes, _params in _walked(source):
        answers = _answers(nodes)

        def answer(node):
            return _is_opener(node) or (isinstance(node, ast.Name) and node.id in answers)

        for node in nodes:
            if isinstance(node, (ast.For, ast.AsyncFor)):
                hit = answer(node.iter)
            elif isinstance(node, ast.comprehension):
                hit = answer(node.iter)
            elif isinstance(node, ast.Call):
                func, first = node.func, node.args[0] if node.args else None
                attr = func.attr if isinstance(func, ast.Attribute) else None
                hit = (attr in ("read", "readlines") and _no_size(node) and answer(func.value)) \
                    or (first is not None and answer(first) and (
                        attr == "join"
                        or (SS.dotted(func) or "").endswith("copyfileobj")
                        or SS.dotted(func) in loaders))
            else:
                hit = False
            if hit:
                found[scope] = found.get(scope, 0) + 1
    return found


def _grows(value, kinds=()):
    """An empty container that entries are added to: {}, [], dict(), list(), set(),
    OrderedDict(), defaultdict(...), or a dict subclass of the module's own (kinds)."""
    if isinstance(value, ast.Dict):
        return not value.keys
    if isinstance(value, (ast.List, ast.Set)):
        return not value.elts
    if isinstance(value, ast.Call):
        name = (SS.dotted(value.func) or "").rsplit(".", 1)[-1]
        return name in ("dict", "list", "set", "OrderedDict", "defaultdict", *kinds) and not value.keywords \
            and len(value.args) <= (1 if name == "defaultdict" else 0)
    return False


def _named_limit(node):
    """A *_MAX / *_LIMIT style constant (MAX or LIMIT in an upper-case name) in an expression."""
    for part in ast.walk(node):
        name = part.id if isinstance(part, ast.Name) else part.attr if isinstance(part, ast.Attribute) else ""
        if name.isupper() and ("MAX" in name or "LIMIT" in name):
            return True
    return False


def _evicts(statements, name):
    """name.pop/popitem/clear(...) or del name[...] among the statements."""
    for node in _own_nodes(statements):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr in ("pop", "popitem", "popleft", "clear") \
                and isinstance(node.func.value, ast.Name) and node.func.value.id == name:
            return True
        if isinstance(node, ast.Delete) and any(
                isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name) and t.value.id == name
                for t in node.targets):
            return True
    return False


def _bounded(source):
    """Module names with a real size bound: a comparison of len(name) with a
    *_MAX/*_LIMIT constant, or one that guards (if/while) an eviction from it;
    never inside a function whose parameter has that name."""
    bounded = set()
    for _scope, nodes, params in _walked(source):
        guards = {id(n.test): n for n in nodes if isinstance(n, (ast.If, ast.While))}
        for node in nodes:
            if not isinstance(node, ast.Compare):
                continue
            parts = [node.left, *node.comparators]
            for part in parts:
                if not (isinstance(part, ast.Call) and SS.dotted(part.func) == "len" and part.args
                        and isinstance(part.args[0], ast.Name)) or part.args[0].id in params:
                    continue
                name = part.args[0].id
                others = [p for p in parts if p is not part]
                guard = guards.get(id(node))
                if any(_named_limit(o) for o in others) or (guard is not None and _evicts(guard.body, name)):
                    bounded.add(name)
    return bounded


def _unbounded_memo(decorator):
    """functools.cache, or lru_cache(maxsize=None) / lru_cache(None)."""
    if not isinstance(decorator, ast.Call):
        return (SS.dotted(decorator) or "").rsplit(".", 1)[-1] == "cache"
    if (SS.dotted(decorator.func) or "").rsplit(".", 1)[-1] != "lru_cache":
        return False
    size = [k.value for k in decorator.keywords if k.arg == "maxsize"] + decorator.args[:1]
    return bool(size) and isinstance(size[0], ast.Constant) and size[0].value is None


def caches(source):
    """{name: 1} of module-level *cache* containers with no size bound (see
    _bounded), and {"function()": 1} for functions memoised with no maxsize."""
    module = SS.tree(source)
    memo = {f"{node.name}()": 1 for node in ast.walk(module)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and any(_unbounded_memo(d) for d in node.decorator_list)}
    # A class of this module built on dict (homestead_cache._Answers) grows like one.
    kinds = tuple(node.name for node in module.body if isinstance(node, ast.ClassDef)
                  and any((SS.dotted(b) or "").rsplit(".", 1)[-1] in ("dict", "OrderedDict", "defaultdict")
                          for b in node.bases))
    named = []
    for node in module.body:
        if isinstance(node, ast.Assign):
            pairs = []
            for target in node.targets:
                if isinstance(target, ast.Tuple) and isinstance(node.value, ast.Tuple):
                    pairs += zip(target.elts, node.value.elts)
                else:
                    pairs.append((target, node.value))
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            pairs = [(node.target, node.value)]
        else:
            continue
        named += [t.id for t, v in pairs
                  if isinstance(t, ast.Name) and "cache" in t.id.lower() and _grows(v, kinds)]
    sized = _bounded(source)
    return {**{name: 1 for name in named if name not in sized}, **memo}


def _fixed(args):
    """No timeout, or one written as a constant, a name or an arithmetic of
    them: not one deadline shared by the loop."""
    if not args:
        return True
    arg = args[0]
    return not any(isinstance(n, ast.Call) for n in ast.walk(arg)) and \
        not isinstance(arg, (ast.List, ast.Tuple, ast.ListComp, ast.GeneratorExp, ast.Set))


def joins(source):
    """{function: count} of loop_variable.join(<fixed timeout>) inside a for loop or comprehension."""
    found = {}
    for scope, nodes, _params in _walked(source):
        for node in nodes:
            if isinstance(node, (ast.For, ast.AsyncFor)):
                targets, inside = [node.target], node.body
            elif isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
                targets = [g.target for g in node.generators]
                inside = [node.elt] if not isinstance(node, ast.DictComp) else [node.key, node.value]
            else:
                continue
            loop = {n.id for t in targets for n in ast.walk(t) if isinstance(n, ast.Name)}
            for call in _own_nodes(inside):
                if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) \
                        and call.func.attr == "join" and isinstance(call.func.value, ast.Name) \
                        and call.func.value.id in loop \
                        and _fixed(call.args + [k.value for k in call.keywords]):
                    found[scope] = found.get(scope, 0) + 1
    return found


def server_files():
    """Every .py under server/ (server.py, homestead_*, probe/), not generated protobuf code."""
    return sorted(p for p in SS.SERVER.rglob("*.py") if not p.name.endswith("_pb2.py"))


@functools.lru_cache(maxsize=None)
def every(scan):
    """{(path under server/, key): count} for one scan over every server file
    (once per run; the files are parsed once for all three scans, by source_scan)."""
    found = {}
    for path in server_files():
        name = path.relative_to(SS.SERVER).as_posix()
        for key, count in scan(path).items():
            found[(name, key)] = count
    return found
