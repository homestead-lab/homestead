import copy
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_operator_manifests as O
import homestead_components as C


class ManifestOperatorTests(unittest.TestCase):
    def setUp(self):
        self.platform = {"distribution": "k3s", "kubevirt": True, "cdi": True}
        self.objects, self.sent, self.runs = {}, [], []
        self.cr = {"apiVersion": "kubevirt.io/v1", "kind": "KubeVirt", "metadata": {
            "name": "kubevirt", "namespace": "kubevirt", "uid": "configuration-uid", "resourceVersion": "21"},
            "spec": {"configuration": {"developerConfiguration": {"featureGates": ["LiveMigration"]},
                                       "permittedHostDevices": {"pciHostDevices": [{"resourceName": "example.com/gpu"}]}},
                     "workloadUpdateStrategy": {"workloadUpdateMethods": ["LiveMigrate"]}},
            "status": {"observedKubeVirtVersion": "v1.9.0", "phase": "Deployed"}}
        self.dep = {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {
            "name": "virt-operator", "namespace": "kubevirt", "uid": "operator-uid", "resourceVersion": "10"},
            "spec": {"template": {"spec": {"containers": [{"name": "virt-operator", "image": "quay.io/kubevirt/virt-operator:v1.9.0"}]}}}}
        self.objects[O.PARTS["kubevirt"][1]] = {"items": [self.cr]}
        self.objects["/apis/apps/v1/namespaces/kubevirt/deployments/virt-operator"] = self.dep
        self.node = {"metadata": {"name": "node-1", "labels": {"node-role.kubernetes.io/control-plane": "true"}},
                     "status": {"conditions": [{"type": "Ready", "status": "True"}]}}
        self.objects["/api/v1/nodes"] = {"items": [self.node]}
        self.desired = [copy.deepcopy(self.dep)]
        for key in ("uid", "resourceVersion"):
            self.desired[0]["metadata"].pop(key)
        self.desired[0]["spec"]["template"]["spec"]["containers"][0]["image"] = "quay.io/kubevirt/virt-operator:v1.9.1"
        self.live = [self.dep]
        self.addons = type("Addons", (), {"KUBEVIRT": "https://example.com/kubevirt", "CDI": "https://example.com/cdi",
                                          "fetch": staticmethod(lambda url: ("upstream manifest", url))})
        self.host = patch.object(O.HOSTRUN, "run", side_effect=self.host_run)
        self.host.start()
        self.addCleanup(self.host.stop)

    def read(self, path):
        if path in self.objects:
            return self.objects[path]
        raise urllib.error.HTTPError(path, 404, "missing", {}, None)

    def send(self, *args, **kw):
        self.sent.append((args, kw))

    def host_run(self, node, script, **kw):
        self.runs.append((node, script, kw))
        if "homestead-files-checked" in script:
            return "homestead-files-checked\n", ""
        if "--dry-run=client" in script:
            # kubectl create prints each resource separately, including with
            # --dry-run=client; unlike get, it does not wrap them in a List.
            return "".join(json.dumps(row, indent=4) + "\n" for row in self.desired) + O.READ_COMPLETE, ""
        if "get --ignore-not-found" in script:
            return json.dumps({"kind": "List", "items": self.live}) + O.READ_COMPLETE, ""
        return "homestead-operator-applied\n", ""

    def enable(self):
        O.enable("kubevirt", "v1.9.0", self.read, self.send, self.platform, self.addons)

    def upgrade(self):
        self.cr["metadata"]["annotations"] = {O.MARK: "manifests"}
        return O.upgrade("kubevirt", "v1.9.1", self.read, self.platform, self.addons)

    def test_unmarked_manifest_install_requires_explicit_management(self):
        state = O.state("kubevirt", self.read, self.platform)
        self.assertEqual(("manual", True), (state["how"], state["can_manage"]))
        self.assertNotIn("outside Homestead", state["note"])
        self.assertFalse(self.runs)

    def test_bootstrap_marker_enables_upgrades_without_adoption(self):
        self.cr["metadata"]["annotations"] = {O.MARK: "manifests"}
        self.assertEqual("manifests", O.state("kubevirt", self.read, self.platform)["how"])

    def test_all_concatenated_release_objects_are_checked_before_enabling(self):
        self.desired.insert(0, {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "kubevirt"}})
        self.enable()
        inventory = next(options["stdin"] for _, script, options in self.runs if "get --ignore-not-found" in script)
        self.assertEqual(self.desired, json.loads(inventory)["items"])
        self.assertEqual(1, len(self.sent))

    def test_manifest_list_and_single_object_outputs_remain_supported(self):
        for output in (json.dumps(self.desired[0]), json.dumps({"kind": "List", "items": self.desired})):
            with self.subTest(output=output), patch.object(O, "_read_input", return_value=(output, "")):
                self.assertEqual(self.desired, O._documents("kubevirt", "v1.9.0", self.addons, "node-1", "k3s kubectl"))

    def test_invalid_or_truncated_manifest_stream_never_enables_upgrades(self):
        valid = json.dumps(self.desired[0], indent=4)
        for output in ("", " \n\t", valid + '\n{"kind":', valid + "\ninvalid tail", valid + "\n[]",
                       valid + '\n{"kind":"List","items":{}}', valid + "\nnull", valid + '\n{"kind":"List"}',
                       valid + '\n{"kind":"List","items":[],"metadata":{"continue":"next"}}'):
            with self.subTest(output=output):
                def read_input(node, command, arguments, payload):
                    if "--dry-run=client" in arguments:
                        return output, ""
                    return json.dumps({"kind": "List", "items": self.live}), ""
                with patch.object(O, "_read_input", side_effect=read_input):
                    with self.assertRaisesRegex(ValueError, "could not read the upstream operator manifest: .+[^ ]"):
                        self.enable()
        self.assertFalse(self.sent)
        self.assertFalse(any("change" in options for _, _, options in self.runs))

    def test_manifest_read_failure_reports_kubectl_diagnostic(self):
        with patch.object(O, "_read_input", return_value=("", "  Unable to connect to the server\n")):
            with self.assertRaisesRegex(ValueError, "manifest: Unable to connect to the server$"):
                self.enable()
        self.assertFalse(self.sent)

    def test_complete_objects_without_command_receipt_never_enable_upgrades(self):
        original = self.host_run
        for stage in ("--dry-run=client", "get --ignore-not-found"):
            with self.subTest(stage=stage):
                def unfinished(node, script, **options):
                    out, err = original(node, script, **options)
                    return (out.removesuffix(O.READ_COMPLETE), err) if stage in script else (out, err)
                with patch.object(O.HOSTRUN, "run", side_effect=unfinished):
                    with self.assertRaisesRegex(ValueError, "kubectl inspection did not finish"):
                        self.enable()
        self.assertFalse(self.sent)

    def test_cdi_release_stream_keeps_every_resource(self):
        rows = [{"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": "cdi-operator", "namespace": "cdi"}},
                {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "cdi-operator", "namespace": "cdi"}}]
        output = " \n" + "\n\n".join(json.dumps(row, indent=4) for row in rows) + "\n\t"
        with patch.object(O, "_read_input", return_value=(output, "")):
            self.assertEqual(rows, O._documents("cdi", "v1.66.1", self.addons, "node-1", "k3s kubectl"))

    def test_enable_only_marks_the_reviewed_configuration_and_never_updates_operator(self):
        before = copy.deepcopy(self.cr)
        self.enable()
        self.assertEqual(before, self.cr)
        args, kw = self.sent[0]
        self.assertEqual("application/json-patch+json", kw["ctype"])
        self.assertEqual(["test", "test", "add"], [step["op"] for step in args[2]])
        self.assertEqual("configuration-uid", args[2][0]["value"])
        self.assertEqual("21", args[2][1]["value"])
        self.assertFalse(any("change" in kw for _, _, kw in self.runs))

    def test_helm_flux_argo_and_operator_owned_installs_are_not_adopted(self):
        for metadata in ({"annotations": {"meta.helm.sh/release-name": "external"}},
                         {"labels": {"kustomize.toolkit.fluxcd.io/name": "platform"}},
                         {"annotations": {"argocd.argoproj.io/tracking-id": "platform:apps/Deployment:ns/name"}},
                         {"ownerReferences": [{"uid": "other-operator"}]},
                         {"labels": {"olm.owner": "external"}}):
            with self.subTest(metadata=metadata):
                old = copy.deepcopy(self.dep["metadata"])
                self.dep["metadata"].update(metadata)
                with self.assertRaisesRegex(ValueError, "managed by"):
                    self.enable()
                self.dep["metadata"] = old
        self.assertFalse(self.sent)
        self.assertFalse(self.runs)

    def test_an_external_owner_on_a_release_role_blocks_before_any_write(self):
        role = {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRole",
                "metadata": {"name": "kubevirt-operator"}, "rules": []}
        self.desired.insert(0, role)
        self.live.insert(0, {**role, "metadata": {"name": "kubevirt-operator", "uid": "role", "resourceVersion": "2",
                                                "annotations": {"meta.helm.sh/release-name": "external"}}})
        with self.assertRaisesRegex(ValueError, "ClusterRole.*Helm"):
            self.enable()
        self.assertFalse(self.sent)

    def test_harvester_and_custom_images_are_not_managed(self):
        for change in ("harvester", "image", "pin"):
            with self.subTest(change=change):
                platform, dep, cr = copy.deepcopy((self.platform, self.dep, self.cr))
                if change == "harvester":
                    self.platform["harvester"] = True
                elif change == "image":
                    self.dep["spec"]["template"]["spec"]["containers"][0]["image"] = "registry.example.com/custom:v1.9.0"
                else:
                    self.cr["spec"]["imageTag"] = "v1.9.0"
                self.assertFalse(O.state("kubevirt", self.read, self.platform)["can_manage"])
                self.platform.clear(); self.platform.update(platform)
                self.dep.clear(); self.dep.update(dep)
                self.cr.clear(); self.cr.update(cr)

    def test_failed_or_partial_inventory_never_becomes_permission_to_adopt(self):
        for listing in ({}, {"items": [self.cr], "metadata": {"continue": "next"}}, {"items": []},
                        {"items": [self.cr, self.cr]}):
            self.objects[O.PARTS["kubevirt"][1]] = listing
            self.assertFalse(O.state("kubevirt", self.read, self.platform)["can_manage"])
        self.assertFalse(self.sent)

    def test_every_control_plane_host_must_be_ready(self):
        self.node["status"]["conditions"][0]["status"] = "False"
        with self.assertRaisesRegex(ValueError, "Every control-plane"):
            self.enable()
        self.assertFalse(self.runs)

    def test_active_autodeploy_file_blocks_without_disabling_it(self):
        with patch.object(O.HOSTRUN, "run", return_value=("", "Active auto-deploy file")):
            with self.assertRaisesRegex(ValueError, "Active auto-deploy"):
                self.enable()
        self.assertFalse(self.sent)

    def test_a_host_kubeconfig_for_another_cluster_cannot_adopt_or_update_it(self):
        self.live = [copy.deepcopy(self.dep)]
        self.live[0]["metadata"]["uid"] = "another-clusters-operator"
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.enable()
        self.assertFalse(self.sent)
        self.assertFalse(any("change" in kw for _, _, kw in self.runs))

    @unittest.skipUnless(os.name == "posix", "executes the host's POSIX shell")
    def test_operator_reads_finish_while_exec_stdin_stays_open(self):
        with patch.object(self.addons, "fetch", return_value=("upstream manifest with a non-ASCII label: café", "")):
            self.enable()
        reads = [(script, options) for _, script, options in self.runs if options.get("stdin")]
        self.assertEqual(2, len(reads), "manifest parsing and live inventory both use stdin")
        with tempfile.TemporaryDirectory() as folder:
            command = Path(folder) / "k3s"
            received = Path(folder) / "received"
            input_path = Path(folder) / "input-path"
            command.write_text('#!/bin/sh\nwhile [ "$1" != -f ]; do shift; done\nshift\n'
                               'printf "%s" "$1" > "$INPUT_PATH"\n'
                               'if [ "$1" = - ]; then cat; else cat "$1"; fi > "$RECEIVED"\n'
                               'printf \'{"kind":"List","items":[]}\\n\'\nexit "${FAIL_READ:-0}"\n')
            command.chmod(0o755)
            env = {**os.environ, "PATH": folder + os.pathsep + os.environ["PATH"],
                   "RECEIVED": str(received), "INPUT_PATH": str(input_path)}
            for script, options in reads:
                for fail in (False, True):
                    with self.subTest(inventory="get --ignore-not-found" in script, failed=fail):
                        process = subprocess.Popen(["sh", "-c", script], stdin=subprocess.PIPE,
                                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                                   env={**env, "FAIL_READ": "7" if fail else "0"})
                        try:
                            process.stdin.write(options["stdin"])
                            process.stdin.flush()
                            # Kubernetes exec v4 leaves stdin open after sending the bytes.
                            # Waiting here (not communicate()) catches readers waiting for EOF.
                            try:
                                process.wait(timeout=3)
                            except subprocess.TimeoutExpired:
                                self.fail("operator inspection waited for stdin EOF")
                            self.assertEqual(7 if fail else 0, process.returncode)
                            self.assertEqual(not fail, process.stdout.read().endswith(O.READ_COMPLETE.encode()),
                                             "only a completed kubectl command produces the receipt")
                            self.assertEqual(options["stdin"], received.read_bytes())
                            self.assertFalse(Path(input_path.read_text()).exists(), "temporary input is removed")
                        finally:
                            if process.poll() is None:
                                process.kill()
                            process.communicate(timeout=3)
                # A truncated stream must not reach kubectl, even if it ends cleanly.
                received.unlink()
                short = subprocess.run(["sh", "-c", script], input=options["stdin"][:-1],
                                       capture_output=True, env=env, timeout=3)
                self.assertNotEqual(0, short.returncode)
                self.assertIn(b"Incomplete operator input", short.stderr)
                self.assertFalse(received.exists())

    def test_update_preserves_configuration_pins_objects_and_dry_runs_before_applying(self):
        self.upgrade()
        _, script, options = self.runs[-1]
        self.assertIn("--dry-run=server", script)
        self.assertLess(script.index("--dry-run=server"), script.rindex("--field-manager=kubectl"))
        self.assertNotIn("--force", script)
        self.assertNotIn(" replace ", script)
        rows = [json.loads(line) for line in options["stdin"].decode().splitlines()]
        self.assertEqual(("operator-uid", "10"), (rows[0]["metadata"]["uid"], rows[0]["metadata"]["resourceVersion"]))
        self.assertEqual(["Deployment"], [row["kind"] for row in rows])
        self.assertFalse(self.sent, "KubeVirt configuration and VMs were never written")

    def test_new_objects_use_create_so_a_racing_owner_cannot_be_taken_over(self):
        self.desired.insert(0, {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {
            "name": "kubevirt-new-helper", "namespace": "kubevirt"}})
        self.upgrade()
        script = self.runs[-1][1]
        self.assertIn('create --dry-run=server -f "$d/0.json"', script)
        self.assertIn('create -f "$d/0.json"', script)

    @unittest.skipUnless(os.name == "posix", "executes the host's POSIX shell")
    def test_generated_update_script_stops_before_all_writes_on_dry_run_failure(self):
        self.upgrade()
        _, script, options = self.runs[-1]
        with tempfile.TemporaryDirectory() as folder:
            executable = Path(folder) / "k3s"
            log = Path(folder) / "calls"
            executable.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$CALLS"\n'
                                  'case "$*" in *--dry-run=server*) [ "$FAIL_DRY" != 1 ] || exit 1;; esac\n')
            executable.chmod(0o755)
            env = {**os.environ, "PATH": folder + os.pathsep + os.environ["PATH"], "CALLS": str(log), "FAIL_DRY": "1"}
            failed = subprocess.run(["sh", "-c", script], input=options["stdin"], capture_output=True, env=env)
            self.assertNotEqual(0, failed.returncode)
            self.assertNotIn(b"homestead-operator-applied", failed.stdout)
            self.assertTrue(all("--dry-run=server" in line for line in log.read_text().splitlines()))
            log.write_text("")
            env["FAIL_DRY"] = "0"
            passed = subprocess.run(["sh", "-c", script], input=options["stdin"], capture_output=True, env=env)
            self.assertEqual(0, passed.returncode, passed.stderr.decode())
            self.assertEqual(b"homestead-operator-applied\n", passed.stdout)
            self.assertEqual(2, len(log.read_text().splitlines()))

    def test_helm_upgrade_carries_all_custom_settings_and_labels(self):
        result = json.loads(O.configuration("kubevirt", self.read))
        self.assertEqual(self.cr["spec"], result["spec"])
        self.assertNotIn("status", result)
        self.assertNotIn("uid", result["metadata"])

    def test_cdi_upgrade_keeps_custom_storage_and_scheduling_configuration(self):
        cdi = {"apiVersion": "cdi.kubevirt.io/v1beta1", "kind": "CDI", "metadata": {"name": "cdi"},
               "spec": {"config": {"scratchSpaceStorageClass": "fast", "featureGates": ["HonorWaitForFirstConsumer"]},
                        "workload": {"nodeSelector": {"storage.example.com/fast": "true"}}}}
        self.objects[O.PARTS["cdi"][1]] = {"items": [cdi]}
        self.assertEqual(cdi["spec"], json.loads(O.configuration("cdi", self.read))["spec"])

    def test_manifest_cannot_escape_the_expected_operator_scope(self):
        for row in ({"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": "kubevirt-helper"}},
                    {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "other"}},
                    {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRole",
                     "metadata": {"name": "cluster-admin"}}):
            self.desired.append(row)
            with self.assertRaisesRegex(ValueError, "unexpected resource"):
                self.enable()
            self.desired.pop()
        self.assertFalse(self.sent)

    def test_kubevirt_release_crd_and_priority_class_are_supported(self):
        self.desired.extend([
            {"apiVersion": "apiextensions.k8s.io/v1", "kind": "CustomResourceDefinition",
             "metadata": {"name": "kubevirts.kubevirt.io"}},
            {"apiVersion": "scheduling.k8s.io/v1", "kind": "PriorityClass", "metadata": {"name": "kubevirt-cluster-critical"}},
        ])
        self.enable()
        self.assertEqual(1, len(self.sent))

    def test_manifest_updates_wait_for_operator_readiness_instead_of_an_old_helm_job(self):
        item = {"ref": {"component": "kubevirt", "name": "KubeVirt", "to": "v1.9.1", "how": "manifests", "started": time.time()}}
        with patch.object(C, "kubevirt_version", return_value=("v1.9.1", "Deploying")):
            self.assertEqual("running", C.status(item)[0])
        with patch.object(C, "kubevirt_version", return_value=("v1.9.1", "Deployed")):
            self.assertEqual("succeeded", C.status(item)[0])

    def test_a_matching_helmchart_name_is_not_enough_to_claim_ownership(self):
        chart = {"metadata": {"name": "homestead-kubevirt"}, "spec": {"chart": "another-chart"}}
        self.assertTrue(C._operator_chart_owner(chart))
        chart["metadata"]["labels"] = {"homestead.io/managed": "true"}
        self.assertEqual("", C._operator_chart_owner(chart))
        chart["metadata"]["labels"]["kustomize.toolkit.fluxcd.io/name"] = "platform"
        self.assertEqual("Flux", C._operator_chart_owner(chart))

    def test_manage_route_requires_confirmation_and_rejects_unknown_components(self):
        request = type("Request", (), {"body": {"component": "kubevirt"}})()
        with self.assertRaisesRegex(ValueError, "Review and confirm"):
            C._manage_operator_route(request)
        request.body = {"component": "longhorn", "confirm": True}
        with self.assertRaisesRegex(ValueError, "Review and confirm"):
            C._manage_operator_route(request)


@unittest.skipUnless(os.name == "posix", "executes the installer's POSIX shell")
class InstallerOwnershipTests(unittest.TestCase):
    def run_installer(self, existing="", failed=False):
        source = (Path(__file__).resolve().parents[1] / "scripts/bootstrap-k3s.sh").read_text()
        function = source[source.index("install_kubevirt() {"):source.index("\nwait_crd() {")]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            command = root / "kubectl"
            command.write_text('#!/bin/sh\n[ "$READ_FAIL" != 1 ] || exit 1\n'
                               'case "$EXISTING:$*" in *kubevirt*:*kubevirts.kubevirt.io*) echo existing;; '
                               '*cdi*:*cdis.cdi.kubevirt.io*) echo existing;; esac\n')
            command.chmod(0o755)
            script = ('set -eu\nsay() { :; }; fail() { echo "$*" >&2; exit 1; }; modprobe() { :; }; wait_crd() { :; }\n'
                      'apply() { echo "${1##*/}" >> "$MANIFESTS/applied"; }\n'
                      'curl() { while [ "$#" -gt 0 ]; do if [ "$1" = -o ]; then printf "release" > "$2"; return; fi; shift; done; exit 1; }\n'
                      + function + '\ninstall_kubevirt\n')
            result = subprocess.run(["sh", "-c", script], capture_output=True, text=True,
                                    env={**os.environ, "MANIFESTS": folder, "KUBECTL": str(command),
                                         "KUBEVIRT_VERSION": "v1.9.0", "CDI_VERSION": "v1.66.1",
                                         "EXISTING": existing, "READ_FAIL": "1" if failed else "0"})
            return result, {path.name: path.read_text() for path in root.iterdir() if path.name != "kubectl"}

    def test_new_installs_receive_durable_manifest_ownership_markers(self):
        result, files = self.run_installer()
        self.assertEqual(0, result.returncode, result.stderr)
        for name in ("kubevirt-cr.yaml", "cdi-cr.yaml"):
            self.assertIn("homestead.io/operator-install: manifests", files[name])
        self.assertEqual(4, len(files["applied"].splitlines()))

    def test_rerun_does_not_take_over_or_reconfigure_existing_installations(self):
        result, files = self.run_installer("kubevirt cdi")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual({}, files)

    def test_one_preexisting_operator_is_preserved_while_the_other_is_installed(self):
        result, files = self.run_installer("kubevirt")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertNotIn("kubevirt-cr.yaml", files)
        self.assertIn("homestead.io/operator-install: manifests", files["cdi-cr.yaml"])

    def test_failed_inspection_is_never_treated_as_an_absent_installation(self):
        result, files = self.run_installer(failed=True)
        self.assertNotEqual(0, result.returncode)
        self.assertEqual({}, files)


if __name__ == "__main__":
    unittest.main()
