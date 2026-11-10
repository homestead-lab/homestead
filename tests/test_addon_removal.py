"""Removing virtual machines (#1336): a review, then a job that removes only
what Homestead installed, each object by the uid of the read that checked it.

On the shared Kubernetes fake (tests/fakes/kube.py) and the real job store."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_addon_removal as REMOVAL
import homestead_addons as ADDONS
import homestead_kube as K
import homestead_names as NAMES
import homestead_operations as OPS
import homestead_routes as ROUTES
from fakes.kube import Kube

CHARTS = "/apis/helm.cattle.io/v1/namespaces/kube-system/helmcharts/"
KUBEVIRT = "/apis/kubevirt.io/v1/namespaces/kubevirt/kubevirts/kubevirt"
CDI = "/apis/cdi.kubevirt.io/v1beta1/cdis/cdi"
CRD = "/apis/apiextensions.k8s.io/v1/customresourcedefinitions/"
CRD_YAML = """apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: {name}
spec:
  group: {group}
"""


def chart(name, crds=(), ours=True):
    manifests = "\n---\n".join(CRD_YAML.format(name=crd, group=crd.split(".", 1)[1]) for crd in crds)
    labels = {NAMES.key("managed"): "true"} if ours else {}
    return {"apiVersion": "helm.cattle.io/v1", "kind": "HelmChart",
            "metadata": {"name": name, "namespace": "kube-system", "labels": labels},
            "spec": {"chartContent": ADDONS.chart_archive(name, "v1.0.0", manifests, ""), "targetNamespace": "kube-system"}}


def released(obj, release):
    obj["metadata"]["annotations"] = {"meta.helm.sh/release-name": release, "meta.helm.sh/release-namespace": "kube-system"}
    return obj


def kubevirt_cr(release="homestead-kubevirt", **meta):
    obj = {"apiVersion": "kubevirt.io/v1", "kind": "KubeVirt", "metadata": {"name": "kubevirt", "namespace": "kubevirt", **meta}}
    return released(obj, release) if release else obj


def cdi_cr(release="homestead-cdi"):
    obj = {"apiVersion": "cdi.kubevirt.io/v1beta1", "kind": "CDI", "metadata": {"name": "cdi"}}
    return released(obj, release) if release else obj


def crd(name, plural, group, kind="", version="v1"):
    return {"apiVersion": "apiextensions.k8s.io/v1", "kind": "CustomResourceDefinition", "metadata": {"name": name},
            "spec": {"group": group, "names": {"plural": plural, "kind": kind or plural.title()},
                     "versions": [{"name": version, "served": True}]}}


# The types KubeVirt and CDI serve, as their CRDs say (a few of the ~30).
TYPES = [crd("kubevirts.kubevirt.io", "kubevirts", "kubevirt.io", "KubeVirt"),
         crd("cdis.cdi.kubevirt.io", "cdis", "cdi.kubevirt.io", "CDI"),
         crd("virtualmachines.kubevirt.io", "virtualmachines", "kubevirt.io", "VirtualMachine"),
         crd("virtualmachineinstances.kubevirt.io", "virtualmachineinstances", "kubevirt.io", "VirtualMachineInstance"),
         crd("datavolumes.cdi.kubevirt.io", "datavolumes", "cdi.kubevirt.io", "DataVolume", "v1beta1"),
         crd("storageprofiles.cdi.kubevirt.io", "storageprofiles", "cdi.kubevirt.io", "StorageProfile", "v1beta1"),
         crd("cdiconfigs.cdi.kubevirt.io", "cdiconfigs", "cdi.kubevirt.io", "CDIConfig", "v1beta1"),
         crd("virtualmachinesnapshots.snapshot.kubevirt.io", "virtualmachinesnapshots", "snapshot.kubevirt.io",
             "VirtualMachineSnapshot", "v1beta1"),
         crd("virtualmachineclusterinstancetypes.instancetype.kubevirt.io", "virtualmachineclusterinstancetypes",
             "instancetype.kubevirt.io", "VirtualMachineClusterInstancetype", "v1beta1")]


def vm(name, ns="lab"):
    return {"apiVersion": "kubevirt.io/v1", "kind": "VirtualMachine", "metadata": {"name": name, "namespace": ns}}


class Removal(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.object(OPS, "DATA_DIR", self.enterContext(tempfile.TemporaryDirectory())))
        self.kube = self.enterContext(Kube(
            chart("homestead-kubevirt", ["kubevirts.kubevirt.io"]), chart("homestead-cdi", ["cdis.cdi.kubevirt.io"]),
            chart("homestead-macvtap"), kubevirt_cr(), cdi_cr(), *TYPES,
            groups=("kubevirt.io", "cdi.kubevirt.io", "snapshot.kubevirt.io", "instancetype.kubevirt.io",
                    "helm.cattle.io", "apiextensions.k8s.io")))
        for name in ("kget", "ksend", "platform", "snapshot"):
            self.addCleanup(setattr, REMOVAL, name, getattr(REMOVAL, name))
        self.platform = {"kubevirt": True}
        REMOVAL.bind(K.kget, K.ksend, lambda: self.platform, OPS.snapshot)
        self.enterContext(mock.patch.dict(OPS.RESOLVERS, {}))
        self.enterContext(mock.patch.dict(OPS.CANCELLERS, {}))
        REMOVAL.register(OPS)
        self.now = 1000.0
        self.enterContext(mock.patch.object(REMOVAL, "_clock", lambda: self.now))

    def route(self, path, body):
        """Through the route an admin's request reaches."""
        return ROUTES.find("POST", path).handler(ROUTES.Request("POST", path, {}, body, "ada", "admin"))

    def review(self):
        return self.route("/api/addons/kubevirt/remove/review", {})

    def remove(self):
        return self.route("/api/addons/kubevirt/remove", {"confirm": "kubevirt"})

    def job(self, ident):
        return next(row for row in OPS.list_operations() if row["id"] == ident)

    def run_out(self, ident, rounds=12):
        for _ in range(rounds):
            row = self.job(ident)
            if row["status"] != "running":
                return row
            self.now += 10
        return row

    def deletes(self):
        return [(path, body) for method, path, body, _ in self.kube.writes if method == "DELETE"]


class ReviewTests(Removal):
    def test_it_lists_what_goes_and_what_stays(self):
        found = self.review()
        self.assertTrue(found["can"], found["blockers"])
        said = " ".join(found["removes"])
        for words in ("KubeVirt and CDI settings", "homestead-macvtap, homestead-kubevirt, homestead-cdi",
                      "including anything a person put there", "kubevirts.kubevirt.io and cdis.cdi.kubevirt.io"):
            self.assertIn(words, said)
        self.assertIn("Anything Homestead did not create is kept, even under the same name.", found["keeps"])

    def test_it_refuses_with_a_vm_present_and_names_it(self):
        self.kube.add(vm("web"))
        found = self.review()
        self.assertFalse(found["can"])
        self.assertIn("1 virtual machine (lab/web) still exists. Delete it first.", found["blockers"])
        with self.assertRaisesRegex(ValueError, "lab/web"):
            self.remove()
        self.assertEqual([], self.deletes(), "nothing is removed")
        self.assertEqual([], OPS.list_operations(), "no job starts")

    def test_it_refuses_with_a_vm_disk_or_its_volume(self):
        self.kube.add({"apiVersion": "cdi.kubevirt.io/v1beta1", "kind": "DataVolume", "metadata": {"name": "disk", "namespace": "lab"}})
        self.kube.add({"apiVersion": "v1", "kind": "PersistentVolumeClaim",
                       "metadata": {"name": "old-disk", "namespace": "lab", "annotations": {"cdi.kubevirt.io/storage.import.endpoint": "x"}}})
        said = " ".join(self.review()["blockers"])
        self.assertIn("1 VM disk (lab/disk)", said)
        self.assertIn("1 volume (lab/old-disk) holds a VM disk image. Delete it first", said)

    def test_a_vm_snapshot_present_refuses_and_nothing_is_deleted(self):
        # Deleting the settings lets virt-operator remove the snapshot type, and the
        # snapshots with it (and the VolumeSnapshots, disk backups, they own).
        self.kube.add({"apiVersion": "snapshot.kubevirt.io/v1beta1", "kind": "VirtualMachineSnapshot",
                       "metadata": {"name": "before-upgrade", "namespace": "lab"}})
        found = self.review()
        self.assertFalse(found["can"])
        self.assertIn("1 VirtualMachineSnapshot (lab/before-upgrade) still exists. Delete it first, or keep virtual machines.",
                      found["blockers"])
        with self.assertRaisesRegex(ValueError, "lab/before-upgrade"):
            self.remove()
        self.assertEqual([], self.deletes())

    def test_what_an_operator_created_by_default_does_not_refuse_but_a_persons_own_does(self):
        cluster_type = {"apiVersion": "instancetype.kubevirt.io/v1beta1", "kind": "VirtualMachineClusterInstancetype"}
        self.kube.add({**cluster_type, "metadata": {"name": "u1.small",
                                                    "labels": {"app.kubernetes.io/managed-by": "virt-operator"}}})
        self.kube.add({"apiVersion": "cdi.kubevirt.io/v1beta1", "kind": "StorageProfile", "metadata": {"name": "longhorn"},
                       "spec": {}})
        cdi = self.kube.object(CDI)["metadata"]["uid"]
        self.kube.add({"apiVersion": "cdi.kubevirt.io/v1beta1", "kind": "CDIConfig", "metadata": {
            "name": "config", "ownerReferences": [{"apiVersion": "cdi.kubevirt.io/v1beta1", "kind": "CDI", "name": "cdi", "uid": cdi}]}})
        self.assertEqual([], self.review()["blockers"])
        self.kube.add({**cluster_type, "metadata": {"name": "my-size"}})
        self.kube.add({"apiVersion": "cdi.kubevirt.io/v1beta1", "kind": "StorageProfile", "metadata": {"name": "local-path"},
                       "spec": {"claimPropertySets": [{"accessModes": ["ReadWriteOnce"]}]}})
        said = " ".join(self.review()["blockers"])
        self.assertIn("1 VirtualMachineClusterInstancetype (my-size)", said)
        self.assertIn("1 StorageProfile (local-path)", said)

    def test_an_operators_label_or_a_made_up_owner_exempts_nothing_a_person_could_lose(self):
        # Labels and ownerReferences can be written by anyone who can create a VM.
        spoofed = {"labels": {"app.kubernetes.io/managed-by": "virt-operator"},
                   "ownerReferences": [{"apiVersion": "kubevirt.io/v1", "kind": "KubeVirt", "name": "kubevirt", "uid": "not-the-live-one"}]}
        self.kube.add({**vm("web"), "metadata": {"name": "web", "namespace": "lab", **spoofed}})
        self.kube.add({"apiVersion": "snapshot.kubevirt.io/v1beta1", "kind": "VirtualMachineSnapshot",
                       "metadata": {"name": "nightly", "namespace": "lab",
                                    "labels": {"app.kubernetes.io/managed-by": "virt-operator"}}})
        self.kube.add({"apiVersion": "cdi.kubevirt.io/v1beta1", "kind": "CDIConfig", "metadata": {
            "name": "config", "ownerReferences": [{"apiVersion": "cdi.kubevirt.io/v1beta1", "kind": "CDI", "name": "cdi", "uid": "made-up"}]}})
        said = " ".join(self.review()["blockers"])
        for words in ("1 virtual machine (lab/web)", "1 VirtualMachineSnapshot (lab/nightly)", "1 CDIConfig (config)"):
            self.assertIn(words, said)
        with self.assertRaises(ValueError):
            self.remove()
        self.assertEqual([], self.deletes())

    def test_the_types_it_cannot_read_refuse(self):
        self.kube.fail("/apis/apiextensions.k8s.io/v1/customresourcedefinitions", 500, times=5)
        self.assertEqual(["Homestead could not read which VM types the cluster serves (HTTP 500), so nothing is removed."],
                         self.review()["blockers"])

    def test_a_list_it_cannot_read_refuses(self):
        self.kube.fail("/apis/kubevirt.io/v1/virtualmachines", 500, times=5)
        found = self.review()
        self.assertFalse(found["can"])
        self.assertIn("Homestead could not check the VirtualMachine objects (HTTP 500), so nothing is removed.", found["blockers"])

    def test_harvester_and_a_kubevirt_homestead_did_not_install_are_refused(self):
        self.platform = {"kubevirt": True, "harvester": True}
        self.assertEqual(["Harvester manages KubeVirt, so Homestead does not remove it."], self.review()["blockers"])
        self.platform = {"kubevirt": True}
        for name in ("homestead-kubevirt", "homestead-cdi", "homestead-macvtap"):
            self.kube.add(chart(name, ours=False))
        self.assertIn("KubeVirt was not installed by Homestead, so Homestead does not remove it.",
                      self.review()["blockers"])


class JobTests(Removal):
    def test_it_removes_in_order_each_by_the_uid_it_checked(self):
        uids = {path: self.kube.object(path)["metadata"]["uid"]
                for path in (KUBEVIRT, CDI, CHARTS + "homestead-macvtap", CHARTS + "homestead-kubevirt",
                             CHARTS + "homestead-cdi", CRD + "kubevirts.kubevirt.io", CRD + "cdis.cdi.kubevirt.io")}
        row = self.run_out(self.remove()["operation"]["id"])
        self.assertEqual("succeeded", row["status"], row["message"])
        self.assertEqual([(path, {"apiVersion": "v1", "kind": "DeleteOptions", "preconditions": {"uid": uid}})
                          for path, uid in uids.items()], self.deletes())
        self.assertTrue(row["message"].startswith("Virtual machines were removed."), row["message"])

    def test_it_waits_for_an_operator_to_let_go(self):
        self.kube.add(kubevirt_cr(finalizers=["kubevirt.io/virtOperatorFinalizer"]))
        ident = self.remove()["operation"]["id"]
        self.assertEqual("Removing the KubeVirt settings object", self.job(ident)["message"])
        self.now += 60
        self.assertEqual("Waiting for the KubeVirt settings object to be removed. It still carries the finalizer "
                         "kubevirt.io/virtOperatorFinalizer.", self.job(ident)["message"])
        self.assertIsNotNone(self.kube.object(CHARTS + "homestead-kubevirt"), "the chart stays until the operator is done")
        obj = self.kube.object(KUBEVIRT)
        obj["metadata"]["finalizers"] = []
        self.kube.discard(KUBEVIRT)
        self.assertEqual("succeeded", self.run_out(ident)["status"])

    def test_an_operator_that_never_lets_go_stops_it_saying_what_was_removed(self):
        self.kube.add(kubevirt_cr(finalizers=["kubevirt.io/virtOperatorFinalizer"]))
        ident = self.remove()["operation"]["id"]
        for _ in range(REMOVAL.WAIT_LIMIT // 60 + 2):    # the first round sends the delete
            self.now += 60
            row = self.job(ident)
        self.assertEqual("failed", row["status"])
        self.assertIn("The KubeVirt settings object (kubevirt/kubevirt) was not gone 30 minutes after it was asked to go, "
                      "so the removal stopped. It still carries the finalizer kubevirt.io/virtOperatorFinalizer.", row["message"])
        self.assertIn("Removing the finalizer by hand lets the object go", row["message"])
        self.assertIn("Nothing was removed.", row["message"])
        self.assertIsNotNone(self.kube.object(CHARTS + "homestead-kubevirt"))

    def test_settings_not_created_by_homesteads_chart_are_kept(self):
        self.kube.add(kubevirt_cr(release=None))
        row = self.run_out(self.remove()["operation"]["id"])
        self.assertEqual("failed", row["status"])
        self.assertIn("was not created by Homestead's chart", row["message"])
        self.assertEqual([], self.deletes())

    def test_an_object_swapped_in_between_the_check_and_the_delete_survives(self):
        ident = self.remove()["operation"]["id"]
        real = REMOVAL._delete

        def swapped(path, found):
            # Checked and found Homestead's; then someone's own takes the name.
            self.kube.discard(path)
            self.kube.add(kubevirt_cr(release=None))
            return real(path, found)
        with mock.patch.object(REMOVAL, "_delete", swapped):
            self.job(ident)
        theirs = self.kube.object(KUBEVIRT)
        self.assertIsNotNone(theirs, "the new one is not the one checked")
        row = self.run_out(ident)
        self.assertEqual("failed", row["status"])
        self.assertEqual(theirs["metadata"]["uid"], self.kube.object(KUBEVIRT)["metadata"]["uid"])

    def test_a_vm_created_after_the_review_stops_it_before_anything_goes(self):
        ident = self.remove()["operation"]["id"]
        self.kube.add(vm("late"))
        row = self.run_out(ident)
        self.assertEqual("failed", row["status"])
        self.assertIn("lab/late", row["message"])
        self.assertEqual([], self.deletes())

    def test_a_chart_homestead_did_not_install_is_kept(self):
        self.kube.add(chart("homestead-macvtap", ours=False))
        row = self.run_out(self.remove()["operation"]["id"])
        self.assertEqual("succeeded", row["status"], row["message"])
        self.assertIsNotNone(self.kube.object(CHARTS + "homestead-macvtap"))
        self.assertIn("Kept: homestead-macvtap.", row["message"])

    def test_a_resource_type_still_in_use_is_kept(self):
        self.kube.add({"apiVersion": "cdi.kubevirt.io/v1", "kind": "CDI", "metadata": {"name": "other"}})
        row = self.run_out(self.remove()["operation"]["id"])
        self.assertIsNotNone(self.kube.object(CRD + "cdis.cdi.kubevirt.io"))
        self.assertIn("cdis.cdi.kubevirt.io (still in use)", row["message"])

    def test_only_the_two_settings_types_are_removed(self):
        self.run_out(self.remove()["operation"]["id"])
        crds = {path.rsplit("/", 1)[-1] for path, _ in self.deletes() if path.startswith(CRD)}
        self.assertEqual({"kubevirts.kubevirt.io", "cdis.cdi.kubevirt.io"}, crds)
        self.assertIsNotNone(self.kube.object(CRD + "virtualmachines.kubevirt.io"), "its operator's to remove")

    def test_a_removal_run_again_waits_on_one_already_going_and_names_what_holds_it(self):
        self.kube.add(kubevirt_cr(finalizers=["kubevirt.io/virtOperatorFinalizer"], deletionTimestamp="2026-11-10T10:00:00Z"))
        ident = self.remove()["operation"]["id"]
        self.assertEqual("Waiting for the KubeVirt settings object to be removed. It still carries the finalizer "
                         "kubevirt.io/virtOperatorFinalizer.", self.job(ident)["message"])
        self.assertEqual([], self.deletes())

    def test_cancel_stops_between_steps_and_says_what_was_removed(self):
        self.kube.add(kubevirt_cr(finalizers=["kubevirt.io/virtOperatorFinalizer"]))
        ident = self.remove()["operation"]["id"]
        self.assertEqual("forget", OPS.cancel_plan(ident)["mode"])
        self.assertIn("Stopped. Nothing was removed.", OPS.cancel(ident)["detail"])
        self.assertIsNotNone(self.kube.object(CHARTS + "homestead-kubevirt"))


if __name__ == "__main__":
    unittest.main()
