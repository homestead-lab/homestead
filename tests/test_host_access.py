"""Host access - privileged mode, capabilities, host network and folders - is an admin's to give."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_host_access as host


def deployment(containers=None, volumes=None, **spec):
    return {"spec": {"template": {"spec": {"containers": containers or [{"name": "app"}],
                                           "volumes": volumes or [], **spec}}}}


class HostAccessTests(unittest.TestCase):
    def tearDown(self):
        host.set_role(None)

    def test_a_config_says_what_it_asks_for(self):
        cfg = {"privileged": True, "cap_add": ["net_admin"], "network_mode": "host",
               "volumes": [{"type": "host", "source": "/"}, {"type": "pvc", "source": "data"}]}
        self.assertEqual({"privileged mode", "the NET_ADMIN capability", "the host's network", "the host folder /"},
                         host.cfg_grants(cfg))
        self.assertEqual(set(), host.cfg_grants({"image": "nginx", "hardware": ["igpu"]}))

    def test_an_operator_cannot_deploy_host_access(self):
        host.set_role("operator")
        for cfg in ({"privileged": True}, {"network_mode": "host"}, {"volumes": [{"type": "host", "source": "/etc"}]},
                    {"cap_add": ["SYS_ADMIN"]}, {"tun": True}):
            with self.assertRaises(host.Refused, msg=cfg):
                host.require_cfg([{"image": "x"}, cfg])
        host.require_cfg([{"image": "nginx", "hardware": ["coral"]}])

    def test_an_admin_and_homestead_itself_can(self):
        host.set_role("admin")
        host.require_cfg([{"privileged": True}])
        host.set_role(None)
        host.require_cfg([{"privileged": True}])

    def test_hardware_an_admin_defined_is_not_host_access(self):
        dep = deployment([{"name": "frigate", "securityContext": {"privileged": True}}],
                         [{"name": "coral", "hostPath": {"path": "/dev/apex_0"}}])
        self.assertEqual(set(), host.grants(dep, {"/dev/apex_0"}))
        self.assertEqual({"privileged mode", "the host folder /dev/apex_0"}, host.grants(dep, set()))

    def test_an_operator_cannot_add_host_access_in_an_edit(self):
        host.set_role("operator")
        current = deployment()
        with self.assertRaises(host.Refused):
            host.require_edit(current, deployment(hostNetwork=True))
        with self.assertRaises(host.Refused):
            host.require_edit(current, deployment(volumes=[{"name": "root", "hostPath": {"path": "/"}}]))
        with self.assertRaises(host.Refused):
            host.require_edit(current, deployment([{"name": "app", "securityContext": {"privileged": True}}]))

    def test_what_a_workload_already_had_does_not_stop_an_operators_edit(self):
        host.set_role("operator")
        had = deployment([{"name": "app", "securityContext": {"privileged": True}}], hostNetwork=True)
        edited = deployment([{"name": "app", "image": "new", "securityContext": {"privileged": True}}], hostNetwork=True)
        host.require_edit(had, edited)


if __name__ == "__main__":
    unittest.main()
