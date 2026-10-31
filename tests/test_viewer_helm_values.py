"""Secret-bearing fields of any object reach only the role that may reveal a Secret.

The resource browser (GET /api/resources/object) is open to viewers. A Secret's
values were already hidden there, and revealing them is admin-only, but other
kinds carry the same passwords and tokens in plain fields: a HelmChart's or
HelmChartConfig's valuesContent and set, a Flux HelmRelease's values, a VM's
inline cloud-init, and the copies of those that controllers keep (the Helm
controller's chart ConfigMaps and install Jobs, KubeVirt's ControllerRevisions,
an "applied" annotation holding the whole object). Homestead's own pages keep
those to admins (/api/helm/release, /api/vm). The browser now masks them for
every role below the one GET /api/resources/reveal needs, keeping each
field's name and size."""
import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_agent_tools as TOOLS
import homestead_auth as AUTH
import homestead_overview_server as OVERVIEW
import homestead_resources as RES
import homestead_routes as ROUTER
import server  # noqa: F401  (binds the agent tools and the route table)
from fakes.kube import Kube

VALUES = "auth:\n  adminPassword: hunter2\n  token: s3cr3t-t0ken\n"
USER_DATA = "#cloud-config\npassword: hunter2\nchpasswd: { expire: False }\n"
APPLIED = "H4sIAAAAAAAA/hunter2-" + "x" * 300
SECRETS = ("hunter2", "s3cr3t-t0ken", "aHVudGVyMg==", "Y2hhcnQtdGFyYmFsbA==", "flux-pass")
CHART_LABEL = {"helmcharts.helm.cattle.io/chart": "app"}
OWNED_BY_CHART = [{"apiVersion": "helm.cattle.io/v1", "kind": "HelmChart", "name": "app", "uid": "c1"}]
INSTALLER = {"name": "helm", "image": "rancher/klipper-helm:v0.9.0",
             "args": ["install", "--set-string", "auth.adminPassword=hunter2"],
             "env": [{"name": "CHART", "value": "app"}, {"name": "TOKEN", "value": "s3cr3t-t0ken"},
                     {"name": "REF", "valueFrom": {"secretKeyRef": {"name": "x", "key": "y"}}}]}


def vm_spec(volume):
    return {"template": {"spec": {"volumes": [{"name": "rootdisk", "persistentVolumeClaim": {"claimName": "vm1-root"}},
                                              dict({"name": "cloudinitdisk"}, **volume)]}}}


# path -> object; each path also says the kind's group, version and resource.
OBJECTS = {
    "/apis/helm.cattle.io/v1/namespaces/kube-system/helmcharts/app": {
        "apiVersion": "helm.cattle.io/v1", "kind": "HelmChart",
        "metadata": {"name": "app", "namespace": "kube-system", "resourceVersion": "5",
                     "annotations": {"objectset.rio.cattle.io/applied": APPLIED, "example.com/owner": "team-a"}},
        "spec": {"chart": "app", "repo": "https://charts.example.com", "valuesContent": VALUES,
                 "set": {"auth.adminPassword": "hunter2", "replicas": 2},
                 "chartContent": "Y2hhcnQtdGFyYmFsbA=="}},
    "/apis/helm.cattle.io/v1/namespaces/kube-system/helmchartconfigs/traefik": {
        "apiVersion": "helm.cattle.io/v1", "kind": "HelmChartConfig",
        "metadata": {"name": "traefik", "namespace": "kube-system", "resourceVersion": "5"},
        "spec": {"valuesContent": VALUES}},
    "/apis/helm.toolkit.fluxcd.io/v2/namespaces/flux/helmreleases/db": {
        "apiVersion": "helm.toolkit.fluxcd.io/v2", "kind": "HelmRelease",
        "metadata": {"name": "db", "namespace": "flux", "resourceVersion": "5"},
        "spec": {"chart": {"spec": {"chart": "db"}},
                 "values": {"auth": {"password": "flux-pass", "users": ["flux-pass"]}, "replicas": 1},
                 "valuesFrom": [{"kind": "Secret", "name": "db-values"}]}},
    "/apis/kubevirt.io/v1/namespaces/lab/virtualmachines/vm1": {
        "apiVersion": "kubevirt.io/v1", "kind": "VirtualMachine",
        "metadata": {"name": "vm1", "namespace": "lab", "resourceVersion": "5"},
        "spec": vm_spec({"cloudInitNoCloud": {"userData": USER_DATA, "networkDataBase64": "aHVudGVyMg=="}})},
    "/apis/kubevirt.io/v1/namespaces/lab/virtualmachineinstances/vm1": {
        "apiVersion": "kubevirt.io/v1", "kind": "VirtualMachineInstance",
        "metadata": {"name": "vm1", "namespace": "lab", "resourceVersion": "5"},
        "spec": {"volumes": [{"name": "cloudinitdisk", "cloudInitConfigDrive": {"userDataBase64": "aHVudGVyMg=="}}]}},
    "/apis/pool.kubevirt.io/v1alpha1/namespaces/lab/virtualmachinepools/pool": {
        "apiVersion": "pool.kubevirt.io/v1alpha1", "kind": "VirtualMachinePool",
        "metadata": {"name": "pool", "namespace": "lab", "resourceVersion": "5"},
        "spec": {"virtualMachineTemplate": {"spec": vm_spec({"cloudInitNoCloud": {"userData": USER_DATA}})}}},
    # A copy kept outside spec: cloud-init is found wherever it sits.
    "/apis/backup.example.com/v1/namespaces/lab/vmbackups/nightly": {
        "apiVersion": "backup.example.com/v1", "kind": "VMBackup",
        "metadata": {"name": "nightly", "namespace": "lab", "resourceVersion": "5"},
        "status": {"source": {"spec": vm_spec({"cloudInitNoCloud": {"userData": USER_DATA}})}}},
    "/apis/apps/v1/namespaces/lab/controllerrevisions/revision-start-vm-u1-1": {
        "apiVersion": "apps/v1", "kind": "ControllerRevision", "revision": 1,
        "metadata": {"name": "revision-start-vm-u1-1", "namespace": "lab", "resourceVersion": "5"},
        "data": {"spec": vm_spec({"cloudInitNoCloud": {"userData": USER_DATA}}),
                 "env": [{"name": "TOKEN", "value": "s3cr3t-t0ken"}]}},
    # The Helm controller's copies of a chart's values and content.
    "/api/v1/namespaces/kube-system/configmaps/chart-values-app": {
        "apiVersion": "v1", "kind": "ConfigMap",
        "metadata": {"name": "chart-values-app", "namespace": "kube-system", "resourceVersion": "5",
                     "ownerReferences": OWNED_BY_CHART},
        "data": {"values-01_HelmChart.yaml": VALUES}},
    "/api/v1/namespaces/kube-system/configmaps/chart-content-app": {
        "apiVersion": "v1", "kind": "ConfigMap",
        "metadata": {"name": "chart-content-app", "namespace": "kube-system", "resourceVersion": "5"},
        "binaryData": {"app.tgz.base64": "Y2hhcnQtdGFyYmFsbA=="}},
    "/apis/batch/v1/namespaces/kube-system/jobs/helm-install-app": {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {"name": "helm-install-app", "namespace": "kube-system", "resourceVersion": "5",
                     "ownerReferences": OWNED_BY_CHART},
        "spec": {"template": {"metadata": {"labels": CHART_LABEL}, "spec": {"containers": [INSTALLER]}}}},
    "/api/v1/namespaces/kube-system/pods/helm-install-app-x1": {
        "apiVersion": "v1", "kind": "Pod",
        "metadata": {"name": "helm-install-app-x1", "namespace": "kube-system", "resourceVersion": "5", "labels": CHART_LABEL},
        "spec": {"initContainers": [INSTALLER], "containers": [INSTALLER]}},
    # Helm values in other tools' objects, wherever a "helm" block sits.
    "/apis/argoproj.io/v1alpha1/namespaces/argocd/applications/web": {
        "apiVersion": "argoproj.io/v1alpha1", "kind": "Application",
        "metadata": {"name": "web", "namespace": "argocd", "resourceVersion": "5"},
        "spec": {"sources": [{"repoURL": "https://charts.example.com", "helm": {
            "values": VALUES, "valuesObject": {"token": "s3cr3t-t0ken"},
            "parameters": [{"name": "auth.adminPassword", "value": "hunter2"}]}}]}},
    "/apis/fleet.cattle.io/v1alpha1/namespaces/fleet-local/bundledeployments/web": {
        "apiVersion": "fleet.cattle.io/v1alpha1", "kind": "BundleDeployment",
        "metadata": {"name": "web", "namespace": "fleet-local", "resourceVersion": "5"},
        "spec": {"options": {"helm": {"values": {"auth": {"password": "hunter2"}}}}}},
    "/apis/catalog.cattle.io/v1/namespaces/lab/apps/web": {
        "apiVersion": "catalog.cattle.io/v1", "kind": "App",
        "metadata": {"name": "web", "namespace": "lab", "resourceVersion": "5"},
        "spec": {"values": {"password": "hunter2"}, "chart": {"values": {"token": "s3cr3t-t0ken"}}}},
    # A Helm release kept in a ConfigMap (HELM_DRIVER=configmap), and a
    # Harvester cloud-init template.
    "/api/v1/namespaces/lab/configmaps/sh.helm.release.v1.web.v1": {
        "apiVersion": "v1", "kind": "ConfigMap",
        "metadata": {"name": "sh.helm.release.v1.web.v1", "namespace": "lab", "resourceVersion": "5",
                     "labels": {"owner": "helm", "name": "web"}},
        "data": {"release": "H4sIhunter2release"}},
    "/api/v1/namespaces/lab/configmaps/ubuntu-user": {
        "apiVersion": "v1", "kind": "ConfigMap",
        "metadata": {"name": "ubuntu-user", "namespace": "lab", "resourceVersion": "5",
                     "labels": {"harvesterhci.io/cloud-init-template": "user"}},
        "data": {"cloudInit": USER_DATA}},
    # A Harvester backup keeps the VM's cloud-init Secrets' data.
    "/apis/harvesterhci.io/v1beta1/namespaces/lab/virtualmachinebackups/vm1-nightly": {
        "apiVersion": "harvesterhci.io/v1beta1", "kind": "VirtualMachineBackup",
        "metadata": {"name": "vm1-nightly", "namespace": "lab", "resourceVersion": "5"},
        "status": {"secretBackups": [{"name": "vm1-cloudinit", "data": {"userdata": "aHVudGVyMg=="}}]}},
    # CDI: an image's download headers often carry a bearer token.
    "/apis/cdi.kubevirt.io/v1beta1/namespaces/lab/datavolumes/disk": {
        "apiVersion": "cdi.kubevirt.io/v1beta1", "kind": "DataVolume",
        "metadata": {"name": "disk", "namespace": "lab", "resourceVersion": "5"},
        "spec": {"source": {"http": {"url": "https://images.example.com/disk.img",
                                     "extraHeaders": ["Authorization: Bearer s3cr3t-t0ken"]}}}},
    # Rancher keeps a cluster's join token and command in status.
    "/apis/management.cattle.io/v3/namespaces/c-m-1/clusterregistrationtokens/default-token": {
        "apiVersion": "management.cattle.io/v3", "kind": "ClusterRegistrationToken",
        "metadata": {"name": "default-token", "namespace": "c-m-1", "resourceVersion": "5"},
        "spec": {"clusterName": "c-m-1"},
        "status": {"token": "s3cr3t-t0ken", "command": "kubectl apply -f https://rancher.example.com/v3/import/s3cr3t-t0ken.yaml",
                   "insecureCommand": "curl --insecure -sfL https://rancher.example.com/v3/import/hunter2.yaml",
                   "manifestUrl": "https://rancher.example.com/v3/import/s3cr3t-t0ken.yaml",
                   "phase": "Active", "observedGeneration": 4,
                   "conditions": [{"type": "Ready", "status": "True", "reason": "Done", "message": "registered",
                                   "lastTransitionTime": "2026-10-28T00:00:00Z", "token": "hunter2"}]}},
    # CDI's importer Pod carries the DataVolume's headers in its env.
    "/api/v1/namespaces/lab/pods/importer-disk": {
        "apiVersion": "v1", "kind": "Pod",
        "metadata": {"name": "importer-disk", "namespace": "lab", "resourceVersion": "5"},
        "spec": {"containers": [{"name": "importer", "image": "quay.io/kubevirt/cdi-importer:v1.60.0",
                                 "env": [{"name": "IMPORTER_ENDPOINT", "value": "https://images.example.com/disk.img"},
                                         {"name": "IMPORTER_EXTRA_HEADER_0", "value": "Authorization: Bearer s3cr3t-t0ken"}]}]}},
    # kapp's copy of the original object.
    "/api/v1/namespaces/lab/configmaps/kapp": {
        "apiVersion": "v1", "kind": "ConfigMap",
        "metadata": {"name": "kapp", "namespace": "lab", "resourceVersion": "5",
                     "annotations": {"kapp.k14s.io/original": '{"data":{"password":"hunter2"}}'}},
        "data": {"app.yaml": "port: 80\n"}},
    "/api/v1/namespaces/lab/configmaps/cfg": {
        "apiVersion": "v1", "kind": "ConfigMap",
        "metadata": {"name": "cfg", "namespace": "lab", "resourceVersion": "5", "annotations": {"homestead.io/group": "web"}},
        "data": {"app.yaml": "port: 80\n"}},
    # Kinds no list can know: a custom resource with a secret anywhere in its
    # spec, a Fleet Bundle's resources, an rke machine config's top-level
    # userData.
    "/apis/widgets.example.com/v1/namespaces/lab/widgets/w1": {
        "apiVersion": "widgets.example.com/v1", "kind": "Widget",
        "metadata": {"name": "w1", "namespace": "lab", "resourceVersion": "5", "labels": {"app": "w1"}},
        "spec": {"deep": {"list": [{"password": "hunter2"}]}},
        "status": {"phase": "Ready"}},
    "/apis/fleet.cattle.io/v1alpha1/namespaces/fleet-local/bundles/web": {
        "apiVersion": "fleet.cattle.io/v1alpha1", "kind": "Bundle",
        "metadata": {"name": "web", "namespace": "fleet-local", "resourceVersion": "5"},
        "spec": {"resources": [{"name": "secret.yaml", "content": "stringData:\n  token: s3cr3t-t0ken\n"}]}},
    "/apis/rke-machine-config.cattle.io/v1/namespaces/fleet-default/harvesterconfigs/pool1": {
        "apiVersion": "rke-machine-config.cattle.io/v1", "kind": "HarvesterConfig",
        "metadata": {"name": "pool1", "namespace": "fleet-default", "resourceVersion": "5"},
        "userData": USER_DATA, "cpuCount": "2"},
    # A kind on the safe list: its spec stays, and annotations are filtered
    # at every depth.
    "/apis/apps/v1/namespaces/lab/deployments/web": {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": "web", "namespace": "lab", "resourceVersion": "5",
                     "annotations": {"deployment.kubernetes.io/revision": "3", "example.com/token": "s3cr3t-t0ken"}},
        "spec": {"replicas": 2, "template": {
            "metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": "2026-10-28T00:00:00Z",
                                         "example.com/pass": "hunter2"}},
            "spec": {"containers": [{"name": "web", "image": "nginx:1.27", "env": [{"name": "PORT", "value": "80"}]}]}}}},
    "/api/v1/namespaces/lab/configmaps/applied": {
        "apiVersion": "v1", "kind": "ConfigMap",
        "metadata": {"name": "applied", "namespace": "lab", "resourceVersion": "5",
                     "annotations": {"objectset.rio.cattle.io/applied": APPLIED,
                                     "example.com/last-applied-state": "hunter2"}},
        "data": {"app.yaml": "port: 80\n"}},
    "/api/v1/namespaces/lab/secrets/pw": {
        "apiVersion": "v1", "kind": "Secret",
        "metadata": {"name": "pw", "namespace": "lab", "resourceVersion": "5",
                     "annotations": {"objectset.rio.cattle.io/applied": APPLIED}},
        "data": {"password": "aHVudGVyMg=="}},
}


def _split(path):
    """(group, version, resource, namespace, name) from an object's path."""
    parts = path.strip("/").split("/")
    group, version = ("", parts[1]) if parts[0] == "api" else (parts[1], parts[2])
    return group, version, parts[-2], parts[-3], parts[-1]


# Each secret-bearing object, and what an admin must still see in it.
TARGETS = {
    "HelmChart": ("/apis/helm.cattle.io/v1/namespaces/kube-system/helmcharts/app", ("hunter2", "s3cr3t-t0ken", "Y2hhcnQtdGFyYmFsbA==")),
    "HelmChartConfig": ("/apis/helm.cattle.io/v1/namespaces/kube-system/helmchartconfigs/traefik", ("hunter2", "s3cr3t-t0ken")),
    "HelmRelease": ("/apis/helm.toolkit.fluxcd.io/v2/namespaces/flux/helmreleases/db", ("flux-pass",)),
    "VirtualMachine": ("/apis/kubevirt.io/v1/namespaces/lab/virtualmachines/vm1", ("hunter2", "aHVudGVyMg==")),
    "VirtualMachineInstance": ("/apis/kubevirt.io/v1/namespaces/lab/virtualmachineinstances/vm1", ("aHVudGVyMg==",)),
    "VirtualMachinePool": ("/apis/pool.kubevirt.io/v1alpha1/namespaces/lab/virtualmachinepools/pool", ("hunter2",)),
    "VMBackup": ("/apis/backup.example.com/v1/namespaces/lab/vmbackups/nightly", ("hunter2",)),
    "ControllerRevision": ("/apis/apps/v1/namespaces/lab/controllerrevisions/revision-start-vm-u1-1", ("hunter2", "s3cr3t-t0ken")),
    "chart-values ConfigMap": ("/api/v1/namespaces/kube-system/configmaps/chart-values-app", ("hunter2", "s3cr3t-t0ken")),
    "chart-content ConfigMap": ("/api/v1/namespaces/kube-system/configmaps/chart-content-app", ("Y2hhcnQtdGFyYmFsbA==",)),
    "helm-install Job": ("/apis/batch/v1/namespaces/kube-system/jobs/helm-install-app", ("hunter2", "s3cr3t-t0ken")),
    "helm-install Pod": ("/api/v1/namespaces/kube-system/pods/helm-install-app-x1", ("hunter2", "s3cr3t-t0ken")),
    "applied annotation": ("/api/v1/namespaces/lab/configmaps/applied", ("hunter2",)),
    "Argo CD Application": ("/apis/argoproj.io/v1alpha1/namespaces/argocd/applications/web", ("hunter2", "s3cr3t-t0ken")),
    "Fleet BundleDeployment": ("/apis/fleet.cattle.io/v1alpha1/namespaces/fleet-local/bundledeployments/web", ("hunter2",)),
    "Rancher App": ("/apis/catalog.cattle.io/v1/namespaces/lab/apps/web", ("hunter2", "s3cr3t-t0ken")),
    "Helm release ConfigMap": ("/api/v1/namespaces/lab/configmaps/sh.helm.release.v1.web.v1", ("hunter2",)),
    "cloud-init template": ("/api/v1/namespaces/lab/configmaps/ubuntu-user", ("hunter2",)),
    "Harvester backup": ("/apis/harvesterhci.io/v1beta1/namespaces/lab/virtualmachinebackups/vm1-nightly", ("aHVudGVyMg==",)),
    "CDI DataVolume": ("/apis/cdi.kubevirt.io/v1beta1/namespaces/lab/datavolumes/disk", ("s3cr3t-t0ken",)),
    "kapp original": ("/api/v1/namespaces/lab/configmaps/kapp", ("hunter2",)),
    "unknown CR": ("/apis/widgets.example.com/v1/namespaces/lab/widgets/w1", ("hunter2",)),
    "Fleet Bundle": ("/apis/fleet.cattle.io/v1alpha1/namespaces/fleet-local/bundles/web", ("s3cr3t-t0ken",)),
    "rke machine config": ("/apis/rke-machine-config.cattle.io/v1/namespaces/fleet-default/harvesterconfigs/pool1", ("hunter2",)),
    "Deployment annotations": ("/apis/apps/v1/namespaces/lab/deployments/web", ("hunter2", "s3cr3t-t0ken")),
    "CDI importer Pod": ("/api/v1/namespaces/lab/pods/importer-disk", ("s3cr3t-t0ken",)),
    "registration token": ("/apis/management.cattle.io/v3/namespaces/c-m-1/clusterregistrationtokens/default-token",
                           ("s3cr3t-t0ken", "hunter2")),
}
NOTE = "••• (hidden: needs the admin role"


class Cluster:
    """The objects above on the shared fake API server (tests/fakes/kube.py), which answers the object reads
    (and their 404s). Discovery stays ours: the fake's /apis lists no resources."""

    def __init__(self):
        self.kube, self.sent = Kube(*copy.deepcopy(list(OBJECTS.values()))), []

    @property
    def objects(self):
        return {path: self.kube.object(path) for path in OBJECTS}

    def change(self, path, mutate):
        obj = self.kube.object(path)
        mutate(obj)
        self.kube.add(obj)

    def discovery(self):
        kinds = {}
        for path, obj in OBJECTS.items():
            group, version, resource, _, _ = _split(path)
            kinds.setdefault((group, version), {})[resource] = obj["kind"]
        return kinds

    def get(self, path):
        kinds = self.discovery()
        if path == "/apis":
            return {"groups": [{"name": g, "preferredVersion": {"version": v}} for g, v in kinds if g]}
        for (group, version), resources in kinds.items():
            if path == (f"/api/{version}" if not group else f"/apis/{group}/{version}"):
                return {"resources": [{"name": r, "kind": k, "namespaced": True, "verbs": ["get", "list", "update", "create"]}
                                      for r, k in resources.items()]}
        return self.kube("GET", path)

    def send(self, method, path, body=None, **kwargs):
        self.sent.append((method, path, copy.deepcopy(body)))
        return body

    def table(self, path):
        return {"columnDefinitions": [{"name": "Name", "priority": 0}], "rows": []}


class Base(unittest.TestCase):
    def setUp(self):
        self.bound = (RES.kget, RES.ksend, RES.table_get)
        self.c = Cluster()
        RES.bind(self.c.get, self.c.send, self.c.table)
        RES._discovery.update(at=0.0, value=None)

    def tearDown(self):
        RES.kget, RES.ksend, RES.table_get = self.bound
        RES._discovery.update(at=0.0, value=None)

    def route(self, path, kind, role):
        group, version, resource, ns, name = _split(TARGETS[kind][0] if kind in TARGETS else kind)
        query = {"group": [group], "version": [version], "resource": [resource], "ns": [ns], "name": [name]}
        handler = OVERVIEW.ROUTES[("GET", path)][1]
        return handler(ROUTER.Request("GET", path, query, {}, "someone", role))

    def assertNoSecrets(self, answer):
        text = repr(answer)
        for secret in SECRETS:
            self.assertNotIn(secret, text)

    def assertAllSecrets(self, answer, kind):
        text = repr(answer)
        for secret in TARGETS[kind][1]:
            self.assertIn(secret, text)


class TheResourceBrowser(Base):
    def test_a_viewer_gets_every_secret_bearing_field_masked(self):
        for kind in TARGETS:
            with self.subTest(kind=kind):
                answer = self.route("/api/resources/object", kind, "viewer")
                self.assertNoSecrets(answer)
                self.assertTrue(answer["values_hidden"])

    def test_an_operator_and_no_role_get_them_masked_too(self):
        for role in ("operator", None, ""):
            for kind in TARGETS:
                with self.subTest(role=role, kind=kind):
                    self.assertNoSecrets(self.route("/api/resources/object", kind, role))

    def test_the_masking_role_is_exactly_the_one_reveal_needs(self):
        need = OVERVIEW.ROUTES[("GET", "/api/resources/reveal")][0]
        self.assertEqual("admin", need)
        for role in AUTH.ROLES:
            answer = self.route("/api/resources/object", "HelmChart", role)
            self.assertEqual(not AUTH.allows(role, need), answer["values_hidden"], role)

    def test_an_admin_gets_the_real_values(self):
        for kind in TARGETS:
            with self.subTest(kind=kind):
                answer = self.route("/api/resources/object", kind, "admin")
                self.assertAllSecrets(answer, kind)
                self.assertFalse(answer["values_hidden"])

    def test_the_reveal_path_is_unchanged(self):
        self.assertEqual("admin", OVERVIEW.ROUTES[("GET", "/api/resources/reveal")][0])
        for kind in TARGETS:
            with self.subTest(kind=kind):
                self.assertAllSecrets(self.route("/api/resources/reveal", kind, "admin"), kind)
        secret = self.route("/api/resources/reveal", "/api/v1/namespaces/lab/secrets/pw", "admin")
        self.assertIn("aHVudGVyMg==", secret["yaml"])
        self.assertIn(APPLIED, secret["yaml"])

    def test_a_secret_stays_hidden_for_everyone_on_the_plain_route(self):
        # Its "applied" annotation is a copy of the whole Secret, data included.
        for role in AUTH.ROLES:
            answer = self.route("/api/resources/object", "/api/v1/namespaces/lab/secrets/pw", role)
            self.assertNotIn("aHVudGVyMg==", repr(answer))
            self.assertNotIn("hunter2", repr(answer))
            self.assertTrue(answer["secret_hidden"])

    def test_the_viewer_sees_that_each_field_exists_and_its_rough_size(self):
        chart = self.route("/api/resources/object", "HelmChart", "viewer")
        spec = chart["object"]["spec"]
        self.assertEqual("••• (hidden, under 10 lines)", spec["valuesContent"])
        self.assertEqual({"auth.adminPassword": "•••", "replicas": "•••"}, spec["set"])
        self.assertEqual("••• (hidden)", spec["chartContent"])
        self.assertEqual("app", spec["chart"])
        self.assertEqual("https://charts.example.com", spec["repo"])
        self.assertIn('valuesContent: "••• (hidden, under 10 lines)"', chart["yaml"])
        self.assertEqual({"objectset.rio.cattle.io/applied": "••• (hidden)",
                          "example.com/owner": "••• (hidden)"}, chart["object"]["metadata"]["annotations"])
        self.assertEqual(["metadata.annotations.objectset.rio.cattle.io/applied", "metadata.annotations.example.com/owner",
                          "spec.valuesContent", "spec.set", "spec.chartContent"], chart["hidden"])
        backup = self.route("/api/resources/object", "VMBackup", "viewer")
        self.assertEqual(["status.source"], backup["hidden"])
        self.assertTrue(backup["object"]["status"]["source"].startswith(NOTE))
        # The second layer alone still finds cloud-init in status.
        raw = copy.deepcopy(OBJECTS[TARGETS["VMBackup"][0]])
        self.assertEqual(["status.source.spec.template.spec.volumes[1].cloudInitNoCloud.userData"],
                         RES.hide_sensitive(raw, "backup.example.com"))

    def test_an_off_list_kinds_status_keeps_only_generic_fields(self):
        answer = self.route("/api/resources/object", "registration token", "viewer")
        status = answer["object"]["status"]
        self.assertEqual("Active", status["phase"])
        self.assertEqual(4, status["observedGeneration"])
        self.assertEqual([{"type": "Ready", "status": "True", "reason": "Done", "message": "registered",
                           "lastTransitionTime": "2026-10-28T00:00:00Z"}], status["conditions"])
        for key in ("token", "command", "insecureCommand", "manifestUrl"):
            self.assertTrue(status[key].startswith(NOTE), key)
            self.assertIn(f"status.{key}", answer["hidden"])
        self.assertNoSecrets(answer)
        admin = self.route("/api/resources/object", "registration token", "admin")["object"]["status"]
        self.assertEqual("s3cr3t-t0ken", admin["token"])

    def test_a_kind_off_the_safe_list_shows_metadata_and_status_only(self):
        for kind in ("unknown CR", "HelmRelease", "VirtualMachine", "VirtualMachineInstance", "Argo CD Application",
                     "Rancher App", "CDI DataVolume", "Fleet Bundle", "Fleet BundleDeployment", "rke machine config",
                     "ControllerRevision"):
            with self.subTest(kind=kind):
                answer = self.route("/api/resources/object", kind, "viewer")
                obj = answer["object"]
                for key, value in obj.items():
                    if key not in ("apiVersion", "kind", "metadata", "status"):
                        self.assertTrue(str(value).startswith(NOTE), (key, value))
                        self.assertIn(key, answer["hidden"])
                self.assertEqual(TARGETS[kind][0].rsplit("/", 1)[1], obj["metadata"]["name"])
        widget = self.route("/api/resources/object", "unknown CR", "viewer")
        self.assertEqual({"phase": "Ready"}, widget["object"]["status"])
        self.assertEqual({"app": "w1"}, widget["object"]["metadata"]["labels"])
        self.assertEqual("••• (hidden: needs the admin role, under 10 lines)", widget["object"]["spec"])
        self.assertIn("spec: \"••• (hidden: needs the admin role, under 10 lines)\"", widget["yaml"])
        rke = self.route("/api/resources/object", "rke machine config", "viewer")["object"]
        self.assertTrue(rke["userData"].startswith(NOTE))
        self.assertTrue(rke["cpuCount"].startswith(NOTE))

    def test_a_kind_on_the_safe_list_still_shows_its_spec(self):
        dep = self.route("/api/resources/object", "Deployment annotations", "viewer")["object"]
        self.assertEqual(2, dep["spec"]["replicas"])
        container = dep["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual({"name": "web", "image": "nginx:1.27", "env": [{"name": "PORT", "value": "80"}]}, container)
        cfg = self.route("/api/resources/object", "/api/v1/namespaces/lab/configmaps/cfg", "viewer")["object"]
        self.assertEqual({"app.yaml": "port: 80\n"}, cfg["data"])

    def test_annotations_are_allowlisted_at_every_depth(self):
        answer = self.route("/api/resources/object", "Deployment annotations", "viewer")
        dep = answer["object"]
        self.assertEqual({"deployment.kubernetes.io/revision": "3", "example.com/token": "••• (hidden)"},
                         dep["metadata"]["annotations"])
        self.assertEqual({"kubectl.kubernetes.io/restartedAt": "2026-10-28T00:00:00Z",
                          "example.com/pass": "••• (hidden)"},
                         dep["spec"]["template"]["metadata"]["annotations"])
        self.assertEqual(["metadata.annotations.example.com/token",
                          "spec.template.metadata.annotations.example.com/pass"], answer["hidden"])
        admin = self.route("/api/resources/object", "Deployment annotations", "admin")["object"]
        self.assertEqual("hunter2", admin["spec"]["template"]["metadata"]["annotations"]["example.com/pass"])

    def test_the_helm_controllers_copies_keep_their_shape(self):
        values = self.route("/api/resources/object", "chart-values ConfigMap", "viewer")["object"]
        self.assertEqual({"values-01_HelmChart.yaml": "••• (hidden, under 10 lines)"}, values["data"])
        content = self.route("/api/resources/object", "chart-content ConfigMap", "viewer")["object"]
        self.assertEqual({"app.tgz.base64": "••• (hidden)"}, content["binaryData"])
        pod = self.route("/api/resources/object", "helm-install Pod", "viewer")["object"]
        for container in pod["spec"]["containers"] + pod["spec"]["initContainers"]:
            self.assertEqual(["•••", "•••", "•••"], container["args"])
            self.assertEqual([{"name": "CHART", "value": "•••"}, {"name": "TOKEN", "value": "•••"},
                              {"name": "REF", "valueFrom": {"secretKeyRef": {"name": "x", "key": "y"}}}], container["env"])
            self.assertEqual("rancher/klipper-helm:v0.9.0", container["image"])
        revision = self.route("/api/resources/object", "ControllerRevision", "viewer")
        self.assertEqual(1, revision["hidden"].count("data"))
        importer = self.route("/api/resources/object", "CDI importer Pod", "viewer")["object"]
        self.assertEqual([{"name": "IMPORTER_ENDPOINT", "value": "https://images.example.com/disk.img"},
                          {"name": "IMPORTER_EXTRA_HEADER_0", "value": "•••"}],
                         importer["spec"]["containers"][0]["env"])

    def test_the_second_layer_masks_kinds_off_the_safe_list_for_a_caller_that_reads_spec(self):
        # hide_sensitive alone (the layer under the safe list) still masks
        # the fields it knows, so a kind added to the list later stays safe.
        argo = copy.deepcopy(OBJECTS["/apis/argoproj.io/v1alpha1/namespaces/argocd/applications/web"])
        RES.hide_sensitive(argo, "argoproj.io")
        self.assertEqual({"values": "••• (hidden, under 10 lines)", "valuesObject": {"token": "•••"},
                          "parameters": ["•••"]}, argo["spec"]["sources"][0]["helm"])
        app = copy.deepcopy(OBJECTS["/apis/catalog.cattle.io/v1/namespaces/lab/apps/web"])
        RES.hide_sensitive(app, "catalog.cattle.io")
        self.assertEqual({"password": "•••"}, app["spec"]["values"])
        self.assertEqual({"token": "•••"}, app["spec"]["chart"]["values"])
        vm = copy.deepcopy(OBJECTS["/apis/kubevirt.io/v1/namespaces/lab/virtualmachines/vm1"])
        RES.hide_sensitive(vm, "kubevirt.io")
        self.assertEqual({"userData": "••• (hidden, under 10 lines)", "networkDataBase64": "••• (hidden)"},
                         vm["spec"]["template"]["spec"]["volumes"][1]["cloudInitNoCloud"])
        disk = copy.deepcopy(OBJECTS["/apis/cdi.kubevirt.io/v1beta1/namespaces/lab/datavolumes/disk"])
        RES.hide_sensitive(disk, "cdi.kubevirt.io")
        self.assertEqual(["•••"], disk["spec"]["source"]["http"]["extraHeaders"])

    def test_other_copies_keep_their_shape(self):
        release = self.route("/api/resources/object", "Helm release ConfigMap", "viewer")["object"]
        self.assertEqual({"release": "••• (hidden)"}, release["data"])
        backup = self.route("/api/resources/object", "Harvester backup", "viewer")["object"]
        self.assertTrue(backup["status"]["secretBackups"].startswith(NOTE))
        raw = copy.deepcopy(OBJECTS[TARGETS["Harvester backup"][0]])
        RES.hide_sensitive(raw, "harvesterhci.io")
        self.assertEqual([{"name": "vm1-cloudinit", "data": {"userdata": "•••"}}], raw["status"]["secretBackups"])

    def test_a_job_found_by_its_template_label_alone(self):
        self.c.change("/apis/batch/v1/namespaces/kube-system/jobs/helm-install-app",
                      lambda job: job["metadata"].pop("ownerReferences"))
        self.assertNoSecrets(self.route("/api/resources/object", "helm-install Job", "viewer"))

    def test_kubectls_last_applied_copy_never_reaches_anyone(self):
        self.c.change("/api/v1/namespaces/lab/configmaps/cfg", lambda cm: cm["metadata"]["annotations"].update(
            {"kubectl.kubernetes.io/last-applied-configuration": '{"password":"hunter2"}'}))
        for role in AUTH.ROLES:
            self.assertNoSecrets(self.route("/api/resources/object", "/api/v1/namespaces/lab/configmaps/cfg", role))

    def test_masking_twice_changes_nothing(self):
        once = self.route("/api/resources/object", "HelmChart", "viewer")["object"]
        twice = copy.deepcopy(once)
        RES.hide_sensitive(twice, "helm.cattle.io")
        self.assertEqual(once, twice)

    def test_an_empty_field_is_left_as_it_is(self):
        self.c.change("/apis/helm.cattle.io/v1/namespaces/kube-system/helmchartconfigs/traefik",
                      lambda config: config["spec"].update(valuesContent=""))
        answer = self.route("/api/resources/object", "HelmChartConfig", "viewer")
        self.assertEqual("", answer["object"]["spec"]["valuesContent"])
        self.assertFalse(answer["values_hidden"])

    def test_other_kinds_are_untouched(self):
        answer = self.route("/api/resources/object", "/api/v1/namespaces/lab/configmaps/cfg", "viewer")
        self.assertEqual({"app.yaml": "port: 80\n"}, answer["object"]["data"])
        self.assertEqual({"homestead.io/group": "web"}, answer["object"]["metadata"]["annotations"])
        self.assertFalse(answer["values_hidden"])

    def test_the_cluster_copy_is_never_changed(self):
        before = copy.deepcopy(self.c.objects)
        for kind in TARGETS:
            self.route("/api/resources/object", kind, "viewer")
        self.assertEqual(before, self.c.objects)
        self.assertEqual([], self.c.sent)


class SavingAMaskedCopy(Base):
    def masked_yaml(self, kind):
        return self.route("/api/resources/object", kind, "viewer")["yaml"]

    def test_saving_a_masked_copy_is_refused(self):
        for kind in TARGETS:
            group, version, resource, ns, name = _split(TARGETS[kind][0])
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, "hidden"):
                RES.save_object(group, version, resource, ns, name, self.masked_yaml(kind))
        self.assertEqual([], self.c.sent)

    def test_a_masked_annotation_alone_is_refused(self):
        text = self.masked_yaml("applied annotation")
        with self.assertRaisesRegex(ValueError, "hidden"):
            RES.save_object("", "v1", "configmaps", "lab", "applied", text)
        self.assertEqual([], self.c.sent)

    def test_creating_from_a_masked_copy_is_refused(self):
        text = self.masked_yaml("HelmChartConfig").replace("name: traefik", "name: traefik-copy")
        with self.assertRaisesRegex(ValueError, "hidden"):
            RES.create_objects(text)
        self.assertEqual([], self.c.sent)

    def test_an_admins_copy_still_saves(self):
        group, version, resource, ns, name = _split(TARGETS["HelmChart"][0])
        text = self.route("/api/resources/object", "HelmChart", "admin")["yaml"]
        RES.save_object(group, version, resource, ns, name, text)
        self.assertEqual(VALUES, self.c.sent[0][2]["spec"]["valuesContent"])
        self.assertEqual(APPLIED, self.c.sent[0][2]["metadata"]["annotations"]["objectset.rio.cattle.io/applied"])

    def test_an_admins_real_value_with_dots_in_it_still_saves(self):
        # Only the mask's own forms are refused, not any value with "•••".
        group, version, resource, ns, name = _split(TARGETS["HelmChartConfig"][0])
        text = self.route("/api/resources/object", "HelmChartConfig", "admin")["yaml"].replace(
            "token: s3cr3t-t0ken", "banner: \"Loading •••\"")
        RES.save_object(group, version, resource, ns, name, text)
        self.assertIn("Loading •••", self.c.sent[0][2]["spec"]["valuesContent"])


class AgentsAndKeys(Base):
    """The route runs for an agent under its key's role (server.py gives a
    read-only key the viewer role); an API key cannot reach it at all."""

    def test_an_agent_running_as_a_low_role_gets_them_masked(self):
        for kind in TARGETS:
            group, version, resource, ns, name = _split(TARGETS[kind][0])
            query = {"group": group, "version": version, "resource": resource, "ns": ns, "name": name}
            for role in ("viewer", "operator"):
                with self.subTest(kind=kind, role=role):
                    answer = TOOLS.call("GET", "/api/resources/object", query, user="api-key:agent", role=role)
                    self.assertEqual(name, answer["object"]["metadata"]["name"])
                    self.assertNoSecrets(answer)

    def test_an_api_key_is_refused_on_the_route(self):
        h = object.__new__(server.H)
        h.path, h.headers, h.command = "/api/resources/object", {}, "GET"
        sent = []
        h._send = lambda code, body, *a, **k: sent.append((code, body))
        self.assertTrue(h._api_key_guard("/api/resources/object", "hs_any"))
        self.assertEqual(401, sent[0][0])


if __name__ == "__main__":
    unittest.main()
