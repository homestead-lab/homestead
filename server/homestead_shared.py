"""What lets more than one Homestead share a data volume.

Homestead can run as several replicas, so one survives a node dying. They
share the RWX data volume, and the files on it - job records, alert history,
push subscriptions, moves - are read, changed and written back. Two copies
doing that at once would lose one's change, so each such file is changed
under a lock both hold: a thread lock inside one process, and an flock on a
file beside the data, which Longhorn's RWX share (NFS) carries between pods.

Writes go to a temporary name of their own before replacing the file, so two
copies never write into the same half-finished file.
"""
import json
import os
import threading
import errno
import time
import sys
from contextlib import nullcontext

try:
    import fcntl
except ImportError:          # Windows uses msvcrt for strict cross-process locks.
    fcntl = None

try:
    import msvcrt
except ImportError:
    msvcrt = None

DIR = os.environ.get("DATA_DIR", "/data")
WRITE_GUARD = None
WRITE_SCOPE = None


def write_scope(path):
    """Hold the self-data activity barrier through the complete write, if bound."""
    return WRITE_SCOPE(path) if WRITE_SCOPE is not None else nullcontext()


def bind(data_dir):
    global DIR
    DIR = data_dir


class SharedLock:
    """Re-entrant within a thread, exclusive across threads and replicas."""

    def __init__(self, name, *, strict=False, directory=None, timeout=10):
        self.name = name
        self.strict, self.directory, self.timeout = strict, directory, timeout
        self._thread = threading.RLock()
        self._depth = 0
        self._handle = None
        self._scope = None

    def _path(self):
        folder = os.path.join(self.directory() if self.directory else DIR, ".locks")
        os.makedirs(folder, exist_ok=True)
        return os.path.join(folder, f"{self.name}.lock")

    def __enter__(self):
        self._thread.acquire()
        if self._depth == 0:
            try:
                directory = self.directory() if self.directory else DIR
                self._scope = write_scope(os.path.join(directory, ".locks", f"{self.name}.lock"))
                self._scope.__enter__()
            except BaseException:
                self._scope = None
                self._thread.release()
                raise
        self._depth += 1
        if self._depth == 1 and self.strict:
            try:
                self._strict_acquire()
            except BaseException:
                try:
                    if self._handle is not None:
                        self._handle.close()
                finally:
                    self._handle = None
                    self._depth -= 1
                    try:
                        self._scope.__exit__(*sys.exc_info())
                    finally:
                        self._scope = None
                        self._thread.release()
                raise
            return self
        if self._depth == 1 and fcntl is not None:
            try:
                self._handle = open(self._path(), "a+")
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX)
            except OSError:
                # No shared directory (a read-only test tree): the thread lock
                # still holds within this process.
                self._handle = None
        return self

    def _strict_acquire(self):
        """Never downgrade a safety journal to a process-local mutex."""
        if fcntl is None and msvcrt is None:
            raise OSError("Shared job locking is unavailable; no operation was started")
        self._handle = open(self._path(), "a+b", buffering=0)
        if fcntl is None and os.fstat(self._handle.fileno()).st_size == 0:
            self._handle.write(b"\0")
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                if fcntl is not None:
                    fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                else:
                    self._handle.seek(0)
                    msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError as error:
                if error.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError("Shared job store is busy; no operation was started") from error
                time.sleep(.05)

    def __exit__(self, *exc):
        self._depth -= 1
        try:
            if self._depth == 0 and self._handle is not None:
                try:
                    if fcntl is not None:
                        fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
                    else:
                        self._handle.seek(0)
                        msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
                finally:
                    self._handle.close()
                    self._handle = None
        finally:
            try:
                if self._depth == 0 and self._scope is not None:
                    self._scope.__exit__(*exc)
                    self._scope = None
            finally:
                self._thread.release()
        return False

    # threading.Lock's spelling, for code that calls these directly
    def acquire(self):
        self.__enter__()
        return True

    def release(self):
        self.__exit__(None, None, None)


def temporary(path):
    """A temporary name no other thread or replica is writing."""
    return f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"


def write_json(path, value, *, durable=False, mode=None, **dump):
    with write_scope(path):
        _write_json(path, value, durable=durable, mode=mode, **dump)


def _write_json(path, value, *, durable=False, mode=None, **dump):
    if WRITE_GUARD is not None:
        WRITE_GUARD(path)
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    tmp = temporary(path)
    descriptor = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), mode if mode is not None else 0o666)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        if mode is not None:
            os.chmod(tmp, mode)
        json.dump(value, handle, **dump)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    if durable and os.name == "posix":
        # fsync(file) alone does not make the replacement directory entry
        # durable across a host crash. Failure must prevent the next mutation.
        descriptor = os.open(folder or ".", os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
