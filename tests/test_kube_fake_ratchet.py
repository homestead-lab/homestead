"""A ratchet on hand-written Kubernetes stand-ins (#910, #619 step 5).

Tests used to keep a `class Cluster` or `class FakeCluster` of their own, each
answering the few paths its file needed and each a little wrong in its own way.
tests/fakes/kube.py is the one fake API they share now. A `Cluster` that wraps the
shared fake (the file imports it) is fine. This counts the classes still named
Cluster or FakeCluster in test files that do not use the shared fake, and lets
the number go down and never up: a new test uses the shared fake, and whoever
moves a file onto it lowers CEILING in the same change."""
import ast
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
NAMES = {"Cluster", "FakeCluster"}
# What is left after the four batches of #910: test_reclass, test_reclass_handoff and
# test_self_data_coordinator (their stand-ins simulate Kubernetes' controllers, not just the
# API), test_self_data_bootstrap (keeps the stand-in test_self_data_anchor used to have),
# test_move_flow and test_vm_edit (other test files import their classes, so they move
# together). Lower it when one moves.
CEILING = 6


def uses_shared_fake(tree):
    """Whether the file imports tests/fakes/kube.py (an import, not a mention in a comment)."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module == "fakes.kube" or (
                node.module == "fakes" and any(a.name == "kube" for a in node.names))):
            return True
        if isinstance(node, ast.Import) and any(a.name == "fakes.kube" for a in node.names):
            return True
    return False


def stand_ins(root):
    """file:line of each class named in NAMES, at any depth, outside root/fakes, in files that do not use the shared fake."""
    found = []
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root)
        if "fakes" in relative.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if uses_shared_fake(tree):
            continue
        found += [f"{relative}:{node.lineno}" for node in ast.walk(tree)
                  if isinstance(node, ast.ClassDef) and node.name in NAMES]
    return found


class RatchetTests(unittest.TestCase):
    def test_hand_written_kubernetes_stand_ins_only_go_down(self):
        found = stand_ins(TESTS)
        self.assertLessEqual(
            len(found), CEILING,
            f"{len(found)} hand-written Cluster/FakeCluster classes under tests/, ceiling {CEILING}. Use the shared "
            "fake (tests/fakes/kube.py, its docstring says what it models) instead of a new one. Found:\n  "
            + "\n  ".join(found))

    def test_the_counter_sees_a_stand_in_at_any_depth_and_skips_the_shared_fakes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "fakes").mkdir()
            (root / "fakes" / "kube.py").write_text("class FakeCluster:\n    pass\n")
            (root / "test_top.py").write_text("class Cluster:\n    pass\n")
            (root / "test_nested.py").write_text("def make():\n    class FakeCluster(object):\n        pass\n")
            (root / "test_wrapper.py").write_text("from fakes.kube import Kube\n\nclass Cluster:\n    pass\n")
            (root / "test_mention.py").write_text("# uses fakes.kube someday\nclass Cluster:\n    pass\n")
            (root / "test_clean.py").write_text("class ClusterTests:\n    pass\n\nclass Scene:\n    pass\n")
            self.assertEqual(["test_mention.py:2", "test_nested.py:2", "test_top.py:1"], stand_ins(root))


if __name__ == "__main__":
    unittest.main()
