"""The Community Applications feed, read the way Community Applications reads it."""
import io
import json
import sys
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import server
import homestead_deploy as DEPLOY
import homestead_deploy_appstore as APPSTORE
import homestead_vm_server as VM_SERVER


def template(name, **fields):
    row = {"Name": name, "Repository": f"example/{name.lower()}:latest", "Overview": f"{name} does things",
           "FirstSeen": "1700000000", "Config": []}
    row.update(fields)
    return row


FEED = {"applist": [
    template("Plain"),
    template("GPU Statistics", Plugin=True, Repository="https://example.com/gpustat.plg"),
    template("Language Pack", Language="German"),
    template("Banned", Blacklist=True),
    template("Old", Deprecated=True),
    template("Spotlit", RecommendedDate="1785556800", RecommendedRaw="2026-08-01",
             RecommendedReason="{'en_US': 'Calling all birders!'}", RecommendedWho="Unraid Staff",
             Project="https://example.com/project", Support="https://forums.example.com/t/1",
             Video="https://youtube.com/watch?v=x", Maintainer={"Name": "ZappyZap"},
             Overview="Line one[br][br][b]Bold[/b] and <i>html</i>\r\nLine two"),
    template("Earlier spotlight", RecommendedDate="1782878400", RecommendedReason={"en_US": "Earlier"}),
    template("Newest", FirstSeen="1790000000"),
]}


class Response(io.BytesIO):
    """An answer handed out at most `piece` bytes at a time."""
    piece = 1 << 16
    headers = {}

    def read1(self, size=-1):
        return super().read1(min(self.piece, size if size >= 0 else self.piece))

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def reset_feed():
    """Forget the kept feed, for a test of its own."""
    with APPSTORE._feed_lock:
        APPSTORE._feed.update(source=None, apps=None, at=0.0, error=None, failed_at=0.0,
                              thread=None, done=None, reading=None)


def answer(feed=FEED, piece=1 << 16):
    response = Response(json.dumps(feed).encode() if not isinstance(feed, bytes) else feed)
    response.piece = piece
    return response


class FeedTests(unittest.TestCase):
    def setUp(self):
        self.clear()
        self.addCleanup(self.clear)
        with mock.patch.object(server.urllib.request, "urlopen", lambda *a, **k: answer()):
            self.apps = APPSTORE.load_feed_now()

    def test_only_containers_are_offered(self):
        self.assertEqual({"Plain", "Spotlit", "Earlier spotlight", "Newest"}, {a["name"] for a in self.apps})

    def test_spotlights_come_newest_first_with_why(self):
        spot = APPSTORE.rank_appstore(self.apps, "spotlight")
        self.assertEqual(["Spotlit", "Earlier spotlight"], [a["name"] for a in spot])
        self.assertEqual({"date": 1785556800, "month": "Aug 2026", "reason": "Calling all birders!",
                          "who": "Unraid Staff"}, spot[0]["spotlight"])
        self.assertEqual("Earlier", spot[1]["spotlight"]["reason"])

    def test_the_newest_template_leads_recently_added(self):
        self.assertEqual("Newest", APPSTORE.rank_appstore(self.apps, "recent")[0]["name"])

    def test_an_app_carries_what_its_page_shows(self):
        app = next(a for a in self.apps if a["name"] == "Spotlit")
        self.assertEqual("ZappyZap", app["maintainer"])
        self.assertEqual({"project": "https://example.com/project", "support": "https://forums.example.com/t/1",
                          "video": "https://youtube.com/watch?v=x"}, app["links"])
        self.assertEqual("Line one\n\nBold and html\nLine two", app["overview"])

    def test_a_list_leaves_the_heavy_parts_for_the_page(self):
        summary = APPSTORE.appstore_summary(next(a for a in self.apps if a["name"] == "Spotlit"))
        self.assertNotIn("overview", summary)
        self.assertNotIn("config", summary)
        self.assertIn("deploy", summary, "a card can still be configured straight away")

    def test_a_kept_app_holds_no_template_and_its_page_gets_the_deploy_form(self):
        app = next(a for a in self.apps if a["name"] == "Plain")
        self.assertNotIn("config", app, "the raw template is not kept")
        summary, detail = APPSTORE.appstore_summary(app), APPSTORE.appstore_detail(app)
        self.assertEqual("example/plain:latest", summary["deploy"]["image"])
        self.assertEqual(summary["deploy"], detail["deploy"])
        self.assertIn("overview", detail)
        self.assertNotIn("config", detail)
        summary["deploy"]["image"] = "changed"
        self.assertEqual("example/plain:latest", APPSTORE.appstore_summary(app)["deploy"]["image"],
                         "each answer gets its own copy")

    def test_the_app_page_answers_with_the_deploy_form_and_no_template(self):
        with mock.patch.object(APPSTORE, "fetch_appstore", return_value=self.apps):
            key = APPSTORE.appstore_key(next(a for a in self.apps if a["name"] == "Plain"))
            page = DEPLOY.appstore_app(mock.Mock(query={"key": [key]}))
        self.assertEqual("example/plain:latest", page["deploy"]["image"])
        self.assertNotIn("config", page)

    clear = staticmethod(lambda: reset_feed())


class StreamTests(unittest.TestCase):
    """The feed is turned into apps as it arrives, never held whole."""

    def setUp(self):
        reset_feed()
        self.addCleanup(reset_feed)

    def load(self, feed, piece=1 << 16):
        with mock.patch.object(server.urllib.request, "urlopen", lambda *a, **k: answer(feed, piece)):
            return APPSTORE.load_feed_now()

    def test_the_apps_are_the_same_however_small_the_pieces(self):
        whole = self.load(FEED)
        for piece in (1, 2, 7, 64):
            with self.subTest(piece=piece):
                self.assertEqual(whole, self.load(FEED, piece))

    def test_keys_before_and_after_the_app_list_are_passed_over(self):
        feed = {"apps": 123456, "last_updated": "2026-10-27", "categories": [{"Cat": "AI:"}],
                "applist": FEED["applist"], "deprecated": {"a/b": "old"}, "count": 9.5e3, "ok": True, "none": None}
        self.assertEqual(self.load(FEED), self.load(feed, 3))

    def test_a_feed_that_is_only_a_list_is_read_too(self):
        self.assertEqual(self.load(FEED), self.load(FEED["applist"], 5))

    def test_a_cut_off_feed_is_refused(self):
        body = json.dumps(FEED).encode()
        with self.assertRaises(ValueError):
            self.load(body[:len(body) // 2], 7)

    def test_a_feed_larger_than_the_cap_is_refused(self):
        with mock.patch.object(APPSTORE, "FEED_MAX_BYTES", 100):
            with self.assertRaisesRegex(ValueError, "larger than"):
                self.load(FEED)

    def test_a_template_that_is_not_an_object_is_skipped(self):
        feed = {"applist": [1, "two", None, template("Plain")]}
        self.assertEqual(["Plain"], [a["name"] for a in self.load(feed, 4)])


class Gate:
    """A feed read that waits until the test lets it finish."""

    def __init__(self, test, result=None):
        self.opened, self.release, self.threads = threading.Event(), threading.Event(), []
        self.result = result
        test.addCleanup(self.release.set)

    def __call__(self, *args, **kwargs):
        self.threads.append(threading.get_ident())
        self.opened.set()
        self.release.wait(5)
        if isinstance(self.result, Exception):
            raise self.result
        return answer(self.result or FEED)


class BackgroundTests(unittest.TestCase):
    """A page never reads the feed itself: one background read at a time does."""

    def setUp(self):
        reset_feed()
        self.addCleanup(reset_feed)
        self.addCleanup(self.settle)
        patch = mock.patch.object(APPSTORE, "catalog_source", return_value="https://feed.example.com/a.json")
        self.source = patch.start()
        self.addCleanup(patch.stop)

    def settle(self):
        thread = APPSTORE._feed.get("thread")
        if thread:
            thread.join(5)

    def open(self, opener):
        patch = mock.patch.object(server.urllib.request, "urlopen", opener)
        patch.start()
        self.addCleanup(patch.stop)

    def test_a_page_before_the_first_feed_is_told_it_is_loading(self):
        gate = Gate(self)
        self.open(gate)
        with mock.patch.object(APPSTORE, "FEED_WAIT", 0.05):
            reply = DEPLOY.appstore(mock.Mock(query={}))
        self.assertEqual(503, reply.code)
        self.assertTrue(reply.body["loading"])
        self.assertIn("loading", reply.body["error"])
        self.assertIn(("Retry-After", "2"), reply.headers)
        self.assertTrue(gate.opened.wait(5))
        self.assertNotIn(threading.get_ident(), gate.threads, "the feed is read off the request thread")
        gate.release.set()
        self.settle()
        page = DEPLOY.appstore(mock.Mock(query={}))
        self.assertEqual(4, page["total"])

    def test_pages_waiting_together_start_one_read(self):
        gate = Gate(self)
        self.open(gate)
        for _ in range(3):
            with self.assertRaises(APPSTORE.FeedLoading):
                APPSTORE.fetch_appstore(wait=0)
        self.assertTrue(gate.opened.wait(5))
        self.assertEqual(1, len(gate.threads))

    def test_a_first_read_that_ends_soon_is_waited_for(self):
        self.open(lambda *a, **k: answer())
        self.assertEqual(4, len(APPSTORE.fetch_appstore(wait=5)))

    def test_an_old_feed_is_served_at_once_while_a_new_one_is_read(self):
        self.open(lambda *a, **k: answer())
        old = APPSTORE.fetch_appstore(wait=5)
        APPSTORE._feed["at"] -= APPSTORE.FEED_TTL + 1
        newer = {"applist": FEED["applist"] + [template("Fresh")]}
        gate = Gate(self, newer)
        self.open(gate)
        started = time.monotonic()
        self.assertIs(old, APPSTORE.fetch_appstore(wait=5))
        self.assertLess(time.monotonic() - started, 1, "the old feed does not wait on the new one")
        self.assertTrue(gate.opened.wait(5))
        gate.release.set()
        self.settle()
        self.assertIn("Fresh", {a["name"] for a in APPSTORE.fetch_appstore(wait=0)})

    def test_a_feed_that_fails_is_a_502_and_is_not_asked_again_at_once(self):
        calls = []

        def offline(*args, **kwargs):
            calls.append(1)
            raise OSError("offline")

        self.open(offline)
        reply = DEPLOY.appstore(mock.Mock(query={}))
        self.assertEqual(502, reply.code)
        self.assertIn("app feed unavailable", reply.body["error"])
        self.assertEqual(502, DEPLOY.appstore(mock.Mock(query={})).code)
        self.assertEqual(1, len(calls), "a failed read is not repeated on every page")

    def test_a_new_catalogue_address_lets_the_old_feed_go(self):
        self.open(lambda *a, **k: answer())
        APPSTORE.fetch_appstore(wait=5)
        self.source.return_value = "https://feed.example.com/b.json"
        gate = Gate(self)
        self.open(gate)
        with self.assertRaises(APPSTORE.FeedLoading):
            APPSTORE.fetch_appstore(wait=0)
        self.assertIsNone(APPSTORE._feed.get("apps"), "the other address's apps are not kept")

    def test_a_settings_save_during_a_read_starts_no_second_one(self):
        gate = Gate(self)
        self.open(gate)
        with self.assertRaises(APPSTORE.FeedLoading):
            APPSTORE.fetch_appstore(wait=0)
        self.assertTrue(gate.opened.wait(5))
        missing = urllib.error.HTTPError("/api/v1", 404, "Not Found", {}, io.BytesIO(b"{}"))
        with mock.patch.object(server.KUBE_HELPERS, "kget", side_effect=missing), \
             mock.patch.object(server.KUBE_HELPERS, "ksend"), \
             mock.patch.object(server.SETTINGS_HELPERS.CHECKS, "invalidate"), \
             mock.patch.object(server.SETTINGS_HELPERS, "_fleet_rename"):
            for name in ("Barn", "Shed", "Loft"):
                server.SETTINGS_HELPERS.save_app_settings({"site_name": name, "revision": ""})
                with self.assertRaises(APPSTORE.FeedLoading):
                    APPSTORE.fetch_appstore(wait=0)
        self.assertEqual(1, len(gate.threads), "one read at a time, whatever is saved meanwhile")
        gate.release.set()
        self.settle()
        self.assertEqual(4, len(APPSTORE.fetch_appstore(wait=0)), "and the read's apps are kept")

    def test_an_old_feed_whose_refresh_failed_is_not_read_again_on_every_page(self):
        self.open(lambda *a, **k: answer())
        old = APPSTORE.fetch_appstore(wait=5)
        APPSTORE._feed["at"] -= APPSTORE.FEED_TTL + 1
        calls = []

        def offline(*args, **kwargs):
            calls.append(1)
            raise OSError("offline")

        self.open(offline)
        self.assertIs(old, APPSTORE.fetch_appstore(wait=0))
        self.settle()
        for _ in range(10):
            self.assertIs(old, APPSTORE.fetch_appstore(wait=0), "the old feed is still answered")
            self.settle()
        self.assertEqual(1, len(calls))
        APPSTORE._feed["failed_at"] -= APPSTORE.FEED_RETRY + 1
        APPSTORE.fetch_appstore(wait=0)
        self.settle()
        self.assertEqual(2, len(calls), "asked again once the pause is over")

    def test_a_new_address_reads_at_once_and_the_old_addresss_read_is_dropped(self):
        first = Gate(self)
        self.open(first)
        with self.assertRaises(APPSTORE.FeedLoading):
            APPSTORE.fetch_appstore(wait=0)
        self.assertTrue(first.opened.wait(5))
        old_thread = APPSTORE._feed["thread"]
        self.source.return_value = "https://feed.example.com/b.json"
        second = Gate(self, {"applist": [template("Other")]})
        self.open(second)
        with self.assertRaises(APPSTORE.FeedLoading):
            APPSTORE.fetch_appstore(wait=0)
        self.assertTrue(second.opened.wait(5), "the new address does not wait for the old one")
        first.release.set()
        old_thread.join(5)
        self.assertIsNone(APPSTORE._feed["apps"], "the old address's apps are not kept")
        second.release.set()
        self.settle()
        self.assertEqual(["Other"], [a["name"] for a in APPSTORE.fetch_appstore(wait=0)])

    def test_each_page_gets_an_error_of_its_own(self):
        self.open(mock.Mock(side_effect=OSError("offline")))
        errors = []
        for _ in range(2):
            with self.assertRaises(Exception) as caught:
                APPSTORE.fetch_appstore(wait=5)
            errors.append(caught.exception)
        self.assertIsNot(errors[0], errors[1])
        self.assertIn("offline", str(errors[1]))

    def test_the_logo_picker_says_loading_too(self):
        with mock.patch.object(APPSTORE, "fetch_appstore", side_effect=APPSTORE.FeedLoading("still loading")):
            reply = VM_SERVER._logos_missing(mock.Mock(query={}))
        self.assertEqual(503, reply.code)
        self.assertTrue(reply.body["loading"])


class SourceTests(unittest.TestCase):
    def test_the_catalogue_can_come_from_another_feed(self):
        settings = server.APP_SETTINGS.validate({"catalog_url": "https://mirror.example.com/feed.json"})
        self.assertEqual("https://mirror.example.com/feed.json", settings["catalog_url"])
        self.assertEqual("", server.APP_SETTINGS.validate({})["catalog_url"], "blank means the public feed")
        for bad in ("ftp://example.com/feed", "file:///etc/passwd", "https://user:pw@example.com/feed", "not a url"):
            with self.subTest(bad):
                with self.assertRaises(ValueError):
                    server.APP_SETTINGS.validate({"catalog_url": bad})

    def test_the_feed_in_use_is_the_one_set_else_the_default(self):
        with mock.patch.object(server.SETTINGS_HELPERS, "get_app_settings", return_value={"catalog_url": ""}):
            server._cache.pop("settings", None)
            self.assertEqual(APPSTORE.CA_FEED, APPSTORE.catalog_source())
        with mock.patch.object(server.SETTINGS_HELPERS, "get_app_settings", return_value={"catalog_url": "https://m.example/f.json"}):
            server._cache.pop("settings", None)
            self.assertEqual("https://m.example/f.json", APPSTORE.catalog_source())
        server._cache.pop("settings", None)


class MarkupTests(unittest.TestCase):
    def test_forum_codes_and_html_are_removed_but_brackets_that_mean_something_stay(self):
        text = "Media server.[br][b][span style='color: #E80000;']Converted[/span][/b] keeps [1] and [x86]"
        self.assertEqual("Media server. · Converted keeps [1] and [x86]", APPSTORE.catalog_text(text))


if __name__ == "__main__":
    unittest.main()
