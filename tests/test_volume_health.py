import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import server


def volume(**status):
    annotations = status.pop("annotations", {})
    return {"metadata": {"name": "pvc-1", "annotations": annotations}, "status": status}


class VolumeHealthReasonTests(unittest.TestCase):
    """"degraded" alone sends people to the Longhorn UI to find out why."""

    def test_a_failing_condition_is_quoted_back(self):
        reason, conditions, _ = server._volume_health_reason(volume(
            robustness="degraded",
            conditions=[{"type": "Scheduled", "status": "False",
                         "reason": "ReplicaSchedulingFailure",
                         "message": "no disk space to create the replicas required"}]))

        self.assertEqual("no disk space to create the replicas required", reason)
        self.assertEqual(1, len(conditions))

    def test_a_condition_without_a_message_falls_back_to_its_reason(self):
        reason, _, _ = server._volume_health_reason(volume(
            robustness="degraded",
            conditions=[{"type": "Scheduled", "status": "False",
                         "reason": "ReplicaSchedulingFailure", "message": ""}]))

        self.assertEqual("ReplicaSchedulingFailure", reason)

    def test_the_scheduling_annotation_is_used_when_conditions_say_nothing(self):
        reason, _, scheduling = server._volume_health_reason(volume(
            robustness="degraded", conditions=[{"type": "Scheduled", "status": "True"}],
            annotations={"longhorn.io/volume-scheduling-error": "insufficient storage"}))

        self.assertEqual("insufficient storage", reason)
        self.assertEqual("insufficient storage", scheduling)

    def test_a_rebuilding_volume_says_it_is_still_usable(self):
        reason, _, _ = server._volume_health_reason(volume(robustness="degraded"))

        self.assertIn("rebuilding", reason)
        self.assertIn("readable and writable", reason)

    def test_a_faulted_volume_says_what_that_means(self):
        reason, _, _ = server._volume_health_reason(volume(robustness="faulted"))

        self.assertIn("cannot be attached", reason)

    def test_a_detached_volume_is_not_waiting_for_a_backing_image(self):
        """Longhorn's conditions do not share a polarity. WaitForBackingImage
        is False for every volume that has no backing image to wait for, which
        is nearly all of them - reading that as a fault called a resting
        volume broken."""
        reason, _, _ = server._volume_health_reason(volume(
            robustness="unknown",
            conditions=[{"type": "WaitForBackingImage", "status": "False",
                         "reason": "", "message": ""},
                        {"type": "Scheduled", "status": "True"},
                        {"type": "Restore", "status": "False"}]))

        self.assertEqual("", reason)

    def test_a_volume_actually_waiting_for_a_backing_image_says_so(self):
        reason, _, _ = server._volume_health_reason(volume(
            robustness="unknown",
            conditions=[{"type": "WaitForBackingImage", "status": "True",
                         "reason": "WaitForBackingImage",
                         "message": "waiting for the backing image to be ready"}]))

        self.assertEqual("waiting for the backing image to be ready", reason)

    def test_too_many_snapshots_is_a_problem_when_true_not_when_false(self):
        many = [{"type": "TooManySnapshots", "status": "True",
                 "reason": "TooManySnapshots", "message": "too many snapshots"}]
        few = [{"type": "TooManySnapshots", "status": "False"}]

        self.assertEqual("too many snapshots",
                         server._volume_health_reason(volume(robustness="healthy",
                                                             conditions=many))[0])
        self.assertEqual("", server._volume_health_reason(volume(robustness="healthy",
                                                                 conditions=few))[0])

    def test_a_healthy_volume_has_nothing_to_explain(self):
        reason, _, _ = server._volume_health_reason(volume(
            robustness="healthy", conditions=[{"type": "Scheduled", "status": "True"}]))

        self.assertEqual("", reason)

    def test_a_long_message_is_trimmed_rather_than_flooding_a_table_cell(self):
        reason, _, _ = server._volume_health_reason(volume(
            robustness="faulted",
            conditions=[{"type": "Scheduled", "status": "False", "message": "x" * 900}]))

        self.assertEqual(300, len(reason))


if __name__ == "__main__":
    unittest.main()
