"""Bind a module for one test, and put back afterwards everything its bind() sets.

    keep_bound(self, MODULE)               snapshot every name MODULE's bind() sets
    rebind(self, MODULE, *args, **kwargs)  keep_bound, then MODULE.bind(*args, **kwargs)

The names come from the AST of the module's bind() (and bind_routes()): the names
it declares global, and the attributes it sets on another object of the module
(STATE.kget = ..., ALERTS.AGENTS = ...). A new one is kept without changing this file. They are put back by name with
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
    """"HOLDER.attr" for each assignment HOLDER.attr = ... in the function (HOLDER a plain name)."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
    found = set()
    for node in ast.walk(tree):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AugAssign) else []
        for target in targets:
            for t in (target.elts if isinstance(target, ast.Tuple) else [target]):
                if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name):
                    found.add(f"{t.value.id}.{t.attr}")
                # An element of a list or dict another module (or this one) keeps: X[0] = ..., X.Y[0] = ...
                if isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant):
                    base = t.value
                    if isinstance(base, ast.Name):
                        found.add(f"{base.id}[{t.slice.value!r}]")
                    elif isinstance(base, ast.Attribute) and isinstance(base.value, ast.Name):
                        found.add(f"{base.value.id}.{base.attr}[{t.slice.value!r}]")
    return tuple(sorted(found))


def bound_targets(module):
    """Everything module.bind() (and bind_routes()) sets: its globals, and HOLDER.attr
    for an object HOLDER of the module (a state module, another module's table)."""
    names = set(bound_names(module))
    for fn in ("bind", "bind_routes"):
        function = getattr(module, fn, None)
        if inspect.isfunction(function) and function.__module__ == module.__name__:
            for name in _attributes_of(function):
                head = name.split("[", 1)[0].split(".", 1)[0]
                if head in vars(module) and not inspect.isfunction(getattr(module, head)):
                    names.add(name)
    return tuple(sorted(names))


MISSING = object()


def _container(module, name):
    """For "X[k]" / "X.Y[k]": (the list or dict, k); for a plain or dotted name: (holder, attribute)."""
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
