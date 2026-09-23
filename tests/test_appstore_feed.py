"""The Community Applications feed, read the way Community Applications reads it."""
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import server


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
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FeedTests(unittest.TestCase):
    def setUp(self):
        server._cache.pop("appstore", None)
        self.addCleanup(server._cache.pop, "appstore", None)
        with mock.patch.object(server.urllib.request, "urlopen",
                               lambda *a, **k: Response(json.dumps(FEED).encode())):
            self.apps = server.fetch_appstore()

    def test_only_containers_are_offered(self):
        self.assertEqual({"Plain", "Spotlit", "Earlier spotlight", "Newest"}, {a["name"] for a in self.apps})

    def test_spotlights_come_newest_first_with_why(self):
        spot = server.rank_appstore(self.apps, "spotlight")
        self.assertEqual(["Spotlit", "Earlier spotlight"], [a["name"] for a in spot])
        self.assertEqual({"date": 1785556800, "month": "Aug 2026", "reason": "Calling all birders!",
                          "who": "Unraid Staff"}, spot[0]["spotlight"])
        self.assertEqual("Earlier", spot[1]["spotlight"]["reason"])

    def test_the_newest_template_leads_recently_added(self):
        self.assertEqual("Newest", server.rank_appstore(self.apps, "recent")[0]["name"])

    def test_an_app_carries_what_its_page_shows(self):
        app = next(a for a in self.apps if a["name"] == "Spotlit")
        self.assertEqual("ZappyZap", app["maintainer"])
        self.assertEqual({"project": "https://example.com/project", "support": "https://forums.example.com/t/1",
                          "video": "https://youtube.com/watch?v=x"}, app["links"])
        self.assertEqual("Line one\n\nBold and html\nLine two", app["overview"])

    def test_a_list_leaves_the_heavy_parts_for_the_page(self):
        summary = server.appstore_summary(next(a for a in self.apps if a["name"] == "Spotlit"))
        self.assertNotIn("overview", summary)
        self.assertNotIn("config", summary)
        self.assertIn("deploy", summary, "a card can still be configured straight away")


class MarkupTests(unittest.TestCase):
    def test_forum_codes_and_html_are_removed_but_brackets_that_mean_something_stay(self):
        text = "Media server.[br][b][span style='color: #E80000;']Converted[/span][/b] keeps [1] and [x86]"
        self.assertEqual("Media server. · Converted keeps [1] and [x86]", server.catalog_text(text))


if __name__ == "__main__":
    unittest.main()
