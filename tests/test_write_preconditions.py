"""The guard on writes that can overwrite someone else's change: a ratchet
that may only shrink.

A PUT replaces a whole object, and a merge patch sets fields without asking
what they hold now. Sent without a precondition, either one lands over a
change made since Homestead read the object (by a person, a controller or
another job), and nothing says so. Fixes since October gave writes a
precondition one module at a time; nothing asked a new write for one.

Counted, per file in server/: a call whose method is "PUT" (a literal, a
module constant, either branch of "PUT" if x else "POST", or a local name
given one), or "PATCH" with a merge-patch or strategic-merge-patch content
type, unless the code that builds THAT write carries a precondition:
- the call's own arguments, or what the function assigns to (or into, or
  calls .update on) the names they use, two names deep, before the write,
  hold a resourceVersion key, a preconditions key, a uid key under
  metadata or preconditions, a JSON patch "test" op, or a name or keyword
  argument such as resource_version or expected_uid; or
- it is a PUT of the object as read: its body, as the write sees it (the
  last assignment on its path), is a read's answer (kget, get_optional,
  KUBE.get, a read or *_read callable) or a copy that keeps its metadata
  (dict(obj, ...), {**obj}, copy.deepcopy(obj)). A dict's .get is not a read.
Only code counts: text in strings, comments and docstrings never does, and
neither does a word elsewhere in the function (a log line, a check).

COUNTS is the sites on dev when the guard came in, per file. A count may go
down (and then has to be lowered here, or the entry removed). It goes up only
for a new write that truly needs no precondition (any first argument "PUT"
counts, also a PUT that is not to Kubernetes, such as an object-store bucket
create): raise that file's entry by one, with a comment on the line saying
why, and the reviewer decides. A file that is not listed has none. Strategic merge patches of labels or
annotations count the same: no special case. A failure names each site in
the file by line and function.

It is a lint, not a proof. Counted though fenced: a write of an object read
in another function or by the caller (put_past_status, commit_prepared), or
fenced by a key list in strings (kubevirt_cr.update's identity), so "no
precondition seen" is not always "none". Passing though unfenced: a read
object whose resourceVersion the function then drops, and a uid key under
metadata that is not compared. Not seen: a content type named through
another module (KUBEVIRT_CR.MERGE), a method built at run time.
"""
import ast
import collections
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import source_scan as SS

WHAT_TO_DO = (
    "A PUT or a merge patch without a precondition overwrites any change made since the object was read. "
    "PUT the object as you read it in the same function (it carries the resourceVersion Kubernetes compares), "
    "or name the resourceVersion or uid you send, or use a JSON patch with a \"test\" op, or write through "
    "homestead_kube.update_json, which does. "
    "When a write is fixed, lower its file's count in COUNTS in tests/test_write_preconditions.py. "
    "When a new write truly needs no precondition (a PUT that is not to Kubernetes, such as an object-store "
    "bucket create, or a create that cannot collide), or carries one this guard cannot see (the object was "
    "read in another function), raise that file's entry in COUNTS in "
    "tests/test_write_preconditions.py by one, with a comment on the line saying why.")

# file under server/ -> writes in it with no precondition seen, when the guard came in.
COUNTS = {
    "homestead_addons.py": 1,
    "homestead_app_copies.py": 1,
    "homestead_app_hold.py": 1,
    "homestead_changes.py": 1,
    "homestead_cluster_server.py": 1,
    "homestead_cluster_shutdown.py": 7,
    "homestead_components.py": 1,
    "homestead_config_backup.py": 1,
    "homestead_copy_move.py": 1,
    "homestead_data_copies.py": 2,
    "homestead_deploy.py": 1,
    "homestead_deploy_server.py": 1,
    "homestead_disks.py": 2,
    "homestead_env_secrets.py": 2,   # prune() PUTs the Secret stage() handed it, resourceVersion and all: not seen here
    "homestead_failover.py": 1,
    "homestead_files.py": 1,
    "homestead_firewall.py": 1,
    "homestead_fleet.py": 1,
    "homestead_fs_freeze.py": 1,
    "homestead_gitops.py": 1,
    "homestead_group_tiers.py": 2,
    "homestead_hardware.py": 1,
    "homestead_helm.py": 2,
    "homestead_host_move.py": 1,
    "homestead_image_cache.py": 1,
    "homestead_ipam.py": 1,
    "homestead_isos.py": 3,
    "homestead_kube.py": 2,
    "homestead_kubevirt_cr.py": 1,
    "homestead_lhcapacity.py": 4,
    "homestead_lhrebuild.py": 3,
    "homestead_longhorn.py": 1,
    "homestead_longhorn_target.py": 1,
    "homestead_longhorn_volumes.py": 2,
    "homestead_move.py": 1,
    "homestead_move_phases.py": 2,
    "homestead_move_source.py": 4,
    "homestead_mqtt.py": 1,
    "homestead_network_services.py": 1,
    "homestead_node_actions.py": 1,
    "homestead_node_parity.py": 1,
    "homestead_nodes.py": 2,
    "homestead_nodeshell.py": 2,
    "homestead_objectstore.py": 3,
    "homestead_onboard.py": 6,
    "homestead_outage.py": 1,
    "homestead_outage_secret.py": 1,
    "homestead_overview_server.py": 6,
    "homestead_passthrough.py": 2,
    "homestead_power_handoff.py": 1,
    "homestead_power_hold.py": 6,
    "homestead_power_jobs.py": 1,
    "homestead_priority.py": 3,
    "homestead_probe.py": 3,
    "homestead_rebalance.py": 1,
    "homestead_reclass.py": 11,
    "homestead_reclass_handoff.py": 1,
    "homestead_requests.py": 1,
    "homestead_resources.py": 1,
    "homestead_restore_swap.py": 6,
    "homestead_restore_test.py": 1,
    "homestead_revert.py": 1,
    "homestead_root_guard.py": 1,
    "homestead_scheduled_jobs.py": 1,
    "homestead_self.py": 4,
    "homestead_self_data_anchor.py": 1,
    "homestead_self_data_server.py": 1,
    "homestead_share_servers.py": 10,
    "homestead_shares.py": 5,
    "homestead_storage.py": 2,
    "homestead_storage_classes.py": 2,
    "homestead_update_checks.py": 1,
    "homestead_update_rollouts.py": 1,
    "homestead_updates.py": 1,
    "homestead_uplinks.py": 1,
    "homestead_vips.py": 1,
    "homestead_vm_edit.py": 4,
    "homestead_vm_profiles.py": 1,
    "homestead_vm_server.py": 1,
    "homestead_vms.py": 6,
    "homestead_volumes.py": 1,
    "homestead_workload_edit.py": 2,
    "homestead_workloads.py": 4,
}

MERGE = ("merge-patch", "strategic-merge-patch")
FENCE = re.compile(r"resource_?version|(?:^|_)uid(?:$|_)|^preconditions?$|^generation$", re.I)
READ_NAMES = {"kget", "get_optional", "read", "get_object"}
Site = collections.namedtuple("Site", "function line")


def _strings(source):
    found = {}
    for node in SS.tree(source).body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    found[target.id] = node.value.value
    return found


def _text(node, constants):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    return None


def _methods(node, constants, own):
    """The methods a call's first argument can be: a literal, a module
    constant, either branch of a conditional ("PUT" if x else "POST"), or a
    local name given one of those in the function."""
    if isinstance(node, ast.IfExp):
        return _methods(node.body, constants, own) | _methods(node.orelse, constants, own)
    text = _text(node, constants)
    if text is not None:
        return {text}
    if isinstance(node, ast.Name) and own is not None:
        found = set()
        for child in ast.walk(own):
            if isinstance(child, ast.Assign) and any(isinstance(t, ast.Name) and t.id == node.id for t in child.targets):
                found |= _methods(child.value, constants, None)
        return found
    return set()


def _is_write(call, constants, own=None):
    methods = _methods(call.args[0], constants, own) if call.args else set()
    if "PUT" in methods:
        return True
    if "PATCH" in methods:
        rest = [*call.args[1:], *(k.value for k in call.keywords)]
        return any(any(kind in (_text(arg, constants) or "") for kind in MERGE) for arg in rest)
    return False


def _key(node):
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _chain(node):
    """The keys and attributes along x["metadata"]["resourceVersion"] or
    x.metadata.uid, outermost first, and the name at its root."""
    keys = []
    while isinstance(node, (ast.Subscript, ast.Attribute)):
        keys.append(_key(node.slice) if isinstance(node, ast.Subscript) else node.attr)
        node = node.value
    return (node.id if isinstance(node, ast.Name) else None), [k for k in reversed(keys) if k]


def _fences(keys):
    """A key path that is a precondition: resourceVersion anywhere, or uid
    under metadata or preconditions."""
    for i, key in enumerate(keys):
        if re.fullmatch(r"resourceVersion|preconditions", key):
            return True
        if key == "uid" and i and keys[i - 1] in ("metadata", "preconditions"):
            return True
    return False


def _fenced(node):
    """Does this code carry a precondition, in its structure (keys, names,
    keyword arguments), never in the text of a string: a resourceVersion
    key, a uid key under metadata or preconditions, a preconditions key, a
    JSON patch test op, a keyword argument such as expected_uid or
    precondition, a name such as resource_version, or a key path read into
    it. A name such as owner_uid alone is not a precondition."""
    for child in ast.walk(node):
        if isinstance(child, ast.Dict):
            keys = [_key(k) for k in child.keys]
            if any(k in ("resourceVersion", "preconditions") for k in keys):
                return True
            if "op" in keys and _key(child.values[keys.index("op")]) == "test":
                return True
            for key, value in zip(keys, child.values):
                if key in ("metadata", "preconditions") and isinstance(value, ast.Dict) \
                        and "uid" in [_key(k) for k in value.keys]:
                    return True
        elif isinstance(child, ast.keyword) and child.arg and FENCE.search(child.arg):
            return True   # write(..., expected_uid=uid), precondition=...
        elif isinstance(child, ast.Name) and re.search(r"resource_?version", child.id, re.I):
            return True   # a resource_version read before
        elif isinstance(child, (ast.Subscript, ast.Attribute)) and _fences(_chain(child)[1]):
            return True
    return False


def _building(own, names, before, depth=2):
    """The code in a function that builds the named values before a line:
    what is assigned to them, assigned into them (x["metadata"][...] = v),
    and calls on them (x.update(...)), and so on for the names that code
    uses, depth levels deep."""
    found, seen = [], set()
    for _ in range(depth):
        wanted = names - seen
        if not wanted:
            break
        seen |= wanted
        more = set()
        for node in ast.walk(own):
            if getattr(node, "lineno", before) >= before:
                continue
            parts = []
            if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    root, keys = _chain(target)
                    root = target.id if isinstance(target, ast.Name) else root
                    if root in wanted:
                        parts.append(node.value)
                        if keys:
                            parts.append(target)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and _chain(node.func.value)[0] in wanted:
                parts.append(node)
            for part in parts:
                if part is None:
                    continue
                found.append(part)
                more |= {n.id for n in ast.walk(part) if isinstance(n, ast.Name)}
        names = names | more
    return found


def _read_call(node, kube):
    if not isinstance(node, ast.Call):
        return False
    if isinstance(node.func, ast.BoolOp):  # (read or kget)(path)
        return all(_read_call(ast.Call(func=v, args=[], keywords=[]), kube) for v in node.func.values)
    name = SS.dotted(node.func) or ""
    last = name.rsplit(".", 1)[-1]
    return (last in READ_NAMES or last.endswith("_read") or last.endswith("_kget")
            or (last == "get" and name.rsplit(".", 1)[0] in kube))


def _assignments(own, name, before):
    return [node for node in ast.walk(own)
            if isinstance(node, ast.Assign) and node.lineno < before
            and any(isinstance(t, ast.Name) and t.id == name for t in node.targets)]


def _as_read(node, own, before, kube, depth=3):
    """Is this value the object as read, whole: a read's answer, a name last
    given one, or a copy of one that keeps its metadata (dict(obj, ...),
    {**obj, ...}, copy.deepcopy(obj))? A dict's .get is not a read."""
    if depth == 0 or node is None:
        return False
    if _read_call(node, kube):
        return True
    if isinstance(node, ast.Name):
        # Every value it is given before the write is a read (or None, for
        # "not there yet"): a name also given a fresh dict is not the object as read.
        given = [a for a in _assignments(own, node.id, before)
                 if not (isinstance(a.value, ast.Constant) and a.value.value is None)]
        return bool(given) and all(_as_read(a.value, own, a.lineno, kube, depth - 1) for a in given)
    if isinstance(node, ast.Call) and node.args and (SS.dotted(node.func) or "") in (
            "dict", "copy.deepcopy", "copy.copy", "deepcopy"):
        return _as_read(node.args[0], own, before, kube, depth - 1)
    if isinstance(node, ast.Dict) and None in node.keys and "metadata" not in [_key(k) for k in node.keys]:
        return any(_as_read(v, own, before, kube, depth - 1) for k, v in zip(node.keys, node.values) if k is None)
    return False


def _reaching(own, call, name):
    """The assignment to name that the write sees: the last one before it in
    the write's own block or a block around it (an assignment in an if or a
    try beside it is another path)."""
    parents = {child: node for node in ast.walk(own) for child in ast.iter_child_nodes(node)}
    blocks, node = [], call
    while node in parents:
        parent = parents[node]
        for field in ("body", "orelse", "finalbody"):
            block = getattr(parent, field, None)
            if isinstance(block, list) and node in block:
                blocks.append(block)
        node = parent
    def lifted(stmt):
        # An assignment in the body of a try or a with runs on the way in:
        # it stands for that try or with statement.
        while stmt in parents and (isinstance(parents[stmt], (ast.With, ast.AsyncWith))
                                   or (isinstance(parents[stmt], ast.Try) and stmt in parents[stmt].body)):
            stmt = parents[stmt]
        return stmt

    ids = {id(stmt) for block in blocks for stmt in block}
    found = [node for node in ast.walk(own)
             if isinstance(node, ast.Assign) and node.lineno < call.lineno
             and any(isinstance(t, ast.Name) and t.id == name for t in node.targets)
             and id(lifted(node)) in ids]
    return max(found, key=lambda stmt: stmt.lineno) if found else None


def _put_of_a_read(call, own, kube):
    """A PUT whose body is the object as read (see _as_read): it carries the
    resourceVersion Kubernetes compares."""
    if len(call.args) < 3:
        return False
    body = call.args[2]
    if isinstance(body, ast.Name):
        given = _reaching(own, call, body.id)
        return given is not None and _as_read(given.value, own, given.lineno, kube)
    return _as_read(body, own, call.lineno, kube)


def _guarded(call, own, kube):
    if _put_of_a_read(call, own, kube):
        return True
    args = [*call.args[1:], *call.keywords]
    if any(_fenced(arg) for arg in args):
        return True
    names = {n.id for arg in args for n in ast.walk(arg) if isinstance(n, ast.Name)}
    return any(_fenced(part) for part in _building(own, names, call.lineno))


def sites(source):
    """Each write in a file that carries no precondition: (function, line)."""
    constants = _strings(source)
    kube = SS.import_aliases(source, "homestead_kube") | {"KUBE"}
    tree = SS.tree(source)   # once: code("...") is parsed again on every call
    every = []

    def visit(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if not isinstance(child, ast.ClassDef):
                    every.append((prefix + child.name, child))
                visit(child, prefix + child.name + ".")
            else:
                visit(child, prefix)

    visit(tree, "")
    found = []
    for call in ast.walk(tree):
        if not isinstance(call, ast.Call) or not call.args:
            continue
        around = [(name, d) for name, d in every if d.lineno <= call.lineno <= d.end_lineno]
        name, own = max(around, key=lambda pair: pair[1].lineno) if around else ("<module>", None)
        if not _is_write(call, constants, own):
            continue
        if own is None or not _guarded(call, own, kube):
            found.append(Site(name, call.lineno))
    return sorted(found, key=lambda site: site.line)


def unfenced(source):
    """How many writes in a file carry no precondition."""
    return len(sites(source))


def server_sites():
    return {path.name: found for path in SS.server_files() for found in [sites(path)] if found}


def server_counts():
    return {name: len(found) for name, found in server_sites().items()}


class ScanFindsWrites(unittest.TestCase):
    """The scan itself, on small pieces of code."""

    def count(self, source):
        return unfenced(SS.code(source))

    def test_a_put_or_a_merge_patch_without_a_precondition_is_counted(self):
        self.assertEqual(3, self.count(
            "MERGE = 'application/merge-patch+json'\n"
            "def a(path, body):\n"
            "    ksend('PUT', path, body)\n"
            "def b(path):\n"
            "    KUBE.ksend('PATCH', path, {'spec': {}}, 'application/strategic-merge-patch+json')\n"
            "def c(path):\n"
            "    send('PATCH', path, {'spec': {}}, ctype=MERGE)\n"))

    def test_a_method_chosen_by_a_condition_or_a_name_is_seen(self):
        self.assertEqual(2, self.count(
            "def a(path, current, body):\n"
            "    ksend('PUT' if current else 'POST', path, body)\n"
            "def b(path, body):\n"
            "    method = 'PUT'\n"
            "    ksend(method, path, body)\n"))

    def test_the_fence_in_the_code_that_builds_the_write_passes(self):
        self.assertEqual(0, self.count(
            "def a(path, obj):\n"
            "    ksend('PUT', path, {'metadata': {'name': 'x', 'resourceVersion': obj['metadata']['resourceVersion']}})\n"
            "def b(path, current, body):\n"
            "    body['metadata']['resourceVersion'] = current['metadata']['resourceVersion']\n"
            "    ksend('PUT' if current else 'POST', path, body)\n"
            "def c(path, uid):\n"
            "    ksend('PATCH', path, {'metadata': {'uid': uid, 'labels': {}}}, 'application/merge-patch+json')\n"
            "def d(path, rv):\n"
            "    meta = {'name': 'x'}\n"
            "    meta['resourceVersion'] = rv\n"
            "    body = {'metadata': meta}\n"
            "    ksend('PUT', path, body)\n"
            "def e(path, expected_uid):\n"
            "    write('PATCH', path, {}, ctype='application/merge-patch+json', expected_uid=expected_uid)\n"))

    def test_a_precondition_word_outside_the_write_does_not_pass(self):
        self.assertEqual(4, self.count(
            "def a(path, obj):\n"
            "    \"\"\"Label the pod by its uid.\"\"\"\n"
            "    ksend('PATCH', path, {'metadata': {'labels': {}}}, 'application/merge-patch+json')\n"
            "def b(path, obj):\n"
            "    log(f\"patching {obj['metadata']['uid']}\")\n"
            "    ksend('PATCH', path, {'metadata': {'labels': {}}}, 'application/merge-patch+json')\n"
            "def c(path, obj, generation):\n"
            "    if obj['status']['observedGeneration'] != generation:\n"
            "        return\n"
            "    ksend('PATCH', path, {'spec': {}}, 'application/merge-patch+json')\n"
            "def d(path, owner_uid):\n"
            "    ksend('PATCH', path, {'metadata': {'ownerReferences': [{'uid': owner_uid}]}},\n"
            "          'application/merge-patch+json')\n"))

    def test_a_put_of_the_object_as_read_passes(self):
        self.assertEqual(0, self.count(
            "def a(path):\n"
            "    lease = KUBE.get_optional(path, read=kget)\n"
            "    lease['spec']['holderIdentity'] = ''\n"
            "    SNAP.ksend('PUT', path, lease)\n"
            "def b(path, read, key, value):\n"
            "    try:\n"
            "        current = (read or kget)(path)\n"
            "    except HTTPError:\n"
            "        current = None\n"
            "    if current is None:\n"
            "        body = {'metadata': {}}\n"
            "        ksend('POST', path, body)\n"
            "    else:\n"
            "        body = dict(current, data={key: value})\n"
            "        ksend('PUT', path, body)\n"))

    def test_a_dict_get_or_a_replaced_read_is_not_the_object_as_read(self):
        self.assertEqual(3, self.count(
            "def a(path, cfg):\n"
            "    body = cfg.get('manifest')\n"
            "    ksend('PUT', path, body)\n"
            "def b(path):\n"
            "    body = kget(path)\n"
            "    body = {'spec': {}}\n"
            "    ksend('PUT', path, body)\n"
            "def c(path, obj):\n"
            "    body = dict(obj)\n"
            "    ksend('PUT', path, body)\n"))

    def test_a_json_patch_post_or_delete_is_not_counted(self):
        self.assertEqual(0, self.count(
            "def a(path):\n"
            "    ksend('PATCH', path, [], 'application/json-patch+json')\n"
            "    ksend('POST', path, {})\n"
            "    ksend('DELETE', path)\n"
            "    ksend('PATCH', path, {})\n"))

    def test_each_site_is_named_by_function_and_line(self):
        self.assertEqual([Site("a", 3)], sites(SS.code(
            "def a(path):\n"
            "    x = 1\n"
            "    ksend('PUT', path, {})\n")))

    def test_a_renamed_local_or_a_moved_function_keeps_the_count(self):
        before = self.count("def a(p):\n    body = {}\n    ksend('PUT', p, body)\ndef b():\n    pass\n")
        after = self.count("def b():\n    pass\ndef a(path):\n    new_body = {}\n    ksend('PUT', path, new_body)\n")
        self.assertEqual(before, after)


class WritePreconditionRatchet(unittest.TestCase):
    """Server code, against COUNTS."""

    @classmethod
    def setUpClass(cls):
        cls.sites = server_sites()
        cls.found = {name: len(found) for name, found in cls.sites.items()}

    def test_no_new_write_without_a_precondition(self):
        grown = {name: (count, COUNTS.get(name, 0)) for name, count in self.found.items()
                 if count > COUNTS.get(name, 0)}
        where = "; ".join(f"{name}:{site.line} ({site.function})"
                          for name in sorted(grown) for site in self.sites[name])
        self.assertEqual({}, grown, "file: (writes with no precondition seen, allowed). One of these is new: "
                         + where + ". " + WHAT_TO_DO)

    def test_counts_only_shrink(self):
        stale = {name: (self.found.get(name, 0), allowed) for name, allowed in COUNTS.items()
                 if self.found.get(name, 0) < allowed}
        self.assertEqual({}, stale,
                         "file: (found, allowed). Fewer than allowed: lower the count in COUNTS in "
                         "tests/test_write_preconditions.py, or remove the entry, so it cannot grow back.")

    def test_the_failure_message_gives_an_honest_new_write_a_route(self):
        self.assertIn("raise that file's entry in COUNTS in tests/test_write_preconditions.py by one, "
                      "with a comment on the line saying why", WHAT_TO_DO)
        self.assertIn("not to Kubernetes", WHAT_TO_DO)

    def test_the_scan_still_sees_writes(self):
        self.assertGreater(sum(self.found.values()), 50)


if __name__ == "__main__":
    unittest.main()
