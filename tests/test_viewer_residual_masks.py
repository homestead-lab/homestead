"""What a role below reveal still saw after the resource browser's masks, now hidden.

Builds on tests/test_viewer_helm_values.py (the same objects on the shared fake):
- annotations are shown in full only under named prefixes, not any subdomain
  of a known domain, and never when the key itself says it carries a command,
  headers or a credential;
- a mask says no exact length: a text shows "(hidden)", several lines a
  rounded count, and a masked map keeps only its own keys;
- a URL loses its user and password, and the values of secret-looking query
  parameters, wherever it appears (object, list columns, Helm's source);
- the Helm controller's install and delete pods print the chart's values, so
  their logs go only to the role that may reveal a Secret.
An admin and the reveal route see everything as before."""
import base64
import copy
import gzip
import io
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_agent_tools as TOOLS
import homestead_helm as HELM
import homestead_imports as IMPORTS
import homestead_joblogs as JOBLOGS
import homestead_operations as OPS
import homestead_overview_server as OVERVIEW
import homestead_resources as RES
import homestead_routes as ROUTER
import homestead_vms as VMS
import homestead_value_masks as MASKS
import homestead_workloads as WL
import server  # noqa: F401  (binds the agent tools and the route table)
import test_viewer_helm_values as BASE
from fakes.kube import Kube

LONG_VALUES = "".join(f"key{i}: value{i}\n" for i in range(47))
CHART = "/apis/helm.cattle.io/v1/namespaces/kube-system/helmcharts/app"
DEPLOYMENT = "/apis/apps/v1/namespaces/lab/deployments/web"
RELEASE = "/apis/helm.toolkit.fluxcd.io/v2/namespaces/flux/helmreleases/db"
WEBHOOK = "/apis/admissionregistration.k8s.io/v1/mutatingwebhookconfigurations/hook"

EXTRA = {
    # A second chart: credentials in its repository and chart addresses, the
    # copy of the previous values Homestead keeps during an upgrade, and
    # values long enough to round.
    "/apis/helm.cattle.io/v1/namespaces/kube-system/helmcharts/private": {
        "apiVersion": "helm.cattle.io/v1", "kind": "HelmChart",
        "metadata": {"name": "private", "namespace": "kube-system", "resourceVersion": "5",
                     "annotations": {"homestead.io/helm-previous": json.dumps({"valuesContent": "password: hunter2\n"}),
                                     "homestead.io/group": "web", "objectset.rio.cattle.io/hash": "abc123",
                                     "helmcharts.cattle.io/managed-by": "helm-controller"}},
        "spec": {"repo": "https://deploy:hunter2@charts.example.com/stable",
                 "chart": "https://charts.example.com/app-1.0.tgz?token=s3cr3t-t0ken&arch=amd64",
                 "valuesContent": LONG_VALUES}},
    # Annotations under well-known domains that still carry secrets.
    "/apis/apps/v1/namespaces/lab/deployments/api": {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": "api", "namespace": "lab", "resourceVersion": "5", "annotations": {
            "kubernetes.io/change-cause": "kubectl set env deployment/api TOKEN=s3cr3t-t0ken",
            "nginx.ingress.kubernetes.io/configuration-snippet": 'more_set_headers "X-Auth: hunter2";',
            "secrets.cattle.io/backup": "hunter2",
            "cdi.kubevirt.io/storage.import.extraHeaders": "Authorization: Bearer s3cr3t-t0ken",
            "deployment.kubernetes.io/revision": "7", "meta.helm.sh/release-name": "api",
            "field.cattle.io/projectId": "local:p-1", "kubevirt.io/latest-observed-api-version": "v1"}},
        "spec": {"replicas": 1, "template": {
            "metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": "2026-10-28T00:00:00Z",
                                         "nginx.ingress.kubernetes.io/server-snippet": "auth hunter2"}},
            "spec": {"containers": [{
                "name": "api", "image": "example/api:1",
                "args": ["--notify=https://hooks.example.com/post?sig=s3cr3t-t0ken&room=ops"],
                "env": [{"name": "DATABASE_URL", "value": "postgres://app:hunter2@db.example.com:5432/app"},
                        {"name": "HOME_PAGE", "value": "https://example.com/start?page=2"}]}]}}}},
    WEBHOOK: {
        "apiVersion": "admissionregistration.k8s.io/v1", "kind": "MutatingWebhookConfiguration",
        "metadata": {"name": "hook", "resourceVersion": "5"},
        "webhooks": [{"name": "hook.example.com", "clientConfig": {
            "url": "https://hook.example.com/mutate?api_key=s3cr3t-t0ken&X-Amz-Signature=hunter2"}}]},
    # Nothing on it is masked but a URL's credentials.
    "/apis/apps/v1/namespaces/lab/deployments/dburl": {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": "dburl", "namespace": "lab", "resourceVersion": "5",
                     "annotations": {"cdi.kubevirt.io/storage.import.endpoint": "https://images.example.com/d.img?token=s3cr3t-t0ken"}},
        "spec": {"replicas": 1, "template": {"spec": {"containers": [{
            "name": "db", "image": "example/db:1",
            "env": [{"name": "DATABASE_URL", "value": "postgres://app:hunter2@db.example.com:5432/app"}]}]}}}},
    "/apis/helm.toolkit.fluxcd.io/v2/namespaces/flux/helmreleases/users": {
        "apiVersion": "helm.toolkit.fluxcd.io/v2", "kind": "HelmRelease",
        "metadata": {"name": "users", "namespace": "flux", "resourceVersion": "5"},
        "spec": {"values": {"users": {"alice-the-admin": {"password": "hunter2"}}, "replicas": 1}}},
}
PRIVATE = "/apis/helm.cattle.io/v1/namespaces/kube-system/helmcharts/private"
API = "/apis/apps/v1/namespaces/lab/deployments/api"
USERS = "/apis/helm.toolkit.fluxcd.io/v2/namespaces/flux/helmreleases/users"
DBURL = "/apis/apps/v1/namespaces/lab/deployments/dburl"
SECRETS = ("hunter2", "s3cr3t-t0ken", "alice-the-admin")


def _path_parts(path):
    parts = path.strip("/").split("/")
    group, version = ("", parts[1]) if parts[0] == "api" else (parts[1], parts[2])
    namespaced = "namespaces" in parts
    return group, version, parts[-2], parts[-3] if namespaced else "", parts[-1]


class Base(BASE.Base):
    """The base file's objects plus EXTRA, on the shared fake."""

    def setUp(self):
        patcher = mock.patch.dict(BASE.OBJECTS, EXTRA)
        patcher.start()
        self.addCleanup(patcher.stop)
        super().setUp()

    def get(self, path, role, what="/api/resources/object"):
        group, version, resource, ns, name = _path_parts(path)
        query = {"group": [group], "version": [version], "resource": [resource], "ns": [ns], "name": [name]}
        return OVERVIEW.ROUTES[("GET", what)][1](ROUTER.Request("GET", what, query, {}, "someone", role))

    def assertHidden(self, answer):
        text = repr(answer)
        for secret in SECRETS:
            self.assertNotIn(secret, text)


class Annotations(Base):
    def test_a_viewer_gets_secret_carrying_annotations_under_known_domains_masked(self):
        for role in ("viewer", "operator", None):
            with self.subTest(role=role):
                answer = self.get(API, role)
                self.assertHidden(answer)
                notes = answer["object"]["metadata"]["annotations"]
                for key in ("kubernetes.io/change-cause", "nginx.ingress.kubernetes.io/configuration-snippet",
                            "secrets.cattle.io/backup", "cdi.kubevirt.io/storage.import.extraHeaders"):
                    self.assertEqual("••• (hidden)", notes[key], key)
                template = answer["object"]["spec"]["template"]["metadata"]["annotations"]
                self.assertEqual("••• (hidden)", template["nginx.ingress.kubernetes.io/server-snippet"])

    def test_plain_annotations_under_named_prefixes_stay(self):
        notes = self.get(API, "viewer")["object"]["metadata"]["annotations"]
        self.assertEqual("7", notes["deployment.kubernetes.io/revision"])
        self.assertEqual("api", notes["meta.helm.sh/release-name"])
        self.assertEqual("local:p-1", notes["field.cattle.io/projectId"])
        self.assertEqual("v1", notes["kubevirt.io/latest-observed-api-version"])
        chart = self.get(PRIVATE, "viewer")["object"]["metadata"]["annotations"]
        self.assertEqual("web", chart["homestead.io/group"])
        self.assertEqual("abc123", chart["objectset.rio.cattle.io/hash"])
        self.assertEqual("helm-controller", chart["helmcharts.cattle.io/managed-by"])

    def test_the_previous_values_kept_during_an_upgrade_are_masked(self):
        answer = self.get(PRIVATE, "viewer")
        self.assertEqual("••• (hidden)", answer["object"]["metadata"]["annotations"]["homestead.io/helm-previous"])
        self.assertHidden(answer)

    def test_the_prefix_rule(self):
        safe = RES._safe_annotation
        for key in ("deployment.kubernetes.io/revision", "kubernetes.io/description", "homestead.io/group",
                    "meta.helm.sh/release-name", "objectset.rio.cattle.io/id", "kube-vip.io/vipHost"):
            self.assertTrue(safe(key), key)
        for key in ("kubernetes.io/change-cause", "nginx.ingress.kubernetes.io/auth-url", "evil.k8s.io/x",
                    "anything.cattle.io/x", "secrets.cattle.io/x", "homestead.io/helm-previous",
                    "cdi.kubevirt.io/storage.import.extraHeaders", "longhorn.io/backup-token",
                    "kubevirt.io/client-password", "objectset.rio.cattle.io/applied", "example.com/x", "plain"):
            self.assertFalse(safe(key), key)


class Sizes(Base):
    def test_a_single_line_mask_says_no_length(self):
        chart = self.get(CHART, "viewer")["object"]
        self.assertEqual("••• (hidden)", chart["spec"]["chartContent"])
        self.assertEqual("••• (hidden)", chart["metadata"]["annotations"]["example.com/owner"])
        self.assertEqual("••• (hidden)", chart["metadata"]["annotations"]["objectset.rio.cattle.io/applied"])

    def test_line_counts_are_rounded(self):
        self.assertEqual("••• (hidden, under 10 lines)", self.get(CHART, "viewer")["object"]["spec"]["valuesContent"])
        self.assertEqual("••• (hidden, about 50 lines)", self.get(PRIVATE, "viewer")["object"]["spec"]["valuesContent"])
        for lines, said in ((1, "under 10 lines"), (9, "under 10 lines"), (10, "about 10 lines"), (14, "about 10 lines"),
                            (15, "about 20 lines"), (149, "about 100 lines"), (151, "about 200 lines"),
                            (1234, "about 1000 lines")):
            self.assertEqual(said, RES._about(lines), lines)

    def test_an_off_list_kinds_fields_say_no_exact_size(self):
        widget = self.get("/apis/widgets.example.com/v1/namespaces/lab/widgets/w1", "viewer")["object"]
        self.assertEqual("••• (hidden: needs the admin role, under 10 lines)", widget["spec"])
        machine = self.get("/apis/rke-machine-config.cattle.io/v1/namespaces/fleet-default/harvesterconfigs/pool1",
                           "viewer")["object"]
        self.assertEqual("••• (hidden: needs the admin role)", machine["cpuCount"])
        self.assertEqual("••• (hidden: needs the admin role, under 10 lines)", machine["userData"])

    def test_a_masked_map_keeps_only_its_own_keys(self):
        # Setting names show what a chart is configured with; anything below
        # them can be data (a map keyed by user name), so it is one mask.
        # A HelmRelease is off the safe list, so its spec is one note for a
        # viewer; the second layer is what any other reader of spec gets.
        for path, wanted in ((USERS, {"users": "•••", "replicas": "•••"}), (RELEASE, {"auth": "•••", "replicas": "•••"})):
            obj = self.c.objects[path]
            RES.hide_sensitive(obj, "helm.toolkit.fluxcd.io")
            self.assertEqual(wanted, obj["spec"]["values"])
            self.assertHidden(obj)
        self.assertEqual({"auth.adminPassword": "•••", "replicas": "•••"}, self.get(CHART, "viewer")["object"]["spec"]["set"])
        self.assertHidden(self.get(USERS, "viewer"))

    def test_an_admin_and_the_reveal_route_still_see_everything(self):
        for path in (USERS, PRIVATE, API, CHART):
            for answer in (self.get(path, "admin"), self.get(path, "admin", "/api/resources/reveal")):
                self.assertEqual(self.c.objects[path]["spec"] if "spec" in self.c.objects[path] else None,
                                 answer["object"].get("spec"))
                self.assertEqual(self.c.objects[path]["metadata"].get("annotations"),
                                 answer["object"]["metadata"].get("annotations"))

    def test_masking_twice_still_changes_nothing(self):
        once = self.get(PRIVATE, "viewer")["object"]
        again = copy.deepcopy(once)
        RES.hide_for_low_role(again, RES._kind("helm.cattle.io", "v1", "helmcharts"), "admin")
        self.assertEqual(once, again)


class Urls(Base):
    def test_strip(self):
        # Only a credential is masked: the login, and the value of a parameter a credential goes by.
        # From a part that is not plain (a credential word in the path, an odd host), the rest goes.
        strip = MASKS.strip_url_secrets
        cases = {
            "https://deploy:hunter2@charts.example.com/stable": "https://•••@charts.example.com/stable",
            "oci://robot@registry.example.com/charts": "oci://•••@registry.example.com/charts",
            "https://x.example.com/a?token=abc&page=2#top": "https://x.example.com/a?token=•••&page=2#top",
            "https://x.example.com/a?X-Amz-Signature=abc&X-Amz-Credential=def":
                "https://x.example.com/a?X-Amz-Signature=•••&X-Amz-Credential=•••",
            "see https://u:p@a.example.com and git://b.example.com?api_key=k1":
                "see https://•••@a.example.com and git://b.example.com?api_key=•••",
            "--repo=https://u:p@h.example.com/x?sig=1&password=2": "--repo=https://•••@h.example.com/x?sig=•••&password=•••",
            "https://example.com/start?page=2": "https://example.com/start?page=2",
            "https://example.com/path@v1/file": "https://•••@v1/file",
            "https://charts.example.com/stable": "https://charts.example.com/stable",
            "https://example.com/token/abc": "https://example.com/•••",
            "user@example.com and a?token=x without a URL": "user@example.com and a?token=x without a URL",
            "": "",
        }
        for given, wanted in cases.items():
            self.assertEqual(wanted, strip(given), given)
            self.assertEqual(wanted, strip(strip(given)), "stripping twice changes nothing")

    def test_a_viewer_sees_urls_without_credentials(self):
        chart = self.get(PRIVATE, "viewer")["object"]["spec"]
        self.assertEqual("https://•••@charts.example.com/stable", chart["repo"])
        self.assertEqual("https://charts.example.com/app-1.0.tgz?token=•••&arch=amd64", chart["chart"])
        api = self.get(API, "viewer")["object"]["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(["--notify=https://hooks.example.com/•••"], api["args"])
        self.assertEqual("postgres://•••@db.example.com:5432/app", api["env"][0]["value"])
        self.assertEqual("https://example.com/start?page=2", api["env"][1]["value"], "no credential: shown whole")
        hook = self.get(WEBHOOK, "viewer")["object"]["webhooks"][0]["clientConfig"]["url"]
        self.assertEqual("https://hook.example.com/mutate?api_key=•••&X-Amz-Signature=•••", hook)
        for path in (PRIVATE, API, WEBHOOK):
            answer = self.get(path, "viewer")
            self.assertHidden(answer)
            self.assertTrue(answer["values_hidden"])

    def test_an_admin_and_reveal_see_the_full_urls(self):
        for answer in (self.get(PRIVATE, "admin"), self.get(PRIVATE, "admin", "/api/resources/reveal")):
            self.assertEqual("https://deploy:hunter2@charts.example.com/stable", answer["object"]["spec"]["repo"])
        self.assertIn("api_key=s3cr3t-t0ken", self.get(WEBHOOK, "admin")["object"]["webhooks"][0]["clientConfig"]["url"])

    def test_list_columns_lose_url_credentials_below_reveal(self):
        table = {"columnDefinitions": [{"name": "Name", "priority": 0}, {"name": "Repo", "priority": 0}],
                 "rows": [{"object": {"metadata": {"name": "private", "namespace": "kube-system"}},
                           "cells": ["private", "https://deploy:hunter2@charts.example.com/stable"]}]}
        with mock.patch.object(RES, "table_get", lambda path: copy.deepcopy(table)):
            viewer = self.get(PRIVATE, "viewer", "/api/resources/list")
            admin = self.get(PRIVATE, "admin", "/api/resources/list")
        self.assertEqual("https://•••@charts.example.com/stable", viewer["rows"][0]["cells"][1])
        self.assertHidden(viewer)
        self.assertEqual("https://deploy:hunter2@charts.example.com/stable", admin["rows"][0]["cells"][1])

    def test_a_mask_that_is_not_what_is_stored_or_a_new_copy_is_refused(self):
        # A masked text that is what the stored one shows keeps the stored one (PartialStrip).
        group, version, resource, ns, name = _path_parts(DBURL)
        viewer = self.get(DBURL, "viewer")
        self.assertEqual("postgres://•••@db.example.com:5432/app",
                         viewer["object"]["spec"]["template"]["spec"]["containers"][0]["env"][0]["value"])
        annotation_only = self.get(DBURL, "admin")["yaml"].replace("https://images.example.com/d.img?token=s3cr3t-t0ken", "https://•••")
        with self.assertRaisesRegex(ValueError, "hidden"):
            RES.save_object(group, version, resource, ns, name, annotation_only)
        with self.assertRaisesRegex(ValueError, "hidden"):
            RES.create_objects(viewer["yaml"].replace("name: dburl", "name: dburl-copy"))
        self.assertEqual([], self.c.sent)

    def test_an_admins_copy_with_full_urls_still_saves(self):
        group, version, resource, ns, name = _path_parts(DBURL)
        RES.save_object(group, version, resource, ns, name, self.get(DBURL, "admin")["yaml"])
        self.assertEqual("postgres://app:hunter2@db.example.com:5432/app",
                         self.c.sent[0][2]["spec"]["template"]["spec"]["containers"][0]["env"][0]["value"])

    def test_helms_release_page_strips_the_source_address_for_a_viewer(self):
        record = {"name": "demo", "namespace": "lab", "version": 1, "info": {}, "config": {},
                  "chart": {"metadata": {"name": "demo"}}}
        secret = {"data": {"release": base64.b64encode(base64.b64encode(gzip.compress(json.dumps(record).encode()))).decode()}}
        chart = {"metadata": {"name": "demo", "namespace": "lab"},
                 "spec": {"repo": "https://deploy:hunter2@charts.example.com", "chart": "https://charts.example.com/d.tgz?token=s3cr3t-t0ken"}}
        with mock.patch.object(HELM, "kget", return_value={"items": [secret]}), \
                mock.patch.object(HELM, "helmcharts", return_value=[chart]):
            public = HELM.release("lab", "demo", include_sensitive=False)
            private = HELM.release("lab", "demo", include_sensitive=True)
        self.assertEqual("https://•••@charts.example.com", public["source"]["repo"])
        self.assertEqual("https://charts.example.com/d.tgz?token=•••", public["source"]["chart"])
        self.assertHidden(public)
        self.assertEqual(chart["spec"]["repo"], private["source"]["repo"])
        self.assertEqual(chart["spec"]["chart"], private["source"]["chart"])


LOG_TEXT = "2026-10-28T00:00:00Z + helm install --set auth.adminPassword=hunter2\n"
INSTALL_POD = {"apiVersion": "v1", "kind": "Pod",
               "metadata": {"name": "helm-install-app-x1", "namespace": "kube-system", "uid": "p1",
                            "labels": {"helmcharts.helm.cattle.io/chart": "app", "job-name": "helm-install-app"},
                            "creationTimestamp": "2026-10-28T00:00:00Z"},
               "spec": {"containers": [{"name": "helm"}]}, "status": {"phase": "Succeeded"}}
# A pod the Job made whose labels were changed: its Job's name still tells.
DELETE_POD = {"apiVersion": "v1", "kind": "Pod",
              "metadata": {"name": "helm-delete-app-z9", "namespace": "kube-system", "uid": "p3",
                           "ownerReferences": [{"apiVersion": "batch/v1", "kind": "Job", "name": "helm-delete-app", "uid": "j2"}]},
              "spec": {"containers": [{"name": "helm"}]}, "status": {"phase": "Succeeded"}}
APP_POD = {"apiVersion": "v1", "kind": "Pod",
           "metadata": {"name": "web-1", "namespace": "lab", "uid": "p2", "labels": {"app": "web"}},
           "spec": {"containers": [{"name": "web"}]}, "status": {"phase": "Running"}}


class Logs(unittest.TestCase):
    def setUp(self):
        self.kube = Kube(copy.deepcopy(INSTALL_POD), copy.deepcopy(DELETE_POD), copy.deepcopy(APP_POD))
        self.kube.__enter__()
        self.addCleanup(self.kube.__exit__, None, None, None)
        self.read = []

        def urlopen(request, **kwargs):
            self.read.append(request.full_url)
            return io.BytesIO(LOG_TEXT.encode())
        # server.py binds the workloads module to homestead_kube, which the fake answers.
        patch = mock.patch.object(WL.urllib.request, "urlopen", urlopen)
        patch.start()
        self.addCleanup(patch.stop)

    def logs(self, role, **query):
        handler = WL.ROUTES[("GET", "/api/logs")][1]
        return handler(ROUTER.Request("GET", "/api/logs", {k: [v] for k, v in query.items()}, {}, "someone", role))

    def test_a_viewer_cannot_read_a_helm_install_pods_log(self):
        for role in ("viewer", "operator", None):
            for query in ({"ns": "kube-system", "pod": "helm-install-app-x1"},
                          {"ns": "kube-system", "pod": "helm-install-app-x1", "container": "helm"},
                          {"ns": "kube-system", "job": "helm-install-app"},
                          {"ns": "kube-system", "pod": "helm-delete-app-z9", "container": "helm"}):
                with self.subTest(role=role, query=query):
                    answer = self.logs(role, **query)
                    self.assertEqual(403, answer.code)
                    self.assertNotIn("hunter2", repr(answer.body))
                    self.assertIn("admin", answer.body["error"])
        self.assertEqual([], self.read, "the log is never read for them")

    def test_a_pod_that_cannot_be_read_is_not_shown_below_reveal(self):
        answer = self.logs("viewer", ns="kube-system", pod="helm-install-gone", container="helm")
        self.assertEqual(409, answer.code)
        self.assertEqual([], self.read)

    def test_an_admin_still_reads_it(self):
        answer = self.logs("admin", ns="kube-system", pod="helm-install-app-x1")
        self.assertEqual(200, answer.code)
        self.assertIn("hunter2", answer.body)

    def test_an_ordinary_pods_log_is_unchanged_for_a_viewer(self):
        answer = self.logs("viewer", ns="lab", pod="web-1", container="web")
        self.assertEqual(200, answer.code)
        self.assertEqual(LOG_TEXT, answer.body)

    def test_an_agent_running_as_viewer_is_refused_too(self):
        # MCP reads logs through this route, under its key's role.
        with self.assertRaises(TOOLS.ToolError) as refused:
            TOOLS.call("GET", "/api/logs", {"ns": "kube-system", "pod": "helm-install-app-x1"}, user="api-key:agent", role="viewer")
        self.assertNotIn("hunter2", repr(refused.exception.args))
        self.assertEqual([], self.read)
        answer = TOOLS.call("GET", "/api/logs", {"ns": "kube-system", "pod": "helm-install-app-x1"}, user="api-key:agent", role="admin")
        self.assertIn("hunter2", repr(answer))


class JobLogs(unittest.TestCase):
    """The job tray's log of a Helm job reads the same pod."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        saved = OPS.DATA_DIR, dict(OPS.LOGGERS), (JOBLOGS.kget, JOBLOGS.ktext)
        self.addCleanup(lambda: (setattr(OPS, "DATA_DIR", saved[0]), OPS.LOGGERS.clear(), OPS.LOGGERS.update(saved[1]),
                                 JOBLOGS.bind(*saved[2])))
        OPS.DATA_DIR = self.tmp.name
        pods = {"helm-install-app": INSTALL_POD, "web": APP_POD}

        def get(path):
            for job, pod in pods.items():
                if f"job-name%3D{job}" in path:
                    return {"items": [copy.deepcopy(pod)]}
            return {"items": []}
        JOBLOGS.bind(get, lambda path: LOG_TEXT)
        JOBLOGS.register(OPS)
        OPS.LOGGERS["plain"] = JOBLOGS._ref_job(title="Copy")
        self.helm = OPS.start("helm", "Helm install app", {}, "/", {"namespace": "kube-system", "name": "helm-install-app"})["id"]
        self.plain = OPS.start("plain", "Copy", {}, "/", {"namespace": "lab", "name": "web"})["id"]

    def log(self, op, role):
        handler = OPS.ROUTES[("GET", "/api/operations/log")][1]
        return handler(ROUTER.Request("GET", "/api/operations/log", {"id": [op]}, {}, "someone", role))

    def test_a_viewer_gets_a_note_instead_of_helms_output(self):
        for role in ("viewer", "operator", None):
            with self.subTest(role=role):
                answer = self.log(self.helm, role)
                self.assertNotIn("hunter2", repr(answer))
                self.assertEqual("", answer["sources"][0]["text"])
                self.assertIn("admin", answer["sources"][0]["note"])

    def test_an_admin_gets_it(self):
        self.assertIn("hunter2", self.log(self.helm, "admin")["sources"][0]["text"])

    def test_other_jobs_output_is_unchanged_for_a_viewer(self):
        self.assertEqual(LOG_TEXT, self.log(self.plain, "viewer")["sources"][0]["text"])


# Found by the review.
RANCHER_POD = {"apiVersion": "v1", "kind": "Pod",
               "metadata": {"name": "helm-operation-abcde", "namespace": "cattle-system", "uid": "p4"},
               "spec": {"containers": [{"name": "helm"}]}, "status": {"phase": "Succeeded"}}

# Forms the reviews found in earlier, finer-grained masks; each must leave no credential.
REVIEW_FORMS = {
    "https://proxy.example.com/?next=https://u:pw@b.example.com/": "pw@",
    "https://proxy.example.com/?next=https%3A%2F%2Fu%3Apw%40b.example.com%2F": "pw",
    "https://app.example.com/cb#access_token=abc123&x=1": "abc123",
    "Endpoint=sb://ns.example.com/;SharedAccessKeyName=r;SharedAccessKey=abc=": "abc=",
    "https://user:p@ss@host.example.com/x": "p@ss",
    "see (https://h.example.com/?token=abc), then": "abc",
    "https://h.example.com/?token=ab)c]d}e&x=1": "ab)",
    "https://u:12'cdef@h.example.com/x": "cdef",
    'https://u:ab"cdef@h.example.com/x': "cdef",
    "https://u:ab\u00a0cdef@h.example.com/x": "cdef",
    'https://h.example.com/?a=1"&token=SECRET': "SECRET",
    "https://h.example.com/?a=1\u00a0&token=SECRET": "SECRET",
    "https://h.example.com/p<x;sig=SECRET": "SECRET",
    "https://h.example.com/fetch/https%3A%2F%2Fuser%3Asecret%40example.com/x": "secret",
    "https://h.example.com/?https://u:p@h2": "u:p",
    "https://h.example.com/#https://u:p@h2": "u:p",
    "https://h.example.com/?a=1&https://u:p@h2": "u:p",
    "https://h.example.com/r/https:/u:p@evil.example.com": "u:p",
    "https://h\\a;SharedAccessKey=SECRET": "SECRET",
    "https://h.example.com/?key%5B%5D=SECRET&code[]=SECRET": "SECRET",
    "https://h.example.com/?hmac=SECRET&otp=SECRET&ticket=SECRET": "SECRET",
    "https://h.example.com/?%2574oken=SECRET": "SECRET",
    "https:\\\\user:pass@h.example.com/x": "pass",
    "https:/user:pass@h.example.com/x": "pass",
    "https://h.example.com/?next=https%25253A%25252F%25252Fu%25253Ap%252540e": "u%",
}


class ReviewStrip(unittest.TestCase):
    def test_every_form_the_reviews_found_leaves_no_credential(self):
        strip = MASKS.strip_url_secrets
        for given, secret in REVIEW_FORMS.items():
            with self.subTest(given=given):
                once = strip(given)
                self.assertNotIn(secret, once)
                self.assertEqual(once, strip(once))
                self.assertTrue(MASKS.has_stripped(once))

    def test_a_colon_in_the_authority_hides_the_token_unless_it_is_a_port(self):
        strip = MASKS.strip_url_secrets
        for shown in ("https://h.example.com:8443/x", "https://h.example.com:8443", "http://198.51.100.7:80/a/b"):
            self.assertEqual(shown, strip(shown))
        for given in ("failed: https://user:secret", "https://user:secret/x", "https://h.example.com:abc/x",
                      "https://h.example.com:/x", "https://a:b:8443/x", "https:\\\\user:secret"):
            with self.subTest(given=given):
                once = strip(given)
                self.assertNotIn("secret", once)
                self.assertNotIn(":abc", once)
                self.assertIn("://•••", once)
                self.assertEqual(once, strip(once))

    def test_a_token_runs_to_ascii_whitespace_only(self):
        # A stretch after '"', "<" or a non-ASCII space is part of the token, so it is checked with it.
        self.assertEqual('see https://h.example.com/?a=•••&token=••• next', MASKS.strip_url_secrets('see https://h.example.com/?a=1"&token=S next'))
        self.assertEqual("https://charts.example.com/stable\tnext", MASKS.strip_url_secrets("https://charts.example.com/stable\tnext"))
        # In JSON an address ends with its string: nothing to hide stays as it is; a login goes alone.
        self.assertEqual('{"url":"https://h.example.com/x","n":1}', MASKS.strip_url_secrets('{"url":"https://h.example.com/x","n":1}'))
        self.assertEqual('{"url":"https://•••@h.example.com/x","n":1}', MASKS.strip_url_secrets('{"url":"https://u:p@h.example.com/x","n":1}'))

    def test_a_text_too_long_to_check_is_hidden_whole(self):
        text = "x" * (MASKS.MAX_TEXT + 1) + " https://u:hunter2@h.example.com/"
        self.assertEqual(MASKS.TOO_LONG, MASKS.strip_url_secrets(text))
        self.assertTrue(MASKS.has_stripped(MASKS.TOO_LONG))
        long_plain = "x" * (MASKS.MAX_TEXT + 1)
        self.assertEqual(long_plain, MASKS.strip_url_secrets(long_plain), "no address: nothing to hide")

    def test_cutting_and_masking_agree(self):
        # Review 5, finding 1: cut() and the mask use the same token rule, so cutting before
        # masking gives what masking before cutting gives, and a cut never splits a token.
        texts = ["failed: https://user:1234@h.example.com/x more", "x" * 30, "short",
                 "a https://h.example.com/ok b https://u:p@h.example.com/x c", "https://u:p\u00a0q@h.example.com/x tail",
                 'pull "https://u:p@h.example.com/x" failed', "https:\\\\u:p@h.example.com x"]
        for text in texts:
            for length in range(0, len(text) + 2):
                with self.subTest(text=text, length=length):
                    cut_first = MASKS.strip_url_secrets(MASKS.cut(text, length))
                    whole = {m.group(0) for m in MASKS.TOKEN.finditer(text)}
                    self.assertNotIn("u:p", cut_first)
                    self.assertNotIn("1234", cut_first)
                    for found in MASKS.TOKEN.finditer(MASKS.cut(text, length).rstrip("…")):
                        self.assertIn(found.group(0), whole, "a cut never leaves part of a token")
        self.assertEqual("failed: …", MASKS.cut("failed: https://user:1234@h.example.com/x", 20))
        self.assertEqual("x" * 20, MASKS.cut("x" * 30, 20))

    def test_squeezing_spaces_never_splits_a_token(self):
        # Review 5, finding 2: collapsing whitespace before masking must not leave part of a token behind.
        message = "import  failed:\thttps://u:ab\u00a0cdef@h.example.com/x\n done"
        squeezed = MASKS.squeeze(message)
        self.assertEqual(MASKS.strip_url_secrets(squeezed), MASKS.squeeze(MASKS.strip_url_secrets(message)))
        self.assertNotIn("cdef", MASKS.strip_url_secrets(squeezed))
        vm = {"status": {"conditions": [{"type": "Failure", "status": "True", "message": message}]}}
        problem = VMS._problem(vm, {})
        self.assertNotIn("cdef", MASKS.strip_url_secrets(problem))

    def test_an_events_message_is_cut_without_splitting_an_address(self):
        event = {"apiVersion": "v1", "kind": "Event", "metadata": {"name": "e", "namespace": "lab"},
                 "involvedObject": {"name": "disk"}, "message": "x" * 380 + " https://user:1234@h.example.com/x",
                 "lastTimestamp": "2026-10-28T00:00:00Z"}
        with mock.patch.object(RES, "kget", lambda path: {"items": [copy.deepcopy(event)]}):
            self.assertNotIn("1234", MASKS.strip_url_secrets(RES.events_for("lab", "disk")[0]["message"]))

    def test_saved_masks_are_recognised_and_admin_text_is_not(self):
        self.assertTrue(MASKS.has_stripped("see https://•••"))
        for text in ("https://h.example.com/?page=2 and •••", "Loading •••", "x=•••", "a/•••"):
            self.assertFalse(MASKS.has_stripped(text), text)

    def test_adversarial_text_is_handled_in_linear_time(self):
        # Anyone who can write an annotation or an event message writes this text.
        n = 200_000
        texts = ["a" * n + "://", "x://" * (n // 4), "https://h/" + "?a" * (n // 2), "https://" + "@" * n,
                 "https://u:p@" * (n // 12), "https://h/?" + "a=" * (n // 2), "https://h/?next=" + "%3A%2F%2F" * (n // 9),
                 "https://h/?token=" + ")" * n, ("https://h/?a=https://h/?b=" * (n // 26)), "https://h/?" + "[" * n + "=v",
                 "?a" * (n // 2) + "=•••", "https://" + "a:" * (n // 2), "a:/" * (n // 3), ('a://"' * (n // 5)),
                 "https://x " * (n // 10), "p" * n]
        for text in texts:
            started = time.monotonic()
            stripped = MASKS.strip_url_secrets(text)
            MASKS.has_stripped(stripped)
            MASKS.has_stripped(text)
            MASKS.cut(text, n // 2)
            MASKS.squeeze(text)
            self.assertLess(time.monotonic() - started, 1.5, text[:40])


class ReviewLogs(Logs):
    def setUp(self):
        super().setUp()
        self.kube.add(copy.deepcopy(RANCHER_POD))

    def test_names_that_could_step_out_of_the_pod_are_refused_for_everyone(self):
        for role in ("viewer", "admin"):
            for query in ({"ns": "kube-system/secrets?x=", "pod": "p", "container": "c"},
                          {"ns": "kube-system", "pod": "helm-install-app-x1/../../secrets", "container": "helm"},
                          {"ns": "kube-system", "pod": "p?x=", "container": "helm"},
                          {"ns": "kube-system", "job": "helm-install-app&x=1"},
                          {"ns": "lab", "pod": "web-1", "container": "web&previous=true"}):
                with self.subTest(role=role, query=query):
                    answer = self.logs(role, **query)
                    self.assertEqual(400, answer.code)
        self.assertEqual([], self.read)

    def test_rancher_helm_operation_pods_need_the_reveal_role(self):
        self.assertEqual(403, self.logs("viewer", ns="cattle-system", pod="helm-operation-abcde", container="helm").code)
        self.assertEqual(200, self.logs("admin", ns="cattle-system", pod="helm-operation-abcde", container="helm").code)


VM_IMAGE = {"metadata": {"name": "img", "namespace": "lab", "creationTimestamp": "2026-10-28T00:00:00Z"},
            "spec": {"displayName": "img", "url": "https://u:hunter2@images.example.com:8443/x.img?token=s3cr3t-t0ken",
                     "sourceType": "download"},
            "status": {"storageClassName": "", "conditions": [
                {"type": "Imported", "status": "False", "reason": "ImportFailed",
                 "message": "GET https://u:hunter2@images.example.com:8443/x.img?token=s3cr3t-t0ken failed"}]}}


class VmImages(Base):
    """The VM image lists keep no credential of an image's address, for any role (at the source),
    and the routes strip again below reveal."""

    def setUp(self):
        super().setUp()
        import homestead_vm_create as VMC
        self.vmc = VMC
        get = lambda path: {"items": [copy.deepcopy(VM_IMAGE)]} if path.endswith("/virtualmachineimages") else {"items": []}
        patch = mock.patch.object(VMC.I, "kget", get)
        patch.start()
        self.addCleanup(patch.stop)
        for key in ("vmimages", "vmimg"):
            ROUTER.forget(key)
            self.addCleanup(ROUTER.forget, key)

    def assertClean(self, answer):
        text = repr(answer)
        for secret in ("hunter2", "s3cr3t-t0ken", "u:"):
            self.assertNotIn(secret, text)
        self.assertNotRegex(text, "token=(?!•••)", "a token's name may show, never its value")

    def test_the_lists_hold_no_credential_for_anyone(self):
        listed = self.vmc.list_vm_images()
        self.assertClean(listed)
        self.assertEqual("https://•••@images.example.com:8443/x.img?token=•••", listed[0]["url"])
        cached = self.vmc.vm_image_cache()
        self.assertClean(cached)
        self.assertEqual("images.example.com:8443", cached["images"][0]["source"])

    def test_the_routes_hold_no_credential_for_a_viewer_or_an_admin(self):
        import homestead_vm_review as REVIEW
        import homestead_vm_server as VM_SERVER
        with mock.patch.object(VM_SERVER.PLATFORM, "detect", return_value={"harvester": True, "cdi": True}), \
                mock.patch.object(REVIEW, "create_options", lambda: {"images": self.vmc.list_vm_images()}):
            for role in ("viewer", "admin"):
                for module, path in ((IMPORTS, "/api/images/vm"), (IMPORTS, "/api/vmimages"), (REVIEW, "/api/vm/create-options")):
                    with self.subTest(role=role, path=path):
                        handler = module.ROUTES[("GET", path)][1]
                        self.assertClean(handler(ROUTER.Request("GET", path, {}, {}, "someone", role)))

    def test_the_image_store_still_matches_its_own_images_by_their_full_address(self):
        listed = self.vmc.list_vm_images(raw=True)
        self.assertEqual(VM_IMAGE["spec"]["url"], listed[0]["url"])

    def test_the_image_stores_own_list_shows_the_host_only(self):
        import homestead_vmstore as VMSTORE
        self.assertEqual("images.example.com:8443", VMSTORE._source({"url": VM_IMAGE["spec"]["url"]}))
        self.assertEqual("upload", VMSTORE._source({"url": "", "source": ""}))


class ReviewLists(Base):
    def test_the_helm_list_strips_a_pending_charts_address(self):
        chart = {"metadata": {"name": "demo", "namespace": "kube-system"},
                 "spec": {"chart": "oci://robot:hunter2@registry.example.com/charts/demo", "targetNamespace": "lab"}}
        with mock.patch.object(HELM, "kget", return_value={"items": []}), \
                mock.patch.object(HELM, "helmcharts", return_value=[chart]):
            rows = HELM.releases()
        self.assertEqual("oci://•••@registry.example.com/charts/demo", rows[0]["chart"])

    def test_event_messages_lose_url_credentials_below_reveal(self):
        event = {"apiVersion": "v1", "kind": "Event",
                 "metadata": {"name": "disk.1", "namespace": "lab"},
                 "involvedObject": {"name": "disk"}, "type": "Warning", "reason": "Failed",
                 "message": "Unable to connect to https://u:hunter2@images.example.com/d.img?token=s3cr3t-t0ken",
                 "lastTimestamp": "2026-10-28T00:00:00Z"}
        with mock.patch.object(RES, "kget", lambda path: {"items": [copy.deepcopy(event)]}):
            handler = RES.ROUTES[("GET", "/api/resources/events")][1]
            ask = lambda role: handler(ROUTER.Request("GET", "/api/resources/events",
                                                      {"ns": ["lab"], "name": ["disk"]}, {}, "someone", role))
            viewer, admin = ask("viewer"), ask("admin")
        self.assertEqual("Unable to connect to https://•••@images.example.com/d.img?token=•••", viewer[0]["message"])
        self.assertIn("hunter2", admin[0]["message"])

    def vm_route(self, path, role, query=None):
        handler = VMS.ROUTES[("GET", path)][1]
        return handler(ROUTER.Request("GET", path, {k: [v] for k, v in (query or {}).items()}, {}, "someone", role))

    def test_a_vms_page_strips_every_address_for_a_viewer(self):
        address = "https://u:hunter2@images.example.com/x?token=s3cr3t-t0ken"
        vm = {"metadata": {"name": "vm", "namespace": "lab"}, "spec": {"template": {"spec": {}}},
              "status": {"conditions": [{"type": "Failure", "status": "True", "message": f"import from {address}"}]}}
        events = [{"type": "Warning", "reason": "Failed", "message": f"import from {address}"}]
        filling = [{"disk": "root", "message": f"copying {address}", "source": address}]
        with mock.patch.object(VMS, "_get", return_value=vm), mock.patch.object(VMS, "kget", return_value={}), \
                mock.patch.object(VMS, "_row", side_effect=lambda *a, **k: {"disks": [], "source": address}), \
                mock.patch.object(VMS, "_claims", return_value={}), \
                mock.patch.object(VMS, "_datavolumes", return_value={}), \
                mock.patch.object(VMS, "_filling", side_effect=lambda *a, **k: copy.deepcopy(filling)), \
                mock.patch.object(VMS, "events_for", lambda *a: copy.deepcopy(events)), \
                mock.patch.object(VMS, "_read_cloud_init", return_value={}):
            viewer = self.vm_route("/api/vm", "viewer", {"ns": "lab", "name": "vm"})
            admin = self.vm_route("/api/vm", "admin", {"ns": "lab", "name": "vm"})
        self.assertHidden(viewer)
        self.assertEqual("import from https://•••@images.example.com/x?token=•••", viewer["events"][0]["message"])
        self.assertIn("https://•••", viewer["conditions"][0]["message"])
        self.assertIn("https://•••", viewer["filling"][0]["message"])
        for text in (admin["events"][0]["message"], admin["conditions"][0]["message"], admin["filling"][0]["source"], admin["source"]):
            self.assertIn("hunter2", text)

    def test_a_vm_disk_cannot_be_made_from_a_stripped_address(self):
        import homestead_vm_edit as VM_EDIT
        with mock.patch.object(VM_EDIT.V, "platform", return_value={"cdi": True}):
            for url in ("https://•••", "see https://•••"):
                with self.subTest(url=url), self.assertRaisesRegex(ValueError, "hidden"):
                    VM_EDIT._disk_volume({"spec": {"template": {"spec": {}}}}, "lab", "d", "10Gi", "", {"url": url}, [], [])

    def test_vm_disk_and_image_lists_strip_addresses_for_a_viewer(self):
        disks = [{"name": "disk", "message": "Unable to connect to https://u:hunter2@images.example.com/x.img"}]
        images = [{"name": "img", "url": "https://u:hunter2@images.example.com/x.img?token=s3cr3t-t0ken"}]
        for key in ("vmimg",):
            ROUTER.forget(key)
            self.addCleanup(ROUTER.forget, key)
        with mock.patch.object(IMPORTS, "list_vm_disks", lambda: copy.deepcopy(disks)), \
                mock.patch.object(IMPORTS, "list_vm_images", lambda: copy.deepcopy(images)):
            for path in ("/api/vm-disks", "/api/vmimages"):
                handler = IMPORTS.ROUTES[("GET", path)][1]
                ask = lambda role: handler(ROUTER.Request("GET", path, {}, {}, "someone", role))
                viewer, admin, again = ask("viewer"), ask("admin"), ask("viewer")
                with self.subTest(path=path):
                    self.assertHidden(viewer)
                    self.assertHidden(again)
                    self.assertIn("https://•••", repr(viewer))
                    self.assertIn("hunter2", repr(admin))

    def test_the_vm_list_strips_addresses_for_a_viewer_without_touching_the_shared_copy(self):
        rows = [{"name": "vm", "namespace": "lab", "filling": [{"message": "copying https://u:hunter2@images.example.com/x"}]}]
        ROUTER.forget("vms")
        self.addCleanup(ROUTER.forget, "vms")
        with mock.patch.object(VMS, "list_vms", lambda: copy.deepcopy(rows)):
            viewer = self.vm_route("/api/vms", "viewer")
            admin = self.vm_route("/api/vms", "admin")
            again = self.vm_route("/api/vms", "viewer")
        self.assertHidden(viewer)
        self.assertHidden(again)
        self.assertIn("hunter2", repr(admin))


# The outage annotation as #942 keeps it: the token lives in a Secret, the
# annotation holds the address with its path hidden and the Secret's name.
OUTAGE_JSON = json.dumps({"webhook": "https://gotify.example.com/(hidden)", "format": "gotify",
                          "secret": "homestead-outage-app-web"}, separators=(",", ":"))
OUTAGE_APP = "/apis/apps/v1/namespaces/lab/deployments/outage"


class PartialStrip(Base):
    """Only a credential is masked: the login of an address and the values of
    parameters a credential goes by. The rest of the address stays, and a
    masked copy saved back keeps what is stored."""

    def setUp(self):
        extra = {OUTAGE_APP: {
            "apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": "outage", "namespace": "lab", "resourceVersion": "5",
                         "annotations": {"homestead.io/outage-actions": OUTAGE_JSON}},
            "spec": {"replicas": 1}}}
        patcher = mock.patch.dict(EXTRA, extra)
        patcher.start()
        self.addCleanup(patcher.stop)
        super().setUp()

    def test_an_address_without_a_credential_is_shown_whole(self):
        for text in ("https://charts.example.com/stable/app-1.0.tgz", "https://h.example.com:8443/a/b?page=2&arch=amd64",
                     "oci://registry.example.com/charts/app", "see https://example.com/start?page=2 now",
                     "https://example.com/a/b#section-2", OUTAGE_JSON,
                     '{"url":"https://h.example.com/x","n":1}'):
            with self.subTest(text=text):
                self.assertEqual(text, MASKS.strip_url_secrets(text))
                self.assertFalse(MASKS.has_stripped(text))

    def test_only_the_login_and_secret_parameters_are_masked(self):
        cases = {
            "https://deploy:hunter2@charts.example.com/stable": "https://•••@charts.example.com/stable",
            "oci://robot@registry.example.com/charts": "oci://•••@registry.example.com/charts",
            "https://x.example.com/a?token=abc&page=2#top": "https://x.example.com/a?token=•••&page=2#top",
            "https://x.example.com/a?X-Amz-Signature=abc&X-Amz-Credential=def":
                "https://x.example.com/a?X-Amz-Signature=•••&X-Amz-Credential=•••",
            "see https://u:p@a.example.com and git://b.example.com?api_key=k1":
                "see https://•••@a.example.com and git://b.example.com?api_key=•••",
            "https://app.example.com/cb#access_token=abc123&x=1": "https://app.example.com/cb#access_token=•••&x=•••",
            "postgres://app:hunter2@db.example.com:5432/app": "postgres://•••@db.example.com:5432/app",
            '{"url":"https://u:p@h.example.com/x","n":1}': '{"url":"https://•••@h.example.com/x","n":1}',
        }
        for given, wanted in cases.items():
            with self.subTest(given=given):
                once = MASKS.strip_url_secrets(given)
                self.assertEqual(wanted, once)
                self.assertEqual(once, MASKS.strip_url_secrets(once))
                self.assertTrue(MASKS.has_stripped(once))

    def test_forms_masked_in_part_still_hide_the_credential(self):
        forms = {
            "https://h.example.com/a;jsessionid=SECRET": "SECRET", "https://h.example.com/?KEY=SECRET": "SECRET",
            "https://h.example.com/?a=b?token=SECRET": "SECRET", "https://h.example.com/x#/r?token=SECRET": "SECRET",
            "https://u:pa/ss@h.example.com/": "pa", "https://u:pa#ss@h.example.com/": "pa",
            '{"u":"https:\\/\\/u:SECRET@h.example.com"}': "SECRET", '{"https://u:SECRET@h":1}': "SECRET",
            '{"u":"https://u:SECRET@h","u":"x"}': "SECRET", "HTTPS://u:SECRET@h.example.com": "SECRET",
            '["see https://h.example.com/?sig=SECRET"]': "SECRET",
        }
        for given, secret in forms.items():
            with self.subTest(given=given):
                once = MASKS.strip_url_secrets(given)
                self.assertNotIn(secret, once)
                self.assertEqual(once, MASKS.strip_url_secrets(once))
                self.assertTrue(MASKS.has_stripped(once))
        for text in ("[" * 200_000 + '"https://u:SECRET@h"', '{"a":' * 50_000 + '"https://u:SECRET@h"' + "}" * 50_000):
            started = time.monotonic()
            self.assertNotIn("SECRET", MASKS.strip_url_secrets(text))
            self.assertLess(time.monotonic() - started, 1.5)

    def test_a_viewer_sees_the_rest_of_the_address(self):
        api = self.get(API, "viewer")["object"]["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(["--notify=https://hooks.example.com/•••"], api["args"])
        self.assertEqual("postgres://•••@db.example.com:5432/app", api["env"][0]["value"])
        self.assertEqual("https://example.com/start?page=2", api["env"][1]["value"])

    def test_a_masked_copy_saved_back_keeps_the_stored_values(self):
        group, version, resource, ns, name = _path_parts(DBURL)
        viewer = self.get(DBURL, "viewer")
        self.assertHidden(viewer)
        RES.save_object(group, version, resource, ns, name, viewer["yaml"])
        sent = self.c.sent[-1][2]
        self.assertEqual("postgres://app:hunter2@db.example.com:5432/app",
                         sent["spec"]["template"]["spec"]["containers"][0]["env"][0]["value"])
        self.assertEqual("https://images.example.com/d.img?token=s3cr3t-t0ken",
                         sent["metadata"]["annotations"]["cdi.kubevirt.io/storage.import.endpoint"])
        self.assertFalse(MASKS.has_stripped(sent))

    def test_a_changed_masked_value_is_refused(self):
        group, version, resource, ns, name = _path_parts(DBURL)
        viewer = self.get(DBURL, "viewer")["yaml"]
        for changed in (viewer.replace("db.example.com:5432/app", "evil.example.com:5432/app"),
                        viewer.replace("d.img?token=•••", "e.img?token=•••")):
            with self.subTest(changed=changed[:0]), self.assertRaisesRegex(ValueError, "hidden"):
                RES.save_object(group, version, resource, ns, name, changed)
        self.assertEqual([], self.c.sent)

    def test_the_outage_annotation_round_trips(self):
        group, version, resource, ns, name = _path_parts(OUTAGE_APP)
        viewer = self.get(OUTAGE_APP, "viewer")
        self.assertEqual(OUTAGE_JSON, viewer["object"]["metadata"]["annotations"]["homestead.io/outage-actions"])
        RES.save_object(group, version, resource, ns, name, viewer["yaml"])
        self.assertEqual(OUTAGE_JSON, self.c.sent[-1][2]["metadata"]["annotations"]["homestead.io/outage-actions"])


# Built in pieces so secret scanners do not read the fixture as a real webhook.
SLACK = "https://hooks.slack.com/services/" + "T0000AAAA/B0000BBBB/" + "X" * 24
GOTIFY = "https://gotify.example.com/message?token=AbCdEf123456"
HOOKS_APP = "/apis/apps/v1/namespaces/lab/deployments/hooks"


class FailClosed(Base):
    """The second pass: secrets in a path, JSON with escapes or a key given
    twice, every parameter value but a few harmless names, an "@" anywhere,
    and list items matched by their name when a copy is saved back."""

    def setUp(self):
        extra = {HOOKS_APP: {
            "apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": "hooks", "namespace": "lab", "resourceVersion": "5"},
            "spec": {"replicas": 1, "template": {"spec": {"containers": [{
                "name": "notify", "image": "example/notify:1",
                "env": [{"name": "SLACK_URL", "value": SLACK}, {"name": "GOTIFY_URL", "value": GOTIFY},
                        {"name": "OTHER_SLACK", "value": SLACK.replace("XXXX", "YYYY")}],
                "args": [SLACK, GOTIFY]}]}}}}}
        patcher = mock.patch.dict(EXTRA, extra)
        patcher.start()
        self.addCleanup(patcher.stop)
        super().setUp()

    def assertMasked(self, given, secret, wanted=None):
        once = MASKS.strip_url_secrets(given)
        self.assertNotIn(secret, once)
        self.assertEqual(once, MASKS.strip_url_secrets(once), "stripping twice changes nothing")
        self.assertTrue(MASKS.has_stripped(once))
        if wanted is not None:
            self.assertEqual(wanted, once)

    def test_a_secret_in_a_path_is_hidden(self):
        cases = [
            (SLACK, "XXXX", "https://hooks.slack.com/•••"),
            ("https://discord.com/api/webhooks/123456/abcDEF-ghi_jkl", "abcDEF", "https://discord.com/api/webhooks/•••"),
            ("https://api.telegram.org/bot123456:ABC-DEF1234ghIkl/sendMessage", "ABC-DEF", "https://api.telegram.org/•••"),
            ("https://example.com/hooks/9f8e7d6c5b4a39281706f5e4", "9f8e7d6c", "https://example.com/hooks/•••"),
            ("https://example.com/webhook/abc", "abc", "https://example.com/webhook/•••"),
            ("https://ntfy.sh/my-alerts-topic", "my-alerts", "https://ntfy.sh/•••"),
            ("https://example.com/x/a1b2c3d4e5f6g7h8i9/y", "a1b2c3", "https://example.com/x/•••"),
            ("https://example.com/x/deadbeefcafebabe00", "deadbeef", "https://example.com/x/•••"),
            ("https://example.com/x/QWxhZGRpbjpPcGVuU2VzYW1l", "QWxh", "https://example.com/x/•••"),
        ]
        for given, secret, wanted in cases:
            with self.subTest(given=given):
                self.assertMasked(given, secret, wanted)
        for shown in ("https://charts.example.com/stable/app-1.0.tgz", "https://example.com/api/v1/items",
                      "https://gotify.example.com/(hidden)", "https://hooks.slack.com/(hidden)"):
            self.assertEqual(shown, MASKS.strip_url_secrets(shown))

    def test_json_with_escaped_slashes_or_a_key_given_twice(self):
        self.assertMasked('{"w":"https:\\/\\/user:pass@host.example.com"}', "pass")
        self.assertMasked('{"a":"x","a":"https://user:pass@host.example.com"}', "pass", MASKS.JSON_HIDDEN)
        self.assertMasked('{"a":"x","a":"y","b":"https://h.example.com/?token=SECRET"}', "SECRET", MASKS.JSON_HIDDEN)
        self.assertMasked('["https:\\u002f\\u002fuser:pass@h.example.com"]', "pass")
        plain = '{"a":"x","a":"y"}'
        self.assertEqual(plain, MASKS.strip_url_secrets(plain), "nothing to hide: a key given twice stays")

    def test_only_a_few_harmless_parameters_show_their_value(self):
        self.assertEqual("https://h.example.com/a?page=2&sort=name&q=lights&arch=amd64#top",
                         MASKS.strip_url_secrets("https://h.example.com/a?page=2&sort=name&q=lights&arch=amd64#top"))
        self.assertMasked("https://hooks.example.com/post?room=ops&channel=C123", "ops",
                          "https://hooks.example.com/•••")
        self.assertMasked("https://h.example.com/?x=SECRET&page=2", "SECRET", "https://h.example.com/?x=•••&page=2")
        self.assertMasked("https://h.example.com/#k=SECRET", "SECRET", "https://h.example.com/#k=•••")
        self.assertMasked("https://h.example.com/?a1b2c3d4e5f6g7h8i9", "a1b2c3")

    def test_everything_before_the_last_at_is_a_login(self):
        for given, secret in (("https://u:123#x@host.example.com", "123"), ("https://admin:12345?@host.example.com/", "12345"),
                              ("https://h.example.com/a?next=u:SECRET@b.example.com", "SECRET")):
            with self.subTest(given=given):
                self.assertMasked(given, secret)
                self.assertTrue(MASKS.strip_url_secrets(given).startswith("https://•••@"))

    def test_slack_and_gotify_addresses_round_trip_through_a_viewers_copy(self):
        group, version, resource, ns, name = _path_parts(HOOKS_APP)
        viewer = self.get(HOOKS_APP, "viewer")
        container = viewer["object"]["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual("https://hooks.slack.com/•••", container["env"][0]["value"])
        self.assertEqual("https://gotify.example.com/•••", container["env"][1]["value"])
        self.assertNotIn("XXXX", viewer["yaml"])
        self.assertNotIn("AbCdEf", viewer["yaml"])
        RES.save_object(group, version, resource, ns, name, viewer["yaml"])
        sent = self.c.sent[-1][2]["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual([SLACK, GOTIFY, SLACK.replace("XXXX", "YYYY")], [e["value"] for e in sent["env"]])
        self.assertEqual([SLACK, GOTIFY], sent["args"])

    def test_a_list_item_keeps_its_own_value_by_its_name(self):
        group, version, resource, ns, name = _path_parts(HOOKS_APP)
        viewer = self.get(HOOKS_APP, "viewer")
        obj = viewer["object"]
        env = obj["spec"]["template"]["spec"]["containers"][0]["env"]
        env.reverse()           # the two Slack addresses look the same masked
        RES.save_object(group, version, resource, ns, name, RES.YAML.dump(obj))
        sent = self.c.sent[-1][2]["spec"]["template"]["spec"]["containers"][0]["env"]
        self.assertEqual({"SLACK_URL": SLACK, "GOTIFY_URL": GOTIFY, "OTHER_SLACK": SLACK.replace("XXXX", "YYYY")},
                         {e["name"]: e["value"] for e in sent})

    def test_a_reordered_list_without_names_is_refused(self):
        group, version, resource, ns, name = _path_parts(HOOKS_APP)
        obj = self.get(HOOKS_APP, "viewer")["object"]
        obj["spec"]["template"]["spec"]["containers"][0]["args"].reverse()
        with self.assertRaisesRegex(ValueError, "hidden"):
            RES.save_object(group, version, resource, ns, name, RES.YAML.dump(obj))
        self.assertEqual([], self.c.sent)

    def test_the_fast_token_scan_finds_what_the_pattern_finds(self):
        texts = list(REVIEW_FORMS) + ["a" * 40 + "://x", "1a" * 20 + "://x b://y", "x://a://b c:/d :/e", "-+a.b://x\ty",
                                      "ab:x ab://y", "é" + "a" * 31 + "://x", "a:/" * 50, 'a://"' * 30, "ſ://x K://y", ""]
        for text in texts:
            with self.subTest(text=text[:40]):
                self.assertEqual([m.span() for m in MASKS.TOKEN.finditer(text)], list(MASKS.tokens(text)))

    def test_a_256_kb_text_is_checked_in_under_a_tenth_of_a_second(self):
        n = MASKS.MAX_TEXT
        texts = ["https://h.example.com/" + "a1" * (n // 2 - 12), "https://" + "@" * (n - 8), "https://h/?" + "a=1&" * (n // 4 - 3),
                 "https://h/" + "x/" * (n // 2 - 5), ("https://u:p@h.example.com/?token=t " * (n // 36))[:n],
                 '["' + "https:\\/\\/u:p@h " * (n // 20) + '"]', "p" * (n - 9) + " https:/", "{" + '"a":1,' * (n // 6 - 1) + '"b":2}',
                 "https://h/" + "pa" * (n // 2 - 6), "https://h/?" + "x" * (n - 12), "https://h/#" + "q=1;" * (n // 4 - 3),
                 ("u:p@tcp(h) " * (n // 11))[:n], "//@" * (n // 3), ("//u:p@h " * (n // 8))[:n], "@tcp(" * (n // 5),
                 ("a=https://h " * (n // 12))[:n], ("u@x://h " * (n // 8))[:n], "x" * (n - 20) + "@x://h",
                 ("https://h/a?x=1&y=2;z#q " * (MASKS.MAX_ADDRESSES - 8)).ljust(n, "x")[:n]]
        for text in texts:
            self.assertLessEqual(len(text), n)
            best = min(_timed(lambda: MASKS.strip_url_secrets(text)) for _ in range(3))
            self.assertLess(best, 0.1, text[:40])


ARGS_APP = "/apis/apps/v1/namespaces/lab/deployments/args"
ARG_URL = "--url=https://u:p4ssw0rd@h.example.com"


class LastPass(Base):
    """The third pass: notification schemes and hosts, scheme-less logins,
    and saving an unnamed list a viewer appended to."""

    def setUp(self):
        extra = {ARGS_APP: {
            "apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": "args", "namespace": "lab", "resourceVersion": "5"},
            "spec": {"replicas": 1, "template": {"spec": {"containers": [{
                "name": "app", "image": "example/app:1",
                "args": [ARG_URL, "-v", ARG_URL.replace("p4ssw0rd", "other")]}]}}}}}
        patcher = mock.patch.dict(EXTRA, extra)
        patcher.start()
        self.addCleanup(patcher.stop)
        super().setUp()

    def assertMasked(self, given, secret, wanted=None):
        once = MASKS.strip_url_secrets(given)
        self.assertNotIn(secret, once)
        self.assertEqual(once, MASKS.strip_url_secrets(once), "stripping twice changes nothing")
        self.assertTrue(MASKS.has_stripped(once))
        if wanted is not None:
            self.assertEqual(wanted, once)

    def test_a_scheme_not_on_the_plain_list_is_hidden_after_its_slashes(self):
        for given, secret, wanted in (("pbul://o.gn5kj6nfhv736I7jC3cj3QLRiyhgl98b", "gn5kj", "pbul://•••"),
                                      ("pover://abcdef@apptoken123", "apptoken", "pover://•••"),
                                      ("gotify://gotify.example.com/AWHJ4c6uZV4OE9N", "AWHJ4", "gotify://•••"),
                                      ("see TGRAM://bot123:abc/456 now", "abc", "see TGRAM://••• now")):
            with self.subTest(given=given):
                self.assertMasked(given, secret, wanted)
        for shown in ("oci://registry.example.com/charts/app", "redis://cache.example.com:6379/0",
                      "mongodb+srv://db.example.com/app", "wss://h.example.com/socket"):
            self.assertEqual(shown, MASKS.strip_url_secrets(shown))

    def test_a_notification_hosts_path_is_hidden_whole(self):
        for given, secret, wanted in (("https://ntfy.example.com/my-alerts", "my-alerts", "https://ntfy.example.com/•••"),
                                      ("https://gotify.example.com/short", "short", "https://gotify.example.com/•••"),
                                      ("https://apprise.example.com/notify/abc", "abc", "https://apprise.example.com/•••"),
                                      ("https://hooks.example.com/x/y", "x/y", "https://hooks.example.com/•••")):
            with self.subTest(given=given):
                self.assertMasked(given, secret, wanted)
        # The outage module's own mask has nothing left to hide (#942 keeps its annotation).
        self.assertEqual("https://gotify.example.com/(hidden)", MASKS.strip_url_secrets("https://gotify.example.com/(hidden)"))
        self.assertEqual("https://ntfy.example.com/", MASKS.strip_url_secrets("https://ntfy.example.com/"))

    def test_a_login_without_a_scheme_is_masked(self):
        for given, secret, wanted in (("--repo //user:SECRET@host.example.com/x", "SECRET", "--repo //•••@host.example.com/x"),
                                      ("dsn=user:SECRET@tcp(db.example.com:3306)/app", "SECRET", "•••@tcp(db.example.com:3306)/app"),
                                      ("user:SECRET@unix(/run/mysql.sock)/app", "SECRET", "•••@unix(/run/mysql.sock)/app"),
                                      ("1x://user:SECRET@h.example.com", "SECRET", None)):
            with self.subTest(given=given):
                self.assertMasked(given, secret, wanted)
        self.assertEqual("mail admin@example.com", MASKS.strip_url_secrets("mail admin@example.com"))
        text = "failed: user:SECRET@tcp(db.example.com:3306)/app"
        for length in range(len(text) + 1):
            self.assertNotIn("SECRET"[:3], MASKS.strip_url_secrets(MASKS.cut(text, length)), length)

    def test_forms_the_review_named(self):
        self.assertMasked("HTTPS://U:SECRET@H.EXAMPLE.COM/?TOKEN=T0K", "SECRET", "HTTPS://•••@H.EXAMPLE.COM/?TOKEN=•••")
        self.assertMasked("https://u:SECRET@[2001:db8::1]:8443/x", "SECRET", "https://•••@[2001:db8::1]:8443/x")
        self.assertMasked('{"https://u:SECRET@h.example.com":1}', "SECRET", MASKS.JSON_HIDDEN)
        self.assertMasked('{"u":"https://user:SECRET\\u0040h.example.com/x"}', "SECRET",
                          '{"u":"https://•••@h.example.com/x"}')

    def _save_args(self, change):
        group, version, resource, ns, name = _path_parts(ARGS_APP)
        obj = self.get(ARGS_APP, "viewer")["object"]
        args = obj["spec"]["template"]["spec"]["containers"][0]["args"]
        self.assertEqual("--url=https://•••@h.example.com", args[0])
        change(args)
        RES.save_object(group, version, resource, ns, name, RES.YAML.dump(obj))
        return self.c.sent[-1][2]["spec"]["template"]["spec"]["containers"][0]["args"]

    def test_appending_to_an_unnamed_list_keeps_the_stored_values(self):
        sent = self._save_args(lambda args: args.append("--debug"))
        self.assertEqual([ARG_URL, "-v", ARG_URL.replace("p4ssw0rd", "other"), "--debug"], sent)

    def test_inserting_at_the_front_never_moves_a_value(self):
        try:
            sent = self._save_args(lambda args: args.insert(0, "--debug"))
        except ValueError as refused:
            self.assertIn("hidden", str(refused))
            self.assertEqual([], self.c.sent)
            return
        self.assertEqual(["--debug", ARG_URL, "-v", ARG_URL.replace("p4ssw0rd", "other")], sent)

    def test_swapped_identical_masks_restore_by_place(self):
        def swap(args):
            args[0], args[2] = args[2], args[0]
        sent = self._save_args(swap)
        self.assertEqual([ARG_URL, "-v", ARG_URL.replace("p4ssw0rd", "other")], sent)


class SecurityReview(unittest.TestCase):
    """The automated review of 7e5d0bb0: a scheme starting inside a word, and
    a Go DSN with "@tcp(" twice."""

    def assertMasked(self, given, secret):
        once = MASKS.strip_url_secrets(given)
        self.assertNotIn(secret, once)
        self.assertEqual(once, MASKS.strip_url_secrets(once), "stripping twice changes nothing")
        self.assertTrue(MASKS.has_stripped(once))
        return once

    def test_a_scheme_inside_a_word_hides_the_word(self):
        for given in ("user:SECRET@x://h", "//user:SECRET@evil.example:x://h", "see user:SECRET@https://h.example.com/ now",
                      "//user:SECRET:x://h@y", "a/user:SECRET:https://h.example.com"):
            with self.subTest(given=given):
                self.assertMasked(given, "SECRET")
        text = "failed: user:SECRET@x://h.example.com/a"
        for length in range(len(text) + 1):
            self.assertNotIn("SEC", MASKS.strip_url_secrets(MASKS.cut(text, length)), length)
        # A scheme after a separator is still read as an address of its own.
        self.assertEqual("--repo=https://•••@h.example.com/x", MASKS.strip_url_secrets("--repo=https://u:p@h.example.com/x"))
        self.assertEqual("(https://h.example.com/x)", MASKS.strip_url_secrets("(https://h.example.com/x)"))

    def test_nothing_before_a_scheme_less_login_shows_unless_it_is_a_separator(self):
        for given, wanted in (("user:SECRET//x@tcp(db.example.com)/app", "•••@tcp(db.example.com)/app"),
                              ("a:SECRET//c:d@host.example.com", "•••@host.example.com"),
                              ("user.SECRET//c:d@host.example.com", "•••@host.example.com"),
                              ("--repo=//u:SECRET@host.example.com", "--repo=//•••@host.example.com"),
                              ("//u:SECRET@host.example.com", "//•••@host.example.com")):
            with self.subTest(given=given):
                self.assertEqual(wanted, self.assertMasked(given, "SECRET"))

    def test_a_text_with_too_many_addresses_is_hidden_whole(self):
        many = "https://u:SECRET@h.example.com/x " * (MASKS.MAX_ADDRESSES + 1)
        self.assertEqual(MASKS.TOO_MANY, MASKS.strip_url_secrets(many))
        self.assertTrue(MASKS.has_stripped(MASKS.TOO_MANY))
        few = "https://u:SECRET@h.example.com/x " * 100
        self.assertEqual("https://•••@h.example.com/x " * 100, MASKS.strip_url_secrets(few))

    def test_a_dsn_is_masked_up_to_its_last_at(self):
        once = self.assertMasked("user:p@tcp(x)ss@tcp(db.example.com:3306)/app", "ss@")
        self.assertEqual("•••@tcp(db.example.com:3306)/app", once)
        self.assertMasked("//u:p@tcp(x)SECRET@h.example.com/x", "SECRET")


def _timed(work):
    started = time.monotonic()
    work()
    return time.monotonic() - started


if __name__ == "__main__":
    unittest.main()
