"""The stylesheet parses the way it reads."""
import re
import unittest
from pathlib import Path

CSS = Path(__file__).resolve().parents[1] / "web" / "style.css"


class StylesheetTests(unittest.TestCase):
    def test_every_block_is_closed(self):
        """One unclosed @media once made every rule after it apply to phones only."""
        body = re.sub(r"/\*.*?\*/", "", CSS.read_text(encoding="utf-8"), flags=re.S)
        body = re.sub(r'"[^"\n]*"', '""', body)
        depth = 0
        for number, line in enumerate(body.split("\n"), 1):
            for char in line:
                depth += (char == "{") - (char == "}")
                self.assertGreaterEqual(depth, 0, f"a stray closing brace on line {number}")
        self.assertEqual(0, depth, "a block is never closed")

    def test_media_blocks_do_not_nest(self):
        body = re.sub(r"/\*.*?\*/", "", CSS.read_text(encoding="utf-8"), flags=re.S)
        depth, opened = 0, []
        for match in re.finditer(r"@media[^{]*\{|\{|\}", body):
            token = match.group(0)
            if token.startswith("@media"):
                self.assertFalse(opened, f"@media opened inside another at {body[:match.start()].count(chr(10)) + 1}")
                opened.append(depth)
                depth += 1
            elif token == "{":
                depth += 1
            else:
                depth -= 1
                if opened and depth == opened[-1]:
                    opened.pop()


if __name__ == "__main__":
    unittest.main()
