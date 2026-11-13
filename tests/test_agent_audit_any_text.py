"""The AI agents' audit log records every call, whatever characters its
arguments hold: a JSON text may carry any escaped code point, and the line
for a call that ran is always written, then trimmed and read back."""
import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_mcp import AUDIT, TOOLS, Base, key

# A JSON text may escape a code point that is not a character on its own (half of a pair).
ODD = json.loads('"lab\\ud800"')


class AnyText(Base):
    def call_with(self, namespace):
        with mock.patch.object(TOOLS, "call", return_value=[{"namespace": "lab", "name": "data"}]):
            return self.call("list_volumes", {"namespace": namespace}, key())

    def test_a_call_whose_argument_holds_any_code_point_is_recorded_once(self):
        printed = io.StringIO()
        with redirect_stdout(printed):
            result = self.call_with(ODD)
        self.assertFalse(result["isError"], result)
        rows = AUDIT.recent()
        self.assertEqual(["ok"], [row["outcome"] for row in rows])
        self.assertEqual(ODD, rows[0]["args"]["namespace"])
        self.assertEqual("", printed.getvalue(), "written to the data volume, not only to the pod log")

    def test_every_line_on_disk_is_plain_ascii_json(self):
        self.call_with(ODD)
        self.call_with("lab-ä")
        raw = Path(AUDIT._path()).read_bytes()
        raw.decode("ascii")
        self.assertEqual(["lab-ä", ODD], [row["args"]["namespace"] for row in AUDIT.recent()])

    def test_the_daily_trim_keeps_such_a_line(self):
        self.call_with(ODD)
        self.call_with("lab")
        printed = io.StringIO()
        with redirect_stdout(printed):
            kept = AUDIT.trim()
        self.assertEqual(2, kept)
        self.assertNotIn("not trimmed", printed.getvalue())
        self.assertEqual(["lab", ODD], [row["args"]["namespace"] for row in AUDIT.recent()])


if __name__ == "__main__":
    unittest.main()
