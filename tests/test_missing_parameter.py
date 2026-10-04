"""A GET route called without a parameter it needs says which, with a 400.

The release tests' run over every route found /api/workload, /api/logs,
/api/move/plan and /api/image-updates/progress answering 500 with a bare
KeyError for 'ns'."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import server  # noqa: E402


class QueryTests(unittest.TestCase):
    def test_a_missing_parameter_is_a_value_error_naming_it(self):
        q = server.Query({"name": ["plex"]})
        self.assertEqual(["plex"], q["name"])
        self.assertEqual([""], q.get("ns", [""]), "optional reads are unchanged")
        with self.assertRaises(ValueError) as caught:
            q["ns"]
        self.assertEqual("missing parameter: ns", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
