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


def crd(name, plural, group):
    return {"apiVersion": "apiextensions.k8s.io/v1", "kind": "CustomResourceDefinition", "metadata": {"name": name},
            "spec": {"group": group, "names": {"plural": plural}, "versions": [{"name": "v1", "served": True}]}}


def vm(name, ns="lab"):
    return {"apiVersion": "kubevirt.io/v1", "kind": "VirtualMachine", "metadata": {"name": name, "namespace": ns}}


class Removal(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.object(OPS, "DATA_DIR", self.enterContext(tempfile.TemporaryDirectory())))
        self.kube = self.enterContext(Kube(
            chart("homestead-kubevirt", ["kubevirts.kubevirt.io"]), chart("homestead-cdi", ["cdis.cdi.kubevirt.io"]),
            chart("homestead-macvtap"), kubevirt_cr(), cdi_cr(),
            crd("kubevirts.kubevirt.io", "kubevirts", "kubevirt.io"), crd("cdis.cdi.kubevirt.io", "cdis", "cdi.kubevirt.io"),
            groups=("kubevirt.io", "cdi.kubevirt.io", "helm.cattle.io", "apiextensions.k8s.io")))
        for name in ("kget", "ksend", "platform", "snapshot"):
            self.addCleanup(setattr, REMOVAL, name, getattr(REMOVAL, name))
        self.platform = {"kubevirt": True}
        REMOVAL.bind(K.kget, K.ksend, lambda: self.platform, OPS.snapshot)
        self.enterContext(mock.patch.dict(OPS.RESOLVERS, {}))
        self.enterContext(mock.patch.dict(OPS.CANCELLERS, {}))
        REMOVAL.register(OPS)
        self.now = 1000.0
        self.enterContext(mock.patch.object(REMOVAL, "_clock", lambda: self.now))

    def remove(self):
        return REMOVAL._remove(type("Request", (), {"body": {"confirm": "kubevirt"}})())

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
        found = REMOVAL.review("kubevirt")
        self.assertTrue(found["can"], found["blockers"])
        said = " ".join(found["removes"])
        for words in ("KubeVirt and CDI settings", "homestead-macvtap, homestead-kubevirt, homestead-cdi",
                      "resource types"):
            self.assertIn(words, said)
        self.assertIn("Anything Homestead did not create is kept, even under the same name.", found["keeps"])

    def test_it_refuses_with_a_vm_present_and_names_it(self):
        self.kube.add(vm("web"))
        found = REMOVAL.review("kubevirt")
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
        said = " ".join(REMOVAL.review("kubevirt")["blockers"])
        self.assertIn("1 VM disk (lab/disk)", said)
        self.assertIn("1 volume (lab/old-disk) holds a VM disk image. Delete it first", said)

    def test_a_list_it_cannot_read_refuses(self):
        self.kube.fail("/apis/kubevirt.io/v1/virtualmachines", 500, times=5)
        found = REMOVAL.review("kubevirt")
        self.assertFalse(found["can"])
        self.assertIn("Homestead could not check the virtual machines (HTTP 500), so nothing is removed.", found["blockers"])

    def test_harvester_and_a_kubevirt_homestead_did_not_install_are_refused(self):
        self.platform = {"kubevirt": True, "harvester": True}
        self.assertEqual(["Harvester manages KubeVirt, so Homestead does not remove it."], REMOVAL.review("kubevirt")["blockers"])
        self.platform = {"kubevirt": True}
        for name in ("homestead-kubevirt", "homestead-cdi", "homestead-macvtap"):
            self.kube.add(chart(name, ours=False))
        self.assertIn("KubeVirt was not installed by Homestead, so Homestead does not remove it.",
                      REMOVAL.review("kubevirt")["blockers"])


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
        self.assertEqual("Removing the KubeVirt settings", self.job(ident)["message"])
        self.now += 60
        self.assertEqual("Waiting for KubeVirt settings to be removed", self.job(ident)["message"])
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
        self.assertIn("KubeVirt settings was not gone 30 minutes after", row["message"])
        self.assertIn("Nothing was removed.", row["message"])
        self.assertIsNotNone(self.kube.object(CHARTS + "homestead-kubevirt"))

    def test_settings_not_created_by_homesteads_chart_are_kept(self):
        self.kube.add(kubevirt_cr(release=None))
        row = self.run_out(self.remove()["operation"]["id"])
        self.assertEqual("failed", row["status"])
        self.assertIn("were not created by Homestead's chart", row["message"])
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

    def test_a_resource_type_the_chart_did_not_carry_is_kept(self):
        self.kube.add(crd("virtualmachines.kubevirt.io", "virtualmachines", "kubevirt.io"))
        self.run_out(self.remove()["operation"]["id"])
        self.assertIsNotNone(self.kube.object(CRD + "virtualmachines.kubevirt.io"))

    def test_cancel_stops_between_steps_and_says_what_was_removed(self):
        self.kube.add(kubevirt_cr(finalizers=["kubevirt.io/virtOperatorFinalizer"]))
        ident = self.remove()["operation"]["id"]
        self.assertEqual("forget", OPS.cancel_plan(ident)["mode"])
        self.assertIn("Stopped. Nothing was removed.", OPS.cancel(ident)["detail"])
        self.assertIsNotNone(self.kube.object(CHARTS + "homestead-kubevirt"))


if __name__ == "__main__":
    unittest.main()
