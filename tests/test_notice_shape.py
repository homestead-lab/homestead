"""A warning, blocker or error reads as one short line first (design.md, Notices).

The page shows a warning or an error by its first sentence - the text up to
the first ". " or "; " - and keeps the rest behind More (UI.callout). So
every message the server raises or lists as a warning or blocker opens with a
sentence short enough to stand on its own; the reason and the way out follow.
Names are counted as eight characters, wherever they are filled in.
"""
import ast
import re
import unittest
from pathlib import Path

SERVER = Path(__file__).resolve().parents[1] / "server"
SHORT_CHARS = 90
# A list or set a message is added to: warnings, blockers, reasons, problems.
LISTS = re.compile(r"warn|block|reason|problem|issue|concern|caveat|risk", re.I)
RAISED = {"ValueError", "RuntimeError", "PermissionError"}
# The first sentence ends at ". " or "; " - not after e.g., i.e., etc. or vs.
BREAK = re.compile(r"(?<!e\.g)(?<!i\.e)(?<!etc)(?<!vs)[.;](?=\s)")


def _text(node):
    """The words of a string, f-string or a + of them; None for anything else."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value if isinstance(v, ast.Constant) else "NAMEXXXX" for v in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _text(node.left), _text(node.right)
        return left + right if left is not None and right is not None else (left or right)
    return None


def _messages(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr in ("append", "add") and node.args:
            target = node.func.value
            name = target.id if isinstance(target, ast.Name) else getattr(target, "attr", "")
            if LISTS.search(name):
                yield node.lineno, _text(node.args[0])
        elif isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call) \
                and getattr(node.exc.func, "id", "") in RAISED and node.exc.args:
            yield node.lineno, _text(node.exc.args[0])


def first_sentence(message):
    found = BREAK.search(message)
    return message[:found.start()] if found else message


class NoticeShapeTests(unittest.TestCase):
    def test_every_warning_blocker_and_error_opens_with_a_short_sentence(self):
        long = []
        for path in sorted(SERVER.glob("*.py")):
            for line, message in _messages(ast.parse(path.read_text(encoding="utf-8"))):
                if message and len(first_sentence(message)) > SHORT_CHARS:
                    long.append(f"{path.name}:{line} ({len(first_sentence(message))}) {message[:90]}")
        self.assertEqual([], long, "Open each with a sentence under "
                         f"{SHORT_CHARS} characters; the reason and the way out follow it")

    def test_the_first_sentence_is_cut_where_the_page_cuts_it(self):
        self.assertEqual("Recreate stops all old pods before replacements start",
                         first_sentence("Recreate stops all old pods before replacements start; all containers stop"))
        self.assertEqual("Stop it first, e.g. from its page", first_sentence("Stop it first, e.g. from its page"))
        self.assertEqual("v1.2.3 is out", first_sentence("v1.2.3 is out"))


if __name__ == "__main__":
    unittest.main()
