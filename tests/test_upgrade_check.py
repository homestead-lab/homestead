"""The upgrade check: what Homestead's table says about the next version, and
about the KubeVirt-Kubernetes pair running now (homestead_upgrade_check)."""
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_upgrade_check as U


class PairTests(unittest.TestCase):
    def status(self, kubevirt, *kube):
        return U.kubevirt_pair(kubevirt, set(kube))["status"]

    def test_kubevirt_1_9_on_kubernetes_1_37_is_outside_the_matrix(self):
        pair = U.kubevirt_pair("v1.9.1", {(1, 37)})
        self.assertEqual("outside", pair["status"])
        self.assertEqual("KubeVirt 1.9 on Kubernetes 1.37", pair["title"])
        self.assertIn("1.34, 1.35 and 1.36", pair["detail"])
        self.assertIn("sig-release", pair["source"])

    def test_a_ticked_pair_is_supported(self):
        self.assertEqual("supported", self.status("v1.9.1", (1, 36)))

    def test_a_pair_whose_kubernetes_has_ended_is_eol(self):
        self.assertEqual("eol", self.status("v1.8.2", (1, 33)))

    def test_a_kubevirt_the_table_does_not_list_is_not_checked(self):
        self.assertEqual("unchecked", self.status("v1.10.0", (1, 36)))

    def test_kubernetes_older_than_the_matrix_is_not_checked(self):
        self.assertEqual("unchecked", self.status("v1.4.0", (1, 30)))

    def test_a_kubernetes_version_not_read_is_not_checked(self):
        self.assertEqual("unchecked", self.status("v1.9.1"))
        self.assertEqual("unchecked", self.status("1.9-custom", (1, 36)))

    def test_one_host_outside_makes_the_pair_outside(self):
        self.assertEqual("outside", self.status("v1.9.1", (1, 36), (1, 37)))


class CheckTests(unittest.TestCase):
    def levels(self, result):
        return [f["level"] for f in result["findings"]]

    def test_k3s_past_kubevirts_matrix_is_blocked(self):
        result = U.check("cluster", "v1.36.4+k3s1", "v1.37.1+k3s1", {(1, 36)}, {"kubevirt": "v1.9.1"})
        self.assertEqual("blocked", result["verdict"])
        self.assertIn("KubeVirt 1.9 is not tested on Kubernetes 1.37", result["findings"][0]["title"])

    def test_k3s_inside_kubevirts_matrix_is_clear(self):
        result = U.check("cluster", "v1.35.9+k3s1", "v1.36.4+k3s1", {(1, 35)}, {"kubevirt": "v1.9.1"})
        self.assertEqual("clear", result["verdict"])
        self.assertEqual(["info"], self.levels(result))

    def test_k3s_without_kubevirt_is_not_checked(self):
        result = U.check("cluster", "v1.35.9+k3s1", "v1.36.4+k3s1", {(1, 35)}, {"longhorn": "v1.13.0"})
        self.assertEqual("unchecked", result["verdict"])

    def test_kubevirt_onto_a_supported_kubernetes_is_clear(self):
        self.assertEqual("clear", U.check("kubevirt", "v1.8.2", "v1.9.1", {(1, 35)}, {})["verdict"])

    def test_kubevirt_onto_an_untested_kubernetes_is_blocked(self):
        self.assertEqual("blocked", U.check("kubevirt", "v1.8.2", "v1.9.1", {(1, 33)}, {})["verdict"])

    def test_longhorn_1_12_1_turns_on_its_network_policies(self):
        result = U.check("longhorn", "v1.12.0", "v1.12.1", {(1, 35)}, {})
        self.assertEqual("check", result["verdict"])
        warn = next(f for f in result["findings"] if f["level"] == "warn")
        self.assertIn("restrictInternalTraffic", warn["detail"])
        self.assertIn("#794", warn["detail"])
        self.assertIn("v1.12.1", warn["source"])

    def test_longhorn_already_fenced_in_gets_no_new_warning(self):
        result = U.check("longhorn", "v1.12.1", "v1.13.0", {(1, 35)}, {})
        self.assertEqual("clear", result["verdict"])

    def test_longhorn_below_its_kubernetes_floor_is_blocked(self):
        result = U.check("longhorn", "v1.12.1", "v1.13.0", {(1, 33), (1, 35)}, {})
        self.assertEqual("blocked", result["verdict"])
        self.assertIn("Kubernetes 1.34", result["findings"][0]["title"])

    def test_longhorn_from_a_version_the_table_lacks_still_checks_the_target(self):
        result = U.check("longhorn", "v1.11.3", "v1.12.0", {(1, 35)}, {})
        self.assertEqual("clear", result["verdict"])

    def test_a_longhorn_target_the_table_lacks_is_not_checked(self):
        result = U.check("longhorn", "v1.13.0", "v1.13.1", {(1, 35)}, {})
        self.assertEqual("unchecked", result["verdict"])
        self.assertEqual(["unknown"], self.levels(result))

    def test_an_unread_kubernetes_version_is_never_clear(self):
        self.assertNotEqual("clear", U.check("longhorn", "v1.12.1", "v1.13.0", set(), {})["verdict"])
        self.assertNotEqual("clear", U.check("kubevirt", "v1.8.2", "v1.9.1", set(), {})["verdict"])

    def test_a_part_the_table_does_not_cover_is_not_checked(self):
        result = U.check("cdi", "v1.61.0", "v1.62.0", {(1, 35)}, {})
        self.assertEqual({"verdict": "unchecked", "findings": []},
                         {k: result[k] for k in ("verdict", "findings")})
        self.assertEqual(U.KNOWN.CHECKED, result["checked"])


class AnnotateTests(unittest.TestCase):
    def test_upgradable_rows_get_a_check_and_the_report_gets_its_pairs(self):
        rows = [{"id": "cluster", "installed": "v1.36.4+k3s1", "next": "v1.37.1+k3s1", "how": "suc"},
                {"id": "kubevirt", "installed": "v1.9.1", "next": "", "how": "helmchart"},
                {"id": "longhorn", "installed": "v1.12.0", "next": "v1.12.1", "how": "manual"}]
        pairs = U.annotate(rows, {(1, 36)})
        self.assertEqual("blocked", rows[0]["check"]["verdict"])
        self.assertNotIn("check", rows[1], "nothing to upgrade to")
        self.assertNotIn("check", rows[2], "Homestead does not upgrade it")
        self.assertEqual(["supported"], [p["status"] for p in pairs])

    def test_no_kubevirt_no_pairs(self):
        self.assertEqual([], U.annotate([{"id": "cluster", "installed": "v1.36.4+k3s1", "next": "", "how": "suc"}], {(1, 36)}))


if __name__ == "__main__":
    unittest.main()


class ReviewFixTests(unittest.TestCase):
    """Review of 2026-10-23: titles name only the minors at fault; a pair
    already outside isn't blamed on an upgrade that doesn't change it."""
    def titles(self, result):
        return [f["title"] for f in result["findings"]]

    def test_titles_name_only_the_minors_at_fault(self):
        self.assertEqual(["KubeVirt 1.9 on Kubernetes 1.37 is already outside its support matrix; this upgrade does not change that"],
                         self.titles(U.check("kubevirt", "v1.8.2", "v1.9.1", {(1, 36), (1, 37)}, {})))
        self.assertEqual(["Kubernetes 1.33 is past its end of life"],
                         self.titles(U.check("kubevirt", "v1.8.1", "v1.8.2", {(1, 33), (1, 35)}, {})))
        self.assertEqual(["1.37"], U.kubevirt_pair("v1.9.1", {(1, 36), (1, 37)})["fault"])
        self.assertEqual(["KubeVirt 1.9 is not tested on Kubernetes 1.33"],
                         self.titles(U.check("kubevirt", "v1.8.2", "v1.9.1", {(1, 33), (1, 35)}, {})))

    def test_a_patch_upgrade_inside_an_already_outside_pair_adds_no_problem(self):
        result = U.check("cluster", "v1.37.1+k3s1", "v1.37.2+k3s1", {(1, 37)}, {"kubevirt": "v1.9.1"})
        self.assertEqual("unchanged", result["verdict"])
        self.assertEqual("already", result["findings"][0]["level"])
        self.assertIn("already outside", result["findings"][0]["title"])
        self.assertIn("Upgrade KubeVirt when a release lists Kubernetes 1.37.", result["findings"][0]["detail"])

    def test_a_kubevirt_patch_that_stays_outside_adds_no_problem(self):
        self.assertEqual("unchanged", U.check("kubevirt", "v1.9.0", "v1.9.1", {(1, 37)}, {})["verdict"])

    def test_an_end_of_life_the_upgrade_leaves_as_it_is_is_already_true(self):
        result = U.check("kubevirt", "v1.8.1", "v1.8.2", {(1, 33)}, {})
        self.assertEqual(["already"], [f["level"] for f in result["findings"]])
        self.assertEqual("unchanged", result["verdict"])

    def test_a_new_end_of_life_or_another_warning_still_asks_for_a_look(self):
        # Kubernetes 1.33 is past its end of life, and the cluster upgrade moves to it from a supported minor.
        result = U.check("cluster", "v1.35.1+k3s1", "v1.33.9+k3s1", {(1, 35)}, {"kubevirt": "v1.8.1"})
        self.assertEqual(["warn"], [f["level"] for f in result["findings"]])
        self.assertEqual("check", result["verdict"])

    def test_an_already_true_finding_beside_a_real_warning_keeps_the_look(self):
        result = U.check("cluster", "v1.37.1+k3s1", "v1.37.2+k3s1", {(1, 37)}, {"kubevirt": "v1.9.1"})
        result["findings"].append({"level": "warn", "title": "t", "detail": "d", "source": ""})
        self.assertEqual("check", U.verdict_of(result["findings"]))

    def test_moving_a_pair_outside_is_still_blocked(self):
        self.assertEqual("blocked", U.check("kubevirt", "v1.8.2", "v1.9.1", {(1, 33)}, {})["verdict"])

    def test_kubernetes_below_a_row_that_starts_unsupported_is_outside(self):
        # The 1.9 row has "-" in its oldest column: support ranges are contiguous.
        self.assertEqual("outside", U.kubevirt_pair("v1.9.1", {(1, 30), (1, 35)})["status"])
        self.assertEqual("unchecked", U.kubevirt_pair("v1.4.0", {(1, 30)})["status"])

    def test_longhorn_warning_for_an_installed_version_the_table_lacks_says_it_is_on(self):
        warn = next(f for f in U.check("longhorn", "v1.11.3", "v1.12.1", {(1, 35)}, {})["findings"] if f["level"] == "warn")
        self.assertIn("is on by default in Longhorn v1.12.1", warn["detail"])
