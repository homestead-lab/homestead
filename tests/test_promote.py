import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_promote as PROMOTE

# dash where it is installed (CI), as the installer's tests do; sh otherwise.
SH = shutil.which("dash") or shutil.which("sh")


def node(name="app-1", args=None, labels=None, ip="192.0.2.11", roles=(), uid="uid-1", ready=True):
    import json
    meta = {"name": name, "uid": uid, "labels": {**{f"node-role.kubernetes.io/{r}": "true" for r in roles}, **(labels or {})},
            "annotations": {"k3s.io/node-args": json.dumps(args)} if args is not None else {}}
    return {"apiVersion": "v1", "kind": "Node", "metadata": meta, "status": {"addresses": [{"type": "InternalIP", "address": ip}],
                                         "conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
                                         "nodeInfo": {"kubeletVersion": "v1.37.1+k3s1"}}}


class FlagTests(unittest.TestCase):
    def test_node_flags_keeps_node_scoped_flags_in_both_forms(self):
        flags, dropped = PROMOTE.node_flags(node(args=["agent", "--node-ip", "192.0.2.11", "--flannel-iface=eth1",
                                                       "--node-label", "zone=a b", "--kubelet-arg", "max-pods=150"]))
        self.assertEqual(["--node-ip", "192.0.2.11", "--flannel-iface", "eth1", "--node-label", "'zone=a b'",
                          "--kubelet-arg", "max-pods=150"], flags)
        self.assertEqual([], dropped)

    def test_node_flags_adds_internal_ip_when_none_given(self):
        flags, _ = PROMOTE.node_flags(node(args=["agent"]))
        self.assertEqual(["--node-ip", "192.0.2.11"], flags)

    def test_node_flags_names_dropped_flags(self):
        _, dropped = PROMOTE.node_flags(node(args=["agent", "--server", "https://192.0.2.5:6443", "--token", "********",
                                                   "--data-dir", "/srv/k3s", "--with-node-id"]))
        self.assertEqual(["--data-dir", "--with-node-id"], dropped)

    def test_own_labels_leaves_out_what_k3s_sets(self):
        labels = PROMOTE.own_labels(node(labels={"kubernetes.io/hostname": "app-1", "beta.kubernetes.io/arch": "amd64",
                                                 "node.kubernetes.io/instance-type": "k3s", "k3s.io/hostname": "app-1",
                                                 "homelab/zone": "a", "node.longhorn.io/create-default-disk": "true"}, roles=("worker",)))
        self.assertEqual({"homelab/zone": "a", "node.longhorn.io/create-default-disk": "true"}, labels)


class ScriptTests(unittest.TestCase):
    def body(self):
        return PROMOTE.promote_script("v1.37.1+k3s1", "192.0.2.5", ["--cluster-cidr", "10.42.0.0/16", "--node-ip", "192.0.2.11"])

    def test_promote_script_order(self):
        s = self.body()
        self.assertTrue(s.startswith("set -eu\n"))
        download = s.index("https://get.k3s.io")
        uninstall = s.index("k3s-agent-uninstall.sh")
        password = s.index(PROMOTE.NODE_PASSWORD)
        install = s.index("sh " + PROMOTE.INSTALLER + " server")
        self.assertLess(download, uninstall)
        self.assertLess(uninstall, password)
        self.assertLess(password, install)
        self.assertIn("exit 10", s)
        self.assertIn("INSTALL_K3S_VERSION=v1.37.1+k3s1 ", s)
        self.assertIn("--server https://192.0.2.5:6443 --cluster-cidr 10.42.0.0/16 --node-ip 192.0.2.11", s)
        # Quoted where it matters: a value with a space stays one argument.
        odd = PROMOTE.promote_script("v1 x", "192.0.2.5", [])
        self.assertIn("INSTALL_K3S_VERSION='v1 x' ", odd)
        self.assertIn(f'K3S_TOKEN="$(cat {PROMOTE.TOKEN})"', s)

    def test_promote_script_cleans_up_on_exit(self):
        s = self.body()
        trap = s[s.index("trap "):s.index("\n", s.index("trap "))]
        self.assertIn(f"rm -f {PROMOTE.TOKEN}", trap)
        # Stopping a server that joined etcd never protects quorum, and with
        # one member before it destroys it: the trap leaves k3s alone.
        self.assertNotIn("systemctl stop k3s", s)

    def test_download_failure_has_its_own_code(self):
        s = self.body()
        self.assertIn(f"curl -sfL https://get.k3s.io -o {PROMOTE.INSTALLER} || exit 20", s)

    def test_launch_script_writes_files_then_starts_unit(self):
        s = PROMOTE.launch_script("op1", 18, self.body())
        self.assertIn("umask 077", s)
        self.assertNotIn("HSTOKEN", s)
        # The token comes over stdin, a fixed length, written last.
        self.assertIn(f"head -c 18 > {PROMOTE.TOKEN}", s)
        self.assertLess(s.index("HSSCRIPT"), s.index("head -c 18"))
        self.assertIn("flock -n 9", s)
        self.assertIn(f"echo op1 > {PROMOTE.JOB}", s)
        self.assertLess(s.index(f"echo op1 > {PROMOTE.JOB}"), s.index("systemd-run"))
        self.assertIn(f"echo $? > {PROMOTE.STATUS}", s)
        self.assertIn(f"--unit={PROMOTE.UNIT}", s)
        self.assertTrue(s.rstrip().endswith("echo STARTED"))

    def test_launch_script_skips_when_marker_names_job(self):
        s = PROMOTE.launch_script("op1", 6, self.body())
        guard = f'[ "$(cat {PROMOTE.JOB} 2>/dev/null)" = op1 ] && {{ echo STARTED; exit 0; }}'
        self.assertIn(guard, s)
        self.assertLess(s.index("flock -n 9"), s.index(guard))
        self.assertLess(s.index(guard), s.index("head -c"))

    def test_launch_script_removes_the_token_unless_the_unit_started(self):
        s = PROMOTE.launch_script("op1", 6, self.body())
        self.assertIn(f"trap 'rm -f {PROMOTE.TOKEN}' EXIT", s)
        self.assertLess(s.index("systemd-run"), s.index("trap - EXIT"))

    def test_launch_script_refuses_bad_token(self):
        for bad in ("", "K10\nrm -rf /", "K10 x", "K10'x", "K10$(id)", "K10\nHSTOKEN\nrm -rf /"):
            with self.assertRaises(ValueError) as caught:
                PROMOTE.check_token(bad)
            self.assertNotIn("rm -rf", str(caught.exception))
        PROMOTE.check_token("K10abc::server:def")

    def test_a_value_with_a_line_break_never_reaches_the_host(self):
        evil = "x" + chr(10) + "HSSCRIPT" + chr(10) + "touch /pwned"
        with self.assertRaisesRegex(ValueError, "control character"):
            PROMOTE.promote_script("v1.33.4+k3s1", "192.0.2.10", ["--node-label", evil])
        self.assertFalse(PROMOTE.unprintable(["--node-ip", "'192.0.2.11'"]))

    def test_scripts_parse_under_dash(self):
        for text in (self.body(), PROMOTE.launch_script("op1", 6, self.body())):
            with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False, newline="") as f:
                f.write(text)
            result = subprocess.run([SH, "-n", f.name], capture_output=True, text=True)
            self.assertEqual(0, result.returncode, result.stderr)


from unittest import mock

import homestead_capacity as CAPACITY
import homestead_kube as K
import homestead_node_actions as NODE_ACTIONS
import homestead_maintenance as MAINTENANCE
import homestead_hostrun as HOSTRUN
import homestead_operations as OPS
import homestead_platform as PLATFORM
from fakes.kube import Kube


class Hostrun:
    def __init__(self, out="PASSWORD\nUNINSTALL\n"):
        self.out, self.calls, self.stdins, self.waits, self.changes = out, [], [], [], []

    def hold(self, node, what, seconds=60, holder=""):
        return "this-replica/1"

    def let_go(self, node, holder):
        pass

    def run(self, node, script, timeout=60, wait=60, stdin=b"", change=""):
        self.changes.append(change)
        self.calls.append((node, script))
        self.stdins.append(stdin)
        self.waits.append(wait)
        return (self.out(node, script) if callable(self.out) else self.out), ""


class Scene:
    """A k3s cluster on the shared fake API (tests/fakes/kube.py): server-1
    and server-2 (etcd), app-1 an agent. Promotion reads it and changes
    nothing itself; the host and the node actions are stand-ins."""
    SECRET = "/api/v1/namespaces/kube-system/secrets/app-1.node-password.k3s"
    LONGHORN_NODE = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system/nodes/app-1"

    def __init__(self, test):
        server_args = ["server", "--cluster-cidr", "10.42.0.0/16"]
        self.kube = test.enterContext(Kube(
            node("server-1", server_args, roles=("control-plane", "etcd", "master"), ip="192.0.2.5", uid="s1"),
            node("server-2", server_args, roles=("control-plane", "etcd", "master"), ip="192.0.2.6", uid="s2"),
            node("app-1", ["agent", "--node-ip", "192.0.2.11"], labels={"homelab/zone": "a"}),
            {"apiVersion": "v1", "kind": "Secret", "metadata": {"namespace": "kube-system", "name": "app-1.node-password.k3s"}},
            {"apiVersion": "longhorn.io/v1beta2", "kind": "Replica", "metadata": {"namespace": "longhorn-system", "name": "r1"},
             "spec": {"nodeID": "app-1"}},
            {"apiVersion": "longhorn.io/v1beta2", "kind": "Node", "metadata": {"namespace": "longhorn-system", "name": "app-1"},
             "spec": {"allowScheduling": True}}))
        saved = (PROMOTE.kget, PROMOTE.ksend, PROMOTE.hostrun, PROMOTE.own_node)
        test.addCleanup(lambda: PROMOTE.bind(*saved))

    def edit(self, name, change):
        """Someone else changes the node name; change(node) edits a copy."""
        obj = self.kube.object(f"/api/v1/nodes/{name}")
        change(obj)
        self.kube.add(obj)

    def label(self, name, **labels):
        self.edit(name, lambda n: n["metadata"]["labels"].update(labels))


class PlanBase(unittest.TestCase):
    def setUp(self):
        self.cluster, self.host = Scene(self), Hostrun()
        PROMOTE.bind(K.kget, K.ksend, self.host, lambda: "server-1")
        self.patches = [
            mock.patch.object(PLATFORM, "detect", lambda force=False: {"distribution": "k3s", "harvester": False,
                                                                        "control_plane": ["192.0.2.5"], "version": "1.37.1+k3s1"}),
            mock.patch.object(NODE_ACTIONS, "quorum_report", lambda: {"members": ["server-1", "server-2"], "ready": ["server-1", "server-2"],
                                                                  "total": 2, "quorum_needs": 2, "can_lose": 0}),
            mock.patch.object(CAPACITY, "every_host", lambda: {"hosts": [{"node": "app-1", "verdict": "fits", "sentence": "fits"}]}),
            mock.patch.object(MAINTENANCE, "inventory", lambda get, pods, **k: {"local_storage": [], "blockers": []}),
            mock.patch.object(OPS, "snapshot", lambda: []),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])


class PlanTests(PlanBase):
    def test_all_clear(self):
        p = PROMOTE.plan("app-1")
        self.assertEqual([], p["blockers"])
        self.assertTrue(any("2 → 3" in w for w in p["warnings"]))
        self.assertEqual("v1.37.1+k3s1", p["facts"]["version"])
        self.assertEqual("192.0.2.5", p["facts"]["server"])
        self.assertIn("--cluster-cidr", p["facts"]["settings"])
        self.assertEqual(64, len(p["review_token"]))

    def blocked(self, needle):
        p = PROMOTE.plan("app-1")
        self.assertTrue(any(needle in b for b in p["blockers"]), p["blockers"])

    def test_blocks_other_platforms(self):
        with mock.patch.object(PLATFORM, "detect", lambda force=False: {"distribution": "rke2", "harvester": True, "control_plane": []}):
            self.blocked("k3s")

    def test_blocks_without_etcd(self):
        with mock.patch.object(NODE_ACTIONS, "quorum_report", lambda: {"members": [], "ready": [], "total": 0, "quorum_needs": 1, "can_lose": 0}):
            self.blocked("etcd")

    def test_blocks_a_server(self):
        self.assertTrue(any("server already" in b for b in PROMOTE.plan("server-2")["blockers"]))

    def test_blocks_a_tiebreaker(self):
        self.cluster.label("app-1", **{"node-role/tiebreaker": "true"})
        self.blocked("tiebreaker")

    def test_blocks_a_host_not_ready(self):
        self.cluster.edit("app-1", lambda n: n["status"]["conditions"][0].update(status="False"))
        self.blocked("not Ready")

    def test_blocks_homestead_on_the_host(self):
        PROMOTE.bind(K.kget, K.ksend, self.host, lambda: "app-1")
        self.blocked("Homestead runs on app-1")

    def test_blocks_unknown_shared_settings(self):
        for name in ("server-1", "server-2"):
            self.cluster.edit(name, lambda n: n["metadata"].update(annotations={}))
        self.blocked("settings")

    def test_blocks_version_mismatch(self):
        self.cluster.edit("app-1", lambda n: n["status"]["nodeInfo"].update(kubeletVersion="v1.36.0+k3s1"))
        self.blocked("version")

    def test_blocks_missing_node_password_file(self):
        self.host.out = "UNINSTALL\n"
        self.blocked("node password")

    def test_blocks_missing_node_password_secret(self):
        self.cluster.kube.discard(self.cluster.SECRET)
        self.blocked("node-password")

    def test_blocks_missing_uninstall_script(self):
        self.host.out = "PASSWORD\n"
        self.blocked("k3s-agent-uninstall.sh")

    def test_blocks_a_member_down(self):
        with mock.patch.object(NODE_ACTIONS, "quorum_report", lambda: {"members": ["server-1", "server-2"], "ready": ["server-1"],
                                                                   "total": 2, "quorum_needs": 2, "can_lose": 0}):
            self.blocked("server-2")

    def test_blocks_a_running_promotion(self):
        with mock.patch.object(OPS, "snapshot", lambda: [{"kind": PROMOTE.KIND, "status": "running"}]):
            self.blocked("already")

    def test_blocks_when_apps_do_not_fit(self):
        for verdict in ("waits", "no-host", "unknown"):
            with mock.patch.object(CAPACITY, "every_host", lambda v=verdict: {"hosts": [{"node": "app-1", "verdict": v, "sentence": "S-" + v}]}):
                self.blocked("S-" + verdict)

    def test_tight_is_a_warning(self):
        with mock.patch.object(CAPACITY, "every_host", lambda: {"hosts": [{"node": "app-1", "verdict": "tight", "sentence": "TIGHT"}]}):
            p = PROMOTE.plan("app-1")
        self.assertEqual([], p["blockers"])
        self.assertIn("TIGHT", " ".join(p["warnings"]))

    def test_one_member_says_two_need_both(self):
        with mock.patch.object(NODE_ACTIONS, "quorum_report", lambda: {"members": ["server-1"], "ready": ["server-1"],
                                                                   "total": 1, "quorum_needs": 1, "can_lose": 0}):
            self.assertTrue(any("1 → 2" in w and "both" in w for w in PROMOTE.plan("app-1")["warnings"]))

    def test_dropped_flags_and_local_storage_warn(self):
        self.cluster.edit("app-1", lambda n: n["metadata"]["annotations"].update({"k3s.io/node-args": '["agent", "--data-dir", "/srv/k3s"]'}))
        with mock.patch.object(MAINTENANCE, "inventory", lambda get, pods, **k: {"local_storage": [{"pod": "lab/db-0"}], "blockers": []}):
            warnings = " ".join(PROMOTE.plan("app-1")["warnings"])
        self.assertIn("--data-dir", warnings)
        self.assertIn("lab/db-0", warnings)

    def test_review_token_follows_the_cluster(self):
        before = PROMOTE.plan("app-1")["review_token"]
        self.assertEqual(before, PROMOTE.plan("app-1")["review_token"])
        self.cluster.edit("server-1", lambda n: n["metadata"]["annotations"].update({"k3s.io/node-args": '["server", "--cluster-cidr", "10.52.0.0/16"]'}))
        self.assertNotEqual(before, PROMOTE.plan("app-1")["review_token"])

    def test_plan_never_reads_the_token(self):
        PROMOTE.plan("app-1")
        self.assertFalse(any(PROMOTE.TOKEN_FILE in script for _, script in self.host.calls))

    def test_flags_that_name_the_node_are_kept_or_block(self):
        flags, _ = PROMOTE.node_flags(node(args=["agent", "--node-name", "box-1"]))
        self.assertIn("--node-name", flags)
        self.cluster.edit("app-1", lambda n: n["metadata"]["annotations"].update({"k3s.io/node-args": '["agent", "--with-node-id"]'}))
        self.assertTrue(any("--with-node-id" in b for b in PROMOTE.plan("app-1")["blockers"]))
    def test_blocks_while_a_host_job_runs(self):
        for kind in ("node-power", "host-os", "os-rollout", "cluster-shutdown"):
            with mock.patch.object(OPS, "snapshot", lambda k=kind: [{"kind": k, "status": "running"}]):
                self.assertTrue(any(kind in b for b in PROMOTE.plan("app-1")["blockers"]), kind)

    def test_route_is_admin(self):
        self.assertEqual("admin", PROMOTE.ROUTES[("GET", "/api/node/promote/plan")][0])


class Ops:
    """homestead_operations, enough of it: one job, phases recorded."""
    def __init__(self):
        self.items, self.phases = [], []

    def start(self, kind, title, resource, href, ref, message="", unless=()):
        item = {"id": "op1", "kind": kind, "title": title, "ref": dict(ref), "status": "running", "progress": 0, "message": message}
        self.items.append(item)
        return item

    def read(self):
        return self.items

    def record_phase(self, op_id, phase, progress, message, owner=None, **updates):
        item = self.items[0]
        worker = item["ref"].get("worker")
        if owner and worker and worker != owner:
            raise OPS.Superseded(f"{worker} carries this job on now")
        if item["status"] in ("succeeded", "failed", "cancelled"):
            raise ValueError("Maintenance job has ended; refusing further actions")
        item["ref"].update(updates, phase=phase)
        item["status"] = "failed" if phase == "failed" else "running"
        item["message"] = message
        self.phases.append((phase, message))


class JobTests(PlanBase):
    def setUp(self):
        super().setUp()
        self.ops, self.cordons, self.drains = Ops(), [], []
        self.host.out = self.answer
        self.token = "K10abcdef::server:0123456789"
        self.probe = "MARKED\nACTIVE\n"
        more = [mock.patch.object(PROMOTE, "OPS_START", self.ops.start),
                mock.patch.object(PROMOTE, "OPS_RECORD", self.ops.record_phase),
                mock.patch.object(PROMOTE, "OPS_READ", self.ops.read),
                mock.patch.object(PROMOTE, "POD", "homestead-a"),
                mock.patch.object(PROMOTE, "_thread", lambda target: target()),
                mock.patch.object(PROMOTE, "is_leader", lambda: True),
                mock.patch.object(NODE_ACTIONS, "set_cordon", lambda n, on: self.cordons.append((n, on))),
                mock.patch.object(NODE_ACTIONS, "drain", lambda n, **k: self.drains.append((n, k)) or {"ok": True}),
                mock.patch.object(PLATFORM, "join_guide", lambda: {"agent": "AGENT-JOIN-CMD"})]
        for p in more:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in more])
        PROMOTE._ACTIVE.clear()

    def answer(self, node, script):
        if PROMOTE.TOKEN_FILE in script:
            return self.token + "\n"
        if "systemd-run" in script:
            return "STARTED\n"
        if "MARKED" in script:
            return self.probe
        return "PASSWORD\nUNINSTALL\n"

    def started(self):
        return PROMOTE.start("app-1", PROMOTE.plan("app-1")["review_token"])

    def test_stale_review_is_refused(self):
        with self.assertRaises(ValueError):
            PROMOTE.start("app-1", "0" * 64)
        self.assertEqual([], self.ops.items)

    def test_blocked_plan_is_refused(self):
        token = PROMOTE.plan("app-1")["review_token"]
        self.cluster.kube.discard(self.cluster.SECRET)
        with self.assertRaises(ValueError):
            PROMOTE.start("app-1", token)

    def test_start_drains_reads_token_and_launches(self):
        item = self.started()
        self.assertEqual([("app-1", True)], self.cordons)
        self.assertEqual("app-1", self.drains[0][0])
        self.assertTrue(self.drains[0][1]["include_system"] and self.drains[0][1]["wait"])
        token_reads = [n for n, s in self.host.calls if PROMOTE.TOKEN_FILE in s]
        self.assertEqual(["server-1"], token_reads)
        launch = [i for i, (n, s) in enumerate(self.host.calls) if n == "app-1" and "systemd-run" in s]
        self.assertEqual(1, len(launch))
        self.assertNotIn(self.token, self.host.calls[launch[0]][1])
        self.assertEqual(self.token.encode(), self.host.stdins[launch[0]])
        self.assertEqual("running", item["ref"]["phase"])
        self.assertEqual("Make app-1 a server", self.host.changes[launch[0]], "the reinstall takes the host's lease")
        self.assertEqual({""}, {c for i, c in enumerate(self.host.changes) if i != launch[0]}, "reads take none")

    def test_token_never_stored_or_reported(self):
        item = self.started()
        import json as _json
        self.assertNotIn(self.token, _json.dumps(item))
        self.assertFalse(any(self.token in m for _, m in self.ops.phases))

    def test_token_read_prefers_a_server_that_is_not_a_tiebreaker(self):
        self.cluster.label("server-1", **{"node-role/tiebreaker": "true"})
        self.started()
        self.assertEqual(["server-2"], [n for n, s in self.host.calls if PROMOTE.TOKEN_FILE in s])

    def test_bad_token_fails_job_without_launching(self):
        self.token = "K10 $(id)"
        item = self.started()
        self.assertEqual("failed", item["status"])
        self.assertNotIn("$(id)", item["message"])
        self.assertFalse(any("systemd-run" in s for _, s in self.host.calls))
        self.assertIn(("app-1", False), self.cordons)

    def test_another_change_on_the_host_refuses_before_the_drain(self):
        def held(node, what, seconds=60, holder=""):
            raise HOSTRUN.Busy(f"another change is running on {node}: Prepare /dev/sdb on {node}; try again once it has finished")
        self.host.hold = held
        item = self.started()
        self.assertEqual("failed", item["status"])
        self.assertIn("another change is running on app-1: Prepare /dev/sdb on app-1", item["message"])
        self.assertEqual([], self.drains, "nothing drained for a refusal")
        self.assertFalse(any("systemd-run" in s for _, s in self.host.calls))

    def test_drain_failure_fails_and_uncordons(self):
        with mock.patch.object(NODE_ACTIONS, "drain", mock.Mock(side_effect=ValueError("drain timed out"))):
            item = self.started()
        self.assertEqual("failed", item["status"])
        self.assertIn("drain timed out", item["message"])
        self.assertEqual([("app-1", True), ("app-1", False)], self.cordons)

    def test_resume_restarts_thread_once(self):
        item = self.started()
        item["ref"]["phase"] = "draining"
        threads = []
        with mock.patch.object(PROMOTE, "_thread", lambda target: threads.append(target)):
            PROMOTE.status(item)
            PROMOTE.status(item)
        self.assertEqual(1, len(threads))

    def run_with(self, out, item=None, now=None):
        item = item or self.started()
        self.probe = out if callable(out) else "MARKED\n" + out
        if callable(out):
            self.host.out = out
        return item, PROMOTE.status(item, now=now or item["ref"]["running_since"] + 30)

    def test_running_follows_the_unit(self):
        item, result = self.run_with("[INFO]  Starting k3s\n")
        self.assertEqual("running", result[0])
        self.assertIn("Starting k3s", result[2])

    def test_running_unreachable_is_running_then_fails_after_limit(self):
        def boom(node, script):
            raise RuntimeError("pod did not start")
        item, result = self.run_with(boom)
        self.assertEqual("running", result[0])
        result = PROMOTE.status(item, now=item["ref"]["running_since"] + PROMOTE.RUN_LIMIT + 1)
        self.assertEqual("failed", result[0])
        self.assertIn(PROMOTE.LOG, result[2])

    def test_running_failure_message_has_rejoin(self):
        _, result = self.run_with("CODE 1\ncurl: (6) Could not resolve host\n")
        self.assertEqual("failed", result[0])
        self.assertIn("AGENT-JOIN-CMD", result[2])
        self.assertIn("code 1", result[2])
        self.assertNotIn("k3s is stopped", result[2])

    def test_running_code_10_is_the_node_password(self):
        _, result = self.run_with("CODE 10\n")
        self.assertEqual("failed", result[0])
        self.assertIn("node password", result[2])

    def test_running_success_moves_to_joining(self):
        item, result = self.run_with("CODE 0\n")
        self.assertEqual("running", result[0])
        self.assertEqual("joining", item["ref"]["phase"])

    def joined_item(self):
        item, _ = self.run_with("CODE 0\n")
        self.cluster.label("app-1", **{"node-role.kubernetes.io/control-plane": "true", "node-role.kubernetes.io/etcd": "true"})
        return item

    def test_joining_waits_for_etcd(self):
        item = self.joined_item()
        result = PROMOTE.status(item, now=item["ref"]["joining_since"] + 5)
        self.assertEqual("running", result[0])

    def test_joining_done_uncordons(self):
        item = self.joined_item()
        with mock.patch.object(NODE_ACTIONS, "quorum_report", lambda: {"members": ["server-1", "server-2", "app-1"],
                                                                   "ready": ["server-1", "server-2", "app-1"], "total": 3,
                                                                   "quorum_needs": 2, "can_lose": 1}):
            result = PROMOTE.status(item, now=item["ref"]["joining_since"] + 5)
        self.assertEqual("succeeded", result[0])
        self.assertIn("3 etcd members", result[2])
        self.assertEqual(("app-1", False), self.cordons[-1])

    def test_joining_new_uid_fails(self):
        item = self.joined_item()
        self.cluster.edit("app-1", lambda n: n["metadata"].update(uid="uid-new"))
        self.assertEqual("failed", PROMOTE.status(item, now=item["ref"]["joining_since"] + 5)[0])

    def test_joining_reports_a_lost_label(self):
        item = self.joined_item()
        self.cluster.edit("app-1", lambda n: n["metadata"]["labels"].pop("homelab/zone"))
        with mock.patch.object(NODE_ACTIONS, "quorum_report", lambda: {"members": ["server-1", "server-2", "app-1"],
                                                                   "ready": ["server-1", "server-2", "app-1"], "total": 3,
                                                                   "quorum_needs": 2, "can_lose": 1}):
            result = PROMOTE.status(item, now=item["ref"]["joining_since"] + 5)
        self.assertEqual("succeeded", result[0])
        self.assertIn("homelab/zone", result[2])

    def test_joining_deadline(self):
        item = self.joined_item()
        self.assertEqual("failed", PROMOTE.status(item, now=item["ref"]["joining_since"] + PROMOTE.JOIN_LIMIT + 1)[0])

    def test_cancel_only_while_draining(self):
        item = self.started()
        self.assertFalse(PROMOTE.cancel_plan(item)["can"])
        item["ref"]["phase"] = "draining"
        self.assertTrue(PROMOTE.cancel_plan(item)["can"])
        PROMOTE.cancel_run(item, {})
        self.assertEqual(("app-1", False), self.cordons[-1])

    def test_bind_registers_resolver_and_canceller(self):
        self.assertIs(OPS.RESOLVERS[PROMOTE.KIND], PROMOTE.status)
        self.assertEqual((PROMOTE.cancel_plan, PROMOTE.cancel_run), OPS.CANCELLERS[PROMOTE.KIND])

    def test_post_route_is_admin(self):
        self.assertEqual("admin", PROMOTE.ROUTES[("POST", "/api/node/promote")][0])


class KindTests(unittest.TestCase):
    def test_host_promote_is_a_host_job(self):
        import homestead_recovery as RECOVERY
        self.assertIn("host-promote", RECOVERY.MOVES_OR_RESTARTS)
        source = (Path(__file__).resolve().parents[1] / "server" / "homestead_storage_conflicts.py").read_text()
        self.assertIn('"host-promote"', source)


class FixTests(JobTests):
    """The final review's findings (2026-10-11)."""

    def test_start_on_a_follower_leaves_the_job_to_the_leader(self):
        with mock.patch.object(PROMOTE, "is_leader", lambda: False):
            item = self.started()
        self.assertEqual([], self.drains)
        self.assertEqual("draining", item["ref"]["phase"])

    def test_the_thread_claims_the_job_and_writes_as_its_worker(self):
        item = self.started()
        self.assertEqual("homestead-a", item["ref"]["worker"])

    def test_another_worker_stops_this_thread(self):
        def taken(*a, **k):
            self.ops.items[0]["ref"]["worker"] = "homestead-b"
            return {"ok": True}
        with mock.patch.object(NODE_ACTIONS, "drain", taken):
            item = self.started()
        self.assertFalse(any("systemd-run" in s for _, s in self.host.calls))
        self.assertNotEqual("failed", item["status"])

    def test_cancelled_before_launch_launches_nothing(self):
        def cancel(*a, **k):
            self.ops.items[0]["status"] = "cancelling"
            return {"ok": True}
        with mock.patch.object(NODE_ACTIONS, "drain", cancel):
            self.started()
        self.assertFalse(any("systemd-run" in s for _, s in self.host.calls))

    def test_a_launch_error_is_followed_not_reported_unchanged(self):
        def lost(node, script):
            if "systemd-run" in script:
                raise ConnectionError("stream closed")
            return self.answer(node, script)
        self.host.out = lost
        item = self.started()
        self.assertEqual("running", item["status"])
        self.assertNotIn(("app-1", False), self.cordons)
        self.assertNotIn("not changed", item["message"])

    def test_a_launch_that_never_started_fails_unchanged_once_seen(self):
        def lost(node, script):
            if "systemd-run" in script:
                raise ConnectionError("stream closed")
            return self.answer(node, script)
        self.host.out = lost
        item = self.started()
        self.probe = "\n"   # reachable: no marker, no unit, no status
        result = PROMOTE.status(item, now=item["ref"]["running_since"] + 30)
        self.assertEqual("failed", result[0])
        self.assertIn("not changed", result[2])
        self.assertEqual(("app-1", False), self.cordons[-1])

    def test_probes_wait_briefly_and_are_spaced(self):
        item = self.started()
        since = item["ref"]["running_since"]
        calls = len(self.host.calls)
        self.assertEqual("running", PROMOTE.status(item, now=since + 5)[0])
        self.assertEqual(calls, len(self.host.calls), "no probe while the unit settles")
        PROMOTE.status(item, now=since + 30)
        PROMOTE.status(item, now=since + 40)
        self.assertEqual(calls + 1, len(self.host.calls), "one probe per interval")
        self.assertLessEqual(self.host.waits[-1], 5)

    def test_download_failure_says_unchanged_and_uncordons(self):
        _, result = self.run_with("CODE 20\n")
        self.assertEqual("failed", result[0])
        self.assertIn("still an app host", result[2])
        self.assertEqual(("app-1", False), self.cordons[-1])

    def test_code_10_says_how_to_rejoin(self):
        _, result = self.run_with("CODE 10\n")
        self.assertIn("app-1.node-password.k3s", result[2])
        self.assertIn("AGENT-JOIN-CMD", result[2])

    def test_installer_error_after_joining_carries_on(self):
        item, _ = self.run_with("CODE 1\n", now=None) if False else (None, None)
        item = self.started()
        self.cluster.label("app-1", **{"node-role.kubernetes.io/control-plane": "true", "node-role.kubernetes.io/etcd": "true"})
        with mock.patch.object(NODE_ACTIONS, "quorum_report", lambda: {"members": ["server-1", "server-2", "app-1"],
                                                                   "ready": ["server-1", "server-2", "app-1"], "total": 3,
                                                                   "quorum_needs": 2, "can_lose": 1}):
            _, result = self.run_with("CODE 1\n", item=item)
        self.assertEqual("running", result[0])
        self.assertEqual("joining", item["ref"]["phase"])

    def test_drain_messages_do_not_mention_power(self):
        def slow(n, progress=None, **k):
            progress("draining", 10, "Waiting for drained pods to leave: lab/db-0; power has not been sent")
            raise ValueError("Host remains cordoned; drain timed out after 2 minutes. Pods have not left the host: lab/db-0. "
                             "Power was not sent; inspect Longhorn volumes, disruption budgets and pod events")
        with mock.patch.object(NODE_ACTIONS, "drain", slow):
            item = self.started()
        text = " ".join(m for _, m in self.ops.phases)
        self.assertNotIn("ower", text)
        self.assertNotIn("remains cordoned", text)
        self.assertIn("lab/db-0", item["message"])

    def test_a_missing_longhorn_node_is_reported(self):
        item = self.joined_item()
        self.cluster.kube.discard(self.cluster.LONGHORN_NODE)
        with mock.patch.object(NODE_ACTIONS, "quorum_report", lambda: {"members": ["server-1", "server-2", "app-1"],
                                                                   "ready": ["server-1", "server-2", "app-1"], "total": 3,
                                                                   "quorum_needs": 2, "can_lose": 1}):
            result = PROMOTE.status(item, now=item["ref"]["joining_since"] + 5)
        self.assertEqual("succeeded", result[0])
        self.assertIn("Longhorn", result[2])


class HostrunStdinTests(unittest.TestCase):
    def test_stdin_reaches_the_exec(self):
        import homestead_hostrun as HOSTRUN
        seen, pods = {}, {}
        def ksend(method, path, body=None):
            if method == "POST":
                pods["p"] = {"status": {"phase": "Running"}}
            return {}
        HOSTRUN.bind(lambda path: pods.get("p") or {}, ksend,
                     lambda ns, pod, argv, **k: seen.update(k) or (b"OK", ""), "lab")
        with mock.patch.object(HOSTRUN.KUBE, "get_optional", lambda path, read=None: pods.get("p")), \
             mock.patch.object(HOSTRUN, "_delete", lambda path: pods.pop("p", None)):
            HOSTRUN.run("app-1", "cat", stdin=b"secret", sleep=lambda s: None)
        self.assertEqual(b"secret", seen.get("stdin"))


class ClaimTests(JobTests):
    def test_a_job_cancelled_before_its_thread_runs_is_left_alone(self):
        threads = []
        with mock.patch.object(PROMOTE, "_thread", lambda target: threads.append(target)):
            item = self.started()
        item["status"] = "cancelling"
        threads[0]()
        self.assertEqual("cancelling", item["status"])
        self.assertEqual([], self.drains)


class WiringTests(unittest.TestCase):
    """server.py sits at its line cap: the module wires itself when
    homestead_routes loads it at start, through hostrun's binding."""
    def test_server_py_does_not_name_the_module(self):
        source = (Path(__file__).resolve().parents[1] / "server" / "server.py").read_text()
        self.assertFalse("homestead_promote" in source, "server.py names homestead_promote")

    def test_import_registers_the_job_and_reaches_kubernetes_through_hostrun(self):
        import importlib
        import homestead_hostrun as HOSTRUN
        import homestead_self as SELF
        seen = []
        saved = (HOSTRUN.kget, HOSTRUN.ksend)
        self.addCleanup(lambda: setattr(HOSTRUN, "kget", saved[0]) or setattr(HOSTRUN, "ksend", saved[1]))
        fresh = importlib.reload(PROMOTE)
        self.assertIs(OPS.RESOLVERS[fresh.KIND], fresh.status)
        self.assertEqual((fresh.cancel_plan, fresh.cancel_run), OPS.CANCELLERS[fresh.KIND])
        HOSTRUN.kget = lambda path: seen.append(path) or {"spec": {"nodeName": "server-1"}}
        with mock.patch.dict("os.environ", {"HOSTNAME": "homestead-a"}):
            self.assertEqual("server-1", fresh.own_node())
        self.assertEqual([f"/api/v1/namespaces/{SELF.NS}/pods/homestead-a"], seen)
        self.assertIs(fresh.hostrun, HOSTRUN)
