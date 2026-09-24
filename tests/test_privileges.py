import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_privileges as PRIV


class FromDockerTests(unittest.TestCase):
    def test_unraid_extra_parameters_for_a_vpn(self):
        # transmission-openvpn's template.
        p = PRIV.from_docker("--cap-add=NET_ADMIN --device=/dev/net/tun --log-opt max-size=10m")
        self.assertEqual({"privileged": False, "cap_add": ["NET_ADMIN"], "tun": True}, p)

    def test_spaced_flags_privileged_and_docker_inspect(self):
        self.assertEqual({"privileged": True, "cap_add": ["SYS_TIME"], "tun": False},
                         PRIV.from_docker("--cap-add SYS_TIME --privileged"))
        self.assertEqual({"privileged": False, "cap_add": ["NET_ADMIN"], "tun": True},
                         PRIV.from_docker("", False, ["CAP_NET_ADMIN"], ["/dev/net/tun"]))

    def test_nothing_asked_is_nothing_given(self):
        self.assertEqual({"privileged": False, "cap_add": [], "tun": False}, PRIV.from_docker(""))


class ApplyTests(unittest.TestCase):
    def test_the_tunnel_mounts_the_device_and_adds_net_admin(self):
        container, pod = {"name": "vpn"}, {"containers": []}
        pod["containers"].append(container)
        PRIV.apply(container, pod, {"tun": True, "cap_add": ["SYS_TIME"]})
        self.assertEqual(["NET_ADMIN", "SYS_TIME"], container["securityContext"]["capabilities"]["add"])
        self.assertNotIn("privileged", container["securityContext"])
        self.assertEqual([{"name": PRIV.TUN_VOLUME, "mountPath": "/dev/net/tun"}], container["volumeMounts"])
        self.assertEqual({"path": "/dev/net/tun", "type": "CharDevice"}, pod["volumes"][0]["hostPath"])
        self.assertEqual({"privileged": False, "tun": True, "cap_add": ["SYS_TIME"]}, PRIV.read(container, pod))
        # Turned off again: the device and its capability go with it.
        PRIV.apply(container, pod, {"tun": False})
        self.assertNotIn("volumes", pod)
        self.assertNotIn("securityContext", container)

    def test_device_passthrough_stays_privileged_whatever_is_asked(self):
        container, pod = {"securityContext": {"privileged": True}}, {}
        PRIV.apply(container, pod, {"privileged": False}, hardware=True)
        self.assertTrue(container["securityContext"]["privileged"])

    def test_nonsense_capabilities_are_refused(self):
        with self.assertRaisesRegex(ValueError, "not a capability"):
            PRIV.apply({}, {}, {"cap_add": ["rm -rf"]})


class DeployTests(unittest.TestCase):
    def test_an_app_store_vpn_template_deploys_with_its_tunnel(self):
        import server
        app = {"name": "transmission-openvpn", "repo": "haugene/transmission-openvpn", "network": "bridge",
               "config": [], "extra_params": "--cap-add=NET_ADMIN --device=/dev/net/tun", "privileged": False}
        with mock.patch.object(server.HW, "features", lambda: []):
            cfg = server.template_to_cfg(app)
            self.assertEqual((True, ["NET_ADMIN"], False), (cfg["tun"], cfg["cap_add"], cfg["privileged"]))
            dep, _ = server.build_deployment(dict(cfg, name="vpn", namespace="lab", ports=[], volumes=[]))
        container = dep["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(["NET_ADMIN"], container["securityContext"]["capabilities"]["add"])
        self.assertIn({"name": PRIV.TUN_VOLUME, "mountPath": "/dev/net/tun"}, container["volumeMounts"])


if __name__ == "__main__":
    unittest.main()
