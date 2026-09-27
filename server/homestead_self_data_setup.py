"""Durable pointer publication, before an independent coordinator may stop apps.

The production setup wrapper must first verify the mounted source claim and all
replicas' protocol support. This primitive does not claim writer quiescence or
authorize a copy. It never overwrites an earlier move's marker, even an identical
one, and never removes a published marker after an uncertain outcome.
"""
import os
import secrets

import homestead_self_data_anchor as A
from homestead_self_data_fence import MARKER, read_marker
from homestead_storage_journal import Held, identity


def _current(anchor):
    handle = anchor.handle()
    fresh = A.Anchor(anchor.read, anchor.send, anchor.namespace, anchor.state["deployment"]["name"])
    fresh.load(**handle)
    if identity(fresh.obj) != identity(anchor.obj) or fresh.state != anchor.state:
        raise Held("The data handoff review changed before its local receipt was confirmed")


def publish_after_drain(directory, anchor, barrier, verify_source_and_jobs, *, timeout=30):
    """Close the check/write race before releasing the independent coordinator.

    Called outside the initiating HTTP request, not while holding a feature
    lock. The callback must recheck current source runtimes, the approved facts
    and idle jobs under the exclusive activity lock. Pod removal is still the
    coordinator's separate proof before it starts copying; draining is not CSI
    fencing and never authorizes force-deleting an unreachable writer.
    """
    if os.path.realpath(barrier.directory) != os.path.realpath(directory):
        raise Held("The data move activity lock does not cover the source directory")
    with barrier.freeze(timeout=timeout):
        _current(anchor)
        if verify_source_and_jobs() is not True:
            raise Held("The source replicas and saved jobs need a fresh review before moving data")
        _current(anchor)
        return publish_pointer(directory, anchor)


def publish_pointer(directory, anchor):
    """Create, fsync and independently acknowledge the local startup fence once.

    File data is synced before an atomic, no-clobber hardlink publishes the name.
    The directory is synced before the API checkpoint can release the coordinator.
    Failing filesystems hold the operation; there is no rename/overwrite fallback.
    """
    if os.name != "posix":
        raise Held("Data handoff publication requires a Linux filesystem with durable directory writes")
    handle = anchor.handle()
    state = anchor.state
    if state["phase"] != "prepare" or "pointer_receipt" in state or state["journal"]["ref"]["storage_writes"]:
        raise Held("This data handoff is no longer awaiting local receipt publication")
    marker = A.pointer(anchor.namespace, state, handle["uid"])
    encoded = A.pointer_bytes(marker)
    if len(encoded) > 8192:
        raise Held("The data handoff local receipt is too large")
    _current(anchor)
    descriptor = None
    temporary = ".self-data-publish-" + secrets.token_hex(16)
    created = False
    try:
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        directory_identity = os.fstat(descriptor)
        try:
            os.stat(MARKER, dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise Held("A local data handoff receipt already exists; it will not be overwritten or adopted")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=descriptor)
        created = True
        with os.fdopen(fd, "wb") as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        # Unlike replace/rename, link cannot overwrite another publisher's marker.
        os.link(temporary, MARKER, src_dir_fd=descriptor, dst_dir_fd=descriptor, follow_symlinks=False)
        os.unlink(temporary, dir_fd=descriptor)
        created = False
        os.fsync(descriptor)
        current_directory = os.stat(directory, follow_symlinks=False)
        if (current_directory.st_dev, current_directory.st_ino) != (directory_identity.st_dev, directory_identity.st_ino):
            raise Held("The source directory changed while publishing the data handoff receipt")
        if read_marker(directory) != marker:
            raise Held("The published data handoff receipt no longer matches the review")
        _current(anchor)
        anchor.pointer_published(A.pointer_digest(anchor.namespace, state, handle["uid"]))
        return handle
    except Held:
        raise
    except Exception:
        raise Held("The local data handoff receipt was not confirmed; inspect the retained data and control record. Nothing was retried") from None
    finally:
        if descriptor is not None:
            # Only our private temporary name may be removed. A published marker
            # must survive even if directory fsync or the API reply was lost.
            if created:
                try:
                    os.unlink(temporary, dir_fd=descriptor)
                except OSError:
                    pass
            os.close(descriptor)
