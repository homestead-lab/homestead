import json
import base64
import gzip
import sys
import tempfile
import unittest
import urllib.error
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_baseline as BASELINE
import homestead_components as C
import homestead_kube as K
import homestead_lhv2_upgrade as V2
import homestead_probe as PROBE
import homestead_self as SELF
from fakes.kube import Kube

KUBE_VIP_INDEX = """apiVersion: v1
entries:
  kube-vip:
  - apiVersion: v2
    appVersion: v1.3.0
    name: kube-vip
    version: 0.12.0
  - apiVersion: v2
    appVersion: v1.2.3
    name: kube-vip
    version: 0.11.1
  - apiVersion: v2
    appVersion: v1.2.2
    name: kube-vip
    version: 0.11.0
  kube-vip-cloud-provider:
  - appVersion: v0.0.12
    version: 0.2.9
"""
RKE2_INDEX = """entries:
  rke2-multus:
  - appVersion: 4.3.1
    version: v4.3.103
  - appVersion: 4.3.1
    version: v4.3.102
  rke2-multus-crd:
  - version: v4.3.001
"""
CHART = "/apis/helm.cattle.io/v1/namespaces/kube-system/helmcharts/"
REQUEST = "/api/v1/namespaces/lab/configmaps/homestead-install"
DS = "/apis/apps/v1/namespaces/kube-system/daemonsets/"
SECRETS = "/api/v1/namespaces/{}/secrets"


def daemonset(image, name="kube-vip", namespace="kube-system"):
    return {"apiVersion": "apps/v1", "kind": "DaemonSet", "metadata": {"name": name, "namespace": namespace},
            "spec": {"template": {"spec": {"containers": [{"image": image}]}}}}


def helmchart(name, spec):
    return {"apiVersion": "helm.cattle.io/v1", "kind": "HelmChart", "metadata": {"name": name, "namespace": "kube-system"},
            "spec": spec}


def job(name, status):
    return {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": name, "namespace": "kube-system"}, "status": status}


class Addons:
    @staticmethod
    def fetch(url):
        return (KUBE_VIP_INDEX if "kube-vip" in url else RKE2_INDEX), url


class Cluster:
    """The cluster the components report reads, on the shared fake API
    (tests/fakes/kube.py)."""

    def __init__(self, test, platform, *objects, groups=(), absent_groups=()):
        self.platform, self.helm_calls = platform, []
        self.kube = test.enterContext(Kube(*objects, groups=groups, absent_groups=absent_groups))
        # The components read the cluster through the shared client here, whatever another test bound.
        test.enterContext(patch.multiple(C, kget=C.kget, ksend=C.ksend, platform=C.platform, helm_upgrade=C.helm_upgrade,
                                         addons=C.addons, fetch_json=C.fetch_json, ktext=C.ktext))
        test.enterContext(patch.multiple(V2, kget=V2.kget, ksend=V2.ksend, platform=V2.platform, version=V2.version,
                                         parse=V2.parse))
        C._releases.clear()
        C._apps.clear()
        C.bind(K.kget, lambda *a, **k: None, lambda force=False: self.platform, self.helm_calls.append, Addons(),
               lambda url: {"data": []} if "k3s.io" in url else [])


K3S = {"distribution": "k3s", "load_balancer": "kube-vip", "multus": True, "servicelb": True, "helm_controller": True}


class IndexTests(unittest.TestCase):
    def test_one_chart_is_read_from_a_repository_index(self):
        self.assertEqual([("0.12.0", "v1.3.0"), ("0.11.1", "v1.2.3"), ("0.11.0", "v1.2.2")],
                         C.chart_index(KUBE_VIP_INDEX, "kube-vip"))
        self.assertEqual([("v4.3.103", "4.3.1"), ("v4.3.102", "4.3.1")], C.chart_index(RKE2_INDEX, "rke2-multus"))
        self.assertEqual([], C.chart_index(RKE2_INDEX, "nothing"))


class NetworkRowTests(unittest.TestCase):
    def row(self, component):
        return next(r for r in C.report()["components"] if r["id"] == component)

    def test_kube_vip_and_multus_installed_by_homestead_move_on_by_chart(self):
        Cluster(self, K3S, helmchart("kube-vip", {"chart": "kube-vip", "version": "0.11.1"}),
                helmchart("multus", {"chart": "rke2-multus", "version": "v4.3.102"}),
                daemonset("ghcr.io/kube-vip/kube-vip:v1.2.3"))
        vip, multus = self.row("kube-vip"), self.row("multus")
        self.assertEqual(("0.11.1", "0.12.0", "helmchart", "v1.2.3"), (vip["installed"], vip["next"], vip["how"], vip["app"]))
        self.assertEqual(("v4.3.102", "v4.3.103", "helmchart"), (multus["installed"], multus["next"], multus["how"]))

    def test_an_unpinned_chart_is_read_from_the_image_it_runs(self):
        Cluster(self, K3S, helmchart("kube-vip", {"chart": "kube-vip"}), daemonset("ghcr.io/kube-vip/kube-vip:v1.2.2"))
        self.assertEqual(("0.11.0", "0.11.1"), (self.row("kube-vip")["installed"], self.row("kube-vip")["next"]))

    def test_installed_another_way_is_shown_not_upgraded(self):
        Cluster(self, K3S, daemonset("ghcr.io/kube-vip/kube-vip:v0.8.9", "kube-vip-ds"))
        self.assertEqual(("manual", "v0.8.9"), (self.row("kube-vip")["how"], self.row("kube-vip")["installed"]))
        with self.assertRaisesRegex(ValueError, "installed outside Homestead"):
            C.upgrade("kube-vip", "0.12.0")

    def test_harvester_s_own_are_shown_as_harvester_s(self):
        Cluster(self, {"harvester": True, "load_balancer": "kube-vip", "multus": True},
                daemonset("ghcr.io/kube-vip/kube-vip:v0.8.1", "kube-vip", "harvester-system"))
        self.assertEqual("harvester", self.row("kube-vip")["how"])

    def test_upgrading_moves_the_chart_version_and_keeps_its_values(self):
        cluster = Cluster(self, K3S, helmchart("kube-vip", {"chart": "kube-vip", "version": "0.11.1"}))
        result = C.upgrade("kube-vip", "0.12.0")
        self.assertEqual([{"namespace": "kube-system", "name": "kube-vip", "version": "0.12.0"}], cluster.helm_calls)
        self.assertIn("0.12.0 (v1.3.0)", result["detail"])


class NetworkUpgradeStatusTests(unittest.TestCase):
    def seed(self, component="multus", namespace="kube-system", agent=True, agent_name=None, api=True):
        self.component = component
        self.target = "v4.3.103" if component == "multus" else "0.12.0"
        self.previous = "v4.3.102" if component == "multus" else "0.11.1"
        self.name = "Multus" if component == "multus" else "kube-vip"
        self.namespace = namespace
        self.item = {"ref": {"component": component, "name": self.name, "from": self.previous, "to": self.target}}
        self.secrets_path = SECRETS.format(namespace)
        self.agent_path = f"/apis/apps/v1/namespaces/{namespace}/daemonsets/{agent_name or component}"
        objects = [helmchart(component, {
            "chart": "rke2-multus" if component == "multus" else component,
            "version": self.target, "targetNamespace": namespace})]
        if agent:
            agent_set = daemonset("example.test/network-agent:4.3.1", agent_name or component, namespace)
            agent_set["metadata"]["generation"] = 2
            agent_set["status"] = {"observedGeneration": 2, "desiredNumberScheduled": 3,
                                   "updatedNumberScheduled": 3, "numberAvailable": 3}
            objects.append(agent_set)
        self.cluster = Cluster(self, K3S, *objects, groups=("k8s.cni.cncf.io",) if api else (),
                               absent_groups=() if api else ("k8s.cni.cncf.io",))
        self.release(self.target)

    def encoded(self, record):
        return base64.b64encode(base64.b64encode(gzip.compress(json.dumps(record).encode()))).decode()

    def secret(self, revision, data):
        """A Helm release record as the cluster keeps it: labelled for the
        lookup, one Secret per revision."""
        return {"apiVersion": "v1", "kind": "Secret",
                "metadata": {"name": f"sh.helm.release.v1.{self.component}.v{revision}", "namespace": self.namespace,
                             "labels": {"owner": "helm", "name": self.component, "version": str(revision)}},
                "data": {"release": data}}

    def release(self, version, state="deployed", revision=2):
        record = {"name": self.component, "namespace": self.namespace, "version": revision,
                  "chart": {"metadata": {"name": "rke2-multus" if self.component == "multus" else self.component,
                                         "version": version, "appVersion": "4.3.1" if self.component == "multus" else "v1.3.0"}},
                  "info": {"status": state}}
        secret = self.secret(revision, self.encoded(record))
        self.cluster.kube.add(secret)
        return secret

    def forget_releases(self):
        for row in K.kget(self.secrets_path)["items"]:
            self.cluster.kube.discard(f"{self.secrets_path}/{row['metadata']['name']}")

    def test_both_network_upgrades_complete_without_reading_cdi(self):
        for component in ("multus", "kube-vip"):
            with self.subTest(component=component):
                self.seed(component)
                with patch.object(C, "cdi_version", side_effect=AssertionError("CDI is unrelated")):
                    state, progress, message = C.status(self.item)
                self.assertEqual(("succeeded", 100), (state, progress))
                self.assertIn(f"chart {self.target}", message)
                self.assertIn("all 3 hosts", message)

    def test_requested_version_and_unchanged_multus_image_do_not_prove_chart_revision(self):
        self.seed()
        self.release(self.previous)
        state, progress, message = C.status(self.item)
        self.assertEqual(("running", 20), (state, progress))
        self.assertIn(f"chart {self.previous}", message)
        self.assertIn(self.target, message)
        self.assertNotIn("1.66.1", message)

    def test_newest_pending_release_does_not_fall_back_to_older_deployed_revision(self):
        self.seed()
        self.release(self.target, revision=1)
        self.release(self.target, "pending-upgrade", revision=2)
        self.assertEqual("running", C.status(self.item)[0])

    def test_v_prefix_difference_is_normalized_for_deployed_chart(self):
        self.seed()
        self.release(self.target.lstrip("v"))
        self.assertEqual("succeeded", C.status(self.item)[0])

    def test_missing_or_unreadable_release_does_not_complete(self):
        self.seed()
        for secrets in ([], [self.secret(2, "invalid")]):
            with self.subTest(secrets=secrets):
                self.forget_releases()
                for secret in secrets:
                    self.cluster.kube.add(secret)
                self.assertEqual("running", C.status(self.item)[0])

    def test_release_api_denial_does_not_complete_or_leak_error_body(self):
        self.seed()
        def refuse(method, path, body):
            raise urllib.error.HTTPError(path, 403, "private error body", {}, None)
        self.cluster.kube.react("GET", self.secrets_path, refuse)
        state, _, message = C.status(self.item)
        self.assertEqual("running", state)
        self.assertNotIn("private error body", message)

    def test_an_unrelated_chart_or_release_identity_does_not_complete(self):
        for field in ("name", "namespace", "chart"):
            with self.subTest(field=field):
                self.seed()
                secret = self.cluster.kube.object(f"{self.secrets_path}/sh.helm.release.v1.{self.component}.v2")
                record = C.HELM.decode(secret)
                if field == "chart":
                    record["chart"]["metadata"]["name"] = "other-chart"
                else:
                    record[field] = "other"
                secret["data"]["release"] = self.encoded(record)
                self.cluster.kube.add(secret)
                self.assertEqual("running", C.status(self.item)[0])

    def test_deployed_chart_waits_for_observed_updated_available_agents(self):
        for change in ({"observedGeneration": 1}, {"updatedNumberScheduled": 2}, {"numberAvailable": 2},
                       {"desiredNumberScheduled": 0, "updatedNumberScheduled": 0, "numberAvailable": 0}):
            with self.subTest(change=change):
                self.seed()
                agent = self.cluster.kube.object(self.agent_path)
                agent["status"].update(change)
                self.cluster.kube.add(agent)
                self.assertEqual("running", C.status(self.item)[0])

    def test_missing_or_deleting_agents_and_missing_multus_api_do_not_complete(self):
        for missing in ("agent", "api", "deleting"):
            with self.subTest(missing=missing):
                self.seed(agent=missing != "agent", api=missing != "api")
                if missing == "deleting":
                    agent = self.cluster.kube.object(self.agent_path)
                    agent["metadata"]["deletionTimestamp"] = "2026-01-01T00:00:00Z"
                    self.cluster.kube.add(agent)
                self.assertEqual("running", C.status(self.item)[0])

    def test_alternate_agent_name_and_target_namespace_are_supported(self):
        self.seed(namespace="network-system", agent_name="rke2-multus")
        self.assertEqual("succeeded", C.status(self.item)[0])

    def test_failed_release_and_active_retry_are_distinguished(self):
        self.seed()
        self.release(self.target, "failed")
        self.assertEqual("failed", C.status(self.item)[0])
        self.cluster.kube.add(job("helm-install-multus", {"active": 1}))
        self.assertEqual(("running", 50), C.status(self.item)[:2])

    def test_controller_job_failure_uses_its_reported_job_name(self):
        self.seed()
        self.release(self.previous)
        chart = self.cluster.kube.object(CHART + self.component)
        chart["status"] = {"jobName": "helm-install-network"}
        self.cluster.kube.add(chart)
        self.cluster.kube.add(job("helm-install-network", {"failed": 1}))
        with patch.object(C, "_elapsed", return_value=121):
            state, _, message = C.status(self.item)
        self.assertEqual("failed", state)
        self.assertIn("helm-install-network", message)

    def test_unknown_component_never_falls_through_to_cdi(self):
        self.seed()
        with patch.object(C, "cdi_version", side_effect=AssertionError("CDI is unrelated")):
            self.assertEqual("failed", C.status({"ref": {"component": "other", "to": "1.0.0"}})[0])


class FakeAddons:
    Held = type("Held", (ValueError,), {})

    def __init__(self, status):
        self._status, self.installed = status, []

    def status(self):
        return self._status

    def install_kube_vip(self, cfg):
        self.installed.append(("kube-vip", cfg))
        return {"detail": "kube-vip is being installed", "job": "helm-install-kube-vip"}

    def install_multus(self, cfg):
        self.installed.append(("multus", cfg))
        return {"detail": "Multus is being installed", "job": "helm-install-multus"}


class BaselineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.kube = self.enterContext(Kube(
            {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "homestead-install", "namespace": "lab"},
             "data": {"kube-vip": "yes", "kube-vip-version": "", "multus": "yes", "multus-version": "v4.3.101"}}))
        # BASELINE reads the cluster through the shared client here, whatever another test bound.
        self.enterContext(patch.multiple(BASELINE, kget=BASELINE.kget, addons=BASELINE.addons, platform=BASELINE.platform,
                                         DEFAULT_NS=BASELINE.DEFAULT_NS, DATA_DIR=BASELINE.DATA_DIR,
                                         macvtap=BASELINE.macvtap, probe=BASELINE.probe,
                                         probe_version=BASELINE.probe_version,
                                         _install_checked=False, create=True))
        self.platform = {"distribution": "k3s", "helm_controller": True, "load_balancer": "servicelb", "multus": False}
        self.addons = FakeAddons({"kube_vip": {"installing": False}, "multus": {"installed": False, "installing": False}})
        BASELINE.bind(K.kget, self.addons, lambda force=False: self.platform, "lab", self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def ask(self, **data):
        """Change what the installer asked for, as it would (None takes a key out)."""
        request = self.kube.object(REQUEST)
        for key, value in data.items():
            if value is None:
                request["data"].pop(key, None)
            else:
                request["data"][key] = value
        self.kube.add(request)

    def withdraw(self):
        self.kube.discard(REQUEST)

    def test_the_installer_s_request_is_installed_once(self):
        results = BASELINE.tick()
        self.assertEqual([("kube-vip", {}), ("multus", {"version": "v4.3.101"})], self.addons.installed)
        self.assertTrue(all(row["ok"] for row in results))
        self.assertEqual([], BASELINE.tick(), "asked once: a part removed later stays removed")
        done = json.loads(Path(self.tmp.name, "baseline.json").read_text())["done"]
        self.assertEqual({"kube-vip", "multus"}, set(done))

    def test_what_is_there_already_is_not_installed(self):
        self.platform["load_balancer"] = "metallb"
        BASELINE.tick()
        self.assertEqual(["multus"], [part for part, _ in self.addons.installed])

    def test_no_request_no_install(self):
        self.withdraw()
        self.assertEqual([], BASELINE.tick())
        self.assertEqual([], self.addons.installed)

    def test_harvester_and_plain_kubernetes_are_left_alone(self):
        for platform in ({"harvester": True, "distribution": "harvester", "helm_controller": True},
                         {"distribution": "kubernetes", "helm_controller": False}):
            self.platform = platform
            self.assertEqual([], BASELINE.tick())
            self.assertFalse(BASELINE.report()["applies"])
        self.assertEqual([], self.addons.installed)

    def test_the_report_lists_what_is_missing(self):
        report = BASELINE.report()
        self.assertEqual((True, ["kube-vip", "multus"]), (report["applies"], report["missing"]))
        self.addons._status["multus"]["installing"] = True
        self.assertEqual(["kube-vip"], BASELINE.report()["missing"])

    def test_the_node_probe_the_installer_asked_for_is_installed_once(self):
        made = []
        probe = type("P", (), {"installed": staticmethod(lambda: "homestead-nodeprobe" if made else ""),
                               "install": staticmethod(lambda version: made.append(version) or {"detail": "installed"})})
        BASELINE.bind(K.kget, self.addons, lambda force=False: self.platform, "lab", self.tmp.name, None, probe, "2.8.235")
        self.ask(**{"node-probe": "yes"})
        BASELINE.tick()
        BASELINE.tick()
        self.assertEqual(["2.8.235"], made, "asked once: a probe removed later stays removed")

    def test_node_probe_is_automatic_on_existing_clusters_and_all_distributions(self):
        made = []
        probe = type("P", (), {"installed": staticmethod(lambda: ""),
                               "install": staticmethod(lambda version: made.append(version) or {"detail": "installed"})})
        self.withdraw()
        for distribution in ("k3s", "rke2", "harvester", "kubernetes"):
            with self.subTest(distribution=distribution), tempfile.TemporaryDirectory() as directory:
                self.platform = {"distribution": distribution, "harvester": distribution == "harvester"}
                BASELINE.bind(K.kget, self.addons, lambda force=False: self.platform, "lab", directory, None, probe, "2.8.283")
                self.assertTrue(BASELINE.probe_tick()["ok"])
                self.assertIsNone(BASELINE.probe_tick(), "a later intentional removal stays removed")
        self.assertEqual(["2.8.283"] * 4, made)

    def test_node_probe_retries_failed_installation_and_respects_explicit_opt_out(self):
        attempts = []
        def install(version):
            attempts.append(version)
            if len(attempts) == 1:
                raise ValueError("API temporarily unavailable")
            return {"detail": "installed"}
        probe = type("P", (), {"installed": staticmethod(lambda: ""), "install": staticmethod(install)})
        BASELINE.bind(K.kget, self.addons, lambda force=False: self.platform, "lab", self.tmp.name, None, probe)
        with patch.dict(BASELINE.os.environ, {"HOMESTEAD_NODEPROBE_AUTO_INSTALL": "false"}):
            self.assertIsNone(BASELINE.probe_tick(), "Helm opt-out is preserved")
        self.ask(**{"node-probe": "no"})
        self.assertIsNone(BASELINE.probe_tick())
        self.assertEqual([], attempts)
        self.ask(**{"node-probe": None})
        self.assertFalse(BASELINE.probe_tick()["ok"])
        self.assertTrue(BASELINE.probe_tick()["ok"])
        self.assertIsNone(BASELINE.probe_tick())
        self.assertEqual(2, len(attempts))

    def test_a_failed_install_is_recorded_with_its_reason(self):
        def refuse(cfg):
            raise ValueError("kube-vip is already being installed")
        self.addons.install_kube_vip = refuse
        results = BASELINE.install(["kube-vip"])
        self.assertEqual((False, "kube-vip is already being installed"), (results[0]["ok"], results[0]["detail"]))

    def test_a_part_held_back_to_spare_longhorns_volumes_is_asked_again(self):
        held, original = [True], self.addons.install_kube_vip
        def maybe(cfg):
            if held[0]:
                raise FakeAddons.Held("kube-vip is waiting: node-1 has 0m CPU unrequested")
            return original(cfg)
        self.addons.install_kube_vip = maybe
        first = {row["id"]: row for row in BASELINE.tick()}
        self.assertTrue(first["kube-vip"]["held"])
        self.assertNotIn("kube-vip", BASELINE._load().get("done", {}), "held is not tried: it is not recorded")
        self.assertEqual(["multus"], [part for part, _ in self.addons.installed])
        held[0] = False
        self.assertEqual(["kube-vip"], [row["id"] for row in BASELINE.tick()])
        self.assertEqual(["multus", "kube-vip"], [part for part, _ in self.addons.installed])
        self.assertEqual([], BASELINE.tick(), "and then, as any part, never again")


class StandInProbe:
    """The node probe as baseline sees it: there or not, and what was asked of it."""

    def __init__(self, present=False):
        self.present, self.made, self.removals, self.install_errors = present, [], 0, []
        self.unreadable = False

    def installed(self):
        return "homestead-nodeprobe" if self.present else ""

    def presence(self):
        return None if self.unreadable else self.present

    def install(self, version):
        if self.install_errors:
            raise self.install_errors.pop(0)
        self.made.append(version)
        self.present = True
        return {"detail": "installed"}

    def remove(self):
        self.removals += 1
        self.present = False
        return {"state": "absent", "detail": "the node probe was removed"}


class ProbePresenceTests(unittest.TestCase):
    """The real read, as the first look uses it: only the API's own 404 says absent."""

    DS = "/apis/apps/v1/namespaces/lab/daemonsets/homestead-nodeprobe"

    def setUp(self):
        self.kube = self.enterContext(Kube())
        self.enterContext(patch.multiple(PROBE, kget=K.kget, NS="lab"))

    def test_it_says_what_the_api_says(self):
        self.assertIs(False, PROBE.presence())
        self.kube.add(daemonset("example.test/probe:1", "homestead-nodeprobe", "lab"))
        self.assertIs(True, PROBE.presence())

    def test_a_failed_read_is_unknown_not_absent(self):
        for failure in (500, 403, TimeoutError("timed out")):
            with self.subTest(failure=failure):
                self.kube.fail(self.DS, failure, times=1, method="GET")
                self.assertIsNone(PROBE.presence())


class ProbeAfterReinstallTests(unittest.TestCase):
    """/data survives a reinstall, so baseline.json still says the probe is done:
    Homestead's own Deployment uid is what says this is a new install."""

    OWN = "/apis/apps/v1/namespaces/lab/deployments/homestead"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.kube = self.enterContext(Kube())
        self.enterContext(patch.multiple(BASELINE, kget=BASELINE.kget, addons=BASELINE.addons, platform=BASELINE.platform,
                                         DEFAULT_NS=BASELINE.DEFAULT_NS, DATA_DIR=BASELINE.DATA_DIR,
                                         macvtap=BASELINE.macvtap, probe=BASELINE.probe,
                                         probe_version=BASELINE.probe_version,
                                         _install_checked=False, create=True))
        self.enterContext(patch.object(SELF, "NS", "lab"))
        self.probe = StandInProbe()
        self.start("old")

    def start(self, uid):
        """A new process, running under a Deployment with this uid (None: none yet)."""
        self.kube.discard(self.OWN)
        if uid:
            self.kube.add({"apiVersion": "apps/v1", "kind": "Deployment",
                           "metadata": {"name": "homestead", "namespace": "lab", "uid": uid}})
        BASELINE.bind(K.kget, None, lambda force=False: {}, "lab", self.tmp.name, None, self.probe, "2.9.0")

    def row(self, **fields):
        BASELINE._save({"done": {"node-probe": dict({"at": 1, "version": "2.8.0", "reason": "automatic", "error": ""},
                                                    **fields)}})

    def saved(self):
        return BASELINE._load()["done"]["node-probe"]

    def route(self, path):
        return BASELINE.ROUTES[("POST", path)][1](None)

    def test_a_reinstall_keeping_its_data_puts_the_probe_back(self):
        self.row(install="old")
        self.start("new")
        self.assertTrue(BASELINE.probe_tick()["ok"])
        self.assertEqual(["2.9.0"], self.probe.made)
        self.assertEqual(("reinstalled", "new", ""), tuple(self.saved()[key] for key in ("reason", "install", "error")))

    def test_the_same_install_leaves_a_missing_probe_alone_and_records_the_removal(self):
        """Deleted with kubectl: the first look of the next process sees it, so a reinstall leaves it away."""
        self.row(install="old")
        self.assertIsNone(BASELINE.probe_tick())
        self.assertEqual([], self.probe.made)
        self.assertTrue(self.saved()["removed"])
        self.start("new")
        self.assertIsNone(BASELINE.probe_tick())
        self.assertEqual([], self.probe.made)

    def test_the_same_install_with_the_probe_there_records_nothing(self):
        self.row(install="old")
        self.probe.present = True
        self.assertIsNone(BASELINE.probe_tick())
        self.assertNotIn("removed", self.saved())

    def test_a_probe_deleted_with_no_restart_before_the_reinstall_comes_back(self):
        """What can't be recorded: no process looked between the delete and the reinstall."""
        self.row(install="old")
        self.probe.present = True
        self.assertIsNone(BASELINE.probe_tick())
        self.probe.present = False
        self.assertIsNone(BASELINE.probe_tick(), "this process has looked already")
        self.assertNotIn("removed", self.saved())
        self.start("new")
        self.assertTrue(BASELINE.probe_tick()["ok"])
        self.assertEqual(["2.9.0"], self.probe.made)

    def test_a_failed_read_of_the_probe_marks_nothing_and_asks_again(self):
        for row in ({"install": "old"}, {}):
            with self.subTest(row=row):
                self.row(**row)
                self.start("old")
                self.probe.present, self.probe.unreadable = False, True
                self.assertIsNone(BASELINE.probe_tick())
                self.assertNotIn("removed", self.saved())
                self.assertEqual(row.get("install"), self.saved().get("install"), "decided nothing, so stamped nothing")
                self.probe.unreadable = False
                self.probe.present = True
                self.assertIsNone(BASELINE.probe_tick())
                self.assertEqual("old", self.saved()["install"])
                self.assertNotIn("removed", self.saved())

    def test_a_recorded_removal_needs_no_look_at_the_probe(self):
        self.row(install="old", removed=5)
        self.probe.unreadable = True
        self.start("new")
        self.assertIsNone(BASELINE.probe_tick())
        self.assertEqual(("new", 5), (self.saved()["install"], self.saved()["removed"]))

    def opted_out(self):
        self.kube.add({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "homestead-install", "namespace": "lab"},
                       "data": {"node-probe": "no"}})

    def test_a_failed_read_of_the_opt_out_installs_nothing_after_a_reinstall(self):
        """A privileged DaemonSet on a cluster that said no, for one 500 or time-out."""
        self.opted_out()
        self.row(install="old")
        self.start("new")
        for failure in (500, TimeoutError("timed out")):
            with self.subTest(failure=failure):
                self.kube.fail(REQUEST, failure, times=1, method="GET")
                self.assertIsNone(BASELINE.probe_tick())
                self.assertEqual([], self.probe.made)
        self.assertIsNone(BASELINE.probe_tick())
        self.assertEqual([], self.probe.made)

    def test_a_failed_read_of_the_opt_out_installs_nothing_on_a_first_install(self):
        self.opted_out()
        self.kube.fail(REQUEST, 500, times=1, method="GET")
        self.assertIsNone(BASELINE.probe_tick())
        self.assertEqual([], self.probe.made)
        self.assertNotIn("node-probe", BASELINE._load().get("done", {}))
        self.assertIsNone(BASELINE.probe_tick())
        self.assertEqual([], self.probe.made)

    def test_a_failed_read_of_the_request_retries_when_it_was_not_opted_out(self):
        self.kube.fail(REQUEST, 500, times=1, method="GET")
        self.assertIsNone(BASELINE.probe_tick())
        self.assertEqual([], self.probe.made)
        self.assertTrue(BASELINE.probe_tick()["ok"], "no request at all (a 404) means the default: installed")
        self.assertEqual(["2.9.0"], self.probe.made)

    def test_homesteads_own_namespace_is_where_it_looks_when_the_apps_are_elsewhere(self):
        """The apps' namespace can differ, and reading it would silently stop this working."""
        self.row(install="old")
        self.kube.discard(self.OWN)
        self.kube.add({"apiVersion": "apps/v1", "kind": "Deployment",
                       "metadata": {"name": "homestead", "namespace": "own-ns", "uid": "new"}})
        with patch.object(SELF, "NS", "own-ns"):
            BASELINE.bind(K.kget, None, lambda force=False: {}, "lab", self.tmp.name, None, self.probe, "2.9.0")
            self.assertTrue(BASELINE.probe_tick()["ok"])
        self.assertEqual(["2.9.0"], self.probe.made)
        self.assertEqual("new", self.saved()["install"])

    def test_a_removal_from_before_this_release_stays_removed_after_a_reinstall(self):
        self.row()
        self.assertIsNone(BASELINE.probe_tick())
        self.assertEqual("old", self.saved()["install"])
        self.assertTrue(self.saved()["removed"], "absent at the first look counts as removed on purpose")
        self.start("new")
        self.assertIsNone(BASELINE.probe_tick())
        self.assertEqual([], self.probe.made)
        self.assertEqual("new", self.saved()["install"])

    def test_a_probe_there_at_the_first_look_on_this_release_comes_back_after_a_reinstall(self):
        self.row()
        self.probe.present = True
        self.assertIsNone(BASELINE.probe_tick())
        self.assertEqual("old", self.saved()["install"])
        self.assertNotIn("removed", self.saved())
        self.probe.present = False
        self.start("new")
        self.assertTrue(BASELINE.probe_tick()["ok"])
        self.assertEqual(["2.9.0"], self.probe.made)

    def test_a_probe_removed_on_the_page_stays_removed_after_a_reinstall(self):
        self.row(install="old")
        self.probe.present = True
        self.assertEqual("absent", self.route("/api/node/probe/remove")["state"])
        self.assertEqual(1, self.probe.removals)
        self.assertTrue(self.saved()["removed"])
        self.start("new")
        self.assertIsNone(BASELINE.probe_tick())
        self.assertEqual([], self.probe.made)
        self.assertEqual("new", self.saved()["install"])
        self.assertTrue(self.saved()["removed"])
        self.route("/api/node/probe/install")
        self.assertEqual(["2.9.0"], self.probe.made)
        self.assertNotIn("removed", self.saved(), "installing it on the page takes the removal back")
        self.probe.present = False
        self.start("newer")
        self.assertTrue(BASELINE.probe_tick()["ok"])
        self.assertEqual(["2.9.0", "2.9.0"], self.probe.made)

    def test_a_removal_ends_a_failed_install_s_retries(self):
        self.row(install="old", error="API temporarily unavailable")
        self.route("/api/node/probe/remove")
        self.assertIsNone(BASELINE.probe_tick())
        self.assertEqual([], self.probe.made)

    def test_a_removal_before_the_first_look_stays_removed(self):
        self.route("/api/node/probe/remove")
        self.assertIsNone(BASELINE.probe_tick())
        self.assertEqual([], self.probe.made)
        self.assertEqual("old", self.saved()["install"])

    def test_a_reinstall_whose_chart_brought_the_probe_makes_nothing(self):
        self.row(install="old")
        self.probe.present = True
        self.start("new")
        BASELINE.probe_tick()
        self.assertEqual([], self.probe.made)
        self.assertEqual(("present", "new"), (self.saved()["reason"], self.saved()["install"]))

    def test_an_unreadable_deployment_decides_nothing_and_asks_again(self):
        self.row(install="old")
        self.start("new")
        self.kube.fail(self.OWN, 500, times=1, method="GET")
        self.assertIsNone(BASELINE.probe_tick())
        self.assertEqual([], self.probe.made)
        self.assertEqual("old", self.saved()["install"])
        self.assertTrue(BASELINE.probe_tick()["ok"])
        self.assertEqual(["2.9.0"], self.probe.made)

    def test_no_deployment_of_its_own_decides_nothing(self):
        self.row(install="old")
        self.start(None)
        self.assertIsNone(BASELINE.probe_tick())
        self.assertEqual([], self.probe.made)
        self.assertEqual("old", self.saved()["install"])

    def test_a_first_install_with_no_deployment_of_its_own_still_installs(self):
        self.start(None)
        self.assertTrue(BASELINE.probe_tick()["ok"])
        self.assertEqual(["2.9.0"], self.probe.made)
        self.assertEqual("", self.saved()["install"])
        self.assertIsNone(BASELINE.probe_tick())

    def test_an_install_recorded_with_no_uid_is_stamped_and_not_put_back(self):
        self.row(install="")
        self.probe.present = True
        self.assertIsNone(BASELINE.probe_tick())
        self.assertEqual("old", self.saved()["install"])
        self.assertEqual([], self.probe.made)

    def test_a_failed_install_after_a_reinstall_retries_with_the_new_uid(self):
        self.row(install="old")
        self.start("new")
        self.probe.install_errors.append(ValueError("API temporarily unavailable"))
        self.assertFalse(BASELINE.probe_tick()["ok"])
        self.assertEqual(("API temporarily unavailable", "new"), (self.saved()["error"], self.saved()["install"]))
        self.assertTrue(BASELINE.probe_tick()["ok"])
        self.assertEqual(["2.9.0"], self.probe.made)
        self.assertEqual(("", "new"), (self.saved()["error"], self.saved()["install"]))
        self.assertIsNone(BASELINE.probe_tick())

    def test_it_reads_the_deployment_once_per_process(self):
        self.row(install="old")
        reads = []
        self.kube.react("GET", self.OWN, lambda method, path, body: reads.append(path) or
                        self.kube.object(self.OWN))
        for _ in range(3):
            self.assertIsNone(BASELINE.probe_tick())
        self.assertEqual(1, len(reads))

    def test_a_first_install_reads_the_deployment_once_too(self):
        reads = []
        self.kube.react("GET", self.OWN, lambda method, path, body: reads.append(path) or
                        self.kube.object(self.OWN))
        self.assertTrue(BASELINE.probe_tick()["ok"])
        self.assertIsNone(BASELINE.probe_tick())
        self.assertEqual(1, len(reads))

    def test_a_retry_that_cant_read_the_deployment_keeps_the_uid_it_had(self):
        self.row(install="old", error="API temporarily unavailable")
        self.kube.fail(self.OWN, 500, times=1, method="GET")
        self.assertTrue(BASELINE.probe_tick()["ok"])
        self.assertEqual("old", self.saved()["install"])

    def test_a_removal_that_cant_be_recorded_deletes_nothing(self):
        self.row(install="old")
        self.probe.present = True
        with patch.object(BASELINE, "_save", side_effect=OSError("the data volume is read-only")):
            with self.assertRaises(OSError):
                self.route("/api/node/probe/remove")
        self.assertEqual(0, self.probe.removals)
        self.assertTrue(self.probe.present)

    def test_an_opted_out_install_ignores_a_reinstall(self):
        self.opted_out()
        self.row(install="old")
        self.start("new")
        self.kube.react("GET", self.OWN, lambda method, path, body: self.fail("read the Deployment"))
        self.assertIsNone(BASELINE.probe_tick())
        self.assertEqual([], self.probe.made)
        with patch.dict(BASELINE.os.environ, {"HOMESTEAD_NODEPROBE_AUTO_INSTALL": "false"}):
            self.assertIsNone(BASELINE.probe_tick())

    def test_the_two_routes_are_admin_only_and_go_to_the_release_it_runs(self):
        for path in ("/api/node/probe/remove", "/api/node/probe/install"):
            self.assertEqual("admin", BASELINE.ROUTES[("POST", path)][0])
        self.route("/api/node/probe/install")
        self.assertEqual(["2.9.0"], self.probe.made)
        self.assertNotIn("node-probe", BASELINE._load().get("done", {}), "the page's install leaves the tick's first look alone")


if __name__ == "__main__":
    unittest.main()
