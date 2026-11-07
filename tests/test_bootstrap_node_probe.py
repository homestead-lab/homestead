"""scripts/bootstrap-k3s.sh: what it writes as homestead-install's node-probe
(#1279). Said on this run wins; otherwise the cluster's earlier choice stays,
because the ConfigMap survives a reinstall that keeps Homestead's data."""
import os
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "bootstrap-k3s.sh"
SH = shutil.which("dash") or shutil.which("sh")


def function(name):
    text = SCRIPT.read_text(encoding="utf-8")
    start = text.index(name + "() {")
    return text[start:text.index("\n}", start) + 2]


@unittest.skipUnless(SH and os.name != "nt", "requires a POSIX shell")
class NodeProbeWordTests(unittest.TestCase):
    def word(self, said, kept, status=0):
        """node_probe_word with NODE_PROBE=said and a kubectl answering
        homestead-install's node-probe with kept and exit status."""
        stub = 'kubectl() { printf "%s\\n" "$*" >&2; printf %s "' + kept + '"; return ' + str(status) + '; }\n'
        result = subprocess.run([SH], input="set -eu\n" + stub + "KUBECTL=kubectl\nNODE_PROBE='" + said + "'\n"
                                + function("node_probe_word") + "\nnode_probe_word || echo FAILED\n",
                                text=True, capture_output=True, timeout=10)
        return result.stdout.strip(), result.stderr

    def test_an_earlier_no_stays_when_this_run_says_nothing(self):
        word, asked = self.word("", "no")
        self.assertEqual("no", word)
        self.assertIn("-n lab get configmap homestead-install --ignore-not-found", asked)

    def test_the_words_homestead_reads_as_no_stay_no(self):
        for kept in ("false", "0", "skip", "No"):
            self.assertEqual("no", self.word("", kept)[0], kept)

    def test_said_on_this_run_wins_over_the_earlier_choice(self):
        self.assertEqual("yes", self.word("1", "no")[0])
        self.assertEqual("no", self.word("0", "yes")[0])

    def test_a_first_install_gets_the_probe(self):
        self.assertEqual("yes", self.word("", "")[0])
        self.assertEqual("yes", self.word("", "yes")[0])

    def test_a_cluster_that_cannot_be_asked_is_never_guessed(self):
        self.assertEqual("FAILED", self.word("", "", status=1)[0])

    def test_the_options_and_the_configmap_use_it(self):
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("    --node-probe) NODE_PROBE=1;", text)
        self.assertIn("    --no-node-probe) NODE_PROBE=0;", text)
        self.assertRegex(text, r'\nNODE_PROBE=""\n')
        self.assertIn('  node-probe: "$PROBE_WORD"', text)
        self.assertIn("PROBE_WORD=$(node_probe_word) || fail", text)


if __name__ == "__main__":
    unittest.main()
