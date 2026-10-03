import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import server


def lh(name, claim, pv_status="Bound"):
    return {"metadata": {"name": name},
            "spec": {"size": str(5 * 1024 ** 3), "numberOfReplicas": 2},
            "status": {"state": "detached", "robustness": "unknown",
                       "kubernetesStatus": {"namespace": "lab", "pvcName": claim, "pvStatus": pv_status}}}


def pvc(name, volume):
    return {"metadata": {"namespace": "lab", "name": name},
            "spec": {"volumeName": volume, "accessModes": ["ReadWriteOnce"]}}


def deployment(name, claim, replicas=0):
    return {"metadata": {"namespace": "lab", "name": name},
            "spec": {"replicas": replicas, "template": {"spec": {"volumes": [
                {"name": "data", "persistentVolumeClaim": {"claimName": claim}}]}}}}


class VolumeUseTests(unittest.TestCase):
    """Detached said nothing about whether the data is wanted: a stopped
    container's volume looked like one nothing uses at all."""

    def setUp(self):
        self.saved = server.kget
        world = {
            "/apis/longhorn.io/v1beta2/volumes": [lh("pv-nc", "nextcloud-data"), lh("pv-scratch", "scratch"),
                                                  lh("pv-old", "mosquitto-appdata", "Released"),
                                                  lh("pv-db", "postgres-data-db-0")],
            "/api/v1/persistentvolumeclaims": [pvc("nextcloud-data", "pv-nc"), pvc("scratch", "pv-scratch"),
                                               pvc("mosquitto-appdata", "pv-new"), pvc("postgres-data-db-0", "pv-db")],
            "/apis/apps/v1/deployments": [deployment("nextcloud", "nextcloud-data")],
            "/apis/apps/v1/statefulsets": [{"metadata": {"namespace": "lab", "name": "db"},
                                            "spec": {"replicas": 0, "template": {"spec": {}},
                                                     "volumeClaimTemplates": [{"metadata": {"name": "postgres-data"}}]}}],
        }

        self.world = world

        def kget(path):
            if path in world:
                return {"items": world[path]}
            if "engines" in path:
                return {"items": []}
            return {"items": []}
        server.kget = kget

    def tearDown(self):
        server.kget = self.saved

    def rows(self):
        return {row["pvc_name"] + ("(old)" if row["name"] == "pv-old" else ""): row for row in server.get_volumes()}

    def test_a_stopped_containers_volume_names_it(self):
        self.assertEqual(["Deployment/nextcloud"], self.rows()["nextcloud-data"]["used_by"])

    def test_a_volume_nothing_refers_to_is_orphaned(self):
        row = self.rows()["scratch"]
        self.assertEqual([], row["used_by"])
        self.assertFalse(row["unclaimed"])

    def test_a_statefulsets_template_claim_belongs_to_it(self):
        self.assertEqual(["StatefulSet/db"], self.rows()["postgres-data-db-0"]["used_by"])

    def test_an_original_kept_after_a_class_change_has_no_claim(self):
        """Longhorn still names the claim it had, which is now the copy's."""
        row = self.rows()["mosquitto-appdata(old)"]
        self.assertTrue(row["unclaimed"])
        self.assertIsNone(row["used_by"])


    def old_importer(self, volume):
        volume["status"]["kubernetesStatus"].update(
            workloadsStatus=[{"workloadName": "prime-268211de", "podName": "importer-prime-268211de",
                              "podStatus": "Succeeded", "workloadType": "PersistentVolumeClaim"}],
            lastPodRefAt="2026-10-02T10:19:11Z")

    def test_released_scratch_volume_has_no_current_attachment_and_keeps_last_used_time(self):
        scratch = lh("pv-prime", "prime-268211de-scratch", "Released")
        self.old_importer(scratch)
        self.world["/apis/longhorn.io/v1beta2/volumes"].append(scratch)
        row = self.rows()["prime-268211de-scratch"]
        self.assertTrue(row["unclaimed"])
        self.assertEqual([], row["attached"])
        self.assertEqual("", row["attached_to"])
        self.assertEqual("", row["pod_status"])
        self.assertEqual("2026-10-02T10:19:11Z", row["last_used"])

    def test_a_previous_importer_does_not_hide_a_stopped_workload_or_an_orphan(self):
        for claim in ("nextcloud-data", "scratch"):
            volume = next(v for v in self.world["/apis/longhorn.io/v1beta2/volumes"]
                          if v["status"]["kubernetesStatus"]["pvcName"] == claim)
            self.old_importer(volume)
            self.assertEqual([], self.rows()[claim]["attached"])
        self.assertEqual(["Deployment/nextcloud"], self.rows()["nextcloud-data"]["used_by"])
        self.assertEqual([], self.rows()["scratch"]["used_by"])

    def test_retained_old_volume_never_borrows_replacement_claim_attachment_names(self):
        volume = next(v for v in self.world["/apis/longhorn.io/v1beta2/volumes"] if v["metadata"]["name"] == "pv-old")
        volume["status"]["kubernetesStatus"]["workloadsStatus"] = [{"workloadName": "mosquitto", "podStatus": "Running"}]
        self.assertEqual([], self.rows()["mosquitto-appdata(old)"]["attached"])

    def test_current_longhorn_workloads_still_show_as_attached(self):
        volume = next(v for v in self.world["/apis/longhorn.io/v1beta2/volumes"] if v["metadata"]["name"] == "pv-nc")
        volume["status"].update(state="attached")
        volume["status"]["kubernetesStatus"]["workloadsStatus"] = [{"workloadName": "nextcloud", "podStatus": "Running"}]
        row = self.rows()["nextcloud-data"]
        self.assertEqual(["nextcloud"], row["attached"])
        self.assertEqual("nextcloud", row["attached_to"])
        self.assertEqual("Running", row["pod_status"])


if __name__ == "__main__":
    unittest.main()
