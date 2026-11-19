"""The guard on helper pods and Jobs that nobody removes: a ratchet that may
only shrink.

A job that starts a bare Pod or a Job to do one step (a copy, a probe, a
power action) leaves it behind when it ends, unless something removes it.
One more is left for every run, and each keeps its log, its node and, for a
pod that never ended, its claim on the volume it reads. Today a helper is
removed one of three ways, and a body that does none of them is counted:

- a Job sets spec.ttlSecondsAfterFinished: Kubernetes removes it;
- the body sets metadata.ownerReferences: it goes with its owner;
- the body carries a task label (homestead_names.labels(task), or
  {"homestead.io/task": task}) that homestead_helper_sweep removes once it
  has ended: a Pod's task in its PODS, a Job's in its JOBS or listed by name
  in it (an import's Job).

Counted: every dict literal in server/ with "kind" "Pod" or "Job" and a
"metadata" (its spec may be added in a later step), or with the rest merged
in (**base); an owner reference or a review row names a kind too, but has
neither. And every call of homestead_runtime.pod (the shared crictl helper
pod), whose task argument is checked the same way. A TTL counts only as a
number written (or a module constant holding one), not None or a parameter;
ownerReferences only when not None and not an empty list.

ALLOWED is the bodies on dev when the guard came in that pass none of the
three, per (file under server/, function), each with the reason it is not
left behind, checked in the code. A count may go down (and then has to be
lowered here, or the entry removed), never up, and a new body that passes
none of the three fails. A body that moves moves its entry.

It is a lint over literal code, not a proof: a task label whose value is
built at run time is not seen (it fails, and goes on ALLOWED with a reason),
the sweep's name pattern is not checked against the body's name, and a body
built another way (dict(kind="Pod"), YAML) is not seen at all. A pod deleted
by the request that made it is on ALLOWED, not passed: a restart between the
create and the delete still leaves it.
"""
import ast
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import source_scan as SS

SWEEP = "server/homestead_helper_sweep.py"
TASK_LABEL = "homestead.io/task"

WHAT_TO_DO = (
    "A helper Pod or Job must be removed once it ends, even when Homestead restarts in between. "
    "Give it one of: spec.ttlSecondsAfterFinished (a Job), metadata.ownerReferences (an owner that goes "
    "away), or a task label (homestead_names.labels(\"<task>\")) that homestead_helper_sweep's PODS or "
    "JOBS lists, with a name its pattern matches. If none fits, add it to ALLOWED in "
    "tests/test_helper_cleanup.py with the reason it is not left behind.")

# (file under server/, function) -> bodies in it that pass none of the three,
# when the guard came in; the reason each is not left behind.
ALLOWED = {
    # Removed by a label sweep of their own module, on the leader's tick.
    ("homestead_files.py", "open_session"): 1,          # the file browser's pod: cleanup(), run by homestead_storage_server
    ("homestead_nodeshell.py", "body"): 1,              # the host shell's pod: sweep(), run by homestead_cluster_server
    ("homestead_image_cache.py", "start_image_scan"): 1,  # image-scan pods: _collect_scans(tidy=True) deletes each ended one
    # Deleted by the request that made it; one a restart leaves reaches its
    # activeDeadlineSeconds and the next run's _clear_ended() deletes it.
    ("homestead_hostrun.py", "body"): 1,
    # Homestead's own data move: created and deleted through its journal, so
    # a restart resumes the delete.
    ("homestead_self_data_copy.py", "job"): 1,          # the copy Job: the coordinator's release-copy step
    ("homestead_self_data_launch.py", "resources"): 1,  # the coordinator Job: homestead_self_data_finish's cleanup
    ("homestead_self_data_prepare.py", "manifests"): 1,  # the binding pod: the release-binding step
    # Never created: a Job's pod, built for a dry-run admission check (dryRun=All) and compared.
    ("homestead_self_data_launch.py", "pod_of"): 1,
    ("homestead_self_data_preflight.py", "copy_pod"): 1,
    # Never created: the Job's pod as read back from the API ({"kind": "Pod", **pod}), compared with the review.
    ("homestead_self_data_bootstrap.py", "job_pod_admitted"): 1,
}


def _key(node):
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _pairs(node):
    return {_key(k): v for k, v in zip(node.keys, node.values) if _key(k)} if isinstance(node, ast.Dict) else {}


def _strings(source):
    """Module-level NAME = "text" assignments: the value a task constant stands for."""
    found = {}
    for node in SS.tree(source).body:
        if isinstance(node, ast.Assign) and _key(node.value) is not None:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    found[target.id] = node.value.value
    return found


def _value(node, constants):
    """A string a node stands for: a literal, or a module-level constant."""
    if _key(node) is not None:
        return node.value
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    return None


def swept_tasks():
    """The tasks homestead_helper_sweep removes: {"Pod": {...}, "Job": {...}}.
    PODS and JOBS keys, and a Job task it lists by name (_listed(".../jobs", "import"))."""
    found = {"Pod": set(), "Job": set()}
    for node in SS.tree(SWEEP).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            kind = {"PODS": "Pod", "JOBS": "Job"}.get(node.targets[0].id)
            if kind and isinstance(node.value, ast.Dict):
                found[kind] |= {_key(k) for k in node.value.keys if _key(k)}
    for call in SS.calls(SWEEP, "_listed"):
        if len(call.args) == 2 and _key(call.args[1]):
            path = ast.unparse(call.args[0])
            found["Job" if "/jobs" in path else "Pod"].add(call.args[1].value)
    return found


def _task_node(labels, aliases):
    """The node a labels expression gives as its task, or None when it names none."""
    if isinstance(labels, ast.Call) and SS.dotted(labels.func) in {f"{a}.labels" for a in aliases}:
        return labels.args[0] if labels.args else next((k.value for k in labels.keywords if k.arg == "task"), None)
    if isinstance(labels, ast.Call) and SS.dotted(labels.func) == "dict":
        for part in [*labels.args, *(k.value for k in labels.keywords if k.arg is None)]:
            found = _task_node(part, aliases)
            if found is not None:
                return found
        return None
    if isinstance(labels, ast.Dict):
        for k, v in zip(labels.keys, labels.values):
            if _key(k) == TASK_LABEL or (
                    isinstance(k, ast.Call) and SS.dotted(k.func) in {f"{a}.key" for a in aliases}
                    and k.args and _key(k.args[0]) == "task"):
                return v
            if k is None:  # {**NAMES.labels("x"), ...}
                found = _task_node(v, aliases)
                if found is not None:
                    return found
    return None


def _is_body(node):
    """A Pod or Job body: a kind and metadata (the spec may come in a later
    step), or a kind with the rest merged in (**base). An owner reference or
    a review row names a kind, but has neither."""
    pairs = _pairs(node)
    return _key(pairs.get("kind")) in ("Pod", "Job") and ("metadata" in pairs or None in node.keys)


def _ttl(node, numbers):
    """A TTL the scan can read: a number written, or a module constant
    holding one. None, a parameter or anything computed is not."""
    if isinstance(node, ast.Constant) and isinstance(node.value, int) and not isinstance(node.value, bool):
        return node.value >= 0
    return isinstance(node, ast.Name) and node.id in numbers


def _owners(node):
    """ownerReferences that name an owner: a list with an entry, or a value
    built elsewhere; not None and not an empty list."""
    if isinstance(node, ast.List):
        return bool(node.elts)
    return node is not None and not isinstance(node, ast.Constant)


def _module_numbers(source):
    return {target.id for node in SS.tree(source).body if isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Constant) and isinstance(node.value.value, int)
            for target in node.targets if isinstance(target, ast.Name)}


def _cleared(kind, body, aliases, constants, swept, numbers=frozenset()):
    """Why a literal body is removed once it ends, or None."""
    pairs = _pairs(body)
    meta, spec = _pairs(pairs.get("metadata")), _pairs(pairs.get("spec"))
    if kind == "Job" and _ttl(spec.get("ttlSecondsAfterFinished"), numbers):
        return "ttl"
    if _owners(meta.get("ownerReferences")):
        return "owner"
    task = _value(_task_node(meta.get("labels"), aliases), constants)
    if task and task in swept[kind]:
        return "sweep"
    return None


def _enclosing(source):
    spans = [(fn.node.lineno, fn.node.end_lineno, name)
             for name, defs in SS.every_function(source).items() for fn in defs]

    def at(line):
        inside = [span for span in spans if span[0] <= line <= span[1]]
        return max(inside, key=lambda span: span[0])[2] if inside else "<module>"
    return at


# (file, function): a Pod builder whose callers name the task. Its body is
# not counted; each call of it is, by the task it passes.
BUILDERS = {("homestead_runtime.py", "pod")}


def _file(source):
    return "<code>" if isinstance(source, SS.Code) else Path(source).name


def uncleared(source, swept=None, builders=BUILDERS):
    """{function: bodies in it that pass none of the three}, for one file."""
    swept = swept_tasks() if swept is None else swept
    aliases = SS.import_aliases(source, "homestead_names") or {"NAMES"}
    constants = _strings(source)
    numbers = _module_numbers(source)
    runtime = {f"{a}.pod" for a in SS.import_aliases(source, "homestead_runtime")}
    at = _enclosing(source)
    found = {}
    for node in ast.walk(SS.tree(source)):
        if isinstance(node, ast.Dict):
            pairs = _pairs(node)
            kind = _key(pairs.get("kind"))
            if not _is_body(node):
                continue
            task = _task_node(_pairs(pairs.get("metadata")).get("labels"), aliases)
            if ((_file(source), at(node.lineno)) in builders and isinstance(task, ast.Name)
                    and task.id in _params(source, node, at)):
                continue  # a builder whose caller names the task (homestead_runtime.pod): its calls are checked
            if _cleared(kind, node, aliases, constants, swept, numbers):
                continue
        elif isinstance(node, ast.Call) and SS.dotted(node.func) in runtime:
            task = node.args[4] if len(node.args) > 4 else next((k.value for k in node.keywords if k.arg == "task"), None)
            if _value(task, constants) in swept["Pod"]:
                continue
        else:
            continue
        name = at(node.lineno)
        found[name] = found.get(name, 0) + 1
    return found


def _params(source, node, at):
    fn = SS.functions(source).get(at(node.lineno))
    if fn is None:
        return set()
    args = fn.node.args
    return {a.arg for a in [*args.posonlyargs, *args.args, *args.kwonlyargs]}


def server_sites():
    swept = swept_tasks()
    found = {}
    for path in SS.server_files():
        for function, count in uncleared(path, swept).items():
            found[(path.name, function)] = count
    return found


class ScanFindsHelpers(unittest.TestCase):
    """The scan itself, on small pieces of code."""

    SWEPT = {"Pod": {"probe"}, "Job": {"restructure", "import"}}

    def sites(self, source):
        return uncleared(SS.code(source), self.SWEPT, {("<code>", "pod")})

    def test_a_bare_pod_is_counted(self):
        self.assertEqual({"start": 1}, self.sites(
            "def start(name):\n"
            "    return {'apiVersion': 'v1', 'kind': 'Pod', 'metadata': {'name': name}, 'spec': {}}\n"))

    def test_a_job_with_a_ttl_passes(self):
        self.assertEqual({}, self.sites(
            "def start(name):\n"
            "    return {'apiVersion': 'batch/v1', 'kind': 'Job', 'metadata': {'name': name},\n"
            "            'spec': {'ttlSecondsAfterFinished': 600, 'template': {}}}\n"))

    def test_a_ttl_on_a_pod_is_not_a_removal(self):
        self.assertEqual({"start": 1}, self.sites(
            "def start(name):\n"
            "    return {'kind': 'Pod', 'metadata': {'name': name}, 'spec': {'ttlSecondsAfterFinished': 1}}\n"))

    def test_an_owner_passes(self):
        self.assertEqual({}, self.sites(
            "def start(name, uid):\n"
            "    return {'kind': 'Pod', 'metadata': {'name': name, 'ownerReferences': [{'uid': uid}]}, 'spec': {}}\n"))

    def test_a_task_the_sweep_knows_passes_in_each_spelling(self):
        self.assertEqual({}, self.sites(
            "import homestead_names as NAMES\n"
            "TASK = 'probe'\n"
            "def a(name):\n"
            "    return {'kind': 'Pod', 'metadata': {'name': name, 'labels': NAMES.labels('probe')}, 'spec': {}}\n"
            "def b(name):\n"
            "    return {'kind': 'Pod', 'metadata': {'name': name, 'labels': {'homestead.io/task': TASK}}, 'spec': {}}\n"
            "def c(name):\n"
            "    return {'kind': 'Job', 'metadata': {'labels': dict({'app': name}, **NAMES.labels('import'))}, 'spec': {}}\n"
            "def d(name):\n"
            "    return {'kind': 'Job', 'metadata': {'labels': {NAMES.key('task'): 'restructure'}}, 'spec': {}}\n"))

    def test_a_task_the_sweep_does_not_know_is_counted(self):
        self.assertEqual({"a": 1, "b": 1, "c": 1}, self.sites(
            "import homestead_names as NAMES\n"
            "def a(name):\n"
            "    return {'kind': 'Pod', 'metadata': {'labels': NAMES.labels('my-helper')}, 'spec': {}}\n"
            "def b(name):\n"
            "    return {'kind': 'Pod', 'metadata': {'labels': NAMES.labels('restructure')}, 'spec': {}}\n"
            "def c(name, task):\n"
            "    return {'kind': 'Job', 'metadata': {'labels': {'homestead.io/task': task + '-x'}}, 'spec': {}}\n"))

    def test_a_body_built_in_two_steps_or_merged_in_is_counted(self):
        self.assertEqual({"a": 1, "b": 1}, self.sites(
            "def a(name, spec):\n"
            "    body = {'kind': 'Pod', 'metadata': {'name': name}}\n"
            "    body['spec'] = spec\n"
            "    return body\n"
            "def b(base):\n"
            "    return {'kind': 'Job', **base}\n"))

    def test_a_ttl_or_owner_that_says_nothing_does_not_pass(self):
        self.assertEqual({"a": 1, "b": 1, "c": 1}, self.sites(
            "def a(name, ttl=None):\n"
            "    return {'kind': 'Job', 'metadata': {}, 'spec': {'ttlSecondsAfterFinished': ttl}}\n"
            "def b(name):\n"
            "    return {'kind': 'Job', 'metadata': {}, 'spec': {'ttlSecondsAfterFinished': None}}\n"
            "def c(name):\n"
            "    return {'kind': 'Pod', 'metadata': {'ownerReferences': []}, 'spec': {}}\n"))

    def test_a_ttl_constant_passes(self):
        self.assertEqual({}, self.sites(
            "KEEP = 600\n"
            "def a(name):\n"
            "    return {'kind': 'Job', 'metadata': {}, 'spec': {'ttlSecondsAfterFinished': KEEP}}\n"))

    def test_an_owner_reference_or_a_job_template_is_not_a_body(self):
        self.assertEqual({}, self.sites(
            "def a(name, uid):\n"
            "    owner = {'apiVersion': 'batch/v1', 'kind': 'Job', 'name': name, 'uid': uid}\n"
            "    return {'kind': 'Deployment', 'metadata': {}, 'spec': {'template': {'metadata': {}, 'spec': {}}}}\n"))

    def test_the_shared_runtime_pod_is_checked_at_each_call(self):
        self.assertEqual({"b": 1}, self.sites(
            "import homestead_runtime as RUNTIME\n"
            "import homestead_names as NAMES\n"
            "def pod(name, ns, node, script, task):\n"
            "    return {'kind': 'Pod', 'metadata': {'labels': NAMES.labels(task)}, 'spec': {}}\n"
            "def a(node):\n"
            "    return RUNTIME.pod('x', 'lab', node, 'true', 'probe')\n"
            "def b(node):\n"
            "    return RUNTIME.pod('x', 'lab', node, 'true', 'image-scan')\n"))

    def test_a_renamed_local_or_a_moved_function_keeps_its_count(self):
        before = self.sites(
            "def helper():\n    pass\n"
            "def start(name):\n"
            "    body = {'kind': 'Pod', 'metadata': {'name': name}, 'spec': {}}\n"
            "    return body\n")
        after = self.sites(
            "def start(n):\n"
            "    pod_body = {'kind': 'Pod', 'metadata': {'name': n}, 'spec': {}}\n"
            "    return pod_body\n"
            "def helper():\n    pass\n")
        self.assertEqual(before, after)

    def test_the_sweep_tables_are_read(self):
        swept = swept_tasks()
        self.assertIn("image-cleanup", swept["Pod"])
        self.assertIn("restructure", swept["Job"])
        self.assertIn("import", swept["Job"])


class HelperCleanupRatchet(unittest.TestCase):
    """Server code, against ALLOWED."""

    @classmethod
    def setUpClass(cls):
        cls.found = server_sites()

    def test_no_new_helper_is_left_behind(self):
        grown = {site: (count, ALLOWED.get(site, 0)) for site, count in self.found.items()
                 if count > ALLOWED.get(site, 0)}
        self.assertEqual({}, grown,
                         "(file, function): (helper bodies with no removal, allowed). " + WHAT_TO_DO)

    def test_allowed_only_shrinks(self):
        stale = {site: (self.found.get(site, 0), allowed) for site, allowed in ALLOWED.items()
                 if self.found.get(site, 0) < allowed}
        self.assertEqual({}, stale,
                         "(file, function): (found, allowed). Fewer than allowed: lower the count in ALLOWED "
                         "in tests/test_helper_cleanup.py, or remove the entry, so it cannot grow back.")

    def test_the_scan_still_finds_the_bodies(self):
        # The scan has not gone blind: the passing kinds are still seen too.
        swept = swept_tasks()
        cleared = set()
        for path in SS.server_files():
            aliases = SS.import_aliases(path, "homestead_names") or {"NAMES"}
            constants = _strings(path)
            for node in ast.walk(SS.tree(path)):
                kind = _key(_pairs(node).get("kind")) if isinstance(node, ast.Dict) else None
                if isinstance(node, ast.Dict) and _is_body(node):
                    cleared.add(_cleared(kind, node, aliases, constants, swept, _module_numbers(path)))
        self.assertTrue({"ttl", "owner", "sweep"} <= cleared, cleared)


if __name__ == "__main__":
    unittest.main()
