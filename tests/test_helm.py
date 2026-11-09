import base64
import gzip
import io
import json
import sys
import tempfile
import unittest
import urllib.error
from types import SimpleNamespace
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import homestead_cancel as CANCEL
import homestead_deploy_server as DEPLOY
import homestead_helm as HELM
import homestead_kube as KUBE
import homestead_operations as OPS
import homestead_yaml as YAML
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fakes.kube import Kube
from rebind import keep_bound

MANIFEST = """---
# Source: grafana/templates/service.yaml
apiVersion: v1
kind: Service
metadata:
  name: grafana
  namespace: monitoring
  labels:
    app: grafana
spec: {}
---
# Source: grafana/templates/deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  labels:
    app: grafana
  name: grafana
"""


def secret(ns, name, version, status="deployed", chart_version="8.0.0", config=None):
    record = {"name": name, "namespace": ns, "version": version,
              "info": {"status": status, "last_deployed": f"2026-09-2{version}T10:00:00Z", "notes": "Open it on port 3000",
                       "description": "Install complete" if version == 1 else "Upgrade complete"},
              "chart": {"metadata": {"name": "grafana", "version": chart_version, "appVersion": "11.0.0",
                                     "icon": "https://example.com/g.png", "description": "Dashboards"},
                        "values": {"replicas": 1, "persistence": {"enabled": False}}},
              "config": config or {}, "manifest": MANIFEST}
    blob = base64.b64encode(gzip.compress(json.dumps(record).encode())).decode()
    return {"metadata": {"name": f"sh.helm.release.v1.{name}.v{version}", "namespace": ns,
                         "labels": {"owner": "helm", "name": name, "version": str(version), "status": status}},
            "data": {"release": base64.b64encode(blob.encode()).decode()}}


class HelmJobTests(unittest.TestCase):
    """The Helm job follows a Job the Helm controller makes. With no such Job it
    used to read "Waiting for the Helm controller" for ever: nothing raises, so
    no limit on failed reads applies. An uninstall that worked ended up there
    too, as the controller removes its Job and then the chart."""
    CHART = "/apis/helm.cattle.io/v1/namespaces/kube-system/helmcharts/grafana"
    LIMIT = 15 * 60
    # What a Job that has really ended failed reads. The controller's Jobs restart a
    # failing container in its own pod, so this is the end, and not `failed` by itself.
    JOB_FAILED = {"failed": 1, "conditions": [{"type": "Failed", "status": "True", "reason": "BackoffLimitExceeded"}]}
    # A pod lost to an eviction, a drain or a delete, before the replacement: the
    # fields a real Job's status has, with no count of the container's restarts in it.
    POD_LOST = {"failed": 1, "active": 0, "ready": 0, "terminating": 0, "uncountedTerminatedPods": {},
                "conditions": [{"type": "Suspended", "status": "False", "reason": "JobResumed"}]}

    def setUp(self):
        self.enterContext(mock.patch.object(OPS, "DATA_DIR", self.enterContext(tempfile.TemporaryDirectory())))
        self.kube = self.enterContext(Kube(groups=("helm.cattle.io",)))
        self.enterContext(mock.patch.object(HELM, "kget", KUBE.kget))
        self.enterContext(mock.patch.dict(OPS.RESOLVERS, {}))
        HELM.register(OPS)
        self.now = 1000.0
        self.enterContext(mock.patch.object(HELM, "_clock", lambda: self.now, create=True))

    def start(self, action="install", job="helm-install-grafana", chart=True):
        if chart:
            self.chart()
        return OPS.start("helm", f"Helm {action} grafana", {"kind": "HelmChart", "name": "grafana", "namespace": "kube-system"},
                         "/helm", {"namespace": "kube-system", "name": job, "action": action},
                         "Waiting for the Helm controller")["id"]

    def chart(self):
        self.kube.add({"apiVersion": "helm.cattle.io/v1", "kind": "HelmChart",
                       "metadata": {"namespace": "kube-system", "name": "grafana"}, "spec": {"chart": "grafana"}})

    def job_object(self, status=None, name="helm-install-grafana"):
        self.kube.add({"apiVersion": "batch/v1", "kind": "Job", "metadata": {"namespace": "kube-system", "name": name},
                       "status": status or {"active": 1}})

    def look(self, ident):
        return next(job for job in OPS.list_operations() if job["id"] == ident)

    def test_a_job_the_controller_never_makes_ends_failed_and_says_what_it_waited_for(self):
        ident = self.start()
        self.assertEqual("Waiting for the Helm controller", self.look(ident)["message"])
        self.now += self.LIMIT - 1
        self.assertEqual("running", self.look(ident)["status"], "a slow controller still gets its time")
        self.now += 1
        job = self.look(ident)
        self.assertEqual("failed", job["status"])
        self.assertIn("helm-install-grafana", job["message"])
        self.assertIn("15 minutes", job["message"])

    def test_a_job_removed_while_it_ran_ends_failed_after_the_same_wait(self):
        ident = self.start()
        self.job_object()
        self.assertEqual("Helm is running", self.look(ident)["message"])
        self.kube.discard("/apis/batch/v1/namespaces/kube-system/jobs/helm-install-grafana")
        self.look(ident)
        self.now += self.LIMIT
        self.assertEqual("failed", self.look(ident)["status"])

    def test_the_controller_replacing_its_job_does_not_end_it(self):
        # helm-controller deletes a Job and makes it again when the chart changes.
        ident = self.start()
        self.look(ident)
        self.now += self.LIMIT - 1
        self.job_object()
        self.assertEqual("running", self.look(ident)["status"])
        self.kube.discard("/apis/batch/v1/namespaces/kube-system/jobs/helm-install-grafana")
        self.look(ident)
        self.now += self.LIMIT - 1
        self.assertEqual("running", self.look(ident)["status"], "seeing the Job started the wait again")
        self.job_object({"succeeded": 1})
        self.assertEqual("succeeded", self.look(ident)["status"])

    def test_an_uninstall_still_waits_while_the_chart_is_there(self):
        ident = self.start("uninstall", "helm-delete-grafana")
        self.assertEqual("running", self.look(ident)["status"])
        self.now += self.LIMIT
        job = self.look(ident)
        self.assertEqual("failed", job["status"], "the chart is still there, so it did not finish")
        self.assertIn("helm-delete-grafana", job["message"])

    def test_an_install_whose_chart_was_removed_ends_failed_at_once(self):
        ident = self.start(chart=False)
        job = self.look(ident)
        self.assertEqual("failed", job["status"])
        self.assertIn("grafana", job["message"])

    # What a failed job still offers. A failed job has no Cancel by itself.
    def cancelling(self):
        self.enterContext(mock.patch.dict(OPS.CANCELLERS, {}))
        self.enterContext(mock.patch.object(OPS, "CLEANUPS", set()))
        self.enterContext(mock.patch.dict(OPS.VIEWS, {}))
        self.enterContext(mock.patch.multiple(CANCEL, kget=KUBE.kget, create=True))
        self.enterContext(mock.patch.object(HELM, "ksend", KUBE.ksend))
        self.enterContext(mock.patch.object(DEPLOY, "_cache", {}, create=True))
        CANCEL.register(OPS)

    def route(self, action, **body):
        """The real route, as the Helm page calls it."""
        request = SimpleNamespace(path=f"/api/helm/{action}", body={"namespace": "kube-system", "name": "grafana", **body})
        return DEPLOY._helm(request)["operation"]["id"]

    def upgraded(self):
        # As on a real cluster: a chart's last Job sits there, Complete, for weeks.
        self.cancelling()
        self.chart()
        self.job_object({"succeeded": 1})
        return self.route("upgrade", version="8.0.0")

    def failed_upgrade(self):
        ident = self.upgraded()
        self.look(ident)
        self.now += self.LIMIT
        self.assertEqual("failed", self.look(ident)["status"])
        self.kube.writes.clear()
        return ident

    def refused(self, ident, words):
        plan = OPS.cancel_plan(ident)
        self.assertFalse(plan["can"])
        self.assertEqual("Put back earlier settings", plan["review_title"], "not \"Stop tracking\"")
        self.assertIn(words, plan["why_not"])
        before = self.kube.object(self.CHART)
        with self.assertRaises(ValueError):
            OPS.cancel(ident)
        self.assertEqual([], self.kube.writes)
        self.assertEqual(before, self.kube.object(self.CHART))

    def test_a_failed_upgrade_with_its_chart_still_offers_to_put_it_back(self):
        ident = self.failed_upgrade()
        job = self.look(ident)
        self.assertTrue(job["cleanable"])
        self.assertEqual("rollback", OPS.cancel_plan(ident)["mode"])
        OPS.cancel(ident)
        self.assertNotIn("version", self.kube.object(self.CHART)["spec"], "as it was before the upgrade")
        job = self.look(ident)
        self.assertEqual("failed", job["status"])
        self.assertFalse(job["cleanable"], "once is enough")

    def test_the_controllers_own_writes_do_not_take_the_offer_away(self):
        # On a live cluster HelmCharts have a status subresource and the controller's
        # finalizer: neither moves the generation, so a normal failed upgrade still offers.
        ident = self.failed_upgrade()
        self.kube("PATCH", self.CHART, {"status": {"jobName": "helm-install-grafana"},
                                        "metadata": {"finalizers": ["wrangler.cattle.io/on-helm-chart-remove"]}},
                  ctype="application/merge-patch+json")
        self.assertEqual("rollback", OPS.cancel_plan(ident)["mode"])

    def test_an_upgrade_whose_helm_job_failed_offers_it_too(self):
        ident = self.upgraded()
        self.job_object(self.JOB_FAILED)
        job = self.look(ident)
        self.assertEqual("failed", job["status"])
        self.assertTrue(job["cleanable"])

    def test_a_chart_upgraded_since_is_not_put_back_over_that(self):
        ident = self.failed_upgrade()
        self.kube("PATCH", self.CHART, {"spec": {"version": "9.0.0"}}, ctype="application/merge-patch+json")
        self.kube.writes.clear()
        self.refused(ident, "changed since")
        self.assertEqual("9.0.0", self.kube.object(self.CHART)["spec"]["version"])

    def test_a_chart_made_again_since_is_not_put_back(self):
        ident = self.failed_upgrade()
        chart = self.kube.object(self.CHART)
        self.kube.discard(self.CHART)
        chart["metadata"]["uid"] = "another-uid"
        self.kube.add(chart)
        self.refused(ident, "changed since")

    def test_a_chart_changed_between_the_review_and_the_put_back_is_left(self):
        ident = self.failed_upgrade()
        mark = OPS._mark_cancelling

        def changed_now(match, *rest):
            self.kube("PATCH", self.CHART, {"spec": {"version": "9.0.0"}}, ctype="application/merge-patch+json")
            return mark(match, *rest)
        with mock.patch.object(OPS, "_mark_cancelling", changed_now), self.assertRaisesRegex(ValueError, "changed since"):
            OPS.cancel(ident)
        self.assertEqual("9.0.0", self.kube.object(self.CHART)["spec"]["version"])
        self.assertEqual("failed", self.look(ident)["status"])

    def test_an_upgrade_from_an_older_homestead_does_not_offer_it(self):
        # It kept no record of which chart it wrote. It never had this offer.
        self.cancelling()
        ident = self.start("upgrade")
        chart = self.kube.object(self.CHART)
        chart["metadata"]["annotations"] = {HELM.PREVIOUS_SPEC: '{"version":"7.0.0"}'}
        self.kube.add(chart)
        self.look(ident)
        self.now += self.LIMIT
        job = self.look(ident)
        self.assertEqual("failed", job["status"])
        self.assertFalse(job["cleanable"])
        self.kube.writes.clear()
        self.refused(ident, "older Homestead")

    def test_an_upgrade_that_failed_because_its_chart_is_gone_does_not_offer_it(self):
        ident = self.upgraded()
        self.kube.discard(self.CHART)
        job = self.look(ident)
        self.assertEqual("failed", job["status"])
        self.assertFalse(job["cleanable"])
        with self.assertRaises(ValueError):
            OPS.cancel(ident)

    def test_a_chart_gone_after_the_failure_is_refused_when_asked(self):
        # The job list reads no chart, under the jobs lock, on every poll: the review does.
        ident = self.failed_upgrade()
        self.kube.discard(self.CHART)
        plan = OPS.cancel_plan(ident)
        self.assertFalse(plan["can"])
        self.assertIn("gone", plan["why_not"])
        with self.assertRaises(ValueError):
            OPS.cancel(ident)
        self.assertEqual([], self.kube.writes)

    def test_a_failed_upgrade_with_nothing_kept_has_nothing_to_put_back(self):
        ident = self.failed_upgrade()
        chart = self.kube.object(self.CHART)
        chart["metadata"]["annotations"].pop(HELM.PREVIOUS_SPEC)
        self.kube.add(chart)
        self.refused(ident, "No earlier settings")

    def test_a_failed_upgrade_review_says_what_it_does(self):
        plan = OPS.cancel_plan(self.failed_upgrade())
        self.assertEqual("Put back earlier settings", plan["review_title"])
        self.assertEqual("Put back earlier settings", plan["action"])
        self.assertTrue(plan["cleanup_says_action"])
        self.assertEqual("This upgrade failed. Restoring brings back the chart's version and values, "
                         "and the job stays in the list as failed.", plan["lead"])

    def test_a_running_upgrades_cancel_keeps_its_own_words(self):
        plan = OPS.cancel_plan(self.upgraded())
        self.assertEqual("Cancel and put back", plan["action"])
        self.assertNotIn("cleanup_says_action", plan)

    # A running upgrade's Cancel: the earlier settings are of the chart as it wrote it.
    def second_upgrade(self):
        first = self.upgraded()
        return first, self.route("upgrade", version="9.0.0")

    def refused_while_running(self, ident):
        plan = OPS.cancel_plan(ident)
        self.assertFalse(plan["can"])
        self.assertIn("has changed since", plan["why_not"])
        before = self.kube.object(self.CHART)
        self.kube.writes.clear()
        with self.assertRaises(ValueError):
            OPS.cancel(ident)
        self.assertEqual([], self.kube.writes)
        self.assertEqual(before, self.kube.object(self.CHART))
        self.assertEqual("running", self.look(ident)["status"])

    def test_cancelling_a_running_upgrade_leaves_a_later_upgrade(self):
        first, _ = self.second_upgrade()
        self.refused_while_running(first)
        chart = self.kube.object(self.CHART)
        self.assertEqual("9.0.0", chart["spec"]["version"])
        self.assertEqual("8.0.0", HELM.previous_spec(chart)["version"], "its settings to go back to are kept")

    def test_the_later_upgrade_can_still_be_cancelled(self):
        first, second = self.second_upgrade()
        self.assertEqual("rollback", OPS.cancel_plan(second)["mode"])
        OPS.cancel(second)
        self.assertEqual("8.0.0", self.kube.object(self.CHART)["spec"]["version"])
        self.assertEqual("forget", OPS.cancel_plan(first)["mode"], "what it wrote is back, and nothing is kept")
        self.kube.writes.clear()
        OPS.cancel(first)
        self.assertEqual([], self.kube.writes)
        self.assertEqual("8.0.0", self.kube.object(self.CHART)["spec"]["version"])

    def test_a_change_by_hand_leaves_a_running_upgrades_cancel_refused(self):
        ident = self.upgraded()
        self.kube("PATCH", self.CHART, {"spec": {"version": "9.0.0"}}, ctype="application/merge-patch+json")
        self.refused_while_running(ident)
        self.assertEqual("9.0.0", self.kube.object(self.CHART)["spec"]["version"])

    def test_a_refused_cancel_still_ends_when_its_job_does(self):
        # Cancel is refused and no other button ends it, so the Job's own end has to.
        ident = self.upgraded()
        self.kube("PATCH", self.CHART, {"spec": {"version": "9.0.0"}}, ctype="application/merge-patch+json")
        self.job_object({"active": 1})
        self.assertEqual("Helm is running", self.look(ident)["message"])
        self.refused_while_running(ident)
        self.job_object({"failed": 1, "conditions": [{"type": "Failed", "status": "True"}]})
        self.assertEqual("failed", self.look(ident)["status"])
        self.assertIn("changed since", OPS.cancel_plan(ident)["why_not"], "and its put-back stays refused")

    def test_a_chart_made_again_is_not_put_back_by_a_running_upgrade(self):
        ident = self.upgraded()
        chart = self.kube.object(self.CHART)
        self.kube.discard(self.CHART)
        chart["metadata"]["uid"] = "another-uid"
        self.kube.add(chart)
        self.refused_while_running(ident)

    def test_a_status_write_does_not_stop_a_running_upgrades_cancel(self):
        # As in test_the_controllers_own_writes_do_not_take_the_offer_away: neither moves the generation.
        ident = self.upgraded()
        self.kube("PATCH", self.CHART, {"status": {"jobName": "helm-install-grafana"},
                                        "metadata": {"finalizers": ["wrangler.cattle.io/on-helm-chart-remove"]}},
                  ctype="application/merge-patch+json")
        self.assertEqual("rollback", OPS.cancel_plan(ident)["mode"])
        OPS.cancel(ident)
        self.assertNotIn("version", self.kube.object(self.CHART)["spec"], "as it was before the upgrade")

    def test_a_chart_changed_between_the_review_and_a_running_cancel_is_left(self):
        ident = self.upgraded()
        mark = OPS._mark_cancelling

        def changed_now(match, *rest):
            self.kube("PATCH", self.CHART, {"spec": {"version": "9.0.0"}}, ctype="application/merge-patch+json")
            return mark(match, *rest)
        with mock.patch.object(OPS, "_mark_cancelling", changed_now), self.assertRaisesRegex(ValueError, "changed since"):
            OPS.cancel(ident)
        self.assertEqual("9.0.0", self.kube.object(self.CHART)["spec"]["version"])
        self.assertEqual("running", self.look(ident)["status"])

    def test_a_running_upgrade_from_an_older_homestead_keeps_its_cancel(self):
        # It kept no record of which chart it wrote, so there is nothing to compare.
        self.cancelling()
        ident = self.start("upgrade")
        chart = self.kube.object(self.CHART)
        chart["metadata"]["annotations"] = {HELM.PREVIOUS_SPEC: '{"version":"7.0.0"}'}
        chart["metadata"]["generation"] = 9
        self.kube.add(chart)
        self.assertEqual("rollback", OPS.cancel_plan(ident)["mode"])
        OPS.cancel(ident)
        self.assertEqual("7.0.0", self.kube.object(self.CHART)["spec"]["version"])

    def test_earlier_settings_that_cannot_be_read_are_not_cleaned_up(self):
        ident = self.failed_upgrade()
        chart = self.kube.object(self.CHART)
        chart["metadata"]["annotations"][HELM.PREVIOUS_SPEC] = "not json"
        self.kube.add(chart)
        self.refused(ident, "No earlier settings")
        self.assertNotIn("Cleaned up", self.look(ident)["message"])

    def assert_closed_as_gone_back(self, ident):
        plan = OPS.cancel_plan(ident)
        self.assertTrue(plan["can"], plan.get("why_not"))
        self.assertEqual("rollback", plan["mode"])
        self.assertEqual("Close it", plan["action"])
        self.assertEqual("Close it", plan["review_title"])
        self.assertTrue(plan["cleanup_says_action"], "or the button reads \"Remove what it made\"")
        self.assertIn("are already restored", plan["lead"])
        self.assertEqual([], plan["undo"])
        self.assertEqual([], plan["keeps"], "nothing is left going for it to name")
        self.kube.writes.clear()
        OPS.cancel(ident)
        self.assertEqual([], self.kube.writes, "it went back once; nothing more is written")
        job = self.look(ident)
        self.assertEqual("failed", job["status"])
        self.assertEqual("Cleaned up after failing: grafana went back to its settings from before the upgrade", job["message"])
        self.assertFalse(job["cleanable"])

    def test_a_put_back_that_lost_its_reply_says_it_went_back(self):
        ident = self.failed_upgrade()
        self.kube.fail(self.CHART, TimeoutError("timed out"), method="PUT", applied=True)
        with self.assertRaisesRegex(ValueError, "timed out"):
            OPS.cancel(ident)
        self.assertNotIn("version", self.kube.object(self.CHART)["spec"], "the write stands")
        self.assert_closed_as_gone_back(ident)

    def cut_off_cancel(self, ident):
        """The server stopped while the put-back ran: the job stays marked, and that mark goes stale."""
        items = OPS._read()
        match = next(item for item in items if item["id"] == ident)
        OPS._mark_cancelling(match, {"action": "Put back earlier settings", "mode": "rollback"}, {}, "")
        match["cancel_started"] = 1
        OPS._write(items)

    def test_a_put_back_cut_off_after_its_write_says_it_went_back(self):
        ident = self.failed_upgrade()
        self.cut_off_cancel(ident)
        HELM.restore_previous(self.kube.object(self.CHART))
        self.assert_closed_as_gone_back(ident)

    def test_a_put_back_cut_off_before_its_write_still_puts_it_back(self):
        ident = self.failed_upgrade()
        self.cut_off_cancel(ident)
        self.assertEqual("Put back earlier settings", OPS.cancel_plan(ident)["action"])
        OPS.cancel(ident)
        self.assertNotIn("version", self.kube.object(self.CHART)["spec"])
        self.assertIn("is going back", self.look(ident)["message"])

    def test_settings_gone_and_changed_twice_are_not_called_its_own_put_back(self):
        ident = self.failed_upgrade()
        HELM.restore_previous(self.kube.object(self.CHART))
        self.kube("PATCH", self.CHART, {"spec": {"version": "9.0.0"}}, ctype="application/merge-patch+json")
        self.kube.writes.clear()
        self.refused(ident, "No earlier settings")

    def test_a_chart_made_again_is_not_called_its_own_put_back(self):
        ident = self.failed_upgrade()
        chart = self.kube.object(self.CHART)
        chart["metadata"]["annotations"].pop(HELM.PREVIOUS_SPEC)
        chart["metadata"]["generation"] += 1
        self.kube.discard(self.CHART)
        chart["metadata"]["uid"] = "another-uid"
        self.kube.add(chart)
        self.kube.writes.clear()
        self.refused(ident, "No earlier settings")

    def test_settings_gone_with_no_change_are_not_called_its_own_put_back(self):
        ident = self.failed_upgrade()
        chart = self.kube.object(self.CHART)
        chart["metadata"]["annotations"].pop(HELM.PREVIOUS_SPEC)
        self.kube.add(chart)
        self.refused(ident, "No earlier settings")

    # Asking for the same settings again.
    def kept(self):
        return self.kube.object(self.CHART)["metadata"]["annotations"][HELM.PREVIOUS_SPEC]

    def test_the_same_settings_again_keep_what_can_be_put_back(self):
        first = self.failed_upgrade()
        kept = self.kept()
        generation = self.kube.object(self.CHART)["metadata"]["generation"]
        self.route("upgrade", version="8.0.0")
        self.assertEqual(kept, self.kept())
        self.assertEqual(generation, self.kube.object(self.CHART)["metadata"]["generation"])
        self.assertEqual("Put back earlier settings", OPS.cancel_plan(first)["action"])
        OPS.cancel(first)
        self.assertNotIn("version", self.kube.object(self.CHART)["spec"], "the good settings, not the same ones again")

    def test_settings_really_changed_are_kept_as_before(self):
        self.failed_upgrade()
        self.route("upgrade", version="9.0.0")
        self.assertEqual('{"version":"8.0.0"}', self.kept())

    def no_change_upgrade(self):
        self.failed_upgrade()
        ident = self.route("upgrade", version="8.0.0")
        return ident

    def test_an_upgrade_that_changed_nothing_ends_at_once_and_says_why(self):
        ident = self.no_change_upgrade()
        job = self.look(ident)
        self.assertEqual("succeeded", job["status"], "nothing went wrong")
        self.assertEqual("This upgrade changed none of grafana's settings, so nothing ran; if its last upgrade failed, it still has", job["message"])

    def test_empty_values_from_an_unchanged_dialog_are_no_change_on_a_chart_with_none(self):
        # The dialog sends values "" for a chart with no valuesContent.
        self.cancelling()
        self.chart()
        self.job_object({"succeeded": 1})
        self.kube.writes.clear()
        ident = self.route("upgrade", values="")
        self.assertEqual([], self.kube.writes)
        job = self.look(ident)
        self.assertEqual("succeeded", job["status"])
        self.assertIn("changed none of", job["message"])

    def test_emptying_values_a_chart_has_is_a_change(self):
        self.cancelling()
        chart = {"apiVersion": "helm.cattle.io/v1", "kind": "HelmChart",
                 "metadata": {"namespace": "kube-system", "name": "grafana"},
                 "spec": {"chart": "grafana", "valuesContent": "a: 1\n"}}
        self.kube.add(chart)
        self.route("upgrade", values="")
        written = self.kube.object(self.CHART)
        self.assertEqual("", written["spec"]["valuesContent"])
        self.assertEqual('{"valuesContent":"a: 1\\n"}', written["metadata"]["annotations"][HELM.PREVIOUS_SPEC])

    def test_kept_empty_values_the_same_as_none_are_not_put_back(self):
        ident = self.failed_upgrade()
        chart = self.kube.object(self.CHART)
        chart["spec"].pop("version")
        chart["metadata"]["annotations"][HELM.PREVIOUS_SPEC] = '{"valuesContent":""}'
        self.kube.add(chart)
        self.refused(ident, "already the ones that were kept")

    def test_an_upgrade_that_changed_nothing_writes_nothing(self):
        ident = self.no_change_upgrade()
        self.assertEqual([], self.kube.writes)
        self.look(ident)
        self.assertEqual([], self.kube.writes)

    def test_an_upgrade_that_changed_nothing_is_not_held_up_by_its_job(self):
        # It follows no Job, so it neither reads one first nor takes an old one for its own.
        self.cancelling()
        self.chart()
        self.job_object(self.JOB_FAILED)
        self.kube.fail("/apis/batch/v1/namespaces/kube-system/jobs/helm-install-grafana", 500)
        ident = self.route("upgrade")
        self.kube.discard("/apis/batch/v1/namespaces/kube-system/jobs/helm-install-grafana")
        self.assertEqual("succeeded", self.look(ident)["status"])

    def test_an_upgrade_that_changed_nothing_has_nothing_of_its_own_to_put_back(self):
        ident = self.no_change_upgrade()
        job = self.look(ident)
        self.assertFalse(job["cleanable"])
        with self.assertRaises(ValueError):
            OPS.cancel_plan(ident)
        with self.assertRaises(ValueError):
            OPS.cancel(ident)
        self.assertEqual([], self.kube.writes)
        self.assertEqual("8.0.0", self.kube.object(self.CHART)["spec"]["version"])

    def test_cancelling_an_upgrade_that_changed_nothing_before_it_ended_writes_nothing(self):
        ident = self.no_change_upgrade()
        plan = OPS.cancel_plan(ident)
        self.assertEqual("forget", plan["mode"])
        self.assertIn("changed nothing", plan["keeps"][0])
        OPS.cancel(ident)
        self.assertEqual([], self.kube.writes)
        self.assertEqual("8.0.0", self.kube.object(self.CHART)["spec"]["version"])
        self.assertNotIn("carries on", self.look(ident)["message"])

    def test_kept_settings_the_same_as_the_chart_are_not_put_back(self):
        # What an older Homestead left: it overwrote them on a same-settings upgrade.
        ident = self.failed_upgrade()
        chart = self.kube.object(self.CHART)
        chart["metadata"]["annotations"][HELM.PREVIOUS_SPEC] = '{"version":"8.0.0"}'
        self.kube.add(chart)
        self.refused(ident, "already the ones that were kept")

    def test_kept_settings_that_are_not_a_mapping_wipe_nothing(self):
        ident = self.failed_upgrade()
        chart = self.kube.object(self.CHART)
        chart["metadata"]["annotations"][HELM.PREVIOUS_SPEC] = "[1]"
        self.kube.add(chart)
        self.refused(ident, "No earlier settings")
        self.assertEqual("8.0.0", self.kube.object(self.CHART)["spec"]["version"])

    # A lost pod is not the end of the Job: the controller goes on.
    def test_a_lost_pod_does_not_end_the_job(self):
        ident = self.upgraded()
        self.job_object(self.POD_LOST)
        job = self.look(ident)
        self.assertEqual("running", job["status"])
        self.assertFalse(job["cleanable"])
        self.assertIn("trying again", job["message"])

    def test_a_retry_that_works_ends_it_green_with_nothing_to_put_back(self):
        ident = self.upgraded()
        self.job_object(self.POD_LOST)
        self.assertEqual("running", self.look(ident)["status"])
        self.job_object({"failed": 1, "active": 1})
        job = self.look(ident)
        self.assertEqual("running", job["status"])
        self.assertEqual("Helm is running", job["message"])
        self.job_object({"failed": 1, "succeeded": 1})
        job = self.look(ident)
        self.assertEqual("succeeded", job["status"])
        self.assertFalse(job["cleanable"])

    def test_a_failed_condition_ends_it_failed_and_offers_the_put_back(self):
        ident = self.upgraded()
        self.job_object(self.JOB_FAILED)
        job = self.look(ident)
        self.assertEqual("failed", job["status"])
        self.assertTrue(job["cleanable"])
        self.assertEqual("Helm failed; the helm-install-grafana job's log in kube-system says why", job["message"])

    def test_a_failed_condition_that_is_false_does_not_end_it(self):
        ident = self.upgraded()
        conditions = self.POD_LOST["conditions"] + [{"type": "Failed", "status": "False"}]
        self.job_object({**self.POD_LOST, "conditions": conditions})
        self.assertEqual("running", self.look(ident)["status"])

    def test_a_failed_condition_ends_it_whatever_the_pod_count_says(self):
        ident = self.upgraded()
        self.job_object({**self.JOB_FAILED, "active": 1})
        self.assertEqual("failed", self.look(ident)["status"])

    def test_another_condition_that_is_true_does_not_end_it(self):
        ident = self.upgraded()
        self.job_object({**self.POD_LOST, "conditions": [{"type": "Suspended", "status": "True"}]})
        self.assertEqual("running", self.look(ident)["status"])

    def test_a_lost_pod_in_an_uninstall_still_ends_helm_removed(self):
        ident = self.start("uninstall", job="helm-delete-grafana")
        self.job_object(self.POD_LOST, name="helm-delete-grafana")
        self.assertEqual("running", self.look(ident)["status"])
        self.kube.discard("/apis/batch/v1/namespaces/kube-system/jobs/helm-delete-grafana")
        self.kube.discard(self.CHART)
        job = self.look(ident)
        self.assertEqual("succeeded", job["status"])
        self.assertIn("Helm removed grafana", job["message"])

    def test_a_lost_pod_in_an_install_is_still_cancellable(self):
        self.cancelling()
        ident = self.start("install")
        self.job_object(self.POD_LOST)
        self.assertEqual("running", self.look(ident)["status"])
        self.assertEqual("rollback", OPS.cancel_plan(ident)["mode"])

    # A Job from before this job's write is not its Job.
    def test_an_old_complete_job_does_not_finish_an_upgrade(self):
        ident = self.upgraded()
        job = self.look(ident)
        self.assertEqual("running", job["status"])
        self.assertEqual("Waiting for the Helm controller", job["message"])
        self.job_object(self.JOB_FAILED)
        job = self.look(ident)
        self.assertEqual("failed", job["status"], "the Job it caused decides")
        self.assertTrue(job["cleanable"])

    def test_an_old_failed_job_does_not_fail_an_upgrade_or_offer_a_put_back(self):
        self.cancelling()
        self.chart()
        self.job_object(self.JOB_FAILED)
        ident = self.route("upgrade", version="9.0.0")
        job = self.look(ident)
        self.assertEqual("running", job["status"])
        self.assertFalse(job["cleanable"])
        self.job_object({"succeeded": 1})
        job = self.look(ident)
        self.assertEqual("succeeded", job["status"])
        self.assertEqual("9.0.0", self.kube.object(self.CHART)["spec"]["version"])

    def test_a_job_never_replaced_ends_at_the_limit_in_true_words(self):
        ident = self.upgraded()
        self.look(ident)
        self.now += self.LIMIT
        job = self.look(ident)
        self.assertEqual("failed", job["status"])
        self.assertIn("did not replace its earlier job helm-install-grafana in kube-system", job["message"])
        self.assertIn("15 minutes", job["message"])
        self.assertIn("Check that the controller is running", job["message"])
        self.assertNotIn("had no job", job["message"], "it has one: the old one")

    def test_the_old_job_going_does_not_start_the_wait_again(self):
        ident = self.upgraded()
        self.look(ident)
        self.now += self.LIMIT - 1
        self.kube.discard("/apis/batch/v1/namespaces/kube-system/jobs/helm-install-grafana")
        self.assertEqual("running", self.look(ident)["status"])
        self.now += 1
        self.assertEqual("failed", self.look(ident)["status"])

    def test_an_older_job_with_no_record_follows_the_job_it_finds(self):
        # It kept no record of the Job before it: as it always did.
        ident = self.start("upgrade")
        self.job_object({"succeeded": 1})
        job = self.look(ident)
        self.assertEqual("succeeded", job["status"])

    def test_an_upgrade_refused_when_the_job_before_it_cannot_be_read_writes_nothing(self):
        self.cancelling()
        self.chart()
        self.kube.writes.clear()
        self.kube.fail("/apis/batch/v1/namespaces/kube-system/jobs/helm-install-grafana", 500)
        with self.assertRaises(urllib.error.HTTPError):
            self.route("upgrade", version="8.0.0")
        self.assertEqual([], self.kube.writes)

    def test_an_install_does_not_follow_a_job_left_from_before(self):
        self.cancelling()
        self.job_object({"succeeded": 1})
        ident = self.route("install", repo="https://example.com/charts", chart="grafana")
        self.assertEqual("running", self.look(ident)["status"])
        self.job_object({"active": 1})
        self.assertEqual("Helm is running", self.look(ident)["message"])

    def test_a_delete_job_left_from_before_is_no_proof_of_an_uninstall(self):
        self.cancelling()
        self.chart()
        self.job_object({"succeeded": 1}, name="helm-delete-grafana")
        ident = self.route("uninstall")
        job = self.look(ident)
        self.assertEqual("failed", job["status"])
        self.assertIn("did not see its uninstall run", job["message"])

    def test_a_job_that_cannot_be_read_is_not_a_job_that_is_missing(self):
        ident = self.start("upgrade", chart=False)
        self.kube.fail("/apis/batch/v1/namespaces/kube-system/jobs/helm-install-grafana", 500, times=5)
        job = self.look(ident)
        self.assertEqual("running", job["status"], "with no chart either, a 404 would fail it at once")
        self.assertIn("temporarily unavailable", job["message"])

    def test_a_running_upgrade_is_never_told_it_went_back(self):
        ident = self.upgraded()
        self.look(ident)
        HELM.restore_previous(self.kube.object(self.CHART))
        plan = OPS.cancel_plan(ident)
        self.assertEqual("forget", plan["mode"])
        self.assertNotIn("review_title", plan)

    def test_a_failed_install_does_not_offer_to_delete_its_chart(self):
        self.cancelling()
        ident = self.start()
        self.look(ident)
        self.now += self.LIMIT
        job = self.look(ident)
        self.assertEqual("failed", job["status"])
        self.assertFalse(job["cleanable"])
        self.kube.writes.clear()
        self.refused(ident, "nothing to put back")

    # Where the chart really is.
    def elsewhere(self):
        self.cancelling()
        self.kube.add({"apiVersion": "helm.cattle.io/v1", "kind": "HelmChart",
                       "metadata": {"namespace": "apps", "name": "grafana",
                                    "finalizers": ["wrangler.cattle.io/on-helm-chart-remove"]},
                       "spec": {"chart": "grafana", "targetNamespace": "kube-system"}})
        return "/apis/helm.cattle.io/v1/namespaces/apps/helmcharts/grafana"

    def test_a_chart_in_another_namespace_is_not_read_as_a_removed_one(self):
        self.elsewhere()
        ident = self.route("uninstall")
        job = self.look(ident)
        self.assertEqual("running", job["status"], "the chart is there, just not in the Helm controller's namespace")

    def test_its_job_is_followed_where_it_runs(self):
        self.elsewhere()
        ident = self.route("upgrade", version="8.0.0")
        self.kube.add({"apiVersion": "batch/v1", "kind": "Job", "metadata": {"namespace": "apps", "name": "helm-install-grafana"},
                       "status": {"active": 1}})
        self.assertEqual("Helm is running", self.look(ident)["message"])

    def test_and_put_back_where_it_is(self):
        path = self.elsewhere()
        ident = self.route("upgrade", version="8.0.0")
        self.look(ident)
        self.now += self.LIMIT
        self.assertEqual("failed", self.look(ident)["status"])
        OPS.cancel(ident)
        self.assertNotIn("version", self.kube.object(path)["spec"])

    # What proves an uninstall ran.
    def test_an_uninstall_the_controller_held_and_let_go_worked(self):
        self.cancelling()
        self.kube.add({"apiVersion": "helm.cattle.io/v1", "kind": "HelmChart",
                       "metadata": {"namespace": "kube-system", "name": "grafana",
                                    "finalizers": ["wrangler.cattle.io/on-helm-chart-remove"]}, "spec": {}})
        ident = self.route("uninstall")
        self.kube.discard(self.CHART)
        job = self.look(ident)
        self.assertEqual("succeeded", job["status"])
        self.assertEqual("Helm removed grafana", job["message"])

    def test_an_uninstall_whose_job_was_seen_worked(self):
        ident = self.start("uninstall", "helm-delete-grafana")
        self.job_object(name="helm-delete-grafana")
        self.look(ident)
        self.kube.discard("/apis/batch/v1/namespaces/kube-system/jobs/helm-delete-grafana")
        self.kube.discard(self.CHART)
        self.assertEqual("succeeded", self.look(ident)["status"])

    def test_a_chart_gone_with_no_sign_of_an_uninstall_does_not_say_it_worked(self):
        ident = self.start("uninstall", "helm-delete-grafana", chart=False)
        job = self.look(ident)
        self.assertEqual("failed", job["status"])
        self.assertIn("did not see its uninstall run", job["message"])
        self.assertNotIn("Helm removed", job["message"])

    def test_an_uninstall_of_a_chart_no_controller_took_up_does_not_say_it_worked(self):
        # No finalizer: nothing held the chart, so it goes at once and nothing ran.
        self.cancelling()
        self.chart()
        ident = self.route("uninstall")
        job = self.look(ident)
        self.assertEqual("failed", job["status"])
        self.assertIn("did not see its uninstall run", job["message"])

    def test_an_uninstall_seen_held_in_deletion_worked(self):
        # A job with no record of a finalizer, as one from before this has: seeing
        # the chart held in deletion is the proof.
        ident = self.start("uninstall", "helm-delete-grafana")
        chart = self.kube.object(self.CHART)
        chart["metadata"]["finalizers"] = ["wrangler.cattle.io/on-helm-chart-remove"]
        self.kube.add(chart)
        self.kube("DELETE", self.CHART)
        self.assertEqual("running", self.look(ident)["status"])
        self.kube.discard(self.CHART)
        job = self.look(ident)
        self.assertEqual("succeeded", job["status"])
        self.assertEqual("Helm removed grafana", job["message"])

    def test_an_older_uninstall_with_no_action_is_called_an_uninstall(self):
        # Jobs from before ref had "action": the job's name says what it is.
        ident = OPS.start("helm", "Helm uninstall grafana", {"kind": "HelmChart", "name": "grafana", "namespace": "kube-system"},
                          "/helm", {"namespace": "kube-system", "name": "helm-delete-grafana"},
                          "Waiting for the Helm controller")["id"]
        job = self.look(ident)
        self.assertEqual("failed", job["status"])
        self.assertIn("did not see its uninstall run", job["message"])
        self.assertNotIn("nothing will run", job["message"])

    def held_uninstall(self):
        self.cancelling()
        self.kube.add({"apiVersion": "helm.cattle.io/v1", "kind": "HelmChart",
                       "metadata": {"namespace": "kube-system", "name": "grafana",
                                    "finalizers": ["wrangler.cattle.io/on-helm-chart-remove"]}, "spec": {"chart": "grafana"}})
        return self.route("uninstall")

    def test_an_uninstall_whose_chart_was_made_again_says_so_at_once(self):
        ident = self.held_uninstall()
        self.kube.discard(self.CHART)
        self.chart()
        job = self.look(ident)
        self.assertEqual("succeeded", job["status"], "the chart it removed is gone; this is another one")
        self.assertEqual("Helm removed grafana; a chart of that name has been made since", job["message"])

    def test_an_unproved_uninstall_whose_chart_was_made_again_does_not_call_it_gone(self):
        self.cancelling()
        self.chart()
        ident = self.route("uninstall")
        self.chart()
        job = self.look(ident)
        self.assertEqual("failed", job["status"])
        self.assertIn("was removed and made again, but Homestead did not see its uninstall run", job["message"])

    def test_a_failed_uninstall_with_its_chart_recorded_offers_nothing(self):
        ident = self.held_uninstall()
        self.look(ident)
        self.now += self.LIMIT
        job = self.look(ident)
        self.assertEqual("failed", job["status"])
        self.assertFalse(job["cleanable"])
        self.kube.writes.clear()
        self.refused(ident, "nothing to put back")

    def test_no_helm_api_at_all_is_not_a_chart_that_is_gone(self):
        self.kube.absent_groups.add("helm.cattle.io")
        for action, job_name in (("uninstall", "helm-delete-grafana"), ("install", "helm-install-grafana")):
            with self.subTest(action=action):
                ident = self.start(action, job_name, chart=False)
                self.assertEqual("running", self.look(ident)["status"], "a 404 here says nothing about the chart")
                self.now += self.LIMIT
                job = self.look(ident)
                self.assertEqual("failed", job["status"])
                self.assertIn("controller", job["message"])
                self.assertNotIn("Helm removed", job["message"])
                self.now += 1

    def test_a_list_of_charts_that_cannot_be_read_says_nothing_about_the_job(self):
        ident = self.start("uninstall", "helm-delete-grafana", chart=False)
        self.kube.fail("/apis/helm.cattle.io/v1/namespaces/kube-system/helmcharts", 500, times=5)
        job = self.look(ident)
        self.assertEqual("running", job["status"], "not read is not gone")
        self.assertIn("temporarily unavailable", job["message"])

    def test_a_chart_that_cannot_be_read_says_nothing_about_the_job(self):
        ident = self.start("uninstall", "helm-delete-grafana", chart=False)
        self.kube.fail(self.CHART, 500, times=5)
        self.assertEqual("running", self.look(ident)["status"], "not read is not gone")


class HelmTests(unittest.TestCase):
    def setUp(self):
        self.secrets = [secret("monitoring", "grafana", 1, "superseded", "7.0.0"),
                        secret("monitoring", "grafana", 2, config={"adminUser": "me", "persistence": {"enabled": True, "size": "10Gi"},
                                                                   "hosts": ["a.lan", "b.lan"], "note": "yes: quoted"}),
                        secret("cattle-system", "rancher", 3)]
        self.charts = [{"metadata": {"name": "grafana", "namespace": "kube-system", "labels": {"homestead.io/managed": "true"}},
                        "spec": {"repo": "https://grafana.github.io/helm-charts", "chart": "grafana", "version": "8.0.0",
                                 "targetNamespace": "monitoring", "valuesContent": "adminUser: me\n"}}]
        self.sent = []

        def get(path):
            if path.startswith("/apis/helm.cattle.io"):
                return {"items": self.charts}
            if "/secrets" in path:
                ns = path.split("/namespaces/")[1].split("/")[0] if "/namespaces/" in path else None
                return {"items": [s for s in self.secrets if ns is None or s["metadata"]["namespace"] == ns]}
            if "/jobs/" in path:   # no Job there before the write
                raise urllib.error.HTTPError(path, 404, "NotFound", None, None)
            raise AssertionError(path)

        keep_bound(self, HELM)
        HELM.bind(get, lambda method, path, body=None, **kw: self.sent.append((method, path, body)) or body)

    def test_every_release_is_read_from_helms_own_secrets(self):
        rows = {r["name"]: r for r in HELM.releases()}
        g = rows["grafana"]
        self.assertEqual((2, "deployed", "8.0.0", "11.0.0", "homestead"),
                         (g["revision"], g["status"], g["chart_version"], g["app_version"], g["managed"]))
        self.assertFalse(g["system"])
        self.assertTrue(rows["rancher"]["system"])
        self.assertEqual("", rows["rancher"]["managed"])

    def test_a_release_shows_its_values_history_and_objects(self):
        r = HELM.release("monitoring", "grafana")
        self.assertEqual([2, 1], [h["revision"] for h in r["history"]])
        self.assertEqual([("Service", "grafana", "monitoring"), ("Deployment", "grafana", "")],
                         [(o["kind"], o["name"], o["namespace"]) for o in r["objects"]])
        self.assertEqual("adminUser: me\n", r["source"]["values"])
        # the values read back as the same YAML
        self.assertEqual({"adminUser": "me", "persistence": {"enabled": True, "size": "10Gi"},
                          "hosts": ["a.lan", "b.lan"], "note": "yes: quoted"}, YAML.loads(r["values"]))

    def test_installing_writes_a_helmchart_for_the_controller(self):
        result = HELM.install({"name": "immich", "namespace": "media", "repo": "https://immich-app.github.io/immich-charts",
                               "chart": "immich", "version": "0.9.3", "values": "persistence: {}\n"})
        method, path, body = self.sent[0]
        self.assertEqual(("POST", "/apis/helm.cattle.io/v1/namespaces/kube-system/helmcharts"), (method, path))
        self.assertEqual({"repo": "https://immich-app.github.io/immich-charts", "chart": "immich", "targetNamespace": "media",
                          "createNamespace": True, "valuesContent": "persistence: {}\n", "version": "0.9.3"}, body["spec"])
        self.assertEqual("true", body["metadata"]["labels"]["homestead.io/managed"])
        self.assertIn("immich", result["detail"])

    def test_an_oci_chart_names_its_address(self):
        HELM.install({"name": "podinfo", "namespace": "lab", "repo": "oci://ghcr.io/stefanprodan/charts", "chart": "podinfo"})
        spec = self.sent[0][2]["spec"]
        self.assertEqual("oci://ghcr.io/stefanprodan/charts/podinfo", spec["chart"])
        self.assertNotIn("repo", spec)

    def test_bad_installs_are_refused(self):
        for cfg in ({"name": "Bad Name", "namespace": "lab", "repo": "https://x", "chart": "c"},
                    {"name": "ok", "namespace": "lab", "repo": "ftp://x", "chart": "c"},
                    {"name": "ok", "namespace": "lab", "repo": "https://x", "chart": "c; rm -rf"},
                    {"name": "grafana", "namespace": "monitoring", "repo": "https://x", "chart": "grafana"}):
            with self.subTest(cfg=cfg), self.assertRaises(ValueError):
                HELM.install(cfg)
        self.assertEqual([], self.sent)

    def test_only_helmchart_releases_are_changed(self):
        HELM.upgrade({"namespace": "monitoring", "name": "grafana", "version": "8.1.0", "values": "adminUser: you\n"})
        method, path, body = self.sent[0]
        self.assertEqual(("PUT", "8.1.0", "adminUser: you\n"), (method, body["spec"]["version"], body["spec"]["valuesContent"]))
        with self.assertRaisesRegex(ValueError, "leaves it alone"):
            HELM.upgrade({"namespace": "cattle-system", "name": "rancher", "version": "9"})
        HELM.uninstall("monitoring", "grafana")
        self.assertEqual("DELETE", self.sent[-1][0])

    def test_search_reads_artifact_hub(self):
        HELM.bind(HELM.kget, HELM.ksend, lambda url, raw=False: {"packages": [
            {"name": "grafana", "version": "8.0.0", "app_version": "11", "description": "d", "package_id": "p1",
             "logo_image_id": "img", "repository": {"url": "https://grafana.github.io/helm-charts", "name": "grafana",
                                                    "verified_publisher": True, "display_name": "Grafana"}}]})
        HELM._cache.clear()
        rows = HELM.search("grafana")
        self.assertEqual(("grafana", "https://artifacthub.io/image/img", True), (rows[0]["repo_name"], rows[0]["logo"], rows[0]["verified"]))
        self.assertEqual([], HELM.search("g"))

    def test_searches_kept_are_bounded_and_the_least_recent_goes_first(self):
        asked = []
        HELM.bind(HELM.kget, HELM.ksend, lambda url, raw=False: asked.append(url) or {"packages": []})
        HELM._cache.clear()
        self.addCleanup(HELM._cache.clear)
        with mock.patch.object(HELM, "SEARCH_MAX", 3):
            for query in ("aa", "bb", "cc"):
                HELM.search(query)
            HELM.search("aa")            # a hit: "aa" is now the most recent
            HELM.search("dd")            # one too many: "bb" goes
            self.assertEqual(3, len(HELM._cache))
            self.assertEqual(4, len(asked))
            HELM.search("aa")
            self.assertEqual(4, len(asked), "a recent search is still kept")
            HELM.search("bb")
            self.assertEqual(5, len(asked), "the least recent one was let go")
            self.assertLessEqual(len(HELM._cache), 3)

    def test_a_search_longer_than_a_chart_name_is_not_sent(self):
        HELM.bind(HELM.kget, HELM.ksend, lambda url, raw=False: self.fail("not asked"))
        HELM._cache.clear()
        self.assertEqual([], HELM.search("x" * (HELM.QUERY_MAX + 1)))

    def test_artifact_hub_answers_are_capped(self):
        class Huge(io.BytesIO):
            headers = {"Content-Length": str(HELM.HUB_MAX_BYTES + 1)}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        with mock.patch.object(HELM.urllib.request, "urlopen", return_value=Huge(b"{}")):
            with self.assertRaises(ValueError):
                HELM._hub_get(HELM.HUB + "/packages/search?ts_query_web=x")


if __name__ == "__main__":
    unittest.main()
