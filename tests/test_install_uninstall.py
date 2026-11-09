"""install.sh --uninstall (#792): it reviews what it will remove, asks on the
terminal, and never touches what Homestead did not create.

The whole script runs, as a shell would run it, against a stand-in for
`k3s kubectl` that keeps a small cluster in a JSON file, and answers through a
real pseudo-terminal. What the stand-in cannot prove (real kubectl's wording
and jsonpath output, a real Helm release, pods actually stopping) is named in
the pull request as needing a temporary cluster.
"""
import json
import os
import pty
import select
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "install.sh"
SH = shutil.which("dash") or shutil.which("sh")
HOLD_IN_SCRIPT = "/etc/apt/apt.conf.d/99-homestead-hold"

FAKE_KUBECTL = r'''#!%(python)s -SE
import json, os, sys
path = os.environ["FAKE_CLUSTER"]
state = json.load(open(path))
args = sys.argv[1:]
if args[:1] == ["kubectl"]:
    args = args[1:]
with open(os.environ["FAKE_CALLS"], "a") as log:
    log.write(" ".join(args) + "\n")

def save():
    json.dump(state, open(path, "w"))

def die(text, code=1):
    sys.stderr.write(text + "\n")
    sys.exit(code)

ns = None
if args[:1] == ["-n"]:
    ns, args = args[1], args[2:]
objects = state["objects"]
verb = args[0]
longhorn = ".longhorn.io"

def served(kind):
    if longhorn in kind and state.get("no_longhorn"):
        die(f'error: the server doesn\'t have a resource type "{kind.split(".")[0]}"')
    if kind in state.get("forbid", []):
        die(f'Error from server (Forbidden): {kind} is forbidden: User "someone" cannot list resource')

def key(kind, name):
    return f"{ns or '_'}/{kind}/{name}"

def flag(name):
    for arg in args:
        if arg.startswith(name + "="):
            return arg.split("=", 1)[1]
    return None

if verb == "get":
    kind = args[1]
    served(kind)
    if kind == "deployment" and "-A" in args:
        for name, row in objects.items():
            if name.endswith("/deployment/homestead"):
                print("|".join([name.split("/")[0], row.get("app", ""), row.get("release", ""), row.get("apps_ns", ""),
                                row.get("claim", "homestead-data")]))
    elif kind == "persistentvolumeclaim" and "-A" in args:
        for name in objects:
            if name.endswith("/persistentvolumeclaim/homestead-data"):
                print(name.split("/")[0])
    elif kind == "nodes":
        for node, taints in state.get("nodes", {}).items():
            print(node + " " + " ".join(taints))
    elif kind == "volumeattachments.longhorn.io":
        for volume, tickets in state.get("tickets", {}).items():
            # What real kubectl prints for a key: the ticket's own JSON, with an id that need not be its key.
            asked = " ".join(args)
            print("\t".join([volume] + ['{"id":"x|y"}' if t in tickets and f".attachmentTickets.{t}" in asked else ""
                                        for t in ("homestead-rebuild", "homestead-revert")]))
    elif kind == "deployments,statefulsets,cronjobs":
        for row in state.get("workloads", []):
            print("|".join(str(row.get(k, "")) for k in ("kind", "ns", "name", "replicas", "suspend", "job", "held",
                                                         "schedule", "autostart", "restructure")))
    elif kind == "virtualmachines.kubevirt.io":
        if state.get("no_kubevirt"):
            die('error: the server doesn\'t have a resource type "virtualmachines"')
        for row in state.get("vms", []):
            print("|".join(str(row.get(k, "")) for k in ("ns", "name", "running", "strategy", "job", "held", "schedule", "autostart")))
    elif kind == "pods":
        for pod in state.get("pods", {}).get(ns, []):
            print("pod/" + pod)
    elif len(args) > 2 and not args[2].startswith("-"):
        row = objects.get(key(kind, args[2]))
        if row is None:
            die(f'Error from server (NotFound): {kind} "{args[2]}" not found')
        if "-o" in args and args[args.index("-o") + 1] == "name":
            print(f"{kind}/{args[2]}")
        else:
            if kind in state.get("identity_forbidden", []):
                die(f'Error from server (Forbidden): cannot read {kind} {args[2]}')
            print(row.get("subject", "") if kind == "clusterrolebinding" else row.get("app", ""))
    else:  # a whole namespace, one kind at a time
        for name in objects:
            there, found, short = name.split("/")
            if there == ns and found == kind:
                print(f"{kind}/{short}")
elif verb == "api-resources":
    print("\n".join(sorted({name.split("/")[1] for name in objects if not name.startswith("_/")} | {"events"})))
elif verb == "delete":
    kind, name = args[1], args[2]
    if kind in state.get("delete_fail", []) or name in state.get("delete_fail", []):
        die(f'Error from server (Forbidden): cannot delete {kind} {name}')
    objects.pop(key(kind, name), None)
    if kind == "deployment" and not state.get("pods_stay"):
        state.get("pods", {}).pop(ns, None)
    if kind == "namespace":
        state.setdefault("namespaces_deleted", []).append(name)
    save()
    print(f"{kind}/{name} deleted")
elif verb == "taint":
    node, taint = args[2], args[3].rstrip("-")
    if state.get("taint_fail"):
        die("Error from server (Forbidden): cannot taint " + node)
    state["nodes"][node].remove(taint)
    save()
elif verb == "patch":
    if state.get("patch_fail"):
        die("Error from server (Forbidden): cannot patch " + args[2])
    body = json.loads(args[args.index("-p") + 1])
    tickets = state["tickets"][args[2]]
    for name, value in body["spec"]["attachmentTickets"].items():
        assert value is None
        tickets.remove(name)
    save()
else:
    die("unexpected kubectl call: " + " ".join(args), 99)
'''

FAKE_HELM = r'''#!%(python)s -SE
import json, os, sys
path = os.environ["FAKE_CLUSTER"]
state = json.load(open(path))
with open(os.environ["FAKE_CALLS"], "a") as log:
    log.write("helm " + " ".join(sys.argv[1:]) + " KUBECONFIG=" + os.environ.get("KUBECONFIG", "") + "\n")
if state.get("helm_fails"):
    sys.exit("Error: uninstallation failed")
if state.get("helm_partial"):
    sys.exit('Error: uninstall: failed to delete release: deployments.apps "homestead" not found')
if state.get("helm_no_release"):
    sys.exit("Error: uninstall: Release not loaded: my-homestead: release: not found")
for name in list(state["objects"]):
    if state["objects"][name].get("release"):
        del state["objects"][name]
state.get("pods", {}).pop(sys.argv[sys.argv.index("-n") + 1], None)
json.dump(state, open(path, "w"))
'''

SYSTEMCTL = """#!/bin/sh
case "$1" in
  is-active|is-enabled) [ "$2" = --quiet ] && [ "$3" = "$FAKE_SERVICE.service" ] ;;
  list-unit-files) exit 1 ;;
  *) exit 0 ;;
esac
"""


def plain_install():
    """What a plain-manifest cluster holds, with apps beside Homestead in lab."""
    objects = {f"lab/{kind}/{name}": {} for kind, name in [
        ("service", "homestead"), ("serviceaccount", "homestead"),
        ("daemonset", "homestead-nodeprobe"), ("configmap", "homestead-nodeprobe"),
        ("secret", "homestead-smart-key"), ("secret", "homestead-allocation-auth"),
        ("role", "homestead-console"), ("rolebinding", "homestead-console"),
        ("secret", "homestead-setup"), ("role", "homestead-setup"), ("rolebinding", "homestead-setup"),
        # kept unless --everything
        ("persistentvolumeclaim", "homestead-data"), ("secret", "homestead-auth"),
        ("configmap", "homestead-install"), ("configmap", "homestead-settings"),
        ("configmap", "homestead-portal"), ("lease", "homestead-leader"),
        # never: an app with the managed label, the object store, the share server
        ("deployment", "web"), ("service", "web"), ("deployment", "homestead-objectstore"),
        ("persistentvolumeclaim", "homestead-objectstore"), ("secret", "homestead-objectstore-keys"),
        ("deployment", "homestead-smb"),
    ]}
    objects["lab/service/homestead-vip"] = {"app": "homestead"}
    objects["lab/deployment/homestead"] = {"app": "homestead", "apps_ns": "lab"}
    objects["lab/service/homestead"] = {"app": "homestead"}
    objects["lab/deployment/web"] = {"app": "web", "managed": "true"}
    objects["_/clusterrole/homestead"] = {}
    objects["_/clusterrolebinding/homestead"] = {"subject": "ServiceAccount|homestead|lab"}
    objects["_/priorityclass/homestead-critical"] = {}
    objects["_/storageclass/homestead-single-copy"] = {}
    objects["kube-system/helmchart/longhorn"] = {}
    objects["longhorn-system/recurringjobs.longhorn.io/homestead-weekly-trim"] = {}
    objects["longhorn-system/networkpolicies.networking.k8s.io/homestead-backend-access"] = {}
    objects["longhorn-system/secret/homestead-backup-target"] = {}
    return {"objects": objects, "pods": {"lab": ["homestead-1"]},
            "nodes": {"node-a": ["homestead.io/standby", "homestead.io/storage-pending"], "node-b": ["other/taint"]},
            "tickets": {"vol-a": ["homestead-rebuild", "csi-x"], "vol-b": ["csi-y"]},
            "workloads": [], "vms": [], "no_kubevirt": True}


def helm_shaped():
    """Homestead in its own namespace, hs, with the apps left in lab."""
    state = plain_install()
    for name in ("deployment/homestead", "service/homestead", "serviceaccount/homestead", "secret/homestead-setup",
                 "role/homestead-setup", "rolebinding/homestead-setup", "persistentvolumeclaim/homestead-data",
                 "service/homestead-vip", "lease/homestead-leader"):
        state["objects"][f"hs/{name}"] = state["objects"].pop(f"lab/{name}")
    state["objects"]["_/clusterrolebinding/homestead"]["subject"] = "ServiceAccount|homestead|hs"
    state["pods"] = {"hs": ["homestead-1"]}
    return state


@unittest.skipUnless(SH and os.name == "posix", "needs a POSIX system")
class Uninstall(unittest.TestCase):
    NEVER = {"lab/deployment/web", "lab/service/web", "lab/deployment/homestead-objectstore",
             "lab/persistentvolumeclaim/homestead-objectstore", "lab/secret/homestead-objectstore-keys",
             "lab/deployment/homestead-smb", "_/priorityclass/homestead-critical",
             "_/storageclass/homestead-single-copy", "kube-system/helmchart/longhorn",
             "longhorn-system/secret/homestead-backup-target"}
    REMOVED = {"lab/deployment/homestead", "lab/service/homestead", "lab/serviceaccount/homestead",
               "_/clusterrole/homestead", "_/clusterrolebinding/homestead",
               "lab/daemonset/homestead-nodeprobe", "lab/configmap/homestead-nodeprobe",
               "lab/secret/homestead-smart-key", "lab/secret/homestead-allocation-auth",
               "lab/role/homestead-console", "lab/rolebinding/homestead-console",
               "lab/secret/homestead-setup", "lab/role/homestead-setup", "lab/rolebinding/homestead-setup",
               "longhorn-system/recurringjobs.longhorn.io/homestead-weekly-trim",
               "longhorn-system/networkpolicies.networking.k8s.io/homestead-backend-access"}
    KEPT_DATA = {"lab/persistentvolumeclaim/homestead-data", "lab/secret/homestead-auth",
                 "lab/configmap/homestead-install", "lab/configmap/homestead-settings",
                 "lab/configmap/homestead-portal", "lab/lease/homestead-leader", "lab/service/homestead-vip"}

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.bin = self.dir / "bin"
        self.bin.mkdir()
        self.cluster = self.dir / "cluster.json"
        self.calls = self.dir / "calls"
        self.calls.write_text("")
        self.hold = self.dir / "99-homestead-hold"
        self.stub("k3s", FAKE_KUBECTL % {"python": sys.executable})
        self.stub("helm", FAKE_HELM % {"python": sys.executable})
        self.stub("systemctl", SYSTEMCTL)
        self.stub("id", '#!/bin/sh\necho 0\n')
        self.stub("sleep", '#!/bin/sh\nexit 0\n')
        self.stub("hostname", '#!/bin/sh\necho node-a\n')

    def stub(self, name, text):
        path = self.bin / name
        path.write_text(text, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)

    def state(self):
        return json.loads(self.cluster.read_text())

    def uninstall(self, state, *args, answer=None, terminal=True, env=None):
        """Runs the script; returns (exit code, everything a person would read)."""
        self.cluster.write_text(json.dumps(state))
        text = SCRIPT.read_text(encoding="utf-8").replace("\r\n", "\n")
        self.assertIn("TTY=/dev/tty", text)
        text = text.replace("TTY=/dev/tty", 'TTY="$TEST_TTY"').replace(HOLD_IN_SCRIPT, str(self.hold))
        script = self.dir / "install.sh"
        script.write_text(text, encoding="utf-8")
        master, slave = pty.openpty()
        full = dict(os.environ, PATH=f"{self.bin}{os.pathsep}{os.environ['PATH']}", TERM="dumb",
                    TMPDIR=str(self.dir), FAKE_CLUSTER=str(self.cluster), FAKE_CALLS=str(self.calls),
                    FAKE_SERVICE="k3s", TEST_TTY=os.ttyname(slave) if terminal else "/nonexistent/tty")
        full.update(env or {})
        full.pop("KUBECONFIG", None)
        process = subprocess.Popen([SH, str(script), *args], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True, env=full)
        seen, asked = b"", False
        try:
            deadline = time.monotonic() + 60
            while process.poll() is None and time.monotonic() < deadline:
                if select.select([master], [], [], 0.05)[0]:
                    seen += os.read(master, 4096)
                if answer is not None and not asked and b"to continue:" in seen:
                    os.write(master, answer.encode() + b"\n")
                    asked = True
            if process.poll() is None:
                process.kill()
                self.fail("the script did not finish: is it waiting for an answer?\n" + seen.decode())
            while select.select([master], [], [], 0)[0]:
                seen += os.read(master, 4096)
            out = process.stdout.read()
        finally:
            process.stdout.close()
            os.close(master)
            os.close(slave)
        self.asked = b"to continue:" in seen
        self.tty = seen.decode()
        return process.returncode, out + seen.decode()

    def changes(self):
        return [line for line in self.calls.read_text().splitlines()
                if line.split()[0] == "helm" or {"delete", "taint", "patch", "apply", "create", "scale"} & set(line.split()[:4])]

    def test_a_dry_run_lists_what_exists_and_changes_nothing(self):
        before = plain_install()
        del before["objects"]["lab/secret/homestead-setup"]  # one that is not there is not listed
        self.hold.write_text("// Homestead installs updates on this host itself (Nodes > OS updates), one host at a time.\n")
        code, out = self.uninstall(before, "--uninstall", "--dry-run")
        self.assertEqual(0, code, out)
        self.assertEqual([], self.changes(), "a dry run changes nothing")
        self.assertEqual(before, self.state())
        self.assertFalse(self.asked, "a dry run does not ask")
        self.assertTrue(self.hold.exists())
        for name in ("deployment lab/homestead", "clusterrole homestead", "daemonset lab/homestead-nodeprobe",
                     "secret lab/homestead-smart-key", "recurringjobs.longhorn.io longhorn-system/homestead-weekly-trim",
                     "homestead.io/standby on node-a", "homestead-rebuild on Longhorn volume vol-a", str(self.hold)):
            self.assertIn(name, out)
        self.assertNotIn("secret lab/homestead-setup", out)
        self.assertNotIn("node-b", out)
        self.assertNotIn("vol-b", out)
        for never in ("homestead-objectstore", "homestead-smb", "lab/web", "homestead-backup-target"):
            self.assertNotIn(never, out.split("What stays")[0])
        for data in ("lab/homestead-data", "lab/homestead-auth", "lab/homestead-settings"):
            self.assertNotIn(data, out, "the data is only listed with --everything")
        self.assertIn("nothing was changed", out.lower())

    def test_yes_removes_exactly_that_list_and_nothing_else(self):
        code, out = self.uninstall(plain_install(), "--uninstall", answer="yes")
        self.assertEqual(0, code, out)
        left = set(self.state()["objects"])
        self.assertEqual(set(), left & self.REMOVED, out)
        self.assertEqual(self.NEVER | self.KEPT_DATA, left, "nothing else goes, and the app stays")
        self.assertEqual({"app": "web", "managed": "true"}, self.state()["objects"]["lab/deployment/web"])
        self.assertEqual(["homestead.io/storage-pending"], self.state()["nodes"]["node-a"])
        self.assertEqual(["other/taint"], self.state()["nodes"]["node-b"])
        self.assertEqual({"vol-a": ["csi-x"], "vol-b": ["csi-y"]}, self.state()["tickets"])
        self.assertEqual([], self.state().get("namespaces_deleted", []))

    def test_nothing_is_ever_deleted_by_label_or_in_bulk(self):
        code, out = self.uninstall(plain_install(), "--uninstall", "--everything", answer="everything")
        self.assertEqual(0, code, out)
        self.assertGreater(len(self.changes()), 20, "the run removed things, or this proves nothing")
        for line in self.changes():
            words = line.split()
            for bad in ("-l", "--selector", "--all", "-A", "--all-namespaces"):
                self.assertNotIn(bad, words, line)

    def test_the_question_says_the_word_it_wants(self):
        code, out = self.uninstall(plain_install(), "--uninstall", answer="no")
        self.assertTrue(self.asked)
        self.assertIn("deployment lab/homestead", out)
        self.assertIn("Type yes to continue:", out)

    def test_anything_but_yes_removes_nothing(self):
        for answer in ("no", "", "y", "YES"):
            with self.subTest(answer=answer):
                self.calls.write_text("")
                before = plain_install()
                code, out = self.uninstall(before, "--uninstall", answer=answer)
                self.assertNotEqual(0, code, out)
                self.assertEqual([], self.changes())
                self.assertEqual(before, self.state())
                self.assertIn("Nothing was removed", out)

    def test_without_a_terminal_it_will_not_guess_a_yes(self):
        before = plain_install()
        code, out = self.uninstall(before, "--uninstall", terminal=False)
        self.assertNotEqual(0, code, out)
        self.assertEqual([], self.changes())
        self.assertEqual(before, self.state())
        self.assertIn("--dry-run", out)
        self.assertIn("deployment lab/homestead", out, "it still shows what it would have removed")

    def test_run_again_it_finds_nothing_and_exits_cleanly(self):
        self.uninstall(plain_install(), "--uninstall", answer="yes")
        after = self.state()
        self.calls.write_text("")
        code, out = self.uninstall(after, "--uninstall")
        self.assertEqual(0, code, out)
        self.assertFalse(self.asked, "nothing to remove: nothing to ask")
        self.assertEqual([], self.changes())
        self.assertIn("Nothing of Homestead's is left to remove", out)
        self.assertIn("lab", out, "it says where it looked")

    def test_homestead_stops_first_so_it_cannot_put_things_back(self):
        code, out = self.uninstall(plain_install(), "--uninstall", answer="yes")
        self.assertEqual(0, code, out)
        changes = self.changes()
        self.assertIn("delete deployment homestead", changes[0])
        calls = self.calls.read_text().splitlines()
        polled = next(i for i, line in enumerate(calls) if "get pods" in line)
        second = calls.index(changes[1])
        self.assertLess(polled, second, "Homestead's pods are seen gone before anything else goes")
        self.assertLess(changes.index(next(c for c in changes if "clusterrolebinding" in c)),
                        changes.index(next(c for c in changes if "delete clusterrole homestead" in c)))

    def test_pods_that_do_not_stop_stop_the_run_there(self):
        state = plain_install()
        state["pods_stay"] = True
        code, out = self.uninstall(state, "--uninstall", answer="yes")
        self.assertNotEqual(0, code, out)
        self.assertIn("still", out)
        self.assertIn("run this again", out.lower())
        self.assertTrue(self.REMOVED - {"lab/deployment/homestead"} <= set(self.state()["objects"]),
                        "nothing but its Deployment has gone")

    def test_a_helm_install_goes_through_helm(self):
        state = plain_install()
        for name in ("lab/deployment/homestead", "lab/service/homestead", "_/clusterrole/homestead"):
            state["objects"][name]["release"] = "my-homestead"
        code, out = self.uninstall(state, "--uninstall", answer="yes")
        self.assertEqual(0, code, out)
        first = self.changes()[0]
        self.assertIn("helm uninstall my-homestead -n lab", first)
        self.assertIn("KUBECONFIG=/etc/rancher/k3s/k3s.yaml", first)
        self.assertEqual(set(), set(self.state()["objects"]) & self.REMOVED)
        self.assertNotIn("delete deployment homestead", "\n".join(self.changes()))
        self.assertEqual(self.NEVER | self.KEPT_DATA, set(self.state()["objects"]))

    def test_a_helm_install_without_helm_here_says_what_to_run(self):
        (self.bin / "helm").unlink()
        state = plain_install()
        state["objects"]["lab/deployment/homestead"]["release"] = "my-homestead"
        if shutil.which("helm"):  # one on this machine would be found
            self.skipTest("helm is on this machine's PATH")
        code, out = self.uninstall(state, "--uninstall", answer="yes")
        self.assertNotEqual(0, code, out)
        self.assertFalse(self.asked)
        self.assertEqual(state, self.state())
        self.assertIn("helm uninstall my-homestead -n lab", out)
        code, out = self.uninstall(state, "--uninstall", "--dry-run")
        self.assertEqual(0, code, out)
        self.assertIn("helm uninstall my-homestead -n lab", out)

    def foreign(self):
        state = plain_install()
        state["objects"]["lab/deployment/homestead"] = {"app": "something-else", "apps_ns": "lab"}
        state["objects"]["lab/service/homestead"] = {"app": "something-else"}
        state["pods"] = {}
        return state

    def test_a_deployment_of_that_name_that_is_not_homesteads_refuses_the_whole_run(self):
        # It may mount homestead-data: nothing here shows that claim is Homestead's.
        for args, answer in ((["--uninstall"], "yes"), (["--uninstall", "--everything"], "everything"),
                             (["--uninstall", "--dry-run"], None)):
            with self.subTest(args=args):
                self.calls.write_text("")
                state = self.foreign()
                code, out = self.uninstall(state, *args, answer=answer)
                self.assertNotEqual(0, code, out)
                self.assertFalse(self.asked)
                self.assertEqual([], self.changes())
                self.assertEqual(state, self.state())
                self.assertIn("deployment lab/homestead", out)
                self.assertIn("not Homestead's", out)
                self.assertIn("Nothing has been removed", out)
                self.assertIn("What-Homestead-changes", out)
                self.assertNotIn("  serviceaccount lab/homestead", out, "what it says it leaves is not on the list")
                self.assertNotIn("  clusterrole homestead", out)
                self.assertNotIn("HS_NAMESPACE", out, "there is no override")

    def test_a_foreign_deployment_refuses_even_with_only_host_marks_left(self):
        # Nothing on the list, but nothing shows this cluster's marks are leftovers either.
        for marks, shown in (({"nodes": {"node-a": ["homestead.io/standby"]}, "tickets": {}}, "homestead.io/standby on node-a"),
                             ({"nodes": {}, "tickets": {"vol-a": ["homestead-rebuild"]}}, "homestead-rebuild on Longhorn volume vol-a"),
                             ({"nodes": {}, "tickets": {"vol-c": ["homestead-revert"]}}, "homestead-revert on Longhorn volume vol-c"),
                             ({"nodes": {}, "tickets": {}}, str(self.hold))):
            with self.subTest(shown=shown):
                self.calls.write_text("")
                self.hold.unlink(missing_ok=True)
                if shown == str(self.hold):
                    self.hold.write_text("// Homestead installs updates on this host itself (Nodes > OS updates).\n")
                state = self.foreign()
                state["objects"] = {k: v for k, v in state["objects"].items()
                                    if k in ("lab/deployment/homestead", "lab/persistentvolumeclaim/homestead-data")}
                state.update(marks)
                code, out = self.uninstall(state, "--uninstall", answer="yes")
                self.assertNotEqual(0, code, out)
                self.assertFalse(self.asked)
                self.assertEqual([], self.changes())
                self.assertEqual(state, self.state())
                self.assertIn(shown, out)
                self.assertIn("Nothing has been removed", out)
                self.assertNotIn("has been removed.", out.replace("Nothing has been removed.", ""))
                if shown == str(self.hold):
                    self.assertTrue(self.hold.exists())
        self.hold.unlink(missing_ok=True)

    def test_a_foreign_deployment_with_nothing_else_to_do_is_simply_nothing(self):
        state = self.foreign()
        state["objects"] = {k: v for k, v in state["objects"].items()
                            if k in ("lab/deployment/homestead", "lab/persistentvolumeclaim/homestead-data")}
        state["nodes"], state["tickets"] = {}, {}
        code, out = self.uninstall(state, "--uninstall")
        self.assertEqual(0, code, out)
        self.assertIn("Nothing of Homestead's is left to remove", out)
        self.assertEqual(state, self.state())

    def test_a_service_that_is_not_homesteads_stays_while_homestead_goes(self):
        state = plain_install()
        state["objects"]["lab/service/homestead"] = {"app": "something-else"}
        code, out = self.uninstall(state, "--uninstall", answer="yes")
        self.assertEqual(0, code, out)
        left = self.state()["objects"]
        self.assertIn("lab/service/homestead", left)
        self.assertNotIn("lab/deployment/homestead", left)

    def test_a_binding_for_someone_else_stays_with_its_role(self):
        for subject in ("ServiceAccount|someone|lab", "ServiceAccount|homestead|other", "User|homestead|",
                        "ServiceAccount|homestead|lab\nUser|someone|", ""):
            with self.subTest(subject=subject):
                state = plain_install()
                state["objects"]["_/clusterrolebinding/homestead"] = {"subject": subject}
                code, out = self.uninstall(state, "--uninstall", answer="yes")
                self.assertEqual(0, code, out)
                left = self.state()["objects"]
                self.assertIn("_/clusterrolebinding/homestead", left)
                self.assertIn("_/clusterrole/homestead", left)
                self.assertNotIn("lab/deployment/homestead", left)
                self.assertIn("clusterrolebinding homestead", out)
                self.assertIn("not Homestead's", out)

    def test_a_role_whose_binding_already_went_can_still_go(self):
        state = plain_install()
        del state["objects"]["_/clusterrolebinding/homestead"]
        code, out = self.uninstall(state, "--uninstall", answer="yes")
        self.assertEqual(0, code, out)
        self.assertNotIn("_/clusterrole/homestead", self.state()["objects"])

    def test_with_no_homestead_and_no_data_volume_it_refuses_to_guess(self):
        state = {"objects": {"_/clusterrole/homestead": {}, "_/clusterrolebinding/homestead": {},
                             "lab/serviceaccount/homestead": {}},
                 "pods": {}, "nodes": {}, "tickets": {}, "workloads": [], "vms": [], "no_kubevirt": True}
        for args in (["--uninstall", "--dry-run"], ["--uninstall"]):
            with self.subTest(args=args):
                code, out = self.uninstall(state, *args, answer="yes")
                self.assertNotEqual(0, code, out)
                self.assertFalse(self.asked)
                self.assertEqual(state, self.state())
                self.assertEqual([], self.changes())
                self.assertIn("Nothing has been removed", out)
                self.assertIn("serviceaccount lab/homestead", out, "it names what it would not touch")
                self.assertIn("What-Homestead-changes", out, "and where the by-hand steps are")
                self.assertNotIn("Homestead has been removed", out)

    def test_it_only_says_homestead_was_removed_when_it_removed_homestead(self):
        code, out = self.uninstall(plain_install(), "--uninstall", answer="yes")
        self.assertIn("Homestead has been removed", out)
        code, out = self.uninstall(self.state(), "--uninstall", "--everything", answer="everything")
        self.assertEqual(0, code, out)
        self.assertNotIn("==> Homestead has been removed", out)
        self.assertIn("What was left of Homestead has been removed", out)

    def test_the_end_of_a_run_repeats_that_the_apps_namespace_was_a_guess(self):
        state = plain_install()
        del state["objects"]["lab/deployment/homestead"]
        state["pods"] = {}
        code, out = self.uninstall(state, "--uninstall", answer="yes")
        self.assertEqual(0, code, out)
        tail = out[out.index("What was left of Homestead has been removed"):]
        self.assertIn("HS_APPS_NAMESPACE", tail)

    def test_a_cluster_with_nothing_of_homesteads_is_simply_nothing(self):
        state = {"objects": {"lab/deployment/web": {"app": "web"}}, "pods": {}, "nodes": {}, "tickets": {},
                 "workloads": [], "vms": [], "no_kubevirt": True}
        code, out = self.uninstall(state, "--uninstall")
        self.assertEqual(0, code, out)
        self.assertIn("Nothing of Homestead's is left to remove", out)

    def test_after_everything_a_second_run_finds_nothing(self):
        state = helm_shaped()
        code, out = self.uninstall(state, "--uninstall", "--everything", answer="everything",
                                   env={"HS_APPS_NAMESPACE": "lab"})
        self.assertEqual(0, code, out)
        self.calls.write_text("")
        code, out = self.uninstall(self.state(), "--uninstall", "--everything")
        self.assertEqual(0, code, out)
        self.assertFalse(self.asked)
        self.assertIn("Nothing of Homestead's is left to remove", out)

    def test_a_failed_everything_keeps_its_data_volume_so_a_rerun_can_finish(self):
        for state, name in ((plain_install(), "homestead-auth"), (helm_shaped(), "homestead-leader")):
            with self.subTest(name=name):
                own = "hs" if name == "homestead-leader" else "lab"
                state["delete_fail"] = [name]
                code, out = self.uninstall(state, "--uninstall", "--everything", answer="everything")
                self.assertEqual(1, code, out)
                self.assertIn(f"{own}/persistentvolumeclaim/homestead-data", self.state()["objects"],
                              "the data volume is the proof a later run needs")
                self.assertIn(f"{own}/homestead-data", out)
                self.assertIn("Could not remove", out)
                after = self.state()
                after["delete_fail"] = []
                code, out = self.uninstall(after, "--uninstall", "--everything", answer="everything")
                self.assertEqual(0, code, out)
                left = [k for k in self.state()["objects"] if name in k or k.endswith("/homestead-data")]
                self.assertEqual([], left, "the rerun finishes the job")

    def test_the_data_volume_is_the_last_thing_on_the_list(self):
        text = SCRIPT.read_text(encoding="utf-8")
        block = text[text.index("UNINSTALL_OBJECTS='"):]
        block = block[block.index("\n") + 1:block.index("\n'\n")].splitlines()
        self.assertEqual("data own persistentvolumeclaim homestead-data", block[-1])

    def test_the_namespace_is_not_deleted_on_a_run_with_no_proof(self):
        state = {"objects": {}, "pods": {}, "nodes": {"node-a": ["homestead.io/standby"]}, "tickets": {},
                 "workloads": [], "vms": [], "no_kubevirt": True}
        code, out = self.uninstall(state, "--uninstall", "--everything", answer="everything", env={"HS_NAMESPACE": "prod"})
        self.assertEqual(0, code, out)
        self.assertEqual([], self.state().get("namespaces_deleted", []))
        self.assertEqual([], self.state()["nodes"]["node-a"], "the taint has its own mark")
        self.assertNotIn("namespace prod", out.lower())

    def test_an_apps_namespace_that_disagrees_with_the_deployment_is_refused(self):
        state = helm_shaped()
        code, out = self.uninstall(state, "--uninstall", answer="yes", env={"HS_APPS_NAMESPACE": "other"})
        self.assertNotEqual(0, code, out)
        self.assertFalse(self.asked)
        self.assertEqual(state, self.state())
        self.assertIn("HS_APPS_NAMESPACE says other", out)
        code, out = self.uninstall(helm_shaped(), "--uninstall", "--dry-run", env={"HS_APPS_NAMESPACE": "lab"})
        self.assertEqual(0, code, out)

    def test_two_data_volumes_are_not_chosen_between(self):
        state = plain_install()
        del state["objects"]["lab/deployment/homestead"]
        state["objects"]["other/persistentvolumeclaim/homestead-data"] = {}
        code, out = self.uninstall(state, "--uninstall", answer="yes")
        self.assertNotEqual(0, code, out)
        self.assertFalse(self.asked)
        self.assertEqual(state, self.state())
        self.assertIn("HS_NAMESPACE", out)
        code, out = self.uninstall(state, "--uninstall", "--dry-run", env={"HS_NAMESPACE": "other"})
        self.assertEqual(0, code, out)
        self.assertIn("Removing Homestead", out)

    def test_a_namespace_named_that_holds_no_homestead_is_refused_while_another_does(self):
        state = helm_shaped()
        code, out = self.uninstall(state, "--uninstall", answer="yes", env={"HS_NAMESPACE": "lab"})
        self.assertNotEqual(0, code, out)
        self.assertFalse(self.asked)
        self.assertEqual(state, self.state(), "a running Homestead keeps its permissions")
        self.assertIn("hs", out)
        self.assertIn("HS_NAMESPACE", out)
        self.assertNotIn("Homestead has been removed", out)

    def test_a_namespace_named_that_holds_no_data_volume_is_refused_while_another_does(self):
        state = plain_install()
        del state["objects"]["lab/deployment/homestead"]
        code, out = self.uninstall(state, "--uninstall", answer="yes", env={"HS_NAMESPACE": "other"})
        self.assertNotEqual(0, code, out)
        self.assertFalse(self.asked)
        self.assertEqual(state, self.state())
        self.assertIn("HS_NAMESPACE says other", out)

    def test_a_namespace_named_that_matches_goes_ahead(self):
        code, out = self.uninstall(helm_shaped(), "--uninstall", answer="yes", env={"HS_NAMESPACE": "hs"})
        self.assertEqual(0, code, out)
        self.assertNotIn("hs/deployment/homestead", self.state()["objects"])

    def test_the_apps_namespace_comes_from_the_deployment_and_lab_is_looked_in_too(self):
        state = helm_shaped()
        state["objects"]["hs/deployment/homestead"]["apps_ns"] = "apps"
        state["objects"]["apps/secret/homestead-smart-key"] = state["objects"].pop("lab/secret/homestead-smart-key")
        code, out = self.uninstall(state, "--uninstall", answer="yes")
        self.assertEqual(0, code, out)
        left = self.state()["objects"]
        self.assertNotIn("apps/secret/homestead-smart-key", left, "where the Deployment says apps are")
        self.assertNotIn("lab/daemonset/homestead-nodeprobe", left, "and lab, which some of the code names outright")

    def test_with_the_deployment_gone_it_says_it_can_only_guess_the_apps_namespace(self):
        state = plain_install()
        del state["objects"]["lab/deployment/homestead"]
        state["pods"] = {}
        for args, answer in ((["--uninstall", "--dry-run"], None), (["--uninstall"], "yes")):
            with self.subTest(args=args):
                code, out = self.uninstall(state, *args, answer=answer)
                self.assertEqual(0, code, out)
                self.assertIn("HS_APPS_NAMESPACE", out)
        code, out = self.uninstall(plain_install(), "--uninstall", "--dry-run")
        self.assertNotIn("HS_APPS_NAMESPACE", out, "with the Deployment there it knows")

    def test_two_homesteads_are_refused_even_when_one_is_named(self):
        # They share a ClusterRole: removing one would break the other.
        state = plain_install()
        state["objects"]["other/deployment/homestead"] = {"app": "homestead", "apps_ns": "other"}
        for env in ({}, {"HS_NAMESPACE": "other"}, {"HS_NAMESPACE": "lab"}):
            with self.subTest(env=env):
                code, out = self.uninstall(state, "--uninstall", answer="yes", env=env)
                self.assertNotEqual(0, code, out)
                self.assertFalse(self.asked)
                self.assertEqual(state, self.state())
                self.assertIn("lab", out)
                self.assertIn("other", out)
                self.assertNotIn("Homestead has been removed", out)

    def test_helm_failing_stops_the_run_there(self):
        state = plain_install()
        state["objects"]["lab/deployment/homestead"]["release"] = "my-homestead"
        state["helm_fails"] = True
        code, out = self.uninstall(state, "--uninstall", answer="yes")
        self.assertNotEqual(0, code, out)
        self.assertEqual(state["objects"], self.state()["objects"])
        self.assertIn("helm uninstall did not finish", out)
        self.assertNotIn("Homestead has been removed", out)

    def test_a_deployment_that_will_not_delete_stops_the_run_there(self):
        state = plain_install()
        state["delete_fail"] = ["deployment"]
        code, out = self.uninstall(state, "--uninstall", answer="yes")
        self.assertNotEqual(0, code, out)
        self.assertEqual(state["objects"], self.state()["objects"])
        self.assertIn("Nothing else has been removed", out)

    def test_a_helm_failure_that_only_mentions_not_found_stops_the_run(self):
        state = plain_install()
        state["objects"]["lab/deployment/homestead"]["release"] = "my-homestead"
        state["helm_partial"] = True
        code, out = self.uninstall(state, "--uninstall", answer="yes")
        self.assertNotEqual(0, code, out)
        self.assertEqual(state["objects"], self.state()["objects"])
        self.assertIn("helm uninstall did not finish", out)
        self.assertNotIn("no record of that release", out)
        self.assertNotIn("delete deployment homestead", self.calls.read_text())

    def test_a_deployment_that_will_not_delete_stops_the_run_even_with_no_pods(self):
        state = plain_install()
        state["delete_fail"] = ["deployment"]
        state["pods"] = {}  # at 0 replicas, so the pod check alone would pass
        code, out = self.uninstall(state, "--uninstall", answer="yes")
        self.assertNotEqual(0, code, out)
        self.assertEqual(state["objects"], self.state()["objects"])
        self.assertIn("Could not remove Homestead's Deployment", out)

    def test_pods_it_cannot_read_stop_the_run(self):
        state = plain_install()
        state["forbid"] = ["pods"]
        code, out = self.uninstall(state, "--uninstall", answer="yes")
        self.assertNotEqual(0, code, out)
        left = set(self.state()["objects"])
        self.assertEqual({"lab/deployment/homestead"}, set(state["objects"]) - left)
        self.assertIn("Could not check that Homestead has stopped", out)

    def test_a_helm_release_with_no_record_does_not_loop(self):
        state = plain_install()
        state["objects"]["lab/deployment/homestead"]["release"] = "my-homestead"
        state["helm_no_release"] = True
        code, out = self.uninstall(state, "--uninstall", answer="yes")
        self.assertEqual(0, code, out)
        self.assertNotIn("lab/deployment/homestead", self.state()["objects"])
        self.assertIn("no record of that release", out)
        self.assertIn("-n lab delete deployment homestead", self.calls.read_text())

    def test_it_warns_that_something_managing_homestead_will_put_it_back(self):
        code, out = self.uninstall(plain_install(), "--uninstall", "--dry-run")
        flat = " ".join(out.split())
        self.assertIn("Flux, Argo CD or a k3s HelmChart", flat)

    def test_a_taint_or_ticket_that_cannot_be_removed_is_named(self):
        for key, name in (("taint_fail", "taint/node-a"), ("patch_fail", "ticket/vol-a")):
            with self.subTest(key=key):
                state = plain_install()
                state[key] = True
                code, out = self.uninstall(state, "--uninstall", answer="yes")
                self.assertEqual(1, code, out)
                self.assertIn("Could not remove", out)
                self.assertIn(name, out)
                self.assertNotIn("Homestead has been removed", out)

    def test_a_ticket_goes_by_its_key_whatever_id_it_holds(self):
        state = plain_install()
        code, out = self.uninstall(state, "--uninstall", "--dry-run")
        self.assertIn("attachment ticket homestead-rebuild on Longhorn volume vol-a", out)

    def test_a_revert_ticket_stays_while_homestead_may_still_be_rolling_back(self):
        state = plain_install()
        state["tickets"] = {"vol-c": ["homestead-revert", "csi-z"], "vol-a": ["homestead-rebuild"]}
        code, out = self.uninstall(state, "--uninstall", answer="yes")
        self.assertEqual(0, code, out)
        self.assertEqual(["homestead-revert", "csi-z"], self.state()["tickets"]["vol-c"])
        self.assertEqual([], self.state()["tickets"]["vol-a"], "a rebuild's ticket is only a leftover")
        self.assertEqual(2, out.count("homestead-revert on vol-c"), "named before the question and again at the end")
        flat = " ".join(out.split())
        self.assertIn("wait at least 5 minutes after any rollback", flat)
        self.assertIn("The app on each may be stopped", flat)
        self.assertLess(out.rindex("homestead-revert on vol-c"), out.index("Homestead has been removed"))
        # With Homestead gone, what is left is a leftover: the second run takes it.
        self.calls.write_text("")
        code, out = self.uninstall(self.state(), "--uninstall", answer="yes")
        self.assertEqual(0, code, out)
        self.assertEqual(["csi-z"], self.state()["tickets"]["vol-c"])

    def test_its_namespace_is_found_from_the_data_volume_once_it_is_gone(self):
        state = plain_install()
        del state["objects"]["lab/deployment/homestead"]
        state["objects"]["hs/persistentvolumeclaim/homestead-data"] = state["objects"].pop("lab/persistentvolumeclaim/homestead-data")
        state["objects"]["hs/secret/homestead-setup"] = state["objects"].pop("lab/secret/homestead-setup")
        code, out = self.uninstall(state, "--uninstall", "--dry-run")
        self.assertEqual(0, code, out)
        self.assertIn("secret hs/homestead-setup", out)
        self.assertIn("daemonset lab/homestead-nodeprobe", out)

    def test_a_read_that_times_out_says_so(self):
        state = plain_install()
        self.stub("timeout", '#!/bin/sh\nexit 124\n')
        code, out = self.uninstall(state, "--uninstall", "--dry-run")
        self.assertNotEqual(0, code, out)
        self.assertIn("no answer in 60 seconds", out)

    def test_something_it_cannot_read_stops_it_before_it_asks(self):
        state = plain_install()
        state["forbid"] = ["secret"]
        code, out = self.uninstall(state, "--uninstall", answer="yes")
        self.assertNotEqual(0, code, out)
        self.assertFalse(self.asked)
        self.assertEqual(state, self.state())
        self.assertIn("Forbidden", out)
        self.assertIn("Nothing has been removed", out)

    def test_a_host_list_or_ticket_list_it_cannot_read_stops_it_too(self):
        # "No hot spares" and "not allowed to look" must not read the same.
        for kind in ("nodes", "volumeattachments.longhorn.io", "deployments,statefulsets,cronjobs",
                     "virtualmachines.kubevirt.io"):
            with self.subTest(kind=kind):
                self.calls.write_text("")
                state = plain_install()
                state["forbid"] = [kind]
                state["no_kubevirt"] = False
                code, out = self.uninstall(state, "--uninstall", "--dry-run")
                self.assertNotEqual(0, code, out)
                self.assertIn("Forbidden", out)
                self.assertIn("Nothing has been removed", out)
                self.assertNotIn("nothing was changed", out.lower(), "it stopped, it did not finish a review")
                code, out = self.uninstall(state, "--uninstall", answer="yes")
                self.assertNotEqual(0, code, out)
                self.assertFalse(self.asked)
                self.assertEqual(state, self.state())

    def test_not_being_able_to_check_whose_it_is_is_not_the_same_as_not_homesteads(self):
        for kind in ("deployment", "service", "clusterrolebinding"):
            with self.subTest(kind=kind):
                state = plain_install()
                state["identity_forbidden"] = [kind]
                code, out = self.uninstall(state, "--uninstall", answer="yes")
                self.assertNotEqual(0, code, out)
                self.assertFalse(self.asked)
                self.assertEqual(state, self.state())
                self.assertNotIn("not Homestead's", out)
                self.assertIn("Forbidden", out)

    def test_a_second_run_still_waits_for_pods_the_first_left_stopping(self):
        state = plain_install()
        del state["objects"]["lab/deployment/homestead"]
        state["pods_stay"] = True
        code, out = self.uninstall(state, "--uninstall", answer="yes")
        self.assertNotEqual(0, code, out)
        self.assertIn("still there", out)
        self.assertEqual(state["objects"], self.state()["objects"], "nothing else goes while Homestead may still run")

    def test_no_longhorn_is_not_an_error(self):
        state = plain_install()
        state["no_longhorn"] = True
        code, out = self.uninstall(state, "--uninstall", "--dry-run")
        self.assertEqual(0, code, out)
        self.assertNotIn("recurringjobs", out)
        self.assertNotIn("attachment ticket", out)

    def test_it_warns_before_starting_about_apps_homestead_stopped(self):
        state = plain_install()
        state["no_kubevirt"] = False
        state["workloads"] = [
            {"kind": "Deployment", "ns": "lab", "name": "parked", "replicas": 0, "schedule": "2"},
            {"kind": "Deployment", "ns": "lab", "name": "restructuring", "replicas": 0, "restructure": "2"},
            {"kind": "CronJob", "ns": "lab", "name": "nightly", "suspend": "true", "held": "false"},
            {"kind": "CronJob", "ns": "lab", "name": "their-cron", "suspend": "true"},
            {"kind": "CronJob", "ns": "lab", "name": "cron-up", "suspend": "false"},
            {"kind": "StatefulSet", "ns": "lab", "name": "held", "replicas": 0, "held": "3"},
            {"kind": "Deployment", "ns": "lab", "name": "running", "replicas": 2, "schedule": "2"},
            {"kind": "Deployment", "ns": "lab", "name": "theirs", "replicas": 0},
        ]
        state["vms"] = [{"ns": "lab", "name": "vm-stopped", "running": "false", "held": "true"},
                        {"ns": "lab", "name": "vm-halted", "strategy": "Halted", "autostart": "1"},
                        {"ns": "lab", "name": "vm-up", "running": "true", "schedule": "1"}]
        code, out = self.uninstall(state, "--uninstall", answer="no")
        warning = out[:out.index("to continue:")]
        for name in ("Deployment lab/parked", "StatefulSet lab/held", "VirtualMachine lab/vm-stopped",
                     "VirtualMachine lab/vm-halted", "Deployment lab/restructuring", "CronJob lab/nightly"):
            self.assertIn(name, warning)
        for name in ("lab/running", "lab/theirs", "lab/vm-up", "lab/their-cron", "lab/cron-up"):
            self.assertNotIn(name, warning)
        self.assertIn("stay stopped", warning)

    def test_what_a_job_holds_is_named_whatever_state_it_is_in_and_never_refused(self):
        state = plain_install()
        state["no_kubevirt"] = False
        state["workloads"] = [
            {"kind": "Deployment", "ns": "lab", "name": "being-held", "replicas": 3, "job": "node-power:1"},
            {"kind": "StatefulSet", "ns": "lab", "name": "held-set", "replicas": 0, "job": "os-rollout:2", "held": "1"},
            {"kind": "CronJob", "ns": "lab", "name": "held-cron", "suspend": "false", "job": "node-power:1"},
        ]
        state["vms"] = [{"ns": "lab", "name": "held-vm", "running": "true", "job": "node-power:1"}]
        code, out = self.uninstall(state, "--uninstall", answer="yes")
        self.assertEqual(0, code, "warn and name, never refuse: " + out)
        warning = out[:out.index("What stays")]
        for name in ("Deployment lab/being-held (a job holds this)", "StatefulSet lab/held-set (a job holds this)",
                     "CronJob lab/held-cron (a job holds this)", "VirtualMachine lab/held-vm (a job holds this)"):
            self.assertIn(name, warning)

    def test_the_warning_says_jobs_too(self):
        code, out = self.uninstall(plain_install(), "--uninstall", "--dry-run")
        flat = " ".join(out.split())
        self.assertIn("no OS update, host reboot, cluster shutdown or other Homestead job", flat)

    def test_it_says_a_reinstall_puts_the_node_probe_back(self):
        code, out = self.uninstall(plain_install(), "--uninstall", "--dry-run")
        flat = " ".join(out.split())
        self.assertIn("A reinstall keeping this data puts the node probe back", flat)
        self.assertIn("a release without this fix", flat)
        self.assertIn("Settings > Cluster > Add-ons", flat)
        self.assertIn("nodeprobe.enabled", flat)

    def test_the_messages_say_how_to_run_it_with_sudo(self):
        (self.bin / "helm").unlink()
        state = plain_install()
        state["objects"]["lab/deployment/homestead"]["release"] = "my-homestead"
        if shutil.which("helm"):
            self.skipTest("helm is on this machine's PATH")
        code, out = self.uninstall(state, "--uninstall", answer="yes")
        self.assertIn("| sudo sh -s -- --uninstall", out)

    def test_not_being_root_says_the_whole_command(self):
        self.stub("id", '#!/bin/sh\necho 1000\n')
        code, out = self.uninstall(plain_install(), "--uninstall", answer="yes")
        self.assertNotEqual(0, code, out)
        self.assertIn("| sudo sh -s -- --uninstall", out)
        self.assertEqual([], self.changes())

    def test_the_three_minute_message_gives_the_force_delete_and_its_condition(self):
        state = plain_install()
        state["pods_stay"] = True
        code, out = self.uninstall(state, "--uninstall", answer="yes")
        self.assertNotEqual(0, code, out)
        self.assertIn("kubectl -n lab delete pod/homestead-1 --force --grace-period=0", out)
        self.assertIn("only if the host is really off", out)

    def test_it_says_what_it_leaves_behind(self):
        for args, answer in ((("--uninstall", "--dry-run"), None), (("--uninstall",), "yes")):
            with self.subTest(args=args):
                code, out = self.uninstall(plain_install(), *args, answer=answer)
                self.assertEqual(0, code, out)
                stays = out[out.rindex("What stays"):]
                self.assertIn("homestead.io/*", stays)
                self.assertIn("annotations", stays)
                self.assertIn("apps", stays)
                self.assertIn("Longhorn", stays)
                self.assertIn("What-Homestead-changes", stays)
                self.assertIn("--everything", stays, "it says how the data can go too")

    def test_everything_needs_its_own_word_and_removes_the_data_too(self):
        before = plain_install()
        code, out = self.uninstall(before, "--uninstall", "--everything", answer="yes")
        self.assertNotEqual(0, code, out)
        self.assertEqual(before, self.state(), "yes is not enough for the data volume")
        self.assertIn("Nothing was removed", out)

        code, out = self.uninstall(plain_install(), "--uninstall", "--everything", answer="everything")
        self.assertEqual(0, code, out)
        self.assertIn("lab/homestead-data", out.split("to continue:")[0])
        self.assertEqual(self.NEVER, set(self.state()["objects"]), "the data goes; the apps do not")

    def test_homesteads_own_address_goes_only_with_everything_and_only_if_it_selects_homestead(self):
        code, out = self.uninstall(plain_install(), "--uninstall", answer="yes")
        self.assertEqual(0, code, out)
        self.assertIn("lab/service/homestead-vip", self.state()["objects"])
        state = plain_install()
        state["objects"]["lab/service/homestead-vip"] = {"app": "something-else"}
        code, out = self.uninstall(state, "--uninstall", "--everything", answer="everything")
        self.assertEqual(0, code, out)
        self.assertIn("lab/service/homestead-vip", self.state()["objects"])
        self.assertIn("not Homestead's", out)

    def test_the_question_names_the_data_volume(self):
        code, out = self.uninstall(plain_install(), "--uninstall", "--everything", answer="no")
        question = out[out.index("Remove Homestead"):]
        self.assertIn("lab/homestead-data", question)

    def moved(self):
        state = plain_install()
        state["objects"]["lab/deployment/homestead"]["claim"] = "my-own-claim"
        state["objects"]["lab/persistentvolumeclaim/my-own-claim"] = {}
        return state

    def test_a_claim_homestead_moved_to_is_named_and_never_deleted(self):
        code, out = self.uninstall(self.moved(), "--uninstall", "--everything", answer="everything")
        self.assertEqual(0, code, out)
        left = self.state()["objects"]
        self.assertIn("lab/persistentvolumeclaim/my-own-claim", left)
        self.assertIn("lab/persistentvolumeclaim/homestead-data", left, "with the data elsewhere, this one is not known to be spare")
        self.assertIn("lab/my-own-claim holds the data", out)
        self.assertNotIn("  persistentvolumeclaim lab/homestead-data", out, "not on the list")
        self.assertNotIn("delete persistentvolumeclaim", self.calls.read_text())
        self.assertNotIn("data volume", self.tty, "the question names no volume it will not delete")

    def test_a_moved_claim_is_named_on_a_default_run_with_what_a_later_run_will_do(self):
        code, out = self.uninstall(self.moved(), "--uninstall", answer="yes")
        self.assertEqual(0, code, out)
        self.assertIn("lab/my-own-claim holds the data", out)
        flat = " ".join(out.split())
        self.assertIn("A later run cannot recognise this", flat)
        self.assertIn("choose --everything on this run", flat)
        self.assertNotIn("To remove those too, run this with --everything", flat, "no promise a later run can break")
        # And it does what it said: the old claim is offered, listed and typed.
        code, out = self.uninstall(self.state(), "--uninstall", "--everything", answer="everything")
        self.assertEqual(0, code, out)
        self.assertIn("lab/homestead-data", self.tty)
        self.assertNotIn("lab/persistentvolumeclaim/homestead-data", self.state()["objects"])
        self.assertIn("lab/persistentvolumeclaim/my-own-claim", self.state()["objects"])

    def test_with_the_deployment_gone_the_data_volume_is_on_the_everything_list(self):
        state = plain_install()
        del state["objects"]["lab/deployment/homestead"]
        state["pods"] = {}
        code, out = self.uninstall(state, "--uninstall", "--everything", answer="everything")
        self.assertEqual(0, code, out)
        self.assertNotIn("lab/persistentvolumeclaim/homestead-data", self.state()["objects"])

    def test_a_moved_install_is_not_promised_a_rerun_that_will_be_refused(self):
        state = self.moved()
        del state["objects"]["lab/persistentvolumeclaim/homestead-data"]
        state["pods_stay"] = True
        code, out = self.uninstall(state, "--uninstall", answer="yes")
        self.assertNotEqual(0, code, out)
        flat = " ".join(out.split())
        self.assertIn("A later run cannot recognise this", flat)
        self.assertNotIn("Run this again once they have gone", flat)
        self.assertIn("What-Homestead-changes", flat)
        # The next run does refuse, as it said.
        after = self.state()
        after["pods"], after["pods_stay"] = {}, False
        code, out = self.uninstall(after, "--uninstall", answer="yes")
        self.assertNotEqual(0, code, out)
        self.assertIn("Nothing has been removed", out)

    def test_a_failed_delete_on_a_moved_install_is_not_promised_a_rerun_either(self):
        state = self.moved()
        del state["objects"]["lab/persistentvolumeclaim/homestead-data"]
        state["delete_fail"] = ["homestead-smart-key"]
        code, out = self.uninstall(state, "--uninstall", answer="yes")
        self.assertEqual(1, code, out)
        self.assertNotIn("Run this again; it only looks", out)
        self.assertIn("A later run cannot recognise this", " ".join(out.split()))

    def test_the_lease_is_looked_for_where_homestead_runs(self):
        # server.py binds the leader to Homestead's own namespace.
        code, out = self.uninstall(helm_shaped(), "--uninstall", "--everything", answer="everything")
        self.assertEqual(0, code, out)
        self.assertNotIn("hs/lease/homestead-leader", self.state()["objects"])
        self.assertEqual(["hs"], self.state()["namespaces_deleted"])

    def test_a_kind_it_cannot_read_keeps_the_namespace(self):
        state = helm_shaped()
        state["objects"]["hs/widgets/a"] = {}
        state["forbid"] = ["widgets"]
        code, out = self.uninstall(state, "--uninstall", "--everything", answer="everything")
        self.assertEqual(0, code, out)
        self.assertEqual([], self.state().get("namespaces_deleted", []))
        self.assertIn("Namespace hs stays", out)
        self.assertIn("could not check", out)

    def test_everything_never_deletes_a_namespace_something_else_is_in(self):
        code, out = self.uninstall(plain_install(), "--uninstall", "--everything", answer="everything")
        self.assertEqual(0, code, out)
        self.assertEqual([], self.state().get("namespaces_deleted", []))
        self.assertIn("Namespace lab stays", out)
        self.assertIn("deployment/web", out)

    def test_everything_deletes_its_own_namespace_only_once_empty(self):
        state = helm_shaped()
        state["objects"]["hs/configmap/kube-root-ca.crt"] = {}
        state["objects"]["hs/serviceaccount/default"] = {}
        code, out = self.uninstall(state, "--uninstall", "--everything", answer="everything",
                                   env={"HS_APPS_NAMESPACE": "lab"})
        self.assertEqual(0, code, out)
        self.assertEqual(["hs"], self.state()["namespaces_deleted"])
        self.assertIn("lab/deployment/web", self.state()["objects"])
        self.assertEqual([], [k for k in self.state()["objects"] if k.startswith("hs/") and "kube-root" not in k and "/default" not in k])

    def test_everything_leaves_the_clusters_own_namespaces_alone(self):
        for own in ("default", "kube-system", "longhorn-system"):
            with self.subTest(own=own):
                state = plain_install()
                state["objects"] = {k: v for k, v in state["objects"].items()
                                    if k.startswith(("_/",)) or k == "lab/deployment/homestead"}
                state["objects"][f"{own}/deployment/homestead"] = state["objects"].pop("lab/deployment/homestead")
                state["pods"] = {}
                code, out = self.uninstall(state, "--uninstall", "--everything", answer="everything")
                self.assertEqual(0, code, out)
                self.assertEqual([], self.state().get("namespaces_deleted", []))
                self.assertIn(f"Namespace {own} stays", out)

    def test_the_apt_hold_goes_only_when_it_is_homesteads_and_other_hosts_are_named(self):
        text = (ROOT / "server" / "homestead_host_os.py").read_text(encoding="utf-8")
        self.assertIn("Homestead installs updates on this host itself", text, "the mark follows the server's file")
        self.hold.write_text("// Homestead installs updates on this host itself (Nodes > OS updates), one host at a time.\n")
        code, out = self.uninstall(plain_install(), "--uninstall", answer="yes")
        self.assertEqual(0, code, out)
        self.assertFalse(self.hold.exists())
        self.assertIn("each other host", out)
        self.assertIn("without being drained", out, "the page's warning is shown")

        self.hold.write_text('APT::Periodic::Unattended-Upgrade "0";\n')
        code, out = self.uninstall(plain_install(), "--uninstall", answer="yes")
        self.assertEqual(0, code, out)
        self.assertTrue(self.hold.exists(), "a file Homestead did not write stays")

    def test_a_delete_that_fails_is_named_and_the_rest_still_go(self):
        state = plain_install()
        state["delete_fail"] = ["homestead-smart-key"]
        code, out = self.uninstall(state, "--uninstall", answer="yes")
        self.assertEqual(1, code, out)
        self.assertIn("Could not remove", out)
        self.assertIn("homestead-smart-key", out)
        self.assertIn("lab/secret/homestead-smart-key", self.state()["objects"])
        self.assertNotIn("lab/role/homestead-console", self.state()["objects"])

    def test_a_host_without_credentials_says_where_to_run_it(self):
        self.stub("systemctl", SYSTEMCTL)
        (self.bin / "k3s").unlink()
        before = plain_install()
        code, out = self.uninstall(before, "--uninstall", env={"FAKE_SERVICE": "k3s-agent"})
        self.assertNotEqual(0, code, out)
        self.assertIn("server node", out)
        self.assertEqual(before, self.state())

    def test_flags_that_mean_nothing_alone_are_refused(self):
        for args in (["--everything"], ["--keep-data"], ["--uninstall", "--everything", "--keep-data"]):
            with self.subTest(args=args):
                code, out = self.uninstall(plain_install(), *args)
                self.assertEqual(2, code, out)
        code, out = self.uninstall(plain_install(), "--uninstall", "--keep-data", "--dry-run")
        self.assertEqual(0, code, out)


if __name__ == "__main__":
    unittest.main()
