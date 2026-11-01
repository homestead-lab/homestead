"""Ratchets that keep new code on the shared job-safety helpers.

Three rules, each a count per server module that may only shrink:

- R1 cordons: a job cordons a host with homestead_cordon.cordon / release /
  busy_with, which record who holds the cordon, so two jobs never release each
  other's. Counted: set_cordon(..., True) and a write of unschedulable True
  (a patch op or a spec dict), outside homestead_cordon (the helper) and
  homestead_node_actions (a person's own Cordon on the Hosts page).
- R2 stops: a job stops an app with homestead_app_hold.hold / stop / restart,
  which record what was running and who stopped it. Counted: a write of
  replicas 0, suspend True or runStrategy "Halted" (in a dict, or as a
  subscript assignment) in a module that owns a job kind (a top-level KIND, or
  one of KIND_OWNERS, whose kind is named elsewhere), outside homestead_app_hold.
- R3 pod walks: finding the pods an app owns goes through
  homestead_app_hold.pods_of. Counted: calls of owned_pods, _held_pod,
  _using_pods and _ancestors outside homestead_app_hold and
  homestead_rollout_capacity (which defines owned_pods).

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
    "homestead_promote.py": 1,
}

# module -> hand-rolled stops, in modules owning a job kind.
STOPS = {
    "homestead_move_source.py": 4,
    "homestead_power_hold.py": 1,
    "homestead_power_jobs.py": 1,
    "homestead_reclass.py": 3,
    "homestead_rename.py": 1,
    "homestead_revert.py": 4,
    "homestead_share_servers.py": 6,
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

CORDON_FREE = {"homestead_cordon.py", "homestead_node_actions.py"}
STOP_FREE = {"homestead_app_hold.py"}
WALK_FREE = {"homestead_app_hold.py", "homestead_rollout_capacity.py"}
# Job modules with no top-level KIND: the kind is named elsewhere (platform-upgrade,
# node-power, move-origin, share-remove ...) or by a literal in the module.
KIND_OWNERS = {"homestead_components.py", "homestead_power_jobs.py", "homestead_power_hold.py",
               "homestead_reclass.py", "homestead_revert.py", "homestead_move_source.py",
               "homestead_share_servers.py"}
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
                and any(_is(a, True) for a in node.args[1:2]):
            found += 1
        elif isinstance(node, ast.Dict):
            pairs = {_key(k): v for k, v in zip(node.keys, node.values) if _key(k)}
            if _is(pairs.get("unschedulable"), True):
                found += 1
            elif str(getattr(pairs.get("path"), "value", "")).endswith("/spec/unschedulable") \
                    and _is(pairs.get("value"), True) and _key(pairs.get("op")) != "test":
                found += 1
    return found


def owns_a_kind(path):
    return path.name in KIND_OWNERS or any(
        isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "KIND" for t in n.targets)
        for n in SS.tree(path).body)


def stop_sites(path):
    """Writes of replicas 0, suspend True, runStrategy "Halted"."""
    writes = (("replicas", 0), ("suspend", True), ("runStrategy", "Halted"))
    found = 0
    for node in ast.walk(SS.tree(path)):
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if any(_key(k) == name and _is(v, value) for name, value in writes):
                    found += 1
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
        counts = scan(stop_sites, STOP_FREE, owns_a_kind)
        self.assertGreater(sum(counts.values()), 3, "the stop scan found too little to trust")
        judge(self, counts, STOPS, "hand-rolled stops", "homestead_app_hold.hold / stop / restart")

    def test_owned_pods_go_through_pods_of(self):
        counts = scan(walk_sites, WALK_FREE)
        self.assertGreater(sum(counts.values()), 3, "the pod-walk scan found too little to trust")
        judge(self, counts, POD_WALKS, "private pod walks", "homestead_app_hold.pods_of")


if __name__ == "__main__":
    unittest.main()
