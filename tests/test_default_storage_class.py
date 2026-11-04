"""The class new volumes go on when none is chosen (#1125): the install class
while it is here; else, of several classes marked the cluster's default -
k3s's local-path and Longhorn's own - Longhorn's; else Longhorn's usual names.
Never a class nobody may put data on."""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_storage_classes as STORAGE_CLASSES


def row(name, provisioner, default=False, **facts):
    return {"name": name, "provisioner": provisioner, "default": default, "internal": False, "made_for": "", **facts}


class DefaultClassTests(unittest.TestCase):
    def chosen(self, rows, install="longhorn-r2"):
        with mock.patch.object(STORAGE_CLASSES, "INSTALL_CLASS", install):
            return STORAGE_CLASSES.default_class(rows), STORAGE_CLASSES.claim_class(rows)

    def test_longhorn_wins_over_local_path_when_both_are_default(self):
        rows = [row("local-path", "rancher.io/local-path", True), row("longhorn", "driver.longhorn.io", True)]
        self.assertEqual(("longhorn", "longhorn"), self.chosen(rows, install=""))
        self.assertEqual(("longhorn", "longhorn"), self.chosen(rows, install="longhorn-r2"))

    def test_the_install_class_wins_over_every_default(self):
        rows = [row("fast", "driver.longhorn.io", True), row("longhorn", "driver.longhorn.io")]
        self.assertEqual("longhorn", self.chosen(rows, install="longhorn")[0])

    def test_a_single_default_is_the_choice_when_the_install_class_is_not_here(self):
        rows = [row("local-path", "rancher.io/local-path", True), row("longhorn", "driver.longhorn.io")]
        self.assertEqual("local-path", self.chosen(rows, install="longhorn-r2")[0])

    def test_an_unusable_install_class_or_default_is_passed_over(self):
        rows = [row("longhorn-r2", "driver.longhorn.io", made_for="image"), row("longhorn-static", "driver.longhorn.io", True, internal=True),
                row("harvester-longhorn", "driver.longhorn.io")]
        self.assertEqual("harvester-longhorn", self.chosen(rows)[0])

    def test_with_no_usable_class_a_claim_still_names_one(self):
        self.assertEqual(("", "longhorn-r2"), self.chosen([], install="longhorn-r2"))
        self.assertEqual(("", "longhorn-r2"), self.chosen([], install=""))


if __name__ == "__main__":
    unittest.main()
