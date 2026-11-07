"""Ratchets that keep new code on the shared job-safety helpers.

Three rules, each a count per server module that may only shrink:

- R1 cordons: a job cordons a host with homestead_cordon.cordon / release /
  busy_with, which record who holds the cordon, so two jobs never release each
  other's. Counted: set_cordon(..., True) and a write of unschedulable True
  (a patch op or a spec dict), outside homestead_cordon (the helper). The
  Hosts page's own Cordon (homestead_lifecycle) passes a value, not True, so
  is not counted; node_actions' power_node (the node-power job) is.
- R2 stops: a job stops an app with homestead_app_hold.hold / stop / restart,
  which record what was running and who stopped it. Counted: a write of
  replicas 0, suspend True or runStrategy "Halted" (in a dict, a json-patch op
  on /spec/<field> that is not a test, a subscript assignment, or a keyword:
  dict(replicas=0), x.update(suspend=True)), and a call on a VM's KubeVirt
  /stop subresource, in any server module outside homestead_app_hold, except
  the STOP_NOT_JOBS modules, which write a stop as a person's action or only
  look like one. The list opts out, so a new job module is counted by default. job_owners() finds
  the job modules from the registry (top-level KIND, register(ops), a write to
  RESOLVERS[...], ops.start(...)); a STOP_NOT_JOBS entry that is one of them
  fails, and so does one that no longer writes a stop.
- R3 pod walks: finding the pods an app owns goes through
  homestead_app_hold.pods_of. Counted: calls of owned_pods, _held_pod,
  _using_pods and _ancestors outside homestead_app_hold and
  homestead_rollout_capacity (which defines owned_pods). Calls only: a function
  passed on by name (map(owned_pods, ...)) is not seen.

The allowlists are the sites on the base when the ratchets came in (several
are fixes that landed hand-rolled before the helpers). A count may go down
(and then has to be lowered here, or the entry removed), never up, and a
module that is not listed fails. A site that moves moves its entry. It is a
lint, not a proof: it sees literal writes only, not a value built elsewhere.
"""
import ast
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import source_scan as SS

# module -> hand-rolled cordons.
CORDONS = {
    "homestead_cluster_shutdown.py": 1,
    "homestead_lhv2_cleanup.py": 1,
    "homestead_node_actions.py": 1,   # power_node, the node-power job's cordon step
    "homestead_promote.py": 1,
}

# module -> hand-rolled stops (job modules, and the helpers they call).
STOPS = {
    "homestead_cluster_shutdown.py": 1,
    "homestead_move_source.py": 4,
    "homestead_power_hold.py": 2,
    "homestead_power_jobs.py": 1,
    "homestead_reclass.py": 3,
    "homestead_rename.py": 1,
    "homestead_restructure.py": 1,
    "homestead_self_data_coordinator.py": 1,
    "homestead_share_servers.py": 6,
    "homestead_vms.py": 2,               # a person's VM stop and stop of boot retries, in a module that also runs jobs
}

# module -> calls of the private pod walks.
POD_WALKS = {
    "homestead_copy_job.py": 1,
    "homestead_deploy.py": 1,
    "homestead_power_hold.py": 1,
    "homestead_reclass.py": 3,
    "homestead_rename.py": 5,
    "homestead_revert.py": 4,
    "homestead_self_data_review.py": 1,
    "homestead_storage_admission.py": 1,
    "homestead_update_rollouts.py": 1,
    "homestead_workloads.py": 1,
}

CORDON_FREE = {"homestead_cordon.py"}
STOP_FREE = {"homestead_app_hold.py"}
WALK_FREE = {"homestead_app_hold.py", "homestead_rollout_capacity.py"}
# Modules that write a stop but are not jobs, each with its reason.
STOP_NOT_JOBS = {
    "homestead_data_copies.py",   # "replicas": 0 is a field of a report on a Longhorn volume, not a write
    "homestead_disk_setup.py",    # same: "replicas": 0 is a field of a disk report
    "homestead_objectstore.py",   # a person's switch-off of the backup store, in the request that asks for it
    "homestead_workload_edit.py", # a person's edit of a workload; restructure (a job) is counted in its own module
}
WALKS = {"owned_pods", "_held_pod", "_using_pods", "_ancestors"}


def _last(node):
    dotted = SS.dotted(node)
    return dotted.rsplit(".", 1)[-1] if dotted else None


def _is(node, value):
    return isinstance(node, ast.Constant) and node.value is value if isinstance(value, bool) \
        else isinstance(node, ast.Constant) and node.value == value and not isinstance(node.value, bool)


def _key(node):
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def cordon_sites(path):
    """set_cordon(x, True) calls, and dicts that write unschedulable True."""
    found = 0
    for node in ast.walk(SS.tree(path)):
        if isinstance(node, ast.Call) and _last(node.func) == "set_cordon" \
                and (any(_is(a, True) for a in node.args[1:2])
                     or any(_is(k.value, True) for k in node.keywords if k.arg in ("cordon", "value", "on"))):
            found += 1
        elif isinstance(node, ast.Dict):
            pairs = {_key(k): v for k, v in zip(node.keys, node.values) if _key(k)}
            if _is(pairs.get("unschedulable"), True):
                found += 1
            elif str(getattr(pairs.get("path"), "value", "")).endswith("/spec/unschedulable") \
                    and _is(pairs.get("value"), True) and _key(pairs.get("op")) != "test":
                found += 1
    return found


def job_owners(path):
    """Whether a module is a job module by the registry: a top-level KIND, a
    register(ops), a write to RESOLVERS[...], or a call of ops.start(...)."""
    for node in SS.tree(path).body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "KIND" for t in node.targets):
            return True
        if isinstance(node, ast.FunctionDef) and node.name == "register":
            return True
    for node in ast.walk(SS.tree(path)):
        if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store) and _last(node.value) == "RESOLVERS":
            return True
        if isinstance(node, ast.Call) and (SS.dotted(node.func) or "").lower() in ("ops.start", "operations.start"):
            return True
    return False


def _text(node):
    """The literal text of a path argument (a string, an f-string or a + of
    them), with a hole for each part that is not literal."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _text(node.left) + _text(node.right)
    if isinstance(node, ast.JoinedStr):
        return "".join(_text(part) for part in node.values)
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else "\x00"


def _ends_stop(node):
    """A path argument on KubeVirt's stop subresource for a VM."""
    path = _text(node)
    return path.endswith("/stop") and "virtualmachines/" in path


def stop_sites(path):
    """Writes of replicas 0, suspend True, runStrategy "Halted" (as a field, or
    as a json-patch op that is not a test), and calls on KubeVirt's /stop."""
    writes = (("replicas", 0), ("suspend", True), ("runStrategy", "Halted"))
    found = 0
    for node in ast.walk(SS.tree(path)):
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if any(_key(k) == name and _is(v, value) for name, value in writes):
                    found += 1
            pairs = {_key(k): v for k, v in zip(node.keys, node.values) if _key(k)}
            target = _key(pairs.get("path")) or ""
            if _key(pairs.get("op")) != "test" and any(
                    target.endswith("/spec/" + name) and _is(pairs.get("value"), value) for name, value in writes):
                found += 1
        elif isinstance(node, ast.Call):
            found += sum(1 for k in node.keywords if any(k.arg == name and _is(k.value, value) for name, value in writes))
            found += any(_ends_stop(arg) for arg in node.args)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Subscript) and any(
                        _key(target.slice) == name and _is(node.value, value) for name, value in writes):
                    found += 1
    return found


def walk_sites(path):
    return sum(1 for node in ast.walk(SS.tree(path))
               if isinstance(node, ast.Call) and _last(node.func) in WALKS)


def scan(sites, free, only=lambda path: True):
    counts = {}
    for path in SS.server_files():
        if path.name in free or not only(path):
            continue
        n = sites(path)
        if n:
            counts[path.name] = n
    return counts


def judge(test, counts, allowed, rule, helper):
    test.assertGreaterEqual(sum(counts.values()), sum(allowed.values()) // 2,
                            f"the {rule} scan found too little to trust")
    for name, n in sorted(counts.items()):
        have = allowed.get(name, 0)
        if n > have:
            test.fail(f"{name} has {n} {rule} ({have} allowed): use {helper} instead of writing it by hand")
    for name, have in sorted(allowed.items()):
        n = counts.get(name, 0)
        if n < have:
            test.fail(f"{name} has {n} {rule}, the allowlist says {have}: "
                      + ("remove it from the allowlist" if not n else "lower its count in the allowlist"))


class JobSafetyRatchets(unittest.TestCase):
    def test_cordons_go_through_homestead_cordon(self):
        counts = scan(cordon_sites, CORDON_FREE)
        self.assertGreaterEqual(sum(counts.values()), 3, "the cordon scan found too little to trust")
        judge(self, counts, CORDONS, "hand-rolled cordons", "homestead_cordon.cordon / release")

    def test_stops_go_through_homestead_app_hold(self):
        counts = scan(stop_sites, STOP_FREE | STOP_NOT_JOBS)
        for name in sorted(STOP_NOT_JOBS):
            path = SS.SERVER / name
            self.assertTrue(stop_sites(path), f"{name} no longer writes a stop: remove it from STOP_NOT_JOBS")
            self.assertFalse(job_owners(path), f"{name} is a job module: its stops belong in STOPS, not STOP_NOT_JOBS")
        judge(self, counts, STOPS, "hand-rolled stops", "homestead_app_hold.hold / stop / restart")

    def test_the_stop_scan_sees_json_patches_and_kubevirt_stops(self):
        source = SS.code(
            "send('PATCH', p, [{'op': 'test', 'path': '/spec/runStrategy', 'value': 'Halted'},\n"
            "                   {'op': 'replace', 'path': '/spec/runStrategy', 'value': 'Halted'},\n"
            "                   {'op': 'add', 'path': '/spec/replicas', 'value': 0},\n"
            "                   {'op': 'add', 'path': '/spec/replicas', 'value': 2},\n"
            "                   {'op': 'replace', 'path': '/spec/suspend', 'value': True}])\n"
            "send('PUT', f'{SUB}/namespaces/{ns}/virtualmachines/{name}/stop', {})\n"
            "send('PUT', SUB + '/virtualmachines/vm1/stop', {})\n"
            "send('PUT', f'{SUB}/virtualmachines/{name}/start', {})\n"
            "send('POST', '/api/images/prepull/stop', {})\n")
        self.assertEqual(stop_sites(source), 5)

    def test_owned_pods_go_through_pods_of(self):
        counts = scan(walk_sites, WALK_FREE)
        judge(self, counts, POD_WALKS, "private pod walks", "homestead_app_hold.pods_of")


if __name__ == "__main__":
    unittest.main()
