"""The shared reader the guard tests use (tests/source_scan.py, #732)."""
import ast
import unittest
from pathlib import Path

import source_scan as SS

PY = SS.code('''\
import os

def go(kind):
    OPS.start("copy", "x")
    # OPS.start("commented", "x")
    text = 'OPS.start("in-a-string")'
    ops.start(kind)
    return x.OPS.start("other")


class H:
    def do_GET(self):
        def inner():
            pass
        return go("a")

    async def _helper(self):
        go("b")
''')

CLASS = SS.code('''\
class H:
    PUBLIC = ("/api/attribute",)

    @wrap("/api/decorator")
    def one(self):
        return 1
        # a trailing comment

    # between one and two
    def two(self):
        pass
''')


class Files(unittest.TestCase):
    def test_text_reads_a_file_once_per_run(self):
        first = SS.text("server/server.py")
        self.assertIs(first, SS.text(SS.SERVER_PY))
        self.assertEqual(SS.SERVER_PY.read_text(encoding="utf-8"), first)

    def test_server_modules_are_every_homestead_module_in_name_order(self):
        found = SS.server_modules()
        self.assertEqual(sorted((SS.ROOT / "server").glob("homestead_*.py")), found)
        self.assertEqual([path.name for path in found], sorted(path.name for path in found))
        self.assertNotIn(SS.SERVER_PY, found)
        self.assertGreater(len(found), 100)

    def test_a_file_is_parsed_once_per_run(self):
        self.assertIs(SS.tree(SS.SERVER_PY), SS.tree(Path("server/server.py")))
        self.assertIs(SS.tree(SS.SERVER_PY), SS.tree("server/server.py"))
        parsed = ast.parse("x = 1")
        self.assertIs(parsed, SS.tree(parsed))


class PathsAndCode(unittest.TestCase):
    """A str names a file, as for text(); source text is passed as code(...).
    A path read as code would parse (server/x.py is a division) and find
    nothing, so a guard written that way would pass for ever."""

    def test_a_str_path_is_read_as_the_file(self):
        self.assertTrue(SS.calls("server/server.py", "threading.Thread"))
        self.assertIn("H.do_GET", SS.functions("server/server.py"))

    def test_a_missing_file_is_an_error_not_an_empty_answer(self):
        with self.assertRaises(FileNotFoundError):
            SS.calls("server/homestead_no_such_module.py", "OPS.start")

    def test_source_text_not_marked_as_code_is_refused(self):
        with self.assertRaisesRegex(TypeError, r"source_scan\.code"):
            SS.calls("def go():\n    f()\n", "f")
        self.assertEqual([2], [node.lineno for node in SS.calls(SS.code("def go():\n    f()\n"), "f")])


class Functions(unittest.TestCase):
    def test_every_function_by_qualified_name_in_source_order(self):
        self.assertEqual(["go", "H.do_GET", "H.do_GET.inner", "H._helper"], list(SS.functions(PY)))

    def test_a_function_s_text_runs_from_its_def_to_its_last_line(self):
        found = SS.functions(PY)["H.do_GET"]
        self.assertEqual("    def do_GET(self):\n        def inner():\n            pass\n        return go(\"a\")",
                         found.text)
        self.assertIsInstance(found.node, ast.FunctionDef)
        self.assertIsInstance(SS.functions(PY)["H._helper"].node, ast.AsyncFunctionDef)

    def test_a_decorated_function_s_text_starts_at_its_decorator(self):
        found = SS.functions(SS.code("x = 1\n\n@wrap(\"/api/x\")\ndef go():\n    pass\n"))["go"]
        self.assertEqual("@wrap(\"/api/x\")\ndef go():\n    pass", found.text)

    def test_a_decorated_method_s_text_starts_at_its_decorator(self):
        self.assertTrue(SS.functions(CLASS)["H.one"].text.startswith('    @wrap("/api/decorator")\n    def one'))

    def test_class_attributes_and_comments_around_functions_belong_to_none(self):
        texts = "\n".join(found.text for found in SS.functions(CLASS).values())
        self.assertNotIn("/api/attribute", texts)
        self.assertNotIn("a trailing comment", texts)
        self.assertNotIn("between one and two", texts)

    def test_a_commented_out_def_is_not_a_function(self):
        self.assertEqual(["live"], list(SS.functions(SS.code("# def gone():\n#     pass\ndef live():\n    pass\n"))))

    def test_a_tree_alone_has_no_text_to_give(self):
        with self.assertRaises(TypeError):
            SS.functions(ast.parse(PY))

    def test_every_function_keeps_each_def_of_a_name_in_source_order(self):
        source = SS.code("if X:\n    def go():\n        return 1\nelse:\n    def go():\n        return 2\n"
                         "class C:\n    @property\n    def v(self):\n        return 3\n"
                         "    @v.setter\n    def v(self, value):\n        pass\n")
        found = SS.every_function(source)
        self.assertEqual(["go", "C.v"], list(found))
        self.assertEqual(["        return 1", "        return 2"], [f.text.splitlines()[-1] for f in found["go"]])
        self.assertEqual(["@property", "@v.setter"], [f.text.split("\n")[0].strip() for f in found["C.v"]])
        self.assertEqual(5, SS.functions(source)["go"].node.lineno, "functions() keeps the last")


class Calls(unittest.TestCase):
    def test_calls_are_matched_by_how_the_callee_is_written(self):
        found = SS.calls(PY, "OPS.start", "ops.start")
        self.assertEqual([4, 7], [node.lineno for node in found])
        self.assertEqual(["copy"], [node.args[0].value for node in found[:1]])

    def test_a_call_in_a_comment_or_a_string_is_not_a_call(self):
        self.assertEqual([4], [node.lineno for node in SS.calls(PY, "OPS.start")])

    def test_a_bare_name_matches_only_a_bare_call(self):
        self.assertEqual([15, 18], [node.lineno for node in SS.calls(PY, "go")])
        self.assertEqual([8], [node.lineno for node in SS.calls(PY, "x.OPS.start")])

    def test_dotted_names_only_plain_names_and_attributes(self):
        self.assertEqual("a.b.c", SS.dotted(ast.parse("a.b.c", mode="eval").body))
        self.assertIsNone(SS.dotted(ast.parse("a().b", mode="eval").body))

    def test_a_file_is_read_by_path(self):
        self.assertTrue(SS.calls(SS.SERVER_PY, "threading.Thread"))


IMPORTS = SS.code('''\
import homestead_x as X, os
import homestead_x
from pkg import homestead_x as Y
from homestead_longhorn import (a,
                                b as c)
from homestead_longhorn import d
import homestead_x.sub
# import homestead_x as Z
note = "import homestead_x as W"


def go():
    import homestead_x as Q
    return X.kget, X.ksend(1), STATE.LH.snap, LH.snap, a.X.hidden, "X.in_a_string"  # X.in_a_comment
''')


class Imports(unittest.TestCase):
    def test_a_module_is_found_under_every_name_a_file_binds_it_to(self):
        self.assertEqual({"X", "homestead_x", "Y", "Q"}, SS.import_aliases(IMPORTS, "homestead_x"))

    def test_a_package_or_an_import_in_a_comment_or_a_string_is_not_the_module(self):
        self.assertEqual({"os"}, SS.import_aliases(IMPORTS, "os"))
        self.assertEqual(set(), SS.import_aliases(IMPORTS, "homestead_x.sub"))
        self.assertEqual(set(), SS.import_aliases(IMPORTS, "homestead_y"))

    def test_the_names_imported_from_a_module_are_found_as_written_there(self):
        self.assertEqual({"a", "b", "d"}, SS.from_imports(IMPORTS, "homestead_longhorn"))
        self.assertEqual(set(), SS.from_imports(IMPORTS, "homestead_x"))

    def test_attributes_are_read_off_a_name_or_a_dotted_name(self):
        self.assertEqual({"kget", "ksend"}, SS.attribute_reads(IMPORTS, "X"))
        self.assertEqual({"snap"}, SS.attribute_reads(IMPORTS, "LH"))
        self.assertEqual({"snap"}, SS.attribute_reads(IMPORTS, "STATE.LH"))
        self.assertEqual({"hidden"}, SS.attribute_reads(IMPORTS, "a.X"))

    def test_names_are_counted_where_they_are_defined_and_used(self):
        counts = SS.name_counts(SS.code(
            "LIMIT = 3\n\n\ndef grow(by, *, LIMIT=1):\n    # LIMIT in a comment\n"
            "    return LIMIT + by.LIMIT + obj.grow(\"LIMIT\", by=2)\n\n\nclass Grow:\n    pass\n"))
        self.assertEqual({"LIMIT": 4, "grow": 2, "by": 2, "Grow": 1}, {k: counts[k] for k in ("LIMIT", "grow", "by", "Grow")})
        self.assertEqual(0, counts["missing"])

    def test_the_file_lists_come_from_the_repository(self):
        self.assertIn(SS.SERVER_PY, SS.server_files())
        self.assertIn(SS.SERVER / "homestead_kube.py", SS.server_files())
        self.assertNotIn(SS.SERVER_PY, SS.server_modules())
        self.assertIn(Path(__file__).resolve(), SS.test_files())
        self.assertNotIn(SS.ROOT / "tests" / "source_scan.py", SS.test_files())


JS = SS.code("""\
async function api(path, opts) {
  return fetch(path, opts);
}
const views = { api(path) { return path; }, other: 1 };
// api("/api/commented")
const said = "api('/api/in-a-string')";
const re = /api\\("x"\\)/;
api("/api/one");
window.api("/api/not-bare");
myapi("/api/not-bare-either");
api(`/api/two/${encodeURIComponent(ns + "/" + name)}?x=${y}`, { method: "POST", body: f(a, b) });
await api(
  "/api/three",
  opts
);
""")


class JsCode(unittest.TestCase):
    def test_comments_strings_and_regular_expressions_are_blanked_where_they_stood(self):
        found = SS.js_code(SS.code('a("x y"); // c\nb(/r+/g, \'q\', `t${ c("z") }u`) /* d */'))
        self.assertEqual('a("   ");     \nb(/  /g, \' \', ` ${ c(" ") } `)        ', found)

    def test_the_text_keeps_its_length_and_its_lines(self):
        source = SS.text("web/js/core.js")
        found = SS.js_code("web/js/core.js")
        self.assertEqual(len(source), len(found))
        self.assertEqual(source.count("\n"), found.count("\n"))

    def test_a_division_is_not_a_regular_expression(self):
        self.assertEqual("x = a / b; f(\" \")", SS.js_code(SS.code('x = a / b; f("/")')))


class JsCalls(unittest.TestCase):
    def test_every_bare_call_with_its_line_and_its_arguments(self):
        found = SS.js_calls(JS, "api")
        self.assertEqual([8, 11, 12], [call.line for call in found])
        self.assertEqual(['"/api/one"'], found[0].args)
        self.assertEqual(['`/api/two/${encodeURIComponent(ns + "/" + name)}?x=${y}`',
                          '{ method: "POST", body: f(a, b) }'], found[1].args)
        self.assertEqual(['"/api/three"', "opts"], found[2].args)

    def test_a_declaration_is_not_a_call(self):
        lines = [call.line for call in SS.js_calls(JS, "api")]
        self.assertNotIn(1, lines)
        self.assertNotIn(4, lines)

    def test_a_call_in_a_comment_a_string_or_a_regular_expression_is_not_a_call(self):
        lines = [call.line for call in SS.js_calls(JS, "api")]
        self.assertFalse({5, 6, 7} & set(lines))

    def test_a_method_or_a_longer_name_is_not_the_bare_name(self):
        lines = [call.line for call in SS.js_calls(JS, "api")]
        self.assertFalse({9, 10} & set(lines))

    def test_the_real_declaration_in_core_js_is_not_counted(self):
        source = SS.text("web/js/core.js")
        declared = source[:source.index("async function api(")].count("\n") + 1
        lines = [call.line for call in SS.js_calls("web/js/core.js", "api")]
        self.assertTrue(lines)
        self.assertNotIn(declared, lines)

    def test_an_unclosed_call_is_an_error_with_its_line(self):
        with self.assertRaisesRegex(ValueError, "line 2"):
            SS.js_calls(SS.code('x();\napi("/api/x",\n'), "api")

    def test_source_text_not_marked_as_code_is_refused(self):
        with self.assertRaisesRegex(TypeError, r"source_scan\.code"):
            SS.js_calls('api("/api/x");\n', "api")


if __name__ == "__main__":
    unittest.main()
