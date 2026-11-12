"""Every kind of job says what ends its wait (a ratchet).

A resolver that keeps answering "running" keeps its job, and the job's
start() guards, for ever: homestead_job_errors' limits only cover one that
fails. Fixes since October gave kinds one at a time a limit; nothing asked a
new kind for one. homestead_job_errors.DEADLINES names, per kind, the number
that ends its wait: a module's constant, or a number written in one of its
functions. NO_DEADLINE says why a kind has none.

Checked here: every registered kind is in exactly one of the two, each named
constant still exists and is positive (or a table holding positive numbers), each
named function still has its number in its code, and each reason says
something. It is a lint, not a proof: the test cannot see that the resolver
compares against the number it names.
"""
import ast
import importlib
import inspect
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import source_scan as SS

sys.path.insert(0, str(SS.SERVER))
import homestead_job_errors as ERRORS

WHAT_TO_DO = (
    "A job whose resolver keeps answering \"running\" keeps its guards for ever. Give the kind a limit "
    "and name it in homestead_job_errors.DEADLINES: (module, CONSTANT), or (module, function, seconds) for "
    "a number written in that function. If it truly has none, say why in NO_DEADLINE (a kind that waits "
    "for a person by design, or one that ends on its first round).")


def _arithmetic(node):
    """The value of a number, or of a product or sum of numbers; None for
    anything else."""
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Mult, ast.Add)):
        left, right = _arithmetic(node.left), _arithmetic(node.right)
        if left is not None and right is not None:
            return left * right if isinstance(node.op, ast.Mult) else left + right
    return None


def _numbers(node):
    """Every number a function's code writes: literals, and products and
    sums of literals (15 * 60)."""
    return {value for child in ast.walk(node) for value in [_arithmetic(child)] if value is not None}


def _positive(value):
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return value > 0
    if isinstance(value, dict):
        return any(_positive(v) for v in value.values())
    if isinstance(value, (tuple, list)):
        return any(_positive(v) for v in value)
    return False


_DEADLINE_WORDS = ("deadline", "limit")


def _ending_numbers(fn_node):
    """The numbers a function writes where they end a wait: in the test of
    an if whose body returns (the wait's end), as an activeDeadlineSeconds
    value, or in the value given to a name, key or keyword argument with
    "deadline" or "limit" in it. A number elsewhere (a force= flag, a
    message, a cadence) does not count."""
    found = set()

    def numbers(node):
        return _numbers(node) if node is not None else set()

    for node in ast.walk(fn_node):
        if isinstance(node, ast.If) and any(isinstance(n, ast.Return) for n in node.body):
            found |= numbers(node.test)
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and isinstance(key.value, str) and (
                        key.value == "activeDeadlineSeconds" or any(w in key.value.lower() for w in _DEADLINE_WORDS)):
                    found |= numbers(value)
        elif isinstance(node, ast.keyword) and node.arg and any(w in node.arg.lower() for w in _DEADLINE_WORDS):
            found |= numbers(node.value)
        elif isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if any(w in n.lower() for n in names for w in _DEADLINE_WORDS):
                found |= numbers(node.value)
    return found


def problem(entry):
    """Why a DEADLINES entry does not hold, or ""."""
    if not (isinstance(entry, tuple) and len(entry) in (2, 3)):
        return "an entry is (module, CONSTANT) or (module, function, seconds)"
    module, name = entry[0], entry[1]
    try:
        value = getattr(importlib.import_module(module), name)
    except (ImportError, AttributeError):
        return f"{module}.{name} does not exist"
    if len(entry) == 2:
        return "" if _positive(value) else f"{module}.{name} is not a positive number or a table holding one"
    if not inspect.isfunction(value):
        return f"{module}.{name} is not a function"
    node = SS.functions(Path(inspect.getsourcefile(value))).get(value.__qualname__)
    if node is None or entry[2] not in _numbers(node.node):
        return f"{module}.{name} no longer writes {entry[2]}"
    if entry[2] not in _ending_numbers(node.node):
        return (f"{module}.{name} writes {entry[2]}, but not where it ends the wait (an if that returns, "
                f"activeDeadlineSeconds, or a deadline or limit value)")
    return ""


# The kinds allowed in NO_DEADLINE: the ones on dev when this guard came in.
# It may only shrink: a new kind names what ends its wait in DEADLINES.
NO_DEADLINE_ALLOWED = frozenset({
    "backup",
    "cluster-cleanup",
    "deployment",
    "harvester-upgrade",
    "helm",
    "host-console",
    "image-rollback",
    "image-update",
    "import",
    "longhorn-runtime",
    "longhorn-v2-cleanup",
    "move",
    "network-service",
    "onboard",
    "protect-run",
    "restructure",
    "self-data-move",
    "vm-create",
    "vm-disk-import",
    "vm-disk-remove",
    "vm-edit",
    "vm-migration",
    "vm-power",
    "volume-delete",
    "volume-restore",
    "weekly-trim",
    "workload-rename",
})


class TheCheckItself(unittest.TestCase):

    def test_a_constant_that_is_gone_is_said(self):
        self.assertIn("does not exist", problem(("homestead_job_errors", "NO_SUCH_LIMIT")))

    def test_a_constant_must_be_a_positive_number(self):
        self.assertEqual("", problem(("homestead_job_errors", "DEFAULT_LIMIT")))
        self.assertIn("not a positive", problem(("homestead_job_errors", "KEY")))

    def test_a_number_written_in_a_function_is_found(self):
        self.assertEqual("", problem(("homestead_shares", "removal_progress", 1200)))
        self.assertEqual("", problem(("homestead_storage_server", "_restore_then_tidy", 900)))  # 15 * 60
        self.assertIn("no longer writes", problem(("homestead_shares", "removal_progress", 1201)))

    def test_a_number_only_outside_the_ending_does_not_count(self):
        tree = SS.tree(SS.code(
            "def step(item, now, since):\n"
            "    state = look(item, force=now - since > 7200)\n"
            "    say(f'after {7200 // 3600} hours')\n"
            "    return 'running', 50, ''\n"))
        self.assertNotIn(7200, _ending_numbers(tree.body[0]))

    def test_each_ending_shape_counts(self):
        tree = SS.tree(SS.code(
            "def step(item, now, since, size):\n"
            "    if now - since > 7200:\n"
            "        return 'failed', 100, ''\n"
            "    body = {'spec': {'activeDeadlineSeconds': 900}}\n"
            "    limit = 3600 + 120 * size\n"
            "    to('restoring', deadline=now + 1800 + 120 * size)\n"))
        self.assertTrue({7200, 900, 3600, 1800} <= _ending_numbers(tree.body[0]))

    def test_a_malformed_entry_is_said(self):
        self.assertIn("an entry is", problem(("homestead_shares",)))


class EveryKindHasADeadline(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import server  # noqa: F401  (fills the registries)
        import homestead_job_kinds as KINDS
        cls.kinds = set(KINDS.kinds())

    def test_every_kind_is_in_one_table(self):
        self.assertGreater(len(self.kinds), 50)
        self.assertEqual(set(), set(ERRORS.DEADLINES) & set(ERRORS.NO_DEADLINE))
        missing = self.kinds - set(ERRORS.DEADLINES) - set(ERRORS.NO_DEADLINE)
        self.assertEqual([], sorted(missing), WHAT_TO_DO)

    def test_every_entry_is_a_kind(self):
        stale = (set(ERRORS.DEADLINES) | set(ERRORS.NO_DEADLINE)) - self.kinds
        self.assertEqual([], sorted(stale), "no registered kind has these names: remove them from "
                                            "homestead_job_errors.DEADLINES or NO_DEADLINE")

    def test_every_named_deadline_exists(self):
        broken = {kind: problem(entry) for kind, entry in ERRORS.DEADLINES.items() if problem(entry)}
        self.assertEqual({}, broken, "Fix the entry in homestead_job_errors.DEADLINES to name the number "
                                     "that now ends this kind's wait.")

    def test_no_new_kind_goes_without_one(self):
        added = sorted(set(ERRORS.NO_DEADLINE) - NO_DEADLINE_ALLOWED)
        self.assertEqual([], added, "A new kind must name what ends its wait in homestead_job_errors.DEADLINES; "
                                    "NO_DEADLINE holds only the kinds that had none when this guard came in "
                                    "(NO_DEADLINE_ALLOWED in tests/test_job_deadlines.py), and may only shrink.")

    def test_the_allowed_list_only_shrinks(self):
        gone = sorted(NO_DEADLINE_ALLOWED - set(ERRORS.NO_DEADLINE))
        self.assertEqual([], gone, "These kinds are no longer in NO_DEADLINE: remove them from NO_DEADLINE_ALLOWED "
                                   "in tests/test_job_deadlines.py, so they cannot come back.")

    def test_every_kind_without_one_says_why(self):
        for kind, reason in ERRORS.NO_DEADLINE.items():
            with self.subTest(kind=kind):
                self.assertGreater(len(reason), 20)


if __name__ == "__main__":
    unittest.main()
