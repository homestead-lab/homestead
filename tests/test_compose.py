"""A Compose file, read into the deployments Homestead would create."""
import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_compose as compose

FEATURES = [
    {"id": "igpu", "name": "Intel/AMD iGPU", "host_path": "/dev/dri", "container_path": "/dev/dri"},
    {"id": "coral_usb", "name": "Google Coral USB", "host_path": "/dev/bus/usb", "container_path": "/dev/bus/usb"},
]

PAPERLESS = """
name: paperless
services:
  broker:
    image: docker.io/library/redis:7
    restart: unless-stopped
    volumes:
      - redisdata:/data
  db:
    image: docker.io/library/postgres:16
    environment:
      POSTGRES_DB: paperless
      POSTGRES_USER: paperless
      POSTGRES_PASSWORD: ${DB_PASSWORD:-paperless}
    volumes:
      - pgdata:/var/lib/postgresql/data
  webserver:
    image: ghcr.io/paperless-ngx/paperless-ngx:latest
    depends_on:
      - db
      - broker
    ports:
      - "8000:8000"
    volumes:
      - data:/usr/src/paperless/data
      - ./consume:/usr/src/paperless/consume
    environment:
      PAPERLESS_REDIS: redis://broker:6379
      PAPERLESS_DBHOST: db
      USERMAP_UID: 1000
volumes:
  data:
  pgdata:
  redisdata:
"""


def convert(text, **kwargs):
    kwargs.setdefault("features", FEATURES)
    return compose.convert(text, **kwargs)


def service(report, name):
    return next(row for row in report["services"] if row["name"] == name)


class ComposeTests(unittest.TestCase):
    def test_a_file_indented_with_tabs_is_read_and_says_so(self):
        report = convert("services:\n\tweb:\n\t\timage: nginx\n\t\tports:\n\t\t\t- \"8080:80\"\n")

        self.assertEqual([], report["errors"])
        self.assertEqual(["web"], [row["name"] for row in report["services"]])
        self.assertEqual({"width": 2, "lines": 4}, report["untab"])
        self.assertEqual(2, report["notes"][0]["line"])
        self.assertIn("tabs", report["notes"][0]["message"])

    def test_a_real_stack_reads_cleanly(self):
        report = convert(PAPERLESS)

        self.assertTrue(report["ok"], report["errors"] + [e for s in report["services"] for e in s["errors"]])
        self.assertEqual(["broker", "db", "webserver"], sorted(s["name"] for s in report["services"]))
        self.assertLess(report["order"].index("db"), report["order"].index("webserver"))
        self.assertLess(report["order"].index("broker"), report["order"].index("webserver"))

    def test_the_web_service_keeps_its_port_env_and_volumes(self):
        web = service(convert(PAPERLESS), "webserver")["config"]

        self.assertEqual([{"container": 8000, "host": 8000, "protocol": "TCP", "expose": True}], web["ports"])
        self.assertEqual("loadbalancer", web["network_mode"])
        self.assertEqual("1000", web["env"]["USERMAP_UID"], "numbers become strings for the container")
        data = next(v for v in web["volumes"] if v["path"] == "/usr/src/paperless/data")
        self.assertEqual(("data", "new-rwo", True), (data["source"], data["kind"], data["create"]))

    def test_a_host_folder_becomes_a_new_volume_and_says_its_data_is_not_copied(self):
        row = service(convert(PAPERLESS), "webserver")

        consume = next(v for v in row["config"]["volumes"] if v["path"] == "/usr/src/paperless/consume")
        self.assertEqual("webserver-consume", consume["source"])
        self.assertEqual("./consume", consume["template_source"])
        self.assertTrue(any("not copied" in n["message"] for n in row["notes"]))

    def test_services_reached_by_name_get_an_address_inside_the_cluster(self):
        """Compose lets redis be found by name; Kubernetes needs a Service for that."""
        report = convert(PAPERLESS)

        broker, db = service(report, "broker")["config"], service(report, "db")["config"]
        self.assertEqual(("internal", 6379), (broker["network_mode"], broker["ports"][0]["container"]))
        self.assertEqual(("internal", 5432), (db["network_mode"], db["ports"][0]["container"]))

    def test_a_default_fills_an_unset_variable_and_a_given_one_wins(self):
        self.assertEqual("paperless", service(convert(PAPERLESS), "db")["config"]["env"]["POSTGRES_PASSWORD"])

        given = convert(PAPERLESS, variables_text="DB_PASSWORD='s3cret'\n")
        self.assertEqual("s3cret", service(given, "db")["config"]["env"]["POSTGRES_PASSWORD"])

    def test_a_missing_variable_is_named_with_its_line(self):
        report = convert("services:\n  app:\n    image: app:${TAG}\n")

        self.assertEqual(["TAG"], report["variables"]["missing"])
        self.assertEqual(3, report["warnings"][0]["line"])

    def test_an_unset_variable_is_an_empty_value_not_a_shell_lookup(self):
        report = convert("""services:
  app:
    image: app
    environment:
      PASSWORD: ${PW}
""")

        row = service(report, "app")
        self.assertEqual({"PASSWORD": ""}, row["config"]["env"])
        self.assertEqual([], row["warnings"], "the missing variable is said once, at file level")

    def test_a_bare_key_still_comes_from_the_variables(self):
        report = convert("""services:
  app:
    image: app
    environment:
      - TOKEN
""", variables_text="TOKEN=abc")

        self.assertEqual({"TOKEN": "abc"}, service(report, "app")["config"]["env"])

    def test_a_required_variable_is_an_error(self):
        report = convert("services:\n  app:\n    image: app\n    environment:\n      KEY: ${KEY:?set a key}\n")

        self.assertFalse(report["ok"])
        self.assertEqual(5, report["errors"][0]["line"])
        self.assertIn("set a key", report["errors"][0]["message"])

    def test_a_yaml_mistake_points_at_its_line(self):
        report = convert("services:\n  app:\n    image: app\n     ports: []\n")

        self.assertFalse(report["ok"])
        self.assertEqual(4, report["errors"][0]["line"])

    def test_a_file_without_services_is_refused(self):
        self.assertIn("services", convert("version: '3'\n")["errors"][0]["message"])

    def test_names_are_made_kubernetes_safe_and_the_rename_is_said(self):
        row = service(convert("services:\n  My_App:\n    image: x\n"), "my-app")

        self.assertTrue(any("my-app" in w["message"] for w in row["warnings"]))

    def test_an_existing_workload_blocks_its_service(self):
        report = convert("services:\n  web:\n    image: x\n", existing_workloads={"web"})

        self.assertFalse(report["ok"])
        self.assertIn("already exists", service(report, "web")["errors"][0]["message"])

    def test_an_existing_claim_is_used_rather_than_created(self):
        report = convert("services:\n  web:\n    image: x\n    volumes:\n      - data:/data\nvolumes:\n  data:\n",
                         existing_claims={"data"})

        volume = service(report, "web")["config"]["volumes"][0]
        self.assertEqual(("existing", False), (volume["kind"], volume["create"]))

    def test_an_external_volume_must_exist(self):
        text = "services:\n  web:\n    image: x\n    volumes:\n      - media:/media\nvolumes:\n  media:\n    external: true\n"

        self.assertFalse(convert(text)["ok"])
        self.assertTrue(convert(text, existing_claims={"media"})["ok"])

    def test_a_volume_two_services_mount_is_shared_storage(self):
        text = """services:
  a:
    image: a
    volumes: [shared:/data]
  b:
    image: b
    volumes: [shared:/data]
volumes:
  shared:
"""
        report = convert(text)

        for name in ("a", "b"):
            volume = service(report, name)["config"]["volumes"][0]
            self.assertEqual(("new-rwx", "ReadWriteMany"), (volume["kind"], volume["access_mode"]))

    def test_the_same_port_twice_on_the_shared_address_is_an_error(self):
        text = "services:\n  a:\n    image: a\n    ports: ['80:80']\n  b:\n    image: b\n    ports: ['80:8080']\n"

        self.assertFalse(convert(text)["ok"])
        self.assertTrue(convert(text, vip_mode="auto")["ok"], "separate addresses do not collide")

    def test_ports_in_every_form(self):
        text = """services:
  app:
    image: app
    ports:
      - "53:53/udp"
      - "127.0.0.1:9000:9000"
      - "3000"
      - "7000-7002:7000-7002"
      - target: 443
        published: 8443
        protocol: tcp
"""
        ports = service(convert(text), "app")["config"]["ports"]
        pairs = [(p["host"], p["container"], p["protocol"]) for p in ports]

        self.assertIn((53, 53, "UDP"), pairs)
        self.assertIn((9000, 9000, "TCP"), pairs)
        self.assertIn((3000, 3000, "TCP"), pairs)
        self.assertIn((7002, 7002, "TCP"), pairs)
        self.assertIn((8443, 443, "TCP"), pairs)

    def test_command_entrypoint_user_and_capabilities_carry_over(self):
        text = """services:
  app:
    image: app
    entrypoint: /bin/sh -c
    command: ["run", "--port", "80"]
    user: "1000:1000"
    working_dir: /app
    cap_add: [NET_ADMIN, CAP_SYS_TIME]
"""
        cfg = service(convert(text), "app")["config"]

        self.assertEqual(["/bin/sh", "-c"], cfg["command"])
        self.assertEqual(["run", "--port", "80"], cfg["args"])
        self.assertEqual((1000, 1000, "/app"), (cfg["run_as_user"], cfg["run_as_group"], cfg["working_dir"]))
        self.assertEqual(["NET_ADMIN", "SYS_TIME"], cfg["cap_add"])

    def test_resources_keep_reservations_and_enforce_memory_limit(self):
        text = """services:
  app:
    image: app
    deploy:
      replicas: 2
      resources:
        reservations: {cpus: "0.5", memory: 512M}
        limits: {memory: 2G}
"""
        row = service(convert(text), "app")

        self.assertEqual(("500m", "512Mi", 2), (row["config"]["cpu"], row["config"]["memory"], row["config"]["replicas"]))
        self.assertEqual("2048Mi", row["config"]["memory_limit"])
        self.assertTrue(any("enforced maximum" in n["message"] for n in row["notes"]))

    def test_ram_disks_and_shared_memory(self):
        text = "services:\n  app:\n    image: app\n    shm_size: 2gb\n    tmpfs:\n      - /run:size=64m\n"

        volumes = service(convert(text), "app")["config"]["volumes"]
        self.assertIn(("/run", "memory", "64Mi"), [(v["path"], v["kind"], v["size_limit"]) for v in volumes])
        self.assertIn(("/dev/shm", "shm", "2048Mi"), [(v["path"], v["kind"], v["size_limit"]) for v in volumes])

    def test_devices_find_their_hardware_feature(self):
        text = "services:\n  frigate:\n    image: f\n    devices:\n      - /dev/dri/renderD128:/dev/dri/renderD128\n      - /dev/bus/usb:/dev/bus/usb\n"

        self.assertEqual(["igpu", "coral_usb"], service(convert(text), "frigate")["config"]["hardware"])

    def test_a_device_with_no_feature_is_an_error(self):
        report = convert("services:\n  z:\n    image: z\n    devices:\n      - /dev/dri:/dev/dri\n      - /dev/ttyUSB0:/dev/ttyUSB0\n")

        error = service(report, "z")["errors"][0]
        self.assertIn("Hardware features", error["message"])
        self.assertEqual(6, error["line"], "the device's own line, not devices:")

    def test_the_docker_socket_is_refused(self):
        report = convert("services:\n  w:\n    image: w\n    volumes:\n      - /var/run/docker.sock:/var/run/docker.sock\n")

        self.assertFalse(report["ok"])
        self.assertEqual(5, service(report, "w")["errors"][0]["line"])

    def test_localtime_is_left_out_and_tz_suggested(self):
        row = service(convert("services:\n  a:\n    image: a\n    volumes:\n      - /etc/localtime:/etc/localtime:ro\n"), "a")

        self.assertEqual([], row["config"]["volumes"])
        self.assertTrue(any("TZ" in w["message"] for w in row["warnings"]))

    def test_build_without_image_is_an_error(self):
        report = convert("services:\n  a:\n    build: .\n")

        self.assertIn("does not build", service(report, "a")["errors"][0]["message"])

    def test_a_dependency_cycle_is_reported(self):
        report = convert("services:\n  a:\n    image: a\n    depends_on: [b]\n  b:\n    image: b\n    depends_on: [a]\n")

        self.assertTrue(any("circle" in e["message"] for e in report["errors"]))

    def test_anchors_carry_shared_settings(self):
        text = """x-env: &env
  TZ: Europe/London
services:
  a:
    image: a
    environment: *env
"""
        self.assertEqual({"TZ": "Europe/London"}, service(convert(text), "a")["config"]["env"])


class VariableTests(unittest.TestCase):
    def test_env_file_lines(self):
        values = compose.parse_variables("# c\nexport A=1\nB='two words'\nC=x # note\nbad line\n")

        self.assertEqual({"A": "1", "B": "two words", "C": "x"}, values)

    def test_dollar_dollar_is_a_literal_dollar(self):
        text, used, missing, errors = compose.interpolate("cmd: echo $$HOME\n", {})

        self.assertEqual("cmd: echo $HOME\n", text)
        self.assertEqual([], missing)

    def test_a_commented_line_is_not_interpolated(self):
        _, used, missing, _ = compose.interpolate("# image: ${NOPE}\na: 1\n", {})

        self.assertEqual([], missing)


class SizeTests(unittest.TestCase):
    def test_memory(self):
        self.assertEqual([512, 1024, 1536, 256, 2048],
                         [compose.memory_mib(v) for v in ("512m", "1g", "1.5G", 268435456, "2gb")])

    def test_cpus(self):
        self.assertEqual(["500m", "2000m"], [compose.cpu_millis(v) for v in ("0.5", 2)])


if __name__ == "__main__":
    unittest.main()
