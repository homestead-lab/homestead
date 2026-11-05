"""A variable named like a password but written as one word (DBPASS,
ROOTPASS, SMTPPASS) is a secret too: Deploy and Edit keep it in the app's
Secret, and the YAML for git shows only the Secret's key (#920 review).

The ordinary words that end in PASS (BYPASS, COMPASS, ...) are not caught.
Nothing moves until the app is saved: there is no upgrade step that reads
saved apps and rewrites them.
"""
import base64
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_env_secrets as ENVSEC

CAUGHT = ("DBPASS", "ROOTPASS", "SMTPPASS", "MYSQL_ROOTPASS", "ADMINPASS", "dbPass", "MailPass",
          # caught before, still caught
          "PASS", "DB_PASS", "BY_PASS", "PASSWORD", "SMTP_PASSWD")
ORDINARY = ("BYPASS", "PROXY_BYPASS", "CACHE_BYPASS", "COMPASS", "ENCOMPASS", "OVERPASS", "UNDERPASS",
            "TRESPASS", "SURPASS", "IMPASS", "PASSIVE", "PASSTHRU", "PASS_THROUGH", "COMPASS_URL")


def deployment(env):
    return {"metadata": {"name": "demo", "namespace": "lab"},
            "spec": {"template": {"spec": {"containers": [
                {"name": "demo", "env": [{"name": k, "value": v} for k, v in env.items()]}]}}}}


class OneWordPasswords(unittest.TestCase):
    def test_names_ending_in_pass_are_secrets_and_ordinary_words_are_not(self):
        self.assertEqual([], [name for name in CAUGHT if not ENVSEC.secretish(name)])
        self.assertEqual([], [name for name in ORDINARY if ENVSEC.secretish(name)])

    def test_a_deploy_keeps_a_one_word_password_in_the_apps_secret(self):
        dep, sent = deployment({"DBPASS": "typed-value", "COMPASS_URL": "https://example.com"}), []
        def read(path):
            import urllib.error
            raise urllib.error.HTTPError(path, 404, "missing", {}, None)
        ENVSEC.externalize("lab", dep, read, lambda *call: sent.append(call))
        (method, path, secret), = sent
        self.assertEqual(("POST", "/api/v1/namespaces/lab/secrets"), (method, path))
        self.assertEqual({"demo.DBPASS": base64.b64encode(b"typed-value").decode()}, secret["data"])
        env = {item["name"]: item for item in dep["spec"]["template"]["spec"]["containers"][0]["env"]}
        self.assertEqual({"secretKeyRef": {"name": "demo-env", "key": "demo.DBPASS"}}, env["DBPASS"]["valueFrom"])
        self.assertNotIn("value", env["DBPASS"])
        self.assertEqual("https://example.com", env["COMPASS_URL"]["value"])

    def test_an_edit_review_shows_a_one_word_password_as_a_reference(self):
        current = deployment({"ROOTPASS": "old"})
        proposed = deployment({"ROOTPASS": "new-value", "BYPASS": "1"})
        secret = ENVSEC.as_saved(proposed, current)
        self.assertEqual({"name": "demo-env", "keys": ["demo.ROOTPASS"]}, secret)
        self.assertNotIn("new-value", str(proposed))
        self.assertIn({"name": "BYPASS", "value": "1"}, proposed["spec"]["template"]["spec"]["containers"][0]["env"])



class EditSaveMovesAPlainOneWordPassword(unittest.TestCase):
    """An app saved before the rule, with DBPASS as a plain value: its next Edit
    save keeps the value in the app's Secret, writing the Secret before the
    Deployment, and the pods restart (the template changes). The review says
    "Restart it now" for every edit, and its YAML shows the reference."""

    def test_the_secret_is_written_before_the_deployment_and_the_template_changes(self):
        import copy, json
        import homestead_lifecycle as lifecycle
        import homestead_workload_edit as WORKLOAD_EDIT
        import test_env_secrets as FIXTURES
        store = FIXTURES.Store()
        path = "/apis/apps/v1/namespaces/lab/deployments/tunnel"
        store.objects[path] = FIXTURES.deployment([{"name": "DBPASS", "value": "legacy"}, {"name": "TZ", "value": "UTC"}])
        before = copy.deepcopy(store.objects[path])
        # put back what server.py bound, or the next file's lifecycle reads go to this fake
        self.addCleanup(vars(lifecycle).update, {n: getattr(lifecycle, n) for n in ("kget", "ksend", "SYS_NS", "_cache", "hardware_features", "create_pvc", "STORAGE_CLASS")})
        lifecycle.bind(store.read, store.send, set(), {}, lambda: [], lambda *a, **k: None, "longhorn-r2")
        WORKLOAD_EDIT.edit_workload({"ns": "lab", "name": "tunnel", "containers": [
            {"original_name": "cloudflared", "name": "cloudflared", "env": {"DBPASS": "legacy", "TZ": "UTC"}}]})
        order = [(method, sent_path) for method, sent_path, *_ in store.sent]
        secret = next(i for i, (method, sent_path) in enumerate(order) if "/secrets" in sent_path and method in ("POST", "PUT"))
        deployment = order.index(("PUT", path))
        self.assertLess(secret, deployment)
        saved = store.sent[deployment][2]
        self.assertNotIn("legacy", json.dumps(saved))
        self.assertTrue(WORKLOAD_EDIT.restarts(before, saved))


if __name__ == "__main__":
    unittest.main()
