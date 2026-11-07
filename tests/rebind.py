"""Bind a module for one test, and put back afterwards everything its bind() sets.

    keep_bound(self, MODULE)               snapshot every name MODULE's bind() sets
    rebind(self, MODULE, *args, **kwargs)  keep_bound, then MODULE.bind(*args, **kwargs)

The names come from the AST of the module's bind() (and bind_routes()): the names
it declares global, the attributes it sets on another object of the module
(STATE.kget = ..., ALERTS.AGENTS = ...), and the entries it sets in a dict, also in a
loop over a fixed tuple, with .update(), under a module constant (RESOLVERS[KIND]),
with globals().update({n: ... for n in NEEDS}) or with globals().update(values) whose keys
bind_routes() checks against VALUES; a dict .update() fills from an argument, or one set
under a key only known when bind() runs, is kept whole ("X[*]"). An .update() that resets
a dict to constants (a runtime cache, homestead_uptime's _state) is put back too, since it
belongs to what bind() was given (DATA_DIR), but the bound-state guard does not report it
(bound_targets(module, resets=False)). A new one is kept without changing this file. They are put back by name with
addCleanup, not by calling bind() again: a bind() may skip an argument that is left
out, and the point is to restore exactly what was there before the test.
"""
import ast
import functools
import inspect
import textwrap


@functools.lru_cache(maxsize=None)
def _globals_of(function):
    tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
    return tuple(sorted({name for node in ast.walk(tree) if isinstance(node, ast.Global) for name in node.names}))


def bound_names(module):
    """The names module.bind() (and module.bind_routes()) declare global."""
    names = set()
    for fn in ("bind", "bind_routes"):
        function = getattr(module, fn, None)
        if inspect.isfunction(function) and function.__module__ == module.__name__:
            names.update(_globals_of(function))
    return tuple(sorted(names))


@functools.lru_cache(maxsize=None)
def _attributes_of(function):
    """What the function sets on a plain name's object, as found in its AST:
    "HOLDER.attr" for HOLDER.attr = ...; "X[k]" / "X.Y[k]" for an entry set under a
    literal key, under a loop variable that runs over a literal tuple (for key, value
    in (("kget", kget), ...): X[key] = value), or by X.update(k=..., **{"k": ...});
    "X[@NAME]" for one under a module constant NAME (resolved by bound_targets);
    "X[*]" for a dict that .update() fills, or X[...] = sets, with keys only known when it
    runs; and "@globals:NAME" for globals().update({n: ... for n in NAME}) and for
    globals().update(values) where the function compares values with NAME
    (if set(values) != set(VALUES): raise ...). An X.update() whose values are all
    constants (a reset of a runtime cache: _state.update(loaded=False, apps={})) names
    each entry as "%reset:X[k]"."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
    loops = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.For) and isinstance(node.iter, (ast.Tuple, ast.List)):
            names = node.target.elts if isinstance(node.target, ast.Tuple) else [node.target]
            for index, name in enumerate(names):
                if not isinstance(name, ast.Name):
                    continue
                values = []
                for element in node.iter.elts:
                    if isinstance(node.target, ast.Tuple):
                        element = element.elts[index] if isinstance(element, (ast.Tuple, ast.List)) \
                            and len(element.elts) > index else None
                    if not isinstance(element, ast.Constant):
                        values = None
                        break
                    values.append(element.value)
                if values is not None:
                    loops.setdefault(name.id, set()).update(values)
    # globals().update(values): the module name the function checks values' keys against,
    # as in "if set(values) != set(VALUES)".
    checked = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            called = {id(n.func) for n in ast.walk(node) if isinstance(n, ast.Call)}
            names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name) and id(n) not in called}
            for name in names:
                checked.setdefault(name, set()).update(names - {name})
    found = set()

    def constant(value):
        try:
            ast.literal_eval(value)
        except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
            return False
        return True

    def holder(base):
        if isinstance(base, ast.Name):
            return base.id
        if isinstance(base, ast.Attribute) and isinstance(base.value, ast.Name):
            return f"{base.value.id}.{base.attr}"
        return None

    for node in ast.walk(tree):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AugAssign) else []
        for target in targets:
            for t in (target.elts if isinstance(target, ast.Tuple) else [target]):
                if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name):
                    found.add(f"{t.value.id}.{t.attr}")
                # An element of a list or dict another module (or this one) keeps: X[0] = ..., X.Y[0] = ...
                if isinstance(t, ast.Subscript) and holder(t.value):
                    if isinstance(t.slice, ast.Constant):
                        found.add(f"{holder(t.value)}[{t.slice.value!r}]")
                    elif isinstance(t.slice, ast.Name) and t.slice.id in loops:
                        found.update(f"{holder(t.value)}[{key!r}]" for key in loops[t.slice.id])
                    elif isinstance(t.slice, ast.Name):
                        found.add(f"{holder(t.value)}[@{t.slice.id}]")
                    else:
                        # A key only known when bind() runs (X[name.upper()] = ...): the whole dict.
                        found.add(f"{holder(t.value)}[*]")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "update":
            base = node.func.value
            if isinstance(base, ast.Call) and isinstance(base.func, ast.Name) and base.func.id == "globals":
                for arg in node.args:
                    if isinstance(arg, ast.DictComp) and len(arg.generators) == 1 \
                            and isinstance(arg.generators[0].iter, ast.Name):
                        found.add(f"@globals:{arg.generators[0].iter.id}")
                    elif isinstance(arg, ast.Name):
                        found.update(f"@globals:{name}" for name in checked.get(arg.id, ()))
                found.update(keyword.arg for keyword in node.keywords if keyword.arg)
                continue
            name = holder(base)
            if name is None:
                continue
            # A reset to constants: runtime state that goes with what bind() is given.
            reset = "%reset:" if all(keyword.arg and constant(keyword.value) for keyword in node.keywords) \
                and all(isinstance(arg, ast.Dict) and None not in arg.keys and constant(arg)
                        for arg in node.args) else ""
            for keyword in node.keywords:
                if keyword.arg:
                    found.add(f"{reset}{name}[{keyword.arg!r}]")
                else:
                    found.add(f"{name}[*]")
            for arg in node.args:
                if isinstance(arg, ast.Dict) and all(isinstance(k, ast.Constant) for k in arg.keys):
                    found.update(f"{reset}{name}[{k.value!r}]" for k in arg.keys)
                else:
                    found.add(f"{name}[*]")
    return tuple(sorted(found))


def bound_targets(module, resets=True):
    """Everything module.bind() (and bind_routes()) sets: its globals, and HOLDER.attr
    for an object HOLDER of the module (a state module, another module's table). With
    resets=False (what the bound-state guard reports), without the entries an .update()
    only resets to constants (homestead_uptime's _state): keep_bound still puts them back."""
    names = set(bound_names(module))
    for fn in ("bind", "bind_routes"):
        function = getattr(module, fn, None)
        if inspect.isfunction(function) and function.__module__ == module.__name__:
            for name in _attributes_of(function):
                if name.startswith("%reset:"):
                    if not resets:
                        continue
                    name = name[len("%reset:"):]
                if name.startswith("@globals:"):
                    # globals().update({n: ... for n in NEEDS}): the globals NEEDS names.
                    listed = getattr(module, name.split(":", 1)[1], None)
                    if isinstance(listed, (tuple, list, frozenset)) and all(isinstance(n, str) for n in listed):
                        names.update(listed)
                    continue
                if "[@" in name:
                    # X[KIND] = ...: the key is the module's constant KIND; X[name] = ... with
                    # a key only known when bind() runs: the whole dict.
                    path, constant = name[:-1].split("[@", 1)
                    key = getattr(module, constant, None)
                    name = f"{path}[{key!r}]" if isinstance(key, (str, int)) else f"{path}[*]"
                head = name.split("[", 1)[0].split(".", 1)[0]
                if head in vars(module) and not inspect.isfunction(getattr(module, head)):
                    names.add(name)
    return tuple(sorted(names))


def expand(module, targets):
    """(target, value) for each of targets, with "X[*]" as one "X[k]" per key the dict
    holds now: what the bound-state guard compares."""
    for target in targets:
        if target.endswith("[*]"):
            owner = get(module, target)
            if owner is not MISSING:
                for key, value in owner.items():
                    if isinstance(key, (str, int)):
                        yield f"{target[:-3]}[{key!r}]", value
        else:
            yield target, get(module, target)


MISSING = object()
ALL = object()   # the key of "X[*]": the whole dict, every key it holds


def _container(module, name):
    """For "X[k]" / "X.Y[k]": (the list or dict, k); for "X[*]": (the dict, ALL); for a
    plain or dotted name: (holder, attribute)."""
    if name.endswith("[*]"):
        path = name[:-3]
        holder, _, attribute = path.rpartition(".")
        owner = getattr(module, holder, None) if holder else module
        return getattr(owner, attribute, None), ALL, True
    if name.endswith("]") and "[" in name:
        path, key = name[:-1].split("[", 1)
        holder, _, attribute = path.rpartition(".")
        owner = getattr(module, holder, None) if holder else module
        return getattr(owner, attribute, None), ast.literal_eval(key), True
    holder, _, attribute = name.rpartition(".")
    return (getattr(module, holder, None) if holder else module), attribute, False


def get(module, name):
    owner, key, item = _container(module, name)
    if owner is None:
        return MISSING
    if key is ALL:
        return dict(owner) if isinstance(owner, dict) else MISSING
    if item:
        try:
            return owner[key]
        except (LookupError, TypeError):
            return MISSING
    return getattr(owner, key, MISSING)


def put(module, name, value):
    owner, key, item = _container(module, name)
    if owner is None:
        return
    if key is ALL:
        # Put back exactly what the dict held: an entry the test added goes too.
        if isinstance(owner, dict) and value is not MISSING:
            owner.clear()
            owner.update(value)
        return
    if item:
        if value is MISSING:
            try:
                del owner[key]
            except (LookupError, TypeError):
                pass
        else:
            owner[key] = value
    elif value is MISSING:
        if hasattr(owner, key):
            delattr(owner, key)
    else:
        setattr(owner, key, value)


def keep(test, holder, names):
    """Put holder's names back as they are now, after the test."""
    missing = object()
    saved = {name: getattr(holder, name, missing) for name in names}

    def restore():
        for name, value in saved.items():
            if value is missing:
                if hasattr(holder, name):
                    delattr(holder, name)
            else:
                setattr(holder, name, value)
    # A class (setUpClass) puts them back when the class is done; a test, when it is.
    (test.addClassCleanup if isinstance(test, type) else test.addCleanup)(restore)


def keep_bound(test, *modules):
    for module in modules:
        saved = {name: get(module, name) for name in bound_targets(module)}
        def restore(module=module, saved=saved):
            for name, value in saved.items():
                put(module, name, value)
        (test.addClassCleanup if isinstance(test, type) else test.addCleanup)(restore)


def rebind(test, module, *args, **kwargs):
    keep_bound(test, module)
    return module.bind(*args, **kwargs)
