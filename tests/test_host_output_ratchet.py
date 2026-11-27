"""A ratchet: what a host command or a pod printed reaches a job's message only
through homestead_operations.HostOutput.

HostOutput carries Homestead's sentence (its str) and the output apart (its
output); a job's write points (_finish, _note, the resolver wrapper) put the
sentence in the message and the output in the job's Log, and an answer to the
person who asked (server.py's error replies) shows the whole of it, as before.

The scan reads every function in server/: a name is "printed" when it is
assigned (directly, or from another printed name in the same function) from a
call that runs a command on a host or in a pod, or reads a pod's log
(RUNNERS, WRAPPERS). It flags a printed name inside: a raised exception other
than HostOutput; the message of a (status, progress, message) result; a
message= keyword or an update({"message": ...}); an assignment to
["message"]; or an argument of the job write calls (WRITERS: record,
record_phase, _finish, _note, report, _end, _fail, and start's message). KNOWN lists the places that do so today and why that is
not a job's message (an answer to the person who asked, a background check, a
marker word or an exit code); the test fails on a new place and on a KNOWN
count that is now too high, so the list only shrinks. It counts places, so one
more place in a listed function fails too.

It does not see: output passed through another function's return value or a
dict to a message built elsewhere (it reads one function at a time), a new
helper that runs commands under another name, or output in a log reader's
sources (the job's Log, which is where output belongs). Its taint is by name,
not by flow: a name assigned once from a host's output counts everywhere in
that function."""
import ast
import collections
import unittest
from pathlib import Path

SERVER = Path(__file__).resolve().parents[1] / "server"
RUNNERS = {"hostrun", "HOST", "HOSTRUN", "POD_EXEC", "pod_exec"}            # their .run(...)
WRAPPERS = {"ktext", "_sh", "_job_logs", "_job_log_tail", "run_state", "upgrade_state", "failure_reason"}
WRITERS = {"record", "record_phase", "_finish", "_note", "report", "start", "_end", "_fail"}
ALLOWED = {"HostOutput", "with_output"}

# (file, function, kind): (how many places, why it is not a host's output in a job's message)
KNOWN = {
    ("homestead_apparmor_fix.py", "tick", "raise"): (1, "a background check; its error goes to the server's log"),
    ("homestead_host_limits.py", "tick", "raise"): (1, "a background check; its error goes to the server's log"),
    ("homestead_manifests.py", "tick", "raise"): (1, "a background check; its error goes to the server's log"),
    ("homestead_disk_setup.py", "wipe", "raise"): (1, "the answer to the Disks page's setup request (admin)"),
    ("homestead_disk_setup.py", "setup", "raise"): (1, "the answer to the Disks page's setup request (admin)"),
    ("homestead_disk_setup.py", "use_os_space", "raise"): (3, "the answer to the Disks page's setup request (admin)"),
    ("homestead_disk_setup.py", "use_region", "raise"): (1, "the answer to the Disks page's setup request (admin)"),
    ("homestead_disk_v2.py", "_other_stores", "raise"): (1, "store names the host registered (not what it printed), in a review's refusal and in the runtime-maintenance and V2 disk jobs' messages"),
    ("homestead_files.py", "list_files", "raise"): (1, "the file manager's answer to the person browsing"),
    ("homestead_files.py", "read_file", "raise"): (2, "the file manager's answer to the person browsing"),
    ("homestead_files.py", "write_file", "raise"): (2, "the file manager's answer to the person browsing"),
    ("homestead_files.py", "_run", "raise"): (1, "the file manager's answer to the person browsing"),
    ("homestead_files.py", "transfer", "raise"): (1, "the file manager's answer to the person browsing"),
    ("homestead_files.py", "_transfer", "raise"): (1, "the file manager's own copy record, not the job list"),
    ("homestead_host_bonds.py", "start", "raise"): (1, "a refusal before any job exists: the answer to the admin"),
    ("homestead_host_bonds.py", "start", "write"): (1, "the message is built from the job's own ref; the scan is not flow-aware, and ref is rebuilt later in the function from the host's marker"),
    ("homestead_host_bridge.py", "start", "raise"): (2, "a refusal before any job exists: the answer to the admin"),
    ("homestead_host_bridge.py", "start", "write"): (1, "the message is built from the job's own ref; the scan is not flow-aware, and ref is rebuilt later in the function from the host's marker"),
    ("homestead_host_network_job.py", "_applied", "result"): (2, "Homestead's marker words read from the output"),
    ("homestead_host_os.py", "read", "raise"): (1, "the name of the last fact read (also in the OS rollout job's message); the host's errors go as HostOutput"),
    ("homestead_host_os.py", "set_hold", "raise"): (1, "an automatic-update hold: an alert and the OS updates record"),
    ("homestead_host_os.py", "_step", "result"): (1, "the package manager's exit code"),
    ("homestead_promote.py", "status", "result"): (1, "the reinstall's exit code"),
    ("homestead_operator_manifests.py", "_check_files", "raise"): (1, "the answer to the admin who asked"),
    ("homestead_operator_manifests.py", "upgrade", "raise"): (1, "the answer to the admin who asked"),
    ("homestead_passthrough.py", "inspect", "raise"): (1, "the answer to the admin who asked"),
    ("homestead_passthrough.py", "capture_vbios", "raise"): (1, "the answer to the admin who asked"),
    ("homestead_passthrough.py", "enable_iommu", "raise"): (1, "the answer to the admin who asked"),
    ("homestead_passthrough.py", "give", "raise"): (1, "the answer to the admin who asked"),
    ("homestead_passthrough.py", "take_back", "raise"): (1, "the answer to the admin who asked"),
    ("homestead_share_recycle.py", "_run", "raise"): (1, "the answer to the person managing a share"),
    ("homestead_snapshot_restore.py", "_walk", "raise"): (1, "the answer to the person reviewing a restore"),
    ("homestead_snapshot_restore.py", "_inspect", "raise"): (1, "the answer to the person reviewing a restore"),
}


def _printing_call(node):
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Attribute):
        return (func.attr == "run" and isinstance(func.value, ast.Name) and func.value.id in RUNNERS) or func.attr in WRAPPERS
    return isinstance(func, ast.Name) and func.id in WRAPPERS


def _names(node):
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _called(node):
    func = node.func
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")


def scan(tree):
    """(function, kind) for each place a printed name reaches an error or a job's message."""
    found = []
    for fn in (n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))):
        printed = set()
        for _ in range(4):
            for node in ast.walk(fn):
                if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)) and node.value is not None:
                    if any(_printing_call(n) for n in ast.walk(node.value)) or _names(node.value) & printed:
                        for target in (node.targets if isinstance(node, ast.Assign) else [node.target]):
                            printed |= _names(target) if not isinstance(target, ast.Subscript) else set()
                if isinstance(node, (ast.For, ast.comprehension)) and (
                        _names(node.iter) & printed or any(_printing_call(n) for n in ast.walk(node.iter))):
                    printed |= _names(node.target)

        def carries(expr):
            return bool(_names(expr) & printed) or any(_printing_call(n) for n in ast.walk(expr))

        for node in ast.walk(fn):
            if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call) and _called(node.exc) not in ALLOWED:
                if any(carries(arg) for arg in node.exc.args):
                    found.append((fn.name, "raise"))
            elif isinstance(node, ast.Return) and isinstance(node.value, ast.Tuple) and len(node.value.elts) == 3 \
                    and isinstance(node.value.elts[0], ast.Constant) and isinstance(node.value.elts[0].value, str):
                if carries(node.value.elts[2]) and not (isinstance(node.value.elts[2], ast.Call)
                                                        and _called(node.value.elts[2]) in ALLOWED):
                    found.append((fn.name, "result"))
            elif isinstance(node, ast.Call):
                if any(kw.arg == "message" and carries(kw.value) for kw in node.keywords):
                    found.append((fn.name, "message="))
                if _called(node) == "update" and any(
                        isinstance(arg, ast.Dict) and any(isinstance(k, ast.Constant) and k.value == "message" and carries(v)
                                                          for k, v in zip(arg.keys, arg.values)) for arg in node.args):
                    found.append((fn.name, "message="))
                said = node.args[5:] if _called(node) == "start" else node.args   # start(kind, title, resource, href, ref, message)
                if _called(node) in WRITERS and any(carries(arg) and not (isinstance(arg, ast.Call) and _called(arg) in ALLOWED)
                                                    for arg in said):
                    found.append((fn.name, "write"))
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Subscript) and isinstance(target.slice, ast.Constant) \
                            and target.slice.value == "message" and carries(node.value):
                        found.append((fn.name, "message"))
    return found


def tree_places():
    places = collections.Counter()
    for path in sorted(SERVER.glob("*.py")):
        for function, kind in scan(ast.parse(path.read_text(encoding="utf-8"))):
            places[(path.name, function, kind)] += 1
    return places


def compare(places, known):
    """(new, gone): the keys with more places than KNOWN lists (a new place, even
    in a listed function), and those with fewer (KNOWN has to come down)."""
    new = sorted(key for key, count in places.items() if count > known.get(key, (0, ""))[0])
    gone = sorted(key for key, (count, _) in known.items() if places.get(key, 0) < count)
    return new, gone


class HostOutputRatchetTests(unittest.TestCase):
    def test_no_new_place_puts_printed_output_into_an_error_or_a_jobs_message(self):
        new, _ = compare(tree_places(), KNOWN)
        self.assertEqual([], new, "Raise homestead_operations.HostOutput(sentence, output) instead, so a job's "
                                  "message gets the sentence and its Log the output; or, if it is not a job's message, "
                                  "list it in KNOWN with the reason and the count.")

    def test_every_known_place_still_does_so(self):
        _, gone = compare(tree_places(), KNOWN)
        self.assertEqual([], gone, "Lower these counts in KNOWN, or remove them: the list only shrinks.")

    def test_one_more_or_one_fewer_place_in_a_listed_function_fails(self):
        known = {("a.py", "f", "raise"): (1, "an answer to the admin")}
        self.assertEqual(([("a.py", "f", "raise")], []), compare(collections.Counter({("a.py", "f", "raise"): 2}), known))
        self.assertEqual(([], [("a.py", "f", "raise")]), compare(collections.Counter(), known))
        self.assertEqual(([], []), compare(collections.Counter({("a.py", "f", "raise"): 1}), known))
        code = "def f(node):\n    out, err = hostrun.run(node, 'x')\n    if out:\n        raise ValueError(out)\n" \
               "    return 'failed', 0, f'it stopped: {err}'\n"
        places = collections.Counter(("a.py", *place) for place in scan(ast.parse(code)))
        self.assertEqual(([("a.py", "f", "result")], []), compare(places, known))

    def test_the_scan_sees_the_shapes_it_is_for(self):
        code = '''
def a(node):
    out, err = hostrun.run(node, "x")
    said = (err or out)[-200:]
    raise ValueError(f"it stopped: {said}")
def b(node):
    out, err = HOST.run(node, "x")
    return "failed", 100, f"it stopped: {err}"
def c(item, node):
    text = ktext("/log")
    item["message"] = text
def d(node):
    out, err = hostrun.run(node, "x")
    raise OPS.HostOutput("it stopped", err)
def e(node):
    out, err = hostrun.run(node, "x")
    record("failed", 100, f"{node}: {out}")
def g(item, node):
    out, err = hostrun.run(node, "x")
    item.update({"message": err})
    return _end(item, ("failed", 100, err))
def h(node, ref):
    out, err = hostrun.run(node, "x")
    return ops.start("k", "Title", {}, "/", ref, f"stopped: {err}")
'''
        self.assertEqual([("a", "raise"), ("b", "result"), ("c", "message"), ("e", "write"), ("g", "message="),
                          ("g", "write"), ("h", "write")],
                         sorted(scan(ast.parse(code))))


if __name__ == "__main__":
    unittest.main()
