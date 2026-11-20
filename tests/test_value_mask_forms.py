"""Addresses as a role below reveal sees them, in a few more forms.

- A login holding a space, or a login a YAML value folds onto a more
  indented next line, is masked up to its "@" like any other login.
- A query or fragment piece that is one code-like word (8 or more letters
  and digits, mixed) hides the rest of the address; short anchors and
  harmless parameters still show.
- Every form hides at least what it hid before: no character that was
  masked is shown now."""
import itertools
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_value_masks as MASKS

strip = MASKS.strip_url_secrets
HIDDEN = MASKS.HIDDEN


def _as_before():
    """The masks with the two new rules switched off."""
    # create: a module without these rules has nothing to switch off.
    spans = mock.patch.object(MASKS, "_spans", lambda text: ((start, end, False) for start, end in MASKS.tokens(text)), create=True)
    code = mock.patch.object(MASKS, "_code_like", lambda piece: False, create=True)

    class Both:
        def __enter__(self):
            spans.__enter__()
            code.__enter__()

        def __exit__(self, *info):
            code.__exit__(*info)
            spans.__exit__(*info)
    return Both()


def _before(*texts):
    """What the masks showed before these forms were added: the address ends
    at its first whitespace and a fragment or query piece is checked only by
    the older rules."""
    with _as_before():
        shown = [strip(text) for text in texts]
    return shown[0] if len(texts) == 1 else shown


def _shows_no_more(now, before):
    """Whether now could be made from before by hiding more of it: each part
    of now outside a mask is a part of before, in the same order."""
    pieces = now.split(HIDDEN)
    if len(pieces) == 1:
        return now == before
    first, middle, last = pieces[0], pieces[1:-1], pieces[-1]
    if not before.startswith(first) or not before.endswith(last) or len(first) + len(last) > len(before):
        return False
    at, limit = len(first), len(before) - len(last)
    for piece in middle:
        found = before.find(piece, at, limit)
        if found < 0:
            return False
        at = found + len(piece)
    return True


class Forms(unittest.TestCase):
    def assertMasked(self, given, *secrets):
        once = strip(given)
        for secret in secrets:
            self.assertNotIn(secret, once)
        self.assertEqual(once, strip(once), "masking twice changes nothing")
        self.assertTrue(MASKS.has_stripped(once))
        return once

    def test_a_login_holding_a_space_is_masked_up_to_its_at(self):
        self.assertEqual("https://•••@h.example.com/x", self.assertMasked("https://user:my pass@h.example.com/x", "pass", "my"))
        self.assertEqual("failed: https://•••@h.example.com/x next",
                         self.assertMasked("failed: https://u:ab cd ef@h.example.com/x next", "cd", "ef"))
        # The part before the space reads as a host and port: masked all the same.
        self.assertEqual("https://•••@h.example.com", self.assertMasked("https://user:12 34@h.example.com", "34", "user"))
        self.assertEqual("--url=https://•••@h.example.com/x", self.assertMasked("--url=https://u:p q@h.example.com/x", "p q"))

    def test_a_login_folded_onto_a_more_indented_line_is_masked(self):
        text = "url: https://user:my\n  pass@h.example.com/x\nnext: 1"
        self.assertEqual("url: https://•••@h.example.com/x\nnext: 1", self.assertMasked(text, "pass", "user"))
        crlf = "url: https://user:my\r\n    long\r\n    pass@h.example.com/x\r\nnext: 1"
        self.assertEqual("url: https://•••@h.example.com/x\r\nnext: 1", self.assertMasked(crlf, "pass", "long"))

    def test_the_next_line_at_the_same_indent_is_left_as_it_was(self):
        for text in ("url: https://h.example.com:8443\nmail: admin@example.com",
                     "  a: https://h.example.com:8443\n  b: admin@example.com",
                     "https://h.example.com:8443\n\n  admin@example.com"):
            with self.subTest(text=text):
                self.assertEqual(_before(text), strip(text))
                self.assertIn("admin@example.com", strip(text))

    def test_an_address_with_a_path_or_a_login_already_is_unchanged(self):
        for text in ("https://h.example.com:8443/x and mail admin@example.com",
                     "https://•••@h.example.com/x by admin@example.com",
                     "https://h.example.com/ ok", "see https://h.example.com:8443 now"):
            with self.subTest(text=text):
                self.assertEqual(_before(text), strip(text))

    def test_a_later_address_on_the_line_keeps_its_own_rule(self):
        # The reach of a login ends where the next address starts: each is masked by its own rule.
        for text in ("https://h.example.com:8443 gotify://tok@x.example.com/a",
                     "https://h.example.com:8443 note gotify://x.example.com/a@b",
                     "https://h.example.com:8443 x@gotify://tok@y.example.com",
                     "url: https://h.example.com:8443\n  gotify://tok@x.example.com/a",
                     "https://h:1 pbul://tok@x https://u:p@y/z"):
            with self.subTest(text=text):
                now, then = strip(text), _before(text)
                self.assertTrue(_shows_no_more(now, then), f"now {now!r}, before {then!r}")
                self.assertNotIn("x.example.com/a", now)
                self.assertNotIn("tok", now)

    def test_a_word_starting_with_its_at_is_not_part_of_a_login(self):
        for text in ("Listening on http://198.51.100.7:8080 using @next/swc", "a: https://h.example.com:8443\n  @scope/pkg x@y",
                     "https://h:1\t@tcp(db)/a", "https://h:1 @@x"):
            with self.subTest(text=text):
                self.assertEqual(_before(text), strip(text))
                self.assertEqual(strip(text), strip(strip(text)), "masking twice changes nothing")

    def test_a_login_reaches_a_word_that_starts_with_its_at(self):
        for text in ("https://user:ab cd @h.example.com/x", "https://user:ab\n  cd @h.example.com/x", "https://u:ab\t@tcp(db)/a"):
            with self.subTest(text=text):
                once = self.assertMasked(text, "cd", "user", "ab")
                self.assertTrue(_shows_no_more(once, _before(text)))

    def test_another_address_inside_the_reach_hides_the_whole_stretch(self):
        for text, wanted in (("https://user:ab a:x://y cd@h.example.com/x next", "https://••• next"),
                             ("https://user:ab gotify://k cd@h.example.com", "https://•••"),
                             ("--url=https://user:ab x@pbul://k@h", "--url=https://•••"),
                             ("https://user:ab\n  x:\\y cd@h", "https://•••")):
            with self.subTest(text=text):
                self.assertEqual(wanted, self.assertMasked(text, "cd", "user"))
                self.assertTrue(_shows_no_more(wanted, _before(text)))

    def test_an_address_inside_a_word_ends_the_reach(self):
        for text in ("https://h.example.com:8443 a:x://y b@c", "https://h:1 •••://••• b@c", "https://h:1 x:\\y b@c"):
            with self.subTest(text=text):
                once = strip(text)
                self.assertEqual(once, strip(once), "masking twice changes nothing")
                self.assertTrue(_shows_no_more(once, _before(text)))

    def test_a_code_like_fragment_or_query_piece_is_hidden(self):
        for given, wanted in (("https://h.example.com/#AbC1dEf2GhI3jK", "https://h.example.com/#•••"),
                              ("https://h.example.com/p?AbC1dEf2", "https://h.example.com/p?•••"),
                              ("https://h.example.com/?page=2&x9y8z7w6", "https://h.example.com/?page=2&•••"),
                              ("https://h.example.com/#12345abc&page=2", "https://h.example.com/#•••")):
            with self.subTest(given=given):
                self.assertEqual(wanted, self.assertMasked(given, "AbC1", "x9y8", "12345"))

    def test_anchors_and_harmless_parameters_still_show(self):
        for shown in ("https://h.example.com/docs#install", "https://h.example.com/blob/main/a.py#L42",
                      "https://h.example.com/?page=2", "https://h.example.com/#section-2b",
                      "https://h.example.com/#Ab1cdEf", "https://h.example.com/#installation",
                      "https://h.example.com/#20261113", "https://h.example.com/?v2"):
            with self.subTest(shown=shown):
                self.assertEqual(shown, strip(shown))

    def test_cutting_before_masking_never_shows_the_rest_of_a_login(self):
        for text in ("failed: https://u:ab cd@h.example.com/x more", "a: https://u:ab\n  cd@h.example.com/x\nb: c"):
            for length in range(len(text) + 2):
                with self.subTest(text=text, length=length):
                    self.assertNotIn("cd", strip(MASKS.cut(text, length)))

    def test_a_viewers_copy_of_a_login_with_a_space_saves_back(self):
        stored = {"url": "https://u:my pass@h.example.com/x", "env": [{"name": "A", "value": "https://h.example.com/#AbC1dEf2"}]}
        given = {"url": strip(stored["url"]), "env": [{"name": "A", "value": strip(stored["env"][0]["value"])}]}
        self.assertEqual(["url", "env[0].value"], MASKS.restore(given, stored))
        self.assertEqual(stored, given)

    def test_many_addresses_on_long_lines_are_checked_in_linear_time(self):
        n = 200_000
        texts = ["https://h:1 " * (n // 12) + "@", "https://h:1\n " * (n // 13) + "@",
                 "".join("https://h:1\n" + " " * i for i in range(600)) + "@",
                 ("https://h:1 x\n" * (n // 14)) + "a@b", "https://h/#" + "a1" * (n // 2),
                 # Many short addresses, then one long word holding an "@".
                 "https://h:1 " * 2000 + "a@" + "x" * (n + 20_000), "https://h:1 " * 2000 + "a:x://y " + "a@" + "x" * n]
        for text in texts:
            started = time.monotonic()
            stripped = strip(text)
            MASKS.has_stripped(stripped)
            MASKS.cut(text, len(text) // 2)
            self.assertLess(time.monotonic() - started, 1.5, text[:40])


def _shown_at(output, text):
    """The places of text that output shows, reading output as text with
    some stretches replaced by the mask (each piece outside a mask at the
    first place it fits after the one before); None when output is not that
    (a JSON text written back compact)."""
    pieces = output.split(HIDDEN)
    if len(pieces) == 1:
        return set(range(len(text))) if output == text else None
    first, last = pieces[0], pieces[-1]
    at, limit = len(first), len(text) - len(last)
    if not text.startswith(first) or not text.endswith(last) or at > limit:
        return None
    shown = set(range(at)) | set(range(limit, len(text)))
    for piece in pieces[1:-1]:
        found = text.find(piece, at, limit)
        if found < 0:
            return None
        shown.update(range(found, found + len(piece)))
        at = found + len(piece)
    return shown


def _shows_no_place_more(text, now, before):
    """Whether now shows no place of text that before hides; where either is
    not text masked in place, whether now could be made from before by
    hiding more of it."""
    places, then = _shown_at(now, text), _shown_at(before, text)
    if places is None or then is None:
        return _shows_no_more(now, before)
    return places <= then


# Parts of a wide set of addresses; every combination is masked both ways.
SCHEMES = ("https", "http", "postgres", "gotify", "ssh")
FOLDED = "u:a" + "\n  b" * getattr(MASKS, "FOLD_LINES", 8) + "@"
LOGINS = ("", "u@", "u:p@", "u:my pass@", "u:12 34@", "u:a\tb c@", "u:a\n  b@", "u:a\nb@", "u:a\n\n  b@", "@",
          FOLDED, FOLDED.replace("@", "\n  c@"))
HOSTS = ("h.example.com", "h.example.com:8443", "198.51.100.7:80", "[2001:db8::1]:443")
PATHS = ("", "/", "/x/y", "/hooks/abc", "/a b")
TAILS = ("", "?page=2", "?token=t", "?AbC1dEf2", "#install", "#AbC1dEf2GhI3", "?a=1&B2c3D4e5f", "#L42", "#x y@z")
AROUND = (("", ""), ("see ", " then"), ("url: ", "\nnext: a@b.example.com"), ('{"u":"', '"}'),
          ("  - ", "\n    more@x.example.com"), ("a ", "\tb c@d"), ("x\n  ", "\n  y@z"),
          # A second address of another scheme after it, on the line and folded, and one before it.
          ("", " gotify://tok@x.example.com/a"), ("", "\n  pbul://k@x/y z@w"), ("", " t@pover://k@x"),
          ("https://h.example.com:8443 ", " a@b"), ("url: https://h:1\n  ", "\n  ssh://u:p@x"),
          ("", " https://u:p@x/hooks/abc c@d"), ("", " x //u:p@h/y and u:p@tcp(db)/a"),
          # An address inside a word between it and a later "@", and a word that starts with its "@".
          ("", " a:x://y b@c"), ("", "\n  q:u://v w@z"), ("", " @scope/pkg x@y"))


class OnlyMoreIsHidden(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.texts = [f"{before}{scheme}://{login}{host}{path}{tail}{after}" for scheme, login, host, path, tail, (before, after)
                     in itertools.product(SCHEMES, LOGINS, HOSTS, PATHS, TAILS, AROUND)]
        cls.before = _before(*cls.texts)
        cls.now = [strip(text) for text in cls.texts]

    def test_no_form_shows_a_place_that_was_hidden_before(self):
        changed = 0
        for text, now, then in zip(self.texts, self.now, self.before):
            changed += now != then
            if not _shows_no_place_more(text, now, then):
                self.fail(f"{text!r}: now {now!r}, before {then!r}")
        self.assertGreater(len(self.texts), 100_000)
        self.assertGreater(changed, 1_000, "the new forms are reached")

    def test_masking_twice_changes_nothing(self):
        for text, now in zip(self.texts, self.now):
            if strip(now) != now:
                self.fail(f"{text!r}: once {now!r}, twice {strip(now)!r}")

    def test_a_cut_text_shows_no_character_that_was_hidden_before(self):
        texts = [f"{before}{scheme}://{login}{host}{path}{tail}{after}" for scheme, login, host, path, tail, (before, after)
                 in itertools.product(SCHEMES, LOGINS[:10], HOSTS[:3], PATHS[:3], TAILS[::2], AROUND[::2])]
        cuts = [(text, length) for text in texts for length in range(0, len(text) + 1, 5)]
        with _as_before():
            before = [strip(MASKS.cut(text, length)) for text, length in cuts]
        for (text, length), then in zip(cuts, before):
            now = strip(MASKS.cut(text, length))
            # The cut mark stands for what was left out, so it counts as a mask here.
            if not _shows_no_more(now.replace("…", HIDDEN), then.replace("…", HIDDEN)):
                self.fail(f"{text!r} cut at {length}: now {now!r}, before {then!r}")
        self.assertGreater(len(cuts), 20_000)

    def test_the_comparisons_themselves_tell_more_from_less(self):
        self.assertTrue(_shows_no_more("https://•••@h", "https://u:p •••@h"))
        self.assertTrue(_shows_no_more("a •••", "a b c"))
        self.assertFalse(_shows_no_more("https://u:p@h", "https://•••@h"))
        self.assertFalse(_shows_no_more("a ••• secret", "a ••• b"))
        self.assertFalse(_shows_no_more("x", "y"))
        # By place: a piece shown elsewhere does not excuse showing it here.
        self.assertTrue(_shows_no_place_more("a b a", "a •••", "a b •••"))
        self.assertFalse(_shows_no_place_more("a b a", "••• b a", "a b •••"))
        self.assertFalse(_shows_no_place_more("x a y a", "x a ••• a", "x a y •••"))


if __name__ == "__main__":
    unittest.main()
