"""The YAML Homestead reads: the corner of it Compose files live in."""
import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_yaml as yaml


class ScalarTests(unittest.TestCase):
    def test_plain_values_are_typed_as_yaml_1_2_does(self):
        doc = yaml.loads("a: 1\nb: 1.5\nc: true\nd: null\ne: ~\nf: yes\ng: 0755\nh: text here\n")

        self.assertEqual({"a": 1, "b": 1.5, "c": True, "d": None, "e": None,
                          "f": "yes", "g": "0755", "h": "text here"}, doc)

    def test_a_port_mapping_stays_a_string(self):
        """80:80 was base 60 in YAML 1.1; nobody meant that."""
        self.assertEqual(["8080:80", "53:53/udp", "127.0.0.1:9000:9000"],
                         yaml.loads('- 8080:80\n- 53:53/udp\n- 127.0.0.1:9000:9000\n'))

    def test_an_image_with_a_tag_is_a_value_not_a_key(self):
        self.assertEqual({"image": "ghcr.io/org/app:1.2"}, yaml.loads("image: ghcr.io/org/app:1.2\n"))

    def test_quotes_and_escapes(self):
        doc = yaml.loads("""a: 'it''s'\nb: "tab\\there"\nc: "# not a comment"\nd: 'x' # a comment\n""")

        self.assertEqual({"a": "it's", "b": "tab\there", "c": "# not a comment", "d": "x"}, doc)

    def test_a_hash_inside_a_value_is_kept(self):
        self.assertEqual({"url": "http://x/#frag"}, yaml.loads("url: http://x/#frag\n"))


class StructureTests(unittest.TestCase):
    def test_a_compose_file(self):
        doc = yaml.loads("""
services:
  web:
    image: nginx:alpine
    ports:
      - "8080:80"
    environment:
      - TZ=Europe/London
    volumes:
      - ./html:/usr/share/nginx/html:ro
  db:
    image: postgres:16
    environment:
      POSTGRES_PASSWORD: secret
volumes:
  data:
""")
        self.assertEqual("nginx:alpine", doc["services"]["web"]["image"])
        self.assertEqual(["8080:80"], doc["services"]["web"]["ports"])
        self.assertEqual({"POSTGRES_PASSWORD": "secret"}, doc["services"]["db"]["environment"])
        self.assertEqual({"data": None}, doc["volumes"])

    def test_a_list_may_sit_at_the_same_indent_as_its_key(self):
        self.assertEqual({"ports": ["80:80", "443:443"], "next": 1},
                         yaml.loads("ports:\n- 80:80\n- 443:443\nnext: 1\n"))

    def test_list_items_that_are_mappings(self):
        doc = yaml.loads("""volumes:
  - type: bind
    source: ./data
    target: /data
  - type: tmpfs
    target: /tmp
""")
        self.assertEqual([{"type": "bind", "source": "./data", "target": "/data"},
                          {"type": "tmpfs", "target": "/tmp"}], doc["volumes"])

    def test_flow_collections(self):
        doc = yaml.loads('command: ["npm", "run", "start"]\nlimits: {cpus: "0.5", memory: 512M}\nempty: []\n')

        self.assertEqual(["npm", "run", "start"], doc["command"])
        self.assertEqual({"cpus": "0.5", "memory": "512M"}, doc["limits"])
        self.assertEqual([], doc["empty"])

    def test_block_text_keeps_its_lines_and_hashes(self):
        doc = yaml.loads("script: |\n  echo one\n  # still text\n  echo two\nfolded: >-\n  one\n  two\nafter: x\n")

        self.assertEqual("echo one\n# still text\necho two\n", doc["script"])
        self.assertEqual("one two", doc["folded"])
        self.assertEqual("x", doc["after"])

    def test_anchors_aliases_and_merge_keys(self):
        doc = yaml.loads("""x-common: &common
  restart: unless-stopped
  environment:
    TZ: UTC
services:
  a:
    <<: *common
    image: a
  b:
    <<: *common
    restart: always
    image: b
""")
        self.assertEqual("unless-stopped", doc["services"]["a"]["restart"])
        self.assertEqual({"TZ": "UTC"}, doc["services"]["a"]["environment"])
        self.assertEqual("always", doc["services"]["b"]["restart"], "a key set here beats the merge")

    def test_lines_are_remembered(self):
        doc = yaml.loads("services:\n  web:\n    image: x\n    ports:\n      - 80:80\n")

        web = doc["services"]["web"]
        self.assertEqual(3, yaml.line_of(web, "image"))
        self.assertEqual(5, yaml.line_of(web["ports"], 0))

    def test_an_empty_document_is_none(self):
        self.assertIsNone(yaml.loads("# only a comment\n\n"))


class ErrorTests(unittest.TestCase):
    def assertErrorAt(self, source, line, words):
        with self.assertRaises(yaml.YamlError) as caught:
            yaml.loads(source)
        self.assertEqual(line, caught.exception.line)
        self.assertIn(words, caught.exception.message)

    def test_tabs(self):
        self.assertErrorAt("a:\n\tb: 1\n", 2, "tabs")

    def test_tab_indentation_is_read_as_the_files_own_step(self):
        doc, fixed = yaml.loads_untabbed("a:\n\tb:\n\t\tc: 1\n\td: |\n\t\tx\ty\n")
        self.assertEqual({"a": {"b": {"c": 1}, "d": "x\ty\n"}}, doc)
        self.assertEqual((2, [2, 3, 4, 5]), fixed[1:])
        _, width, _ = yaml.untab("a:\n    b:\n\t\tc: 1\n")
        self.assertEqual(4, width)

    def test_a_file_without_tabs_is_read_unchanged(self):
        self.assertEqual(({"a": "b\tc"}, None), yaml.loads_untabbed("a: b\tc\n"))
        with self.assertRaises(yaml.YamlError):
            yaml.loads_untabbed("a:\n\tb: 1\n  c: [\n")

    def test_a_key_set_twice(self):
        self.assertErrorAt("a: 1\nb: 2\na: 3\n", 3, "set twice")

    def test_an_unclosed_quote(self):
        self.assertErrorAt('a: "open\n', 1, "not closed")

    def test_an_unknown_alias(self):
        self.assertErrorAt("a: *nowhere\n", 1, "not defined")

    def test_bad_indentation(self):
        self.assertErrorAt("a:\n  b: 1\n    c: 2\n", 3, "indentation")

    def test_a_line_that_is_not_a_key(self):
        self.assertErrorAt("services:\n  web\n  db: x\n", 2, "key: value")

    def test_a_second_document(self):
        self.assertErrorAt("a: 1\n---\nb: 2\n", 2, "second")


if __name__ == "__main__":
    unittest.main()
