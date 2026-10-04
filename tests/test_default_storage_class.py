"""The class new volumes go on when none is chosen: of several classes marked
the cluster's default - k3s's local-path and Longhorn's own - the one
Homestead was installed with, then Longhorn, never host-local storage."""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import server


def row(name, provisioner, default=False):
    return {"name": name, "provisioner": provisioner, "default": default, "internal": False, "made_for": ""}


class DefaultClassTests(unittest.TestCase):
    def chosen(self, rows, env="longhorn-r2"):
        with mock.patch.object(server, "ENV_STORAGE_CLASS", env), mock.patch.object(server, "STORAGE_CLASS", "unset"), \
                mock.patch.object(server.LH, "STORAGE_CLASS", "unset"):
            server._note_default_class(rows)
            return server.STORAGE_CLASS, server.LH.STORAGE_CLASS

    def test_longhorn_wins_over_local_path_when_both_are_default(self):
        rows = [row("local-path", "rancher.io/local-path", True), row("longhorn", "driver.longhorn.io", True)]
        self.assertEqual(("longhorn", "longhorn"), self.chosen(rows, env="longhorn"))
        self.assertEqual(("longhorn", "longhorn"), self.chosen(rows, env="longhorn-r2"))

    def test_the_installed_class_wins_among_defaults(self):
        rows = [row("fast", "driver.longhorn.io", True), row("longhorn", "driver.longhorn.io", True)]
        self.assertEqual("longhorn", self.chosen(rows, env="longhorn")[0])

    def test_a_single_default_is_still_the_choice(self):
        rows = [row("local-path", "rancher.io/local-path", True), row("longhorn", "driver.longhorn.io")]
        self.assertEqual("local-path", self.chosen(rows, env="longhorn")[0])


if __name__ == "__main__":
    unittest.main()
