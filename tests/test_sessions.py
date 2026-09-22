"""A session should outlast a working day without outlasting a stolen laptop."""
import base64
import json
import sys
import time
import unittest
import urllib.error
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_auth as auth


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.store = {"users": {}, "signing_key": "k" * 48}
        auth.bind(self._get, self._send, "lab")
        auth._store_cache.update(at=0, data=None)
        auth.create_user("ada", "correct-horse-battery", first_only=True)

    def _get(self, path):
        if "/secrets/" in path:
            return {"metadata": {"name": path.rsplit("/", 1)[-1]},
                    "data": {"store.json": base64.b64encode(
                        json.dumps(self.store).encode()).decode()}}
        raise urllib.error.HTTPError(path, 404, "missing", {}, None)

    def _send(self, _method, _path, body=None, **_kw):
        raw = (body or {}).get("data", {}).get("store.json", "")
        if raw:
            self.store = json.loads(base64.b64decode(raw).decode())
        return body or {}

    def _claims(self, token):
        raw = token.rsplit(".", 1)[0]
        return json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))

    def test_an_ordinary_session_lasts_the_configured_idle_window(self):
        token = auth.issue_token("ada")

        claims = self._claims(token)
        self.assertFalse(claims["rem"])
        self.assertAlmostEqual(auth.SESSION_TTL, claims["exp"] - claims["iat"], delta=2)

    def test_a_remembered_session_lasts_far_longer(self):
        claims = self._claims(auth.issue_token("ada", remember=True))

        self.assertTrue(claims["rem"])
        self.assertAlmostEqual(auth.REMEMBER_TTL, claims["exp"] - claims["iat"], delta=2)
        self.assertGreater(auth.REMEMBER_TTL, auth.SESSION_TTL)

    def test_a_fresh_session_is_not_yet_worth_refreshing(self):
        self.assertFalse(auth.verify_token(auth.issue_token("ada"))["stale"])

    def test_a_session_past_halfway_asks_to_be_extended(self):
        """Refreshing on use is what stops anyone being signed out mid-task."""
        old = auth.issue_token("ada")
        claims = self._claims(old)
        claims["exp"] = int(time.time()) + int(auth.SESSION_TTL * 0.4)
        aged = self._resign(claims)

        self.assertTrue(auth.verify_token(aged)["stale"])

    def test_an_extension_does_not_move_the_absolute_deadline(self):
        started = int(time.time()) - 3600
        extended = auth.issue_token("ada", remember=True, started=started)

        self.assertEqual(started, self._claims(extended)["iat"],
                         "a refresh carries the original sign-in time forward")

    def test_a_session_older_than_the_cap_is_refused_however_active(self):
        claims = self._claims(auth.issue_token("ada", remember=True))
        claims["iat"] = int(time.time()) - auth.ABSOLUTE_TTL - 60
        claims["exp"] = int(time.time()) + 3600        # still well within idle

        self.assertIsNone(auth.verify_token(self._resign(claims)))

    def test_an_idle_session_still_expires(self):
        claims = self._claims(auth.issue_token("ada", remember=True))
        claims["exp"] = int(time.time()) - 1

        self.assertIsNone(auth.verify_token(self._resign(claims)))

    def test_signing_out_everywhere_kills_a_remembered_session(self):
        """The reason a long session is safe: it can always be revoked."""
        token = auth.issue_token("ada", remember=True)
        self.assertIsNotNone(auth.verify_token(token))

        auth.logout_everywhere("ada")

        self.assertIsNone(auth.verify_token(token))

    def test_a_changed_password_kills_a_remembered_session_too(self):
        token = auth.issue_token("ada", remember=True)

        auth.change_password("ada", "correct-horse-battery", "a-whole-new-passphrase")

        self.assertIsNone(auth.verify_token(token))

    def test_a_tampered_token_is_refused(self):
        claims = self._claims(auth.issue_token("ada"))
        claims["r"] = "admin"
        claims["u"] = "mallory"
        raw = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")

        self.assertIsNone(auth.verify_token(f"{raw}.not-a-real-signature"))

    def test_a_session_from_before_the_absolute_cap_existed_still_works(self):
        claims = self._claims(auth.issue_token("ada"))
        claims.pop("iat")
        claims.pop("rem")

        self.assertEqual("ada", auth.verify_token(self._resign(claims))["user"])

    def _resign(self, claims):
        raw = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
        return f"{raw}.{auth._sign(raw, auth._signing_key())}"


if __name__ == "__main__":
    unittest.main()
