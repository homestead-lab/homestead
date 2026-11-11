"""docs/architecture.md tells a contributor how jobs, the leader and the
Kubernetes client work, by naming the modules and functions to read. This
keeps those names true, so the page cannot fall behind the code unnoticed.

Every name the page writes in backticks is checked:
- a path under server/, tests/, docs/ or scripts/ must exist;
- a module, `homestead_x`, must be server/homestead_x.py;
- a name in a module, `homestead_x.name`, must be defined at the top of
  that module (a def, a class or an assignment). Only the first name after
  the module is checked: `homestead_x.Class.method` checks Class.
A link to another page under docs/ must reach a file, and its #anchor a
heading on that page.
"""
import ast
import re
import unittest
from pathlib import Path

import source_scan as SS

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "docs" / "architecture.md"
CONTRIBUTING = ROOT / "CONTRIBUTING.md"

CODE = re.compile(r"`([^`\n]+)`")
PATH = re.compile(r"(?:server|tests|docs|scripts)/[\w./-]*\w")
MODULE = re.compile(r"\b(homestead_\w+)((?:\.\w+)*)")
LINK = re.compile(r"\]\(([^)#\s]*)(#[^)\s]*)?\)")


def top_level_names(tree):
    """The names a module defines at its top level."""
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                for name in ast.walk(target):
                    if isinstance(name, ast.Name):
                        names.add(name.id)
    return names


def references(text):
    """(kind, name) for each path, module and module name the page writes in backticks."""
    found = []
    for span in CODE.findall(text):
        for path in PATH.findall(span):
            found.append(("path", path))
        # A path's file name is not also a module name: `server/homestead_x.py` is not homestead_x.py.
        for module, attributes in MODULE.findall(PATH.sub(" ", span)):
            first = attributes.split(".")[1] if attributes else ""
            found.append(("name", f"{module}.{first}") if first else ("module", module))
    return found


def slug(heading):
    """The anchor GitHub gives a heading."""
    text = re.sub(r"[^\w\- ]", "", heading.strip().lower())
    return text.replace(" ", "-")


def problems(text, root=ROOT, here=None):
    """What the page names that the code or the docs no longer have."""
    here = here or root / "docs"
    out = []
    for kind, name in references(text):
        if kind == "path":
            if not (root / name).exists():
                out.append(f"{name} does not exist")
            continue
        module, _, attribute = name.partition(".")
        source = root / "server" / f"{module}.py"
        if not source.exists():
            out.append(f"{module} is not a module in server/")
        elif attribute and attribute not in top_level_names(ast.parse(source.read_text(encoding="utf-8"))):
            out.append(f"{module} has no {attribute}")
    for target, anchor in LINK.findall(text):
        if not target or "://" in target:
            continue
        linked = (here / target).resolve()
        if not linked.exists():
            out.append(f"the link to {target} reaches no file")
        elif anchor and linked.suffix == ".md":
            headings = {slug(line.lstrip("#")) for line in linked.read_text(encoding="utf-8").splitlines()
                        if line.startswith("#")}
            if anchor[1:] not in headings:
                out.append(f"the link to {target}{anchor} reaches no heading")
    return sorted(set(out))


class ArchitectureDocTest(unittest.TestCase):
    def test_everything_the_page_names_exists(self):
        self.assertEqual(problems(SS.text(PAGE)), [],
                         "Change docs/architecture.md to say what the code does now")

    def test_the_page_names_enough_for_the_check_to_mean_something(self):
        found = references(SS.text(PAGE))
        kinds = {kind for kind, _ in found}
        self.assertEqual(kinds, {"path", "module", "name"})
        self.assertGreaterEqual(len(found), 30, "the scan found almost nothing: is the pattern still right?")

    def test_a_name_that_is_gone_is_reported(self):
        text = ("Read `homestead_kube.get_optional` and `server/homestead_leader.py`. "
                "Then `homestead_no_such_module`, `homestead_kube.no_such_function()`, "
                "`server/no_such_file.py` and [the page](no-such-page.md), "
                "and [a heading](reference.md#no-such-heading).")
        self.assertEqual(problems(text), [
            "homestead_kube has no no_such_function",
            "homestead_no_such_module is not a module in server/",
            "server/no_such_file.py does not exist",
            "the link to no-such-page.md reaches no file",
            "the link to reference.md#no-such-heading reaches no heading",
        ])

    def test_a_name_assigned_at_the_top_counts(self):
        tree = ast.parse("A = B = None\nC: int = 1\nx, y = 1, 2\ndef f():\n    inner = 1\nclass K:\n    pass\n")
        self.assertEqual(top_level_names(tree), {"A", "B", "C", "x", "y", "f", "K"})

    def test_the_write_guards_are_listed_in_their_order(self):
        tree = ast.parse((ROOT / "server" / "homestead_kube.py").read_text(encoding="utf-8"))
        order = next([element.id for element in node.value.elts] for node in tree.body
                     if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "WRITE_GUARDS")
        named = []
        for name in re.findall(r"`homestead_kube\.(\w+)`", SS.text(PAGE)):
            if name in order and name not in named:
                named.append(name)
        self.assertEqual(named, order, "List homestead_kube.WRITE_GUARDS on the page in the order the code runs them")

    def test_contributing_links_to_the_page(self):
        self.assertIn("](docs/architecture.md)", SS.text(CONTRIBUTING))


if __name__ == "__main__":
    unittest.main()
