"""Finds unbounded work in the server's source, for tests/test_capped_reads.py.

reads(source), caches(source) and joins(source) each give {key: count} for
one file; every(scan) runs one of them over every server file and keys the
counts by (file under server/, key). Standard library only; see the test's
docstring for what is counted and what is not seen.
"""
import ast

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
    """(qualified name, statements) for the module ("<module>") and every def."""
    module = SS.tree(source)
    yield "<module>", module.body
    found = []

    def visit(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = prefix + child.name
                if not isinstance(child, ast.ClassDef):
                    found.append((name, child.body))
                visit(child, name + ".")
            else:
                visit(child, prefix)

    visit(module, "")
    yield from found


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


def reads(source):
    """{function: count} of answers read whole: x.read() with no size, or json.load(x)."""
    found = {}
    for scope, body in _scopes(source):
        nodes = list(_own_nodes(body))
        answers = _answers(nodes)

        def answer(node):
            return _is_opener(node) or (isinstance(node, ast.Name) and node.id in answers)

        for node in nodes:
            if not isinstance(node, ast.Call):
                continue
            whole = (isinstance(node.func, ast.Attribute) and node.func.attr == "read"
                     and not node.args and not node.keywords and answer(node.func.value))
            loaded = SS.dotted(node.func) == "json.load" and node.args and answer(node.args[0])
            if whole or loaded:
                found[scope] = found.get(scope, 0) + 1
    return found


def _grows(value):
    """An empty mapping that keys are added to: {}, dict(), OrderedDict(), defaultdict(...)."""
    if isinstance(value, ast.Dict):
        return not value.keys
    if isinstance(value, ast.Call):
        name = (SS.dotted(value.func) or "").rsplit(".", 1)[-1]
        return name in ("dict", "OrderedDict", "defaultdict") and not value.keywords \
            and len(value.args) <= (1 if name == "defaultdict" else 0)
    return False


def caches(source):
    """{name: 1} of module-level *cache* mappings with no len(name) in a comparison."""
    module = SS.tree(source)
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
                  if isinstance(t, ast.Name) and "cache" in t.id.lower() and _grows(v)]
    sized = set()
    for node in ast.walk(module):
        if isinstance(node, ast.Compare):
            for part in [node.left, *node.comparators]:
                if isinstance(part, ast.Call) and SS.dotted(part.func) == "len" and part.args \
                        and isinstance(part.args[0], ast.Name):
                    sized.add(part.args[0].id)
    return {name: 1 for name in named if name not in sized}


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
    for scope, body in _scopes(source):
        for node in _own_nodes(body):
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


def every(scan):
    """{(path under server/, key): count} for one scan over every server file."""
    found = {}
    for path in server_files():
        name = path.relative_to(SS.SERVER).as_posix()
        for key, count in scan(path).items():
            found[(name, key)] = count
    return found
