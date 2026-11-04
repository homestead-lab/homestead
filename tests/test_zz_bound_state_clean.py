"""Near the end of a run: no test left a module's bound names changed (#TBD-bind-resets-names).

tests/bound_guard.py, installed when this file loads (before any test runs),
compares everything each loaded module's bind() sets before and after every test,
records (name, test file) for what a test left changed; it changes nothing.
scripts/run_tests.py ends every share (and every
--changed run) with this file, then test_zz_guard_state_clean.

Here, at the end. Failed, because none of these depends on CI's split:
- the list may only grow smaller (CEILING), and its fake lines' names must still be bound;
- in a run of the whole suite in one process (how the list is measured), a listed
  fake line whose file no longer leaks the name, so the list holds real leaks.
Failed too (ENFORCE_FAKES below):
- a name left holding a fake the test made (code from tests/, a Mock, or a path
  under the temp folder; credited to the test file that left it) that bound_leaks.txt
  does not list with that file.
Reported, not failed:
- any other change (a name put back to its default, a server value, a fake another
  test made): whether it reads as a change depends on what ran before.
Fakes bound in setUpClass/setUpModule run before any test, so the per-test recorder
does not see them.

Fix a leak with tests/rebind.py (rebind / keep_bound) or mock.patch.object, then
take its line off bound_leaks.txt and lower CEILING.
"""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bound_guard as GUARD

GUARD.install()
LISTED = GUARD.allowed()
# Lines for leaks that come with an open PR of ours (note "from #NNNN"): allowed, so the
# combined run passes, but not counted, not stale and not checked as bound (bound_guard.pending).
SIDES = GUARD.sides(LISTED, GUARD.pending())
GUARD.watch.update(name for name, _, kind in LISTED if kind == "fake")
CEILING = 528
# The open-PR block at the end of bound_leaks.txt, capped on its own: lower it as each
# PR merges and its lines go (WORKFLOW.md, after merge).
PENDING_CEILING = 7
# On since 3 Nov: recording every share of CI's 4-way and the gate's 8-way split found
# no fake pair the whole-suite list lacks. (Advisory before that: test_smart_readings'
# Base bound SMART, and through it NAMES, to its module-level probe_pods and kept it, so
# whichever file ran a Base test first in an order was credited.) If a split shows a
# pair this list lacks, that is an order-dependent leak to fix at its source, not to list.
ENFORCE_FAKES = True
# BOUND_LEAKS_RECORD=<file>: write the pairs this run found there (to measure the list).
RECORD = os.environ.get("BOUND_LEAKS_RECORD")


class BoundStateClean(unittest.TestCase):
    def test_no_test_left_a_fake_that_is_not_listed(self):
        if RECORD:
            with open(RECORD, "w", encoding="utf-8") as out:
                for kind, found in (("fake", GUARD.left), ("other", GUARD.others)):
                    for (name, test_file), test in sorted(found.items()):
                        out.write(f"{name}\t{test_file}\t{kind}\t{test}\n")
            self.skipTest(f"recorded {len(GUARD.left)} fakes and {len(GUARD.others)} other changes to {RECORD}")
        found = GUARD.new_leaks(GUARD.left, SIDES.fakes)
        lines = [f"{name} holds a fake from {test_file} ({GUARD.left[(name, test_file)]})" for name, test_file in found]
        if not ENFORCE_FAKES:
            for line in lines[:20]:
                print(f"bound state: {line} (advisory: ENFORCE_FAKES)", file=sys.stderr)
            self.skipTest(f"advisory: {len(lines)} fake(s) not on bound_leaks.txt, reported above")
        self.assertEqual([], lines, "a test bound these to its own fake and did not put them back (tests/rebind.py)")

    def test_other_changes_are_reported(self):
        # A name put back to its module default, or to another server value, is
        # reported, not failed: whether it reads as a change depends on what ran
        # before, so a check of it would pass or fail by CI's split.
        unlisted = sorted(pair for pair in GUARD.others if (*pair, "other") not in LISTED)
        for name, test_file in unlisted[:20]:
            print(f"bound state: {name} changed by {test_file} (not a fake: reported, not failed)", file=sys.stderr)
        if unlisted:
            self.skipTest(f"{len(unlisted)} other change(s) not on bound_leaks.txt, reported above")

    def test_no_listed_leak_is_gone(self):
        if RECORD:
            self.skipTest("recording")
        everything = GUARD.files_before((p.stem for p in Path(__file__).resolve().parent.glob("test_*.py")),
                                        Path(__file__).stem)
        if not GUARD.full_run(GUARD.ran, everything):
            self.skipTest("only a run of the whole suite in one process can tell: the list was measured that way")
        # Fakes only: the other changes are reported, not enforced, and whether one reads
        # as a change depends on what ran before.
        gone = GUARD.stale(GUARD.left, SIDES.stale_fakes,
                           GUARD.ran, GUARD.unchecked, GUARD.covered, GUARD.held)
        self.assertEqual([], [f"{name}\t{test_file}" for name, test_file in gone],
                         "these files no longer leak these names: take the lines off bound_leaks.txt")

    def test_the_allowlist_only_shrinks(self):
        self.assertLessEqual(len(SIDES.on_dev), CEILING, "bound_leaks.txt may only shrink: fix the leak, do not add it")
        if len(SIDES.on_dev) < CEILING:
            self.fail(f"bound_leaks.txt has {len(SIDES.on_dev)} lines (not counting open PRs'): lower CEILING to match")

    def test_the_open_pr_block_only_shrinks(self):
        # Lines for leaks that come with an open PR of ours are capped too: when a PR
        # merges, its follow-up takes its lines off and lowers this (WORKFLOW.md).
        self.assertLessEqual(len(SIDES.pending), PENDING_CEILING,
                             "the open-PR block of bound_leaks.txt may only shrink: fix the leak in the PR")
        if len(SIDES.pending) < PENDING_CEILING:
            self.fail(f"bound_leaks.txt has {len(SIDES.pending)} open-PR lines: lower PENDING_CEILING to match")
        self.assertEqual([], GUARD.bad_notes(), "a note must read \"from #NNNN, ...\": the open PR it comes with")

    def test_each_listed_name_is_still_bound(self):
        # Fake lines only, as the stale check: the other lines are reported, not enforced, and
        # one can name what another PR has since removed.
        loaded = {name for name in sys.modules if name.startswith("homestead_")}
        self.assertEqual([], GUARD.unknown(SIDES.stale_fakes, GUARD.state(), loaded),
                         "these are no longer bound names: take them off bound_leaks.txt")


# Test files that call a module's bind() and never restore anything (no addCleanup,
# tearDown, restore, _reset, rebind or keep_bound in the file's code). They may only go
# away: move one onto tests/rebind.py and take it off this list. A file listed here
# that is not in the tree yet comes with an open PR of ours, so the combined run passes.
UNRESTORED_FILES = frozenset({
    # From #1095 (fix/move-retries-end), not on dev yet: restore its source/engine binds once it merges (wrap-up gaps).
    "test_move_gives_up",
    "test_api_keys",
    "test_autoupdate",
    "test_data_copies",
    "test_firewall",
    "test_host_bridge",
    "test_hostrun_lease",
    "test_hostrun_retry",
    "test_image_pull_progress",
    "test_ipam",
    "test_iso_storage",
    "test_job_errors",
    "test_longhorn_tags",
    "test_names",
    "test_node_power_plan",
    "test_node_removal_k3s",
    "test_orphan_volume",
    "test_os_rollout",
    "test_portal",
    "test_reclass",
    "test_released_volume_delete",
    "test_root_guard",
    "test_sessions",
    "test_smart",
    "test_snapshot_delete",
    "test_vip_places",
    "test_vm_hardware",
    "test_vm_lan",
    "test_vmstore",
    "test_volume_deletion",
    "test_volume_files",
})


class BindRatchet(unittest.TestCase):
    def test_no_new_test_file_binds_without_restoring(self):
        here = Path(__file__).resolve().parent
        found = {p.stem for p in here.glob("test_*.py") if GUARD.binds_without_restoring(p.read_text(encoding="utf-8"))}
        self.assertEqual(sorted(found - UNRESTORED_FILES), [], "these bind without restoring: use tests/rebind.py")
        # A listed file that is not in this tree (it comes with an open PR) is left alone.
        present = {p.stem for p in here.glob("test_*.py")}
        self.assertEqual(sorted((UNRESTORED_FILES - found) & present), [],
                         "these restore now: take them off UNRESTORED_FILES")


if __name__ == "__main__":
    unittest.main()
