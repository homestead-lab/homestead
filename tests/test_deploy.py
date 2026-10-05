import copy
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
import server


class AppStoreTemplateTests(unittest.TestCase):
    def test_category_shapes_are_normalized(self):
        self.assertEqual(["MediaServer", "Tools"], server.category_values("MediaServer Tools"))
        self.assertEqual(["Network", "Security"], server.category_values(["Network", "Security"]))
        self.assertEqual(["HomeAutomation"], server.category_values([{"name": "HomeAutomation"}]))
        self.assertEqual(["Backup/Sync"], server.category_values({"Category": "Backup/Sync"}))

    def test_search_prioritizes_name_matches_over_description_matches(self):
        apps = [
            {"name": "YA-WAMF", "desc": "Consumes Frigate events"},
            {"name": "Super Frigate Tools", "desc": "Utilities"},
            {"name": "Frigate Plate Recognizer", "desc": "Plate recognition"},
            {"name": "Frigate", "desc": "Network video recorder"},
            {"name": "MyFrigateBridge", "desc": "Bridge"},
            {"name": "Amcrest PTZ", "desc": "Controls cameras for Frigate"},
        ]
        result = server.search_appstore(apps, "frigate")
        self.assertEqual([
            "Frigate",
            "Frigate Plate Recognizer",
            "Super Frigate Tools",
            "MyFrigateBridge",
            "YA-WAMF",
            "Amcrest PTZ",
        ], [app["name"] for app in result])

    def test_search_is_stable_within_the_same_relevance_tier(self):
        apps = [
            {"name": "First", "desc": "Frigate integration"},
            {"name": "Second", "desc": "Frigate helper"},
        ]
        self.assertEqual(apps, server.search_appstore(apps, "frigate"))

    def test_catalogue_popular_ranking_uses_feed_performance_and_deduplicates_templates(self):
        apps = [
            {"name": "Small", "repo": "demo/small:latest", "downloads": 10, "top_performing": 9.2},
            {"name": "Large", "repo": "demo/large:latest", "downloads": 500, "top_performing": 2.1},
            {"name": "Large", "repo": "demo/large:latest", "downloads": 500, "top_performing": 2.1},
        ]
        ranked = server.rank_appstore(apps, "popular")
        self.assertEqual(["Small", "Large"], [app["name"] for app in ranked])

    def test_catalogue_recent_and_trending_use_native_feed_metrics(self):
        apps = [
            {"name": "Older fast", "repo": "demo/fast", "first_seen": 10,
             "top_performing": 8.4, "top_trending": 3.1, "trending": 8.4, "downloads": 200},
            {"name": "Newest", "repo": "demo/new", "first_seen": 30,
             "top_performing": 0, "top_trending": 9.2, "trending": 10.1, "downloads": 20},
            {"name": "Middle", "repo": "demo/middle", "first_seen": 20,
             "top_performing": 4.0, "top_trending": 4.0, "trending": 4.0, "downloads": 100},
        ]
        self.assertEqual(["Newest", "Middle", "Older fast"],
                         [app["name"] for app in server.rank_appstore(apps, "recent")])
        self.assertEqual(["Newest", "Middle", "Older fast"],
                         [app["name"] for app in server.rank_appstore(apps, "trending")])
        self.assertEqual("Older fast", server.appstore_spotlight(apps)["name"])

    def test_catalogue_metric_coercion_tolerates_missing_and_malformed_values(self):
        self.assertEqual(0, server.appstore_number(None))
        self.assertEqual(0, server.appstore_number("not-a-number"))
        self.assertEqual(12.5, server.appstore_number("12.5"))

    def test_catalogue_markup_is_rendered_as_plain_readable_text(self):
        self.assertEqual(
            "Container Variable: PLEX_CLAIM_TOKEN · Example: claim-abc",
            server.catalog_text(
                "Container Variable: PLEX_CLAIM_TOKEN&lt;br&gt;<b>Example:</b>&nbsp;claim-abc"),
        )

    def test_template_variable_metadata_does_not_expose_html(self):
        app = {
            "name": "Plex",
            "repo": "plexinc/pms-docker:latest",
            "config": [{"@attributes": {
                "Name": "Claim token&lt;br&gt;optional",
                "Target": "PLEX_CLAIM_TOKEN",
                "Type": "Variable",
                "Description": "Container Variable: PLEX_CLAIM_TOKEN<br/>Example: claim-abc",
            }, "value": ""}],
        }
        meta = server.template_to_cfg(app)["env_meta"][0]
        self.assertEqual("Claim token · optional", meta["label"])
        self.assertEqual("Container Variable: PLEX_CLAIM_TOKEN · Example: claim-abc", meta["description"])

    def test_openspeedtest_repository_named_latest_is_not_rewritten(self):
        """`latest` is this publisher's repository name as well as its default tag."""
        cfg = server.template_to_cfg({
            "name": "SpeedTest-By-OpenSpeedTest",
            "repo": "openspeedtest/latest",
            "config": [],
        })
        self.assertEqual("openspeedtest/latest", cfg["image"])
        with mock.patch.object(server.HW, "features", return_value=[]):
            deployment, _ = server.build_deployment(cfg)
        self.assertEqual(
            "openspeedtest/latest:latest",
            deployment["spec"]["template"]["spec"]["containers"][0]["image"],
        )

    def test_template_preserves_ports_environment_storage_and_devices(self):
        app = {
            "name": "Demo App", "repo": "example/demo:latest", "icon": "https://example.com/icon.png",
            "config": [
                {"@attributes": {"Name": "Web", "Target": "8080", "Type": "Port",
                                  "Mode": "udp", "Required": "true"}, "value": "18080"},
                {"@attributes": {"Name": "Timezone", "Target": "TZ", "Type": "Variable",
                                  "Default": "UTC", "Description": "Local timezone"}, "value": ""},
                {"@attributes": {"Name": "App data", "Target": "/config", "Type": "Path",
                                  "Mode": "rw", "Default": "/mnt/user/appdata/demo",
                                  "Required": "true"}, "value": "/mnt/user/appdata/demo"},
                {"@attributes": {"Name": "USB", "Target": "/dev/bus/usb", "Type": "Device"},
                 "value": "/dev/bus/usb"},
            ],
        }
        cfg = server.template_to_cfg(app)
        self.assertEqual([{"container": 8080, "host": 18080, "expose": True, "name": "p8080-udp",
                           "protocol": "UDP", "label": "Web", "description": "", "required": True}],
                         cfg["ports"])
        self.assertEqual({"TZ": "UTC"}, cfg["env"])
        self.assertEqual("demo-app-appdata", cfg["volumes"][0]["source"])
        # One path takes the whole volume.
        self.assertNotIn("sub_path", cfg["volumes"][0])
        self.assertTrue(cfg["volumes"][0]["create"])
        self.assertEqual("/mnt/user/appdata/demo", cfg["volumes"][0]["template_source"])
        self.assertEqual("/dev/bus/usb", cfg["template_devices"][0]["host_path"])

    def test_an_apps_paths_share_one_volume_in_folders_of_their_own(self):
        path = lambda target, name="": {"@attributes": {"Name": name or target, "Target": target, "Type": "Path"},
                                       "value": f"/mnt/user/appdata/demo{target}"}
        cfg = server.template_to_cfg({"name": "Demo", "repo": "demo", "config": [
            path("/config"), {"@attributes": {"Target": "/data", "Type": "Path"}, "value": "/mnt/user/demo"},
            path("/app/config"), path("/transcode"),
            {"@attributes": {"Name": "Movies", "Target": "/movies", "Type": "Path"}, "value": "/mnt/user/movies"},
        ]})
        vols = {v["path"]: v for v in cfg["volumes"]}
        shared = [vols["/config"], vols["/data"], vols["/app/config"]]
        self.assertEqual({"demo-appdata"}, {v["source"] for v in shared})
        self.assertEqual(["config", "data", "config-2"], [v["sub_path"] for v in shared])
        # Sized for everything in it: 5 + 50 + 5.
        self.assertEqual({60}, {v["size_gb"] for v in shared})
        self.assertEqual("emptyDir", vols["/transcode"]["type"])
        self.assertEqual("", vols["/movies"]["source"])
        self.assertNotIn("sub_path", vols["/movies"])

    def test_a_deploy_creates_each_new_volume_once(self):
        claims = server.new_claims([
            {"type": "pvc", "create": True, "source": "demo-appdata", "size_gb": 5},
            {"type": "pvc", "create": True, "source": "demo-appdata", "size_gb": 60, "sub_path": "data"},
            {"type": "pvc", "create": False, "source": "media"},
            {"type": "emptyDir", "source": ""},
        ])
        self.assertEqual([("demo-appdata", 60)], [(c["name"], c["size_gb"]) for c in claims])

    def test_two_paths_share_one_planned_claim_and_keep_their_folders(self):
        volumes = [
            {"type": "pvc", "kind": "new-rwx", "create": True, "source": "demo-appdata",
             "path": "/config", "sub_path": "config", "size_gb": 55,
             "storage_class": "longhorn-r3", "access_mode": "ReadWriteMany"},
            {"type": "pvc", "kind": "new-rwx", "create": True, "reuse_new": True,
             "source": "demo-appdata", "path": "/data", "sub_path": "data", "size_gb": 55,
             "storage_class": "longhorn-r3", "access_mode": "ReadWriteMany", "read_only": True},
        ]
        self.assertEqual([{"name": "demo-appdata", "size_gb": 55,
                           "storage_class": "longhorn-r3", "access_mode": "ReadWriteMany"}],
                         server.new_claims(volumes))
        with mock.patch.object(server.HW, "features", return_value=[]):
            deployment, _ = server.build_deployment({"name": "demo", "image": "demo:1",
                                                    "namespace": "lab", "volumes": volumes})
        pod = deployment["spec"]["template"]["spec"]
        self.assertEqual([{"name": "vol0", "persistentVolumeClaim": {"claimName": "demo-appdata"}}],
                         pod["volumes"])
        self.assertEqual([{"name": "vol0", "mountPath": "/config", "subPath": "config"},
                          {"name": "vol0", "mountPath": "/data", "subPath": "data", "readOnly": True}],
                         pod["containers"][0]["volumeMounts"])

    def test_system_localtime_is_imported_as_read_only_host_path(self):
        cfg = server.template_to_cfg({"name": "Demo", "repo": "demo", "config": [{
            "@attributes": {"Target": "/etc/localtime", "Type": "Path", "Mode": "ro"},
            "value": "/etc/localtime",
        }]})
        self.assertEqual("host", cfg["volumes"][0]["type"])
        self.assertFalse(cfg["volumes"][0]["create"])
        self.assertTrue(cfg["volumes"][0]["read_only"])

    def test_dns_ports_drive_dedicated_vip_and_safe_password(self):
        cfg = server.template_to_cfg({
            "name": "Any DNS appliance", "repo": "example/dns:latest", "network": "Bridge",
            "config": [
                {"@attributes": {"Target": "53", "Type": "Port", "Mode": "tcp"}, "value": "53"},
                {"@attributes": {"Target": "53", "Type": "Port", "Mode": "udp"}, "value": "53"},
                {"@attributes": {"Target": "67", "Type": "Port", "Mode": "udp"}, "value": "67"},
                {"@attributes": {"Target": "WEBPASSWORD", "Type": "Variable", "Mask": "true"},
                 "value": "catalogue-password"},
                {"@attributes": {"Target": "ServerIP", "Type": "Variable"}, "value": "192.0.2.1"},
            ],
        })
        self.assertEqual(("loadbalancer", "auto"), (cfg["network_mode"], cfg["vip_mode"]))
        self.assertEqual("Dedicated VIP", cfg["app_profile"]["label"])
        self.assertEqual("", cfg["env"]["WEBPASSWORD"])
        self.assertTrue(next(x for x in cfg["env_meta"] if x["key"] == "WEBPASSWORD")["generate"])
        self.assertEqual({"ServerIP": "vip"}, cfg["env_bindings"])
        dhcp = next(x for x in cfg["ports"] if x["container"] == 67)
        self.assertFalse(dhcp["expose"])
        cfg["lb_ip"] = "192.0.2.243"
        self.assertEqual("192.0.2.243", server.apply_deploy_bindings(cfg)["env"]["ServerIP"])

    def test_paths_classify_config_media_cache_and_infer_web_port(self):
        cfg = server.template_to_cfg({
            "name": "Any media server", "repo": "example/media", "network": "host",
            "webui": "http://[IP]:[PORT:32400]/web",
            "config": [
                {"@attributes": {"Name": "Config", "Target": "/config", "Type": "Path"},
                 "value": "/mnt/user/appdata/plex"},
                {"@attributes": {"Name": "Movies", "Target": "/movies", "Type": "Path"},
                 "value": "/mnt/user/media/movies"},
                {"@attributes": {"Name": "Transcode", "Target": "/transcode", "Type": "Path"},
                 "value": "/tmp/plex"},
            ],
        })
        volumes = {item["role"]: item for item in cfg["volumes"]}
        self.assertEqual(5, volumes["config"]["size_gb"])
        self.assertTrue(volumes["config"]["create"])
        self.assertEqual("", volumes["media"]["source"])
        self.assertFalse(volumes["media"]["create"])
        self.assertEqual("emptyDir", volumes["cache"]["type"])
        self.assertEqual(32400, cfg["ports"][0]["container"])
        self.assertFalse(cfg["ports"][0]["expose"])

    def test_options_and_dependency_shaped_variables_are_structured(self):
        options = server.template_to_cfg({
            "name": "VPN", "repo": "example/vpn", "config": [{
                "@attributes": {"Target": "VPN_ENABLED", "Type": "Variable"},
                "value": "false|true",
            }],
        })
        self.assertEqual("false", options["env"]["VPN_ENABLED"])
        self.assertEqual(["false", "true"], options["env_meta"][0]["options"])

        stateful = server.template_to_cfg({
            "name": "Stateful app", "repo": "example/stateful", "config": [
                {"@attributes": {"Target": "DB_HOST", "Type": "Variable", "Required": "true"},
                 "value": "database"},
                {"@attributes": {"Name": "Shared user data", "Description": "RWX shared data",
                                  "Target": "/data", "Type": "Path"}, "value": "/mnt/user/stateful"},
            ],
        })
        self.assertEqual("dependency", stateful["app_profile"]["level"])
        self.assertEqual("database", stateful["app_profile"]["dependencies"][0]["kind"])
        self.assertEqual("ReadWriteMany", stateful["volumes"][0]["access_mode"])
        self.assertEqual(50, stateful["volumes"][0]["size_gb"])

    def test_generated_secrets_and_preview_redaction(self):
        cfg = {"env": {"WEBPASSWORD": ""}, "env_meta": [
            {"key": "WEBPASSWORD", "masked": True, "generate": True},
        ]}
        generated = server.apply_generated_secrets(cfg)
        self.assertGreaterEqual(len(generated["env"]["WEBPASSWORD"]), 20)
        self.assertEqual("", cfg["env"]["WEBPASSWORD"])
        dep = {"spec": {"template": {"spec": {"containers": [{"env": [
            {"name": "WEBPASSWORD", "value": generated["env"]["WEBPASSWORD"]},
            {"name": "MODE", "value": "normal"},
        ]}]}}}}
        safe = server.redact_deployment_preview(dep, cfg)
        self.assertEqual("••••••", safe["spec"]["template"]["spec"]["containers"][0]["env"][0]["value"])
        self.assertEqual("normal", safe["spec"]["template"]["spec"]["containers"][0]["env"][1]["value"])
        self.assertNotEqual("••••••", dep["spec"]["template"]["spec"]["containers"][0]["env"][0]["value"])

    def test_profiles_do_not_depend_on_app_name_or_image(self):
        common = [{"@attributes": {"Name": "Movies", "Target": "/library", "Type": "Path"},
                   "value": "/mnt/user/media/movies"}]
        first = server.template_to_cfg({"name": "Alpha", "repo": "vendor/one", "config": common})
        second = server.template_to_cfg({"name": "Beta", "repo": "another/two", "config": common})
        self.assertEqual(first["app_profile"], second["app_profile"])
        self.assertEqual("media", first["volumes"][0]["role"])

    def test_runtime_socket_is_blocked_for_any_image(self):
        cfg = server.template_to_cfg({
            "name": "Generic controller", "repo": "example/controller:latest", "config": [{
                "@attributes": {"Target": "/var/run/docker.sock", "Type": "Path"},
                "value": "/var/run/docker.sock",
            }],
        })
        self.assertEqual("safety", cfg["app_profile"]["intent"])
        self.assertTrue(cfg["app_profile"]["blocked"])
        self.assertEqual("host", cfg["volumes"][0]["type"])
        with self.assertRaisesRegex(ValueError, "Needs Kubernetes design"):
            server.ensure_profile_compatible(cfg)


class SidecarDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.features = mock.patch.object(server.HW, "features", return_value=[])
        self.features.start()
        self.addCleanup(self.features.stop)
        self.current = {
            "apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": "media", "namespace": "lab", "resourceVersion": "7"},
            "spec": {
                "selector": {"matchLabels": {"app": "media"}},
                "template": {"metadata": {"labels": {"app": "media"}}, "spec": {
                    "containers": [{"name": "main", "image": "example/main"}],
                    "volumes": [{"name": "shared", "persistentVolumeClaim": {"claimName": "media-data"}}],
                }},
            },
        }

    def test_sidecar_reuses_pod_volume_and_routes_service_to_owner(self):
        cfg = {
            "name": "helper", "image": "example/helper", "namespace": "lab",
            "target_workload": "media", "target_mode": "existing", "network_mode": "internal",
            "ports": [{"container": 9000, "host": 19000, "protocol": "TCP", "expose": True}],
            "volumes": [{"path": "/data", "source": "shared", "type": "pod", "read_only": True}],
            "env": {"MODE": "sidecar"}, "hardware": [],
        }
        updated, service = server.build_sidecar_deployment(cfg, copy.deepcopy(self.current))
        self.assertEqual("7", updated["metadata"]["resourceVersion"])
        helper = updated["spec"]["template"]["spec"]["containers"][1]
        self.assertEqual("helper", helper["name"])
        self.assertEqual([{"name": "shared", "mountPath": "/data", "readOnly": True}], helper["volumeMounts"])
        self.assertEqual({"app": "media"}, service["spec"]["selector"])
        self.assertEqual(19000, service["spec"]["ports"][0]["port"])

    def test_sidecar_uses_explicit_container_name(self):
        cfg = {"name": "legacy", "container_name": "metrics-helper", "image": "example/helper",
               "target_workload": "media", "ports": [], "volumes": [], "hardware": []}
        updated, _ = server.build_sidecar_deployment(cfg, copy.deepcopy(self.current))
        self.assertEqual("metrics-helper", updated["spec"]["template"]["spec"]["containers"][1]["name"])

    def test_sidecar_new_claim_gets_a_collision_safe_pod_volume(self):
        cfg = {
            "name": "helper", "image": "example/helper", "target_workload": "media",
            "volumes": [{"path": "/cache", "source": "helper-cache", "type": "pvc"}],
            "ports": [], "hardware": [],
        }
        updated, _ = server.build_sidecar_deployment(cfg, copy.deepcopy(self.current))
        volumes = updated["spec"]["template"]["spec"]["volumes"]
        self.assertEqual("helper-cache", volumes[-1]["persistentVolumeClaim"]["claimName"])
        self.assertEqual(volumes[-1]["name"], updated["spec"]["template"]["spec"]["containers"][1]["volumeMounts"][0]["name"])

    def test_sidecar_can_add_ephemeral_cache(self):
        cfg = {
            "name": "helper", "image": "example/helper", "target_workload": "media",
            "volumes": [{"path": "/cache", "source": "", "type": "emptyDir"}],
            "ports": [], "hardware": [],
        }
        updated, _ = server.build_sidecar_deployment(cfg, copy.deepcopy(self.current))
        volume = updated["spec"]["template"]["spec"]["volumes"][-1]
        self.assertEqual({}, volume["emptyDir"])
        self.assertEqual(volume["name"], updated["spec"]["template"]["spec"]["containers"][1]["volumeMounts"][0]["name"])

    def test_duplicate_container_and_missing_pod_volume_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "already exists"):
            server.build_sidecar_deployment({"name": "main", "image": "x", "target_workload": "media"},
                                               copy.deepcopy(self.current))
        with self.assertRaisesRegex(ValueError, "does not exist"):
            server.build_sidecar_deployment({"name": "helper", "image": "x", "target_workload": "media",
                                               "volumes": [{"path": "/x", "source": "missing", "type": "pod"}]},
                                               copy.deepcopy(self.current))

    def test_host_network_cannot_be_forced_on_shared_pod(self):
        with self.assertRaisesRegex(ValueError, "host networking"):
            server.build_sidecar_deployment({"name": "helper", "image": "x", "target_workload": "media",
                                               "network_mode": "host"}, copy.deepcopy(self.current))


class DeployOptionsTests(unittest.TestCase):
    def test_options_describe_claim_access_and_existing_pod_volumes(self):
        def get(path):
            if path.endswith("/deployments"):
                return {"items": [self._deployment()]}
            if path.endswith("/persistentvolumeclaims"):
                return {"items": [{"metadata": {"name": "shared"}, "spec": {
                    "accessModes": ["ReadWriteMany"], "storageClassName": "longhorn-r2",
                    "resources": {"requests": {"storage": "5Gi"}}},
                    "status": {"phase": "Bound", "capacity": {"storage": "5Gi"}}}]}
            if path.endswith("/storageclasses"):
                return {"items": [{"metadata": {"name": "longhorn-r2"}}]}
            raise AssertionError(path)

        with mock.patch.object(server, "kget", side_effect=get):
            options = server.deploy_options("lab")
        self.assertEqual(["ReadWriteMany"], options["pvcs"][0]["access_modes"])
        self.assertEqual("pvc", options["deployments"][0]["volumes"][0]["kind"])

    def _deployment(self):
        return {"metadata": {"name": "media"}, "spec": {"template": {"spec": {
            "containers": [{"name": "main"}],
            "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "shared"}}],
        }}}}


class NewWorkloadNamingTests(unittest.TestCase):
    def test_workload_and_container_names_can_differ(self):
        cfg = {"name": "legacy", "workload_name": "camera-stack", "container_name": "frigate",
               "image": "example/frigate", "namespace": "lab", "ports": [{"container": 5000,
               "host": 5000, "protocol": "TCP", "expose": True}], "volumes": [], "hardware": []}
        with mock.patch.object(server.HW, "features", return_value=[]):
            deployment, service = server.build_deployment(cfg)
        self.assertEqual("camera-stack", deployment["metadata"]["name"])
        self.assertEqual("frigate", deployment["spec"]["template"]["spec"]["containers"][0]["name"])
        self.assertEqual("camera-stack", service["metadata"]["name"])
        self.assertEqual({"app": "camera-stack"}, service["spec"]["selector"])

    def test_command_user_and_capabilities_reach_the_container(self):
        """What a Compose service runs, and as whom, is not lost on the way."""
        cfg = {"name": "app", "image": "app:1", "namespace": "lab", "ports": [], "volumes": [],
               "hardware": [], "command": ["/bin/sh", "-c"], "args": ["run"], "working_dir": "/app",
               "run_as_user": 1000, "run_as_group": 1000, "cap_add": ["NET_ADMIN"]}
        with mock.patch.object(server.HW, "features", return_value=[]):
            deployment, _ = server.build_deployment(cfg)
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual((["/bin/sh", "-c"], ["run"], "/app"),
                         (container["command"], container["args"], container["workingDir"]))
        self.assertEqual({"runAsUser": 1000, "runAsGroup": 1000, "capabilities": {"add": ["NET_ADMIN"]}},
                         container["securityContext"])

    def test_a_plain_workload_gets_no_empty_security_context(self):
        cfg = {"name": "app", "image": "app:1", "namespace": "lab", "ports": [], "volumes": [], "hardware": []}
        with mock.patch.object(server.HW, "features", return_value=[]):
            deployment, _ = server.build_deployment(cfg)
        self.assertNotIn("securityContext", deployment["spec"]["template"]["spec"]["containers"][0])

    def test_a_command_must_be_words(self):
        cfg = {"name": "app", "image": "app:1", "namespace": "lab", "ports": [], "volumes": [],
               "hardware": [], "command": "rm -rf /"}
        with mock.patch.object(server.HW, "features", return_value=[]):
            with self.assertRaisesRegex(ValueError, "list of words"):
                server.build_deployment(cfg)


class HomesteadManifestTests(unittest.TestCase):
    def test_removing_a_dead_host_has_the_permissions_it_needs(self):
        manifest = (ROOT / "deploy" / "deploy.yaml").read_text()
        for rule in ("resources: [nodes]\n    verbs: [get, list, watch, patch, update, delete]",
                     "resources: [nodes]\n    verbs: [get, list, watch, patch, delete]",
                     "resources: [replicas]\n    verbs: [get, list, watch, delete]",
                     "resources: [kubevirts]\n    verbs: [get, list, watch, patch]",
                     'apiGroups: ["cluster.x-k8s.io"]\n    resources: [machines]\n    verbs: [get, list, patch, delete]',
                     "resources: [volumeattachments]\n    verbs: [get, list, delete]"):
            with self.subTest(rule=rule.split("\n")[-1]):
                self.assertIn(rule, manifest)

    def test_runtime_workload_uses_homestead_names_and_image(self):
        manifest = (ROOT / "deploy" / "deploy.yaml").read_text()
        self.assertIn("kind: Deployment\nmetadata:\n  name: homestead", manifest)
        self.assertIn("- name: homestead\n          image: ghcr.io/homestead-lab/homestead:2.8.313-dev.5",
                      manifest)
        self.assertIn("homestead.io/update-sources: '{\"homestead\":", manifest)

    def test_the_version_is_not_pinned_beside_the_image_it_describes(self):
        """The image bakes HOMESTEAD_VERSION in; a copy here goes stale the
        moment the updater replaces that image."""
        manifest = (ROOT / "deploy" / "deploy.yaml").read_text()
        self.assertNotIn("name: HOMESTEAD_VERSION", manifest)
        self.assertNotIn("name: HARVUI_VERSION", manifest)

    def test_the_data_claim_is_the_one_the_deployment_mounts(self):
        manifest = (ROOT / "deploy" / "deploy.yaml").read_text()
        self.assertIn("claimName: homestead-data", manifest)
        self.assertIn("kind: PersistentVolumeClaim\nmetadata:\n  name: homestead-data",
                      manifest)


if __name__ == "__main__":
    unittest.main()
