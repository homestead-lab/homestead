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
import homestead_helm as HELM
import homestead_kube as K
import homestead_macvtap as MACVTAP
from fakes.kube import Kube

CHARTS = "/apis/helm.cattle.io/v1/namespaces/kube-system/helmcharts"


def chart(name, **spec):
    return {"apiVersion": "helm.cattle.io/v1", "kind": "HelmChart",
            "metadata": {"namespace": "kube-system", "name": name, "uid": f"uid-{name}", "generation": 1,
                         "labels": {"homestead.io/managed": "true"}},
            "spec": {"chart": name, **spec}}


class Chart:
    """A chart on the fake API, and what can happen to it around a PUT."""

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

    def refused(self):
        with self.assertRaises(ValueError) as caught:
            self.write()
        self.assertEqual(1, self.puts(), "the retry writes nothing")
        return str(caught.exception)

    def puts(self):
        return len([w for w in self.kube.writes if w[0] == "PUT"])

    def on_put_do(self, action):
        done = []

        def answer(method, path, body):
            if not done:
                done.append(True)
                action()
                raise self.kube._error(409, "Conflict", "the object has been modified", path)
        self.kube.react("PUT", self.path, answer)


class Multus(Chart):
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


class Writes(Multus, unittest.TestCase):
    """The same cases at each site that PUTs a chart it read a moment ago: here the
    Multus repair, and below the others, which override what differs."""

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
        before = self.kube.object(self.path)["spec"]
        self.on_put(self.STATUS, {"status": {"jobName": "again"}})
        with self.assertRaises(ValueError) as caught:
            self.write()
        self.assertIn(self.NAME, str(caught.exception))
        self.assertIn("kept being updated", str(caught.exception))
        self.assertEqual(2, self.puts(), "one try, and one more")
        self.assertEqual(before, self.kube.object(self.path)["spec"], "nothing was written")

    def test_what_others_wrote_on_the_chart_survives_the_second_try(self):
        self.on_put({"metadata": {"labels": {"example.com/label": "theirs"},
                                  "annotations": {"example.com/note": "theirs"},
                                  "finalizers": ["example.com/hold"]},
                     **self.STATUS})
        self.write()
        saved = self.kube.object(self.path)
        self.assertEqual("theirs", saved["metadata"]["labels"]["example.com/label"])
        self.assertEqual("true", saved["metadata"]["labels"]["homestead.io/managed"])
        self.assertEqual("theirs", saved["metadata"]["annotations"]["example.com/note"])
        self.assertIn("example.com/hold", saved["metadata"]["finalizers"])
        self.assertEqual(2, self.puts(), "and it took the second try")
        self.assertIn(self.CHANGED, saved["spec"], "the change went in with them")

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

    def test_a_uid_that_cannot_be_read_is_not_taken_as_unchanged(self):
        stored = self.kube.object(self.path)
        stored["metadata"]["uid"] = None   # the fake leaves out what is given as None
        self.kube.add(stored)
        self.on_put(self.STATUS)
        self.assertIn("changed", self.refused())

    def test_a_read_that_fails_after_the_conflict_is_not_said_as_gone(self):
        self.on_put_do(lambda: self.kube.fail(self.path, 500, method="GET"))
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.write()
        self.assertEqual(500, caught.exception.code)
        self.assertEqual(1, self.puts())

    def test_a_second_try_that_fails_otherwise_is_not_said_as_more_updates(self):
        self.on_put(self.STATUS)
        self.kube.fail(self.path, 500, method="PUT", skip=1)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.write()
        self.assertEqual(500, caught.exception.code)
        self.assertEqual(2, self.puts())

    def test_any_other_error_is_left_alone(self):
        self.kube.fail(self.path, 500, method="PUT")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.write()
        self.assertEqual(500, caught.exception.code)


def disown(test):
    """Someone else takes the chart over: metadata only, so its generation stays."""
    test.on_put({"metadata": {"labels": {"homestead.io/managed": None}, "annotations": {"example.com/owner": "another"}}})


class MultusRepair(Multus, unittest.TestCase):
    """What only the Multus repair does. Its shared cases are Writes' own."""

    def test_the_multus_crd_chart_it_just_made_does_not_refuse_its_own_second_try(self):
        """The values are right already and only the NAD API is missing: the repair
        makes the multus-crd chart, which is one of the things its first check asks
        about, so that check can't be asked again after it."""
        stored = self.kube.object(self.path)
        stored["spec"]["valuesContent"] = ADDONS.K3S_MULTUS_VALUES
        self.kube.add(stored)
        self.kube.absent_groups.add("k8s.cni.cncf.io")
        self.on_put(self.STATUS)
        self.write()
        self.assertIn("multus-crd", ADDONS._helmcharts())
        self.assertEqual(2, self.puts(), "it took the second try")

    def test_a_chart_that_is_not_ours_is_refused_before_anything_is_written(self):
        stored = self.kube.object(self.path)
        stored["metadata"]["labels"] = {}
        self.kube.add(stored)
        with self.assertRaises(ValueError) as caught:
            self.write()
        self.assertIn("Only the original Homestead", str(caught.exception))
        self.assertEqual([], self.kube.writes)

    def test_custom_values_are_refused_before_anything_is_written(self):
        stored = self.kube.object(self.path)
        stored["spec"]["valuesContent"] = "custom: true\n"
        self.kube.add(stored)
        with self.assertRaises(ValueError) as caught:
            self.write()
        self.assertIn("Only the original Homestead", str(caught.exception))
        self.assertEqual([], self.kube.writes)

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

    def test_a_chart_someone_else_manages_is_refused_before_anything_is_written(self):
        stored = self.kube.object(self.path)
        stored["metadata"]["labels"] = {}
        self.kube.add(stored)
        with self.assertRaises(ValueError) as caught:
            self.write()
        self.assertIn("managed by", str(caught.exception))
        self.assertEqual([], self.kube.writes)

    def test_a_chart_no_longer_ours_is_not_upgraded_on_the_second_try(self):
        disown(self)
        self.assertIn("managed by", self.refused())


class HelmPageUpgrade(Writes):
    """The Helm page's Upgrade (#1309). It keeps the settings it replaces in an
    annotation for Cancel, so put_past_status, which keeps the fresh metadata,
    would drop them: it reads the chart again and writes the upgrade once more."""
    NAME = "grafana"
    CHANGED = "version"

    def objects(self):
        return [chart(self.NAME, version="8.0.0")]

    def bind(self):
        self.enterContext(mock.patch.multiple(HELM, kget=K.kget, ksend=K.ksend))

    def write(self):
        return HELM.upgrade({"namespace": "kube-system", "name": self.NAME, "version": "8.1.0"})

    def test_the_second_try_keeps_the_settings_to_go_back_to(self):
        self.on_put(self.STATUS)
        result = self.write()
        saved = self.kube.object(self.path)
        self.assertEqual(2, self.puts(), "it took the second try")
        self.assertEqual("8.1.0", saved["spec"]["version"])
        self.assertEqual({"version": "8.0.0"}, HELM.previous_spec(saved), "Cancel can still put it back")
        self.assertEqual(saved["metadata"]["generation"], result["chart"]["chart_generation"],
                         "the job records the chart as the second try left it")

    def summary_fits(self, *changes):
        """The refusal's first sentence stands alone under 90 characters for names up to 30."""
        self.on_put(*changes)
        with self.assertRaises(ValueError) as caught:
            self.write()
        first = str(caught.exception).split(". ")[0]
        self.assertLess(len(first) + 30 - len(self.NAME), 90, first)

    def test_a_change_in_between_is_refused_in_a_short_summary(self):
        self.summary_fits({"spec": {"version": "9.9.9"}})

    def test_status_writes_that_keep_coming_are_refused_in_a_short_summary(self):
        self.summary_fits(self.STATUS, {"status": {"jobName": "again"}})


if __name__ == "__main__":
    unittest.main()
