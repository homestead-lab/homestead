"""An import source's password Secret goes with the source. Runs on the shared
fake API (tests/fakes/kube.py)."""
import json
import sys
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_kube as K
import homestead_imports as imports
import homestead_operations as ops
from fakes.kube import Kube

SECRETS = "/api/v1/namespaces/lab/secrets"
INVENTORY = "/api/v1/namespaces/lab/configmaps/homestead-sources"


def helper(kind, name, secret):
    """A pod or Job in Homestead's namespace that reads secret's password."""
    spec = {"restartPolicy": "Never", "containers": [{
        "name": "copy", "image": "alpine:3.24",
        "env": [{"name": "SSHPASS", "valueFrom": {"secretKeyRef": {"name": secret, "key": "password"}}}]}]}
    if kind == "Pod":
        return {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": name, "namespace": "lab"}, "spec": spec}
    return {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": name, "namespace": "lab"},
            "spec": {"template": {"spec": spec}}}


class ImportSourceCleanupTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.kube = self.enterContext(Kube())
        for patch in (mock.patch.object(ops, "DATA_DIR", tmp.name),
                      mock.patch.object(imports, "NS", "lab"),
                      mock.patch.object(imports, "_sources_map", return_value="homestead-sources"),
                      mock.patch.object(imports, "kget", K.kget),
                      mock.patch.object(imports, "ksend", K.ksend)):
            patch.start()
            self.addCleanup(patch.stop)

    def secret_of(self, name):
        return next(row["credential_secret"] for row in imports.list_sources() if row["name"] == name)

    def labelled(self):
        listing = K.kget(f"{SECRETS}?labelSelector=homestead.io/import-source")
        return sorted(row["metadata"]["name"] for row in listing["items"])

    def later(self):
        return time.time() + imports.SOURCE_SECRET_GRACE + 1

    def test_removing_a_source_removes_its_password(self):
        imports.add_source("source-a", "192.0.2.11", "root", "fixture-password")
        secret = self.secret_of("source-a")
        imports.del_source("source-a")
        self.assertIsNone(self.kube.object(f"{SECRETS}/{secret}"))
        self.assertEqual([], imports.list_sources())

    def test_a_helper_still_using_it_keeps_the_password_until_it_has_gone(self):
        for kind in ("Pod", "Job"):
            with self.subTest(kind=kind):
                imports.add_source("source-a", "192.0.2.11", "root", "fixture-password")
                secret = self.secret_of("source-a")
                path = self.kube.add(helper(kind, "homestead-import-app", secret))
                imports.del_source("source-a")
                self.assertIsNotNone(self.kube.object(f"{SECRETS}/{secret}"), "a running copy keeps its password")
                self.assertEqual([], imports.sweep_source_secrets(now=self.later()))
                K.ksend("DELETE", path)
                self.assertEqual([secret], imports.sweep_source_secrets(now=self.later()))
                self.assertIsNone(self.kube.object(f"{SECRETS}/{secret}"))

    def test_the_sweep_removes_passwords_no_source_names(self):
        imports.add_source("source-a", "192.0.2.11", "root", "fixture-password")
        imports.add_source("source-b", "192.0.2.12", "root", "fixture-password")
        kept = self.secret_of("source-a")
        orphan = self.secret_of("source-b")
        # An older Homestead removed source-b but left its password.
        inventory = self.kube.object(INVENTORY)
        rows = [row for row in json.loads(inventory["data"]["sources.json"]) if row["name"] != "source-b"]
        inventory["data"]["sources.json"] = json.dumps(rows)
        self.kube.add(inventory)
        self.kube.add({"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "homestead-other", "namespace": "lab"},
                       "stringData": {"password": "unrelated"}})
        self.assertEqual([], imports.sweep_source_secrets(), "one just made may still be on its way to the list")
        self.assertEqual([orphan], imports.sweep_source_secrets(now=self.later()))
        self.assertEqual([kept], self.labelled())
        self.assertIsNotNone(self.kube.object(f"{SECRETS}/homestead-other"))

    def test_an_add_the_inventory_refused_takes_its_password_back(self):
        self.kube.fail("/api/v1/namespaces/lab/configmaps", 409, method="POST")
        with self.assertRaises(urllib.error.HTTPError):
            imports.add_source("source-a", "192.0.2.11", "root", "fixture-password")
        self.assertEqual([], self.labelled())

    def test_an_add_whose_inventory_write_is_uncertain_leaves_it_to_the_sweep(self):
        # The write may have landed: the password is never taken back on a guess.
        real = K.ksend

        def timeout(method, path, body=None, **options):
            if "/configmaps" in path:
                raise TimeoutError("unknown")
            return real(method, path, body, **options)
        with mock.patch.object(imports, "ksend", timeout):
            with self.assertRaises(TimeoutError):
                imports.add_source("source-a", "192.0.2.11", "root", "fixture-password")
        self.assertEqual(1, len(self.labelled()))
        self.assertEqual(self.labelled(), imports.sweep_source_secrets(now=self.later()))

    def test_nothing_is_removed_when_the_inventory_cannot_be_read(self):
        imports.add_source("source-a", "192.0.2.11", "root", "fixture-password")
        self.kube.fail(INVENTORY, 503, times=5)
        with self.assertRaises(urllib.error.HTTPError):
            imports.sweep_source_secrets(now=self.later())
        self.assertEqual(1, len(self.labelled()))

    def test_nothing_is_removed_when_the_helpers_cannot_be_listed(self):
        imports.add_source("source-a", "192.0.2.11", "root", "fixture-password")
        secret = self.secret_of("source-a")
        self.kube.fail("/apis/batch/v1/namespaces/lab/jobs", 503, times=10)
        imports.del_source("source-a")
        self.assertIsNotNone(self.kube.object(f"{SECRETS}/{secret}"), "unknown users count as users")
        with self.assertRaises(urllib.error.HTTPError):
            imports.sweep_source_secrets(now=self.later())
        self.assertIsNotNone(self.kube.object(f"{SECRETS}/{secret}"))

    def test_removing_a_source_never_removes_a_secret_of_another_name(self):
        imports.add_source("source-a", "192.0.2.11", "root", "fixture-password")
        self.kube.add({"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "homestead-other", "namespace": "lab"},
                       "stringData": {"password": "unrelated"}})
        inventory = self.kube.object(INVENTORY)
        rows = json.loads(inventory["data"]["sources.json"])
        rows[0]["credential_secret"] = "homestead-other"
        inventory["data"]["sources.json"] = json.dumps(rows)
        self.kube.add(inventory)
        imports.del_source("source-a")
        self.assertIsNotNone(self.kube.object(f"{SECRETS}/homestead-other"))

    def test_the_leaders_minute_sweeps_them(self):
        import homestead_image_cache as CACHE
        with mock.patch.object(CACHE, "_load_scans"), mock.patch.object(CACHE, "_collect_scans", return_value=True), \
                mock.patch.object(CACHE, "prepull_status"), mock.patch.object(imports, "_drop_finished_copies"), \
                mock.patch.object(imports, "sweep_source_secrets") as sweep, \
                mock.patch.dict(sys.modules, {"homestead_image_cleanup": mock.Mock()}):
            CACHE.tick()
        sweep.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
