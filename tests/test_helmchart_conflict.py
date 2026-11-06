"""helm-controller writes a HelmChart's status while it works, and every status
write moves the resourceVersion our PUT carries. That is not a change anyone
made, but it surfaced as a raw "HTTP 409". On the shared fake API."""
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_addons as ADDONS
import homestead_components as COMPONENTS
import homestead_kube as K
import homestead_macvtap as MACVTAP
from fakes.kube import Kube

CHARTS = "/apis/helm.cattle.io/v1/namespaces/kube-system/helmcharts"


def chart(name, **spec):
    return {"apiVersion": "helm.cattle.io/v1", "kind": "HelmChart",
            "metadata": {"namespace": "kube-system", "name": name, "uid": f"uid-{name}", "generation": 1,
                         "labels": {"homestead.io/managed": "true"}},
            "spec": {"chart": name, **spec}}


class Writes(unittest.TestCase):
    """The same cases at each site that PUTs a chart it read a moment ago: here the
    Multus repair, and below the others, which override what differs."""
    NAME = "multus"
    CHANGED = "valuesContent"

    def objects(self):
        old = ADDONS.K3S_MULTUS_VALUES.replace("    multusAutoconfigDir: /var/lib/rancher/k3s/agent/etc/cni/net.d\n", "")
        return [chart("multus", chart="rke2-multus", repo=ADDONS.RKE2_CHARTS, targetNamespace="kube-system",
                      version="v4.3.102", valuesContent=old)]

    def bind(self):
        self.kube.groups.add("k8s.cni.cncf.io")
        self.enterContext(mock.patch.multiple(ADDONS, kget=K.kget, ksend=K.ksend,
                                              platform=lambda *a: {"distribution": "k3s", "helm_controller": True}))

    def write(self):
        ADDONS.repair_multus()

    def setUp(self):
        self.kube = self.enterContext(Kube(*self.objects()))
        self.path = f"{CHARTS}/{self.NAME}"
        self.bind()

    def on_put(self, *changes):
        """Each PUT finds one of these written just before it, then the fake answers."""
        left = list(changes)

        def answer(method, path, body):
            if left:
                self.kube("PATCH", self.path, left.pop(0), ctype="application/merge-patch+json")
        self.kube.react("PUT", self.path, answer)

    STATUS = {"status": {"jobName": "helm-install"}}

    def test_a_status_write_in_between_is_not_a_conflict(self):
        self.on_put(self.STATUS)
        self.write()
        saved = self.kube.object(self.path)
        self.assertIn(self.CHANGED, saved["spec"], "the change went in")
        self.assertEqual("helm-install", saved["status"]["jobName"], "and the status was not written over")

    def test_a_real_change_in_between_is_refused_in_words_and_kept(self):
        self.on_put({"spec": {"version": "9.9.9"}})
        with self.assertRaises(ValueError) as caught:
            self.write()
        self.assertIn(self.NAME, str(caught.exception))
        self.assertIn("changed", str(caught.exception))
        saved = self.kube.object(self.path)
        self.assertEqual("9.9.9", saved["spec"]["version"])

    def test_status_writes_that_keep_coming_are_said_in_words(self):
        self.on_put(self.STATUS, {"status": {"jobName": "again"}})
        with self.assertRaises(ValueError) as caught:
            self.write()
        self.assertIn(self.NAME, str(caught.exception))

    def refused(self):
        with self.assertRaises(ValueError) as caught:
            self.write()
        self.assertEqual(1, len([w for w in self.kube.writes if w[0] == "PUT"]), "the retry writes nothing")
        return str(caught.exception)

    def on_put_do(self, action):
        done = []

        def answer(method, path, body):
            if not done:
                done.append(True)
                action()
                raise self.kube._error(409, "Conflict", "the object has been modified", path)
        self.kube.react("PUT", self.path, answer)

    def test_a_chart_deleted_and_made_again_is_not_written_over(self):
        def replaced():
            again = self.kube.object(self.path)
            self.kube.discard(self.path)
            again["metadata"].update(uid="another-uid", generation=1)
            self.kube.add(again)
        self.on_put_do(replaced)
        self.assertIn("changed", self.refused())
        self.assertEqual("another-uid", self.kube.object(self.path)["metadata"]["uid"])

    def test_a_chart_being_deleted_is_not_written_over(self):
        self.on_put_do(lambda: self.kube.add({**self.kube.object(self.path), "metadata": {
            **self.kube.object(self.path)["metadata"], "deletionTimestamp": "2026-11-06T00:00:00Z",
            "finalizers": ["example.com/hold"]}}))
        self.assertIn("being deleted", self.refused())

    def test_a_chart_gone_by_then_is_said_in_words(self):
        self.on_put_do(lambda: self.kube.discard(self.path))
        self.assertIn("gone", self.refused())

    def test_a_generation_that_cannot_be_read_is_not_taken_as_unchanged(self):
        stored = self.kube.object(self.path)
        stored["metadata"].pop("generation")
        self.kube.add(stored)
        self.on_put(self.STATUS)
        self.assertIn("changed", self.refused())

    def test_any_other_error_is_left_alone(self):
        self.kube.fail(self.path, 500, method="PUT")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.write()
        self.assertEqual(500, caught.exception.code)


def disown(test):
    """Someone else takes the chart over: metadata only, so its generation stays."""
    test.on_put({"metadata": {"labels": {"homestead.io/managed": None}, "annotations": {"example.com/owner": "another"}}})


class MultusRepair(Writes):
    def test_a_chart_no_longer_ours_is_not_repaired_on_the_second_try(self):
        disown(self)
        self.assertIn("Only the original Homestead", self.refused())

    def test_the_second_try_still_carries_the_repair(self):
        self.on_put(self.STATUS)
        self.write()
        self.assertIn("multusAutoconfigDir", self.kube.object(self.path)["spec"]["valuesContent"])


class Macvtap:
    NAME = MACVTAP.CHART
    CHANGED = "chartContent"

    def objects(self):
        return [chart(self.NAME)]

    def bind(self):
        for name in ("kget", "ksend", "addons", "platform"):
            self.addCleanup(setattr, MACVTAP, name, getattr(MACVTAP, name))
        addons = mock.Mock(chart_archive=lambda *a: {"archive": "new"}, preemption_hold=lambda what: None)
        MACVTAP.bind(K.kget, K.ksend, addons,
                     lambda force=False: {"distribution": "k3s", "kubevirt": True, "helm_controller": True})
        self.enterContext(mock.patch.object(MACVTAP, "NS", "kube-system"))
        self.enterContext(mock.patch.object(MACVTAP, "register_binding", lambda: None))
        self.enterContext(mock.patch.object(MACVTAP, "inspect", lambda: {"installed": False}))


class MacvtapUpgrade(Macvtap, Writes):
    def write(self):
        MACVTAP.upgrade("v0.14.0")


class MacvtapInstallAgain(Macvtap, Writes):
    def write(self):
        MACVTAP.install()


class OperatorUpgrade(Writes):
    NAME = COMPONENTS.CHARTS["kubevirt"]
    CHANGED = "chartContent"

    def objects(self):
        return [chart(self.NAME)]

    def bind(self):
        self.enterContext(mock.patch.multiple(COMPONENTS, kget=K.kget, ksend=K.ksend))
        self.enterContext(mock.patch.object(COMPONENTS, "HELM_NS", "kube-system"))
        self.enterContext(mock.patch.object(COMPONENTS.OPERATOR_MANIFESTS, "configuration", lambda *a: ""))
        self.enterContext(mock.patch.object(COMPONENTS, "addons", mock.Mock(
            KUBEVIRT="https://example.com/kv", CDI="https://example.com/cdi", fetch=lambda url: ("manifest",),
            chart_archive=lambda *a: {"archive": "new"})))

    def write(self):
        COMPONENTS._start_operator("kubevirt", "v1.2.3")

    def test_a_chart_no_longer_ours_is_not_upgraded_on_the_second_try(self):
        disown(self)
        self.assertIn("managed by", self.refused())


if __name__ == "__main__":
    unittest.main()
