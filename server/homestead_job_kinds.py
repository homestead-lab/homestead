"""What each kind of job is, in one record (#622).

A kind is spread over the job store's registries today - how it advances
(RESOLVERS), what cancelling it does (CANCELLERS), whether a failed one can
carry on (RESUMABLE), where its output is (LOGGERS), whether cancelling also
cleans up after a failure (CLEANUPS, CDI_CANCELLABLE). homestead_operations
seeds some resolvers itself; server.py, homestead_cancel.register and
homestead_joblogs.register fill most of the rest, and requests, priority,
promote and set_restart add their own at import or in bind(), and the
modules server.py lists call register(ops) to add theirs (#622).
kinds() reads them as they stand on each call, so nothing has to
move for it to be right; the registrations and homestead_operations' kind
branches move into these records later, a few kinds at a time. The view's
kind branches have moved into a per-kind table in homestead_operations
(OPS.VIEWS), which a kind's `fields` names, and the cancel plan's into
another (OPS.CANCEL_BLOCKS), which a kind's `cancel_block` names, and
start()'s rules for one kind into a third (OPS.CONFLICTS), which a kind's
`conflicts` names."""
from dataclasses import dataclass
from typing import Callable, Optional

import homestead_operations as OPS

# The kinds of Homestead job that cordon a host, or hold one that is cordoned,
# in one place: a host one of them is on keeps its cordon when an upgrade ends
# (homestead_components), and the shared cordon helper marks its holders by these
# names. tests/test_job_kinds.py checks every module that cordons is listed.
HOST_CORDON_KINDS = ("node-power", "host-os", "os-rollout", "cluster-shutdown", "host-bond", "host-bridge",
                     "host-promote", "platform-upgrade", "longhorn-v2-cleanup", "longhorn-v2-prepare",
                     "disk-v2-convert", "disk-retire")


def _no_rules_of_its_own(items, kind, ref, resource):
    """A kind no running job refuses: only start()'s shared checks apply."""
    return ""


@dataclass(frozen=True)
class Kind:
    name: str
    resolve: Optional[Callable] = None        # item -> (status, progress, message)
    cancel: Optional[tuple] = None            # (plan(item), run(item, options))
    resumable: Optional[Callable] = None      # item -> why it cannot carry on, or ""
    log: Optional[Callable] = None            # item -> [{"title", "text", "note"}]
    cleanup: bool = False                     # cancelling a failed one tidies up after it
    cdi_cancellable: bool = False             # cancellable while its CDI receipt is held
    view: Callable = OPS._public              # item -> what the job tray is sent
    fields: Optional[Callable] = None         # (item, out) -> what this kind adds to that view
    cancel_block: Optional[Callable] = None   # item -> why it cannot be cancelled now, or ""
    conflicts: Callable = _no_rules_of_its_own  # (items, kind, ref, resource) -> why it cannot start, or ""


def kinds(ops=OPS):
    """Every kind any registry names, by name. A kind no resolver advances
    still shows, with resolve None: that is a half-registered kind."""
    names = (set(ops.RESOLVERS) | set(ops.CANCELLERS) | set(ops.RESUMABLE) | set(ops.LOGGERS)
             | set(ops.CLEANUPS) | set(ops.CDI_CANCELLABLE))
    return {name: Kind(name, resolve=ops.RESOLVERS.get(name), cancel=ops.CANCELLERS.get(name),
                       resumable=ops.RESUMABLE.get(name), log=ops.LOGGERS.get(name),
                       cleanup=name in ops.CLEANUPS, cdi_cancellable=name in ops.CDI_CANCELLABLE,
                       view=ops._public, fields=ops.VIEWS.get(name),
                       cancel_block=ops.CANCEL_BLOCKS.get(name),
                       conflicts=ops.CONFLICTS.get(name, _no_rules_of_its_own))
            for name in sorted(names)}


def kind(name, ops=OPS):
    """One kind; KeyError when no registry names it."""
    return kinds(ops)[name]
