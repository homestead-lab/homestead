"""A review that is only too old says so (the image update queue, Nov 2026).

An update queue applied its first app 25 minutes after the review. The token
had run out, and the refusal read as if placement or memory had changed. It
now says the review is older than its time, and that nothing changed. A token
for another input, or one never confirmed, keeps the old words."""
import os
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.environ.get("HOMESTEAD_TEST_SERVER_DIR", str(Path(__file__).resolve().parents[1] / "server")))
import homestead_capacity_review as review

PLAN = {"requires_confirmation": True}
CONTEXT = {"action": "image-update", "uid": "u1", "settled": "abc"}


class ExpiredReviewTests(unittest.TestCase):
    def setUp(self):
        self.key = review._key
        review.bind(lambda: b"test-key")

    def tearDown(self):
        review._key = self.key

    def config(self, at):
        body = {"ns": "lab", "name": "crafty", "action": "update"}
        with mock.patch.object(review.time, "time", return_value=at):
            token = review.issue(body, CONTEXT)
        return {**body, "capacity_token": token, "confirm_capacity": True}

    def refusal(self, config, at, context=CONTEXT):
        with mock.patch.object(review.time, "time", return_value=at):
            with self.assertRaises(review.Rejected) as caught:
                review.enforce(config, PLAN, context)
        return str(caught.exception)

    def test_a_fresh_review_is_accepted(self):
        config = self.config(1000)
        with mock.patch.object(review.time, "time", return_value=1000 + review.TTL - 1):
            review.enforce(config, PLAN, CONTEXT)

    def test_an_old_review_says_it_is_old(self):
        said = self.refusal(self.config(1000), 1000 + 25 * 60)
        self.assertTrue(said.startswith("Placement or memory needs a fresh review"), said)
        self.assertIn("more than 10 minutes old", said)
        self.assertIn("Nothing has changed", said)

    def test_a_changed_input_keeps_the_general_words(self):
        said = self.refusal(self.config(1000), 1000 + 25 * 60, {**CONTEXT, "settled": "changed"})
        self.assertEqual(said, "Placement or memory needs a fresh review and explicit acknowledgement before deploying.")
        said = self.refusal(self.config(1000), 1000 + 60, {**CONTEXT, "settled": "changed"})
        self.assertNotIn("minutes old", said)

    def test_an_old_review_not_confirmed_keeps_the_general_words(self):
        config = {**self.config(1000), "confirm_capacity": False}
        self.assertNotIn("minutes old", self.refusal(config, 1000 + 25 * 60))


class QueueRechecksBeforeTheTokenRunsOutTests(unittest.TestCase):
    """The update queue reviews an app again once its review is REVIEW_FRESH_MS
    old. That must stay well under the server's TTL, or a long queue applies
    tokens the server has already let run out, as it did in Nov 2026."""

    def test_the_queue_rechecks_well_before_the_server_ttl(self):
        source = (Path(__file__).resolve().parents[1] / "web" / "js" / "views-workloads-updates.js").read_text(encoding="utf-8")
        match = re.search(r"const REVIEW_FRESH_MS = (\d+) \* 60 \* 1000;", source)
        self.assertIsNotNone(match, "REVIEW_FRESH_MS is no longer written as N * 60 * 1000; update this test")
        fresh_seconds = int(match.group(1)) * 60
        self.assertLessEqual(fresh_seconds, review.TTL // 2,
                             f"The queue re-checks after {fresh_seconds}s, but the server keeps a review for only {review.TTL}s")


if __name__ == "__main__":
    unittest.main()
