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
import copy
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


class Unreadable(ValueError):
    """A store on the data volume that is there but could not be read."""


def read_json(path, default):
    """A JSON store on the data volume. A missing file is a fresh copy of
    default; a file that is there but cannot be read, is not JSON or holds
    another kind of value raises Unreadable, so nothing is saved over it."""
    try:
        with open(path, encoding="utf-8") as handle:
            return parse_json(handle, path, default)
    except FileNotFoundError:
        return copy.deepcopy(default)
    except OSError as error:
        raise _unreadable(path, error) from error


def _unreadable(path, error):
    reason = getattr(error, "strerror", None) or "it is not valid JSON"
    return Unreadable(f"Homestead could not read {os.path.basename(path)} on its data volume ({reason}), "
                      "so it changed nothing; the saved data is kept")


def parse_json(handle, path, default):
    """read_json for a file already open, as read_json judges it."""
    name = os.path.basename(path)
    try:
        value = json.load(handle)
    except (OSError, ValueError) as error:
        raise _unreadable(path, error) from error
    if not isinstance(value, type(default)):
        raise Unreadable(f"{name} on Homestead's data volume does not hold what Homestead saved there, "
                         "so it changed nothing; the saved data is kept")
    return value


class Unchanged:
    """What fn returns from update_file when it changed nothing: the value is
    handed back as the answer and the store is not serialised or written."""
    def __init__(self, result=None):
        self.result = result


def update_file(path, fn, lock, default, **dump):
    """Read, change and save one JSON store on the data volume as one step:
    under lock (a SharedLock, which every copy of Homestead takes), read it
    strictly, let fn change the value in place and return fn's answer. If fn
    raises, nothing is saved."""
    with lock:
        value = read_json(path, default)
        result = fn(value)
        if isinstance(result, Unchanged):
            return result.result
        write_json(path, value, **dump)
        return result


def write_json(path, value, *, durable=False, mode=None, **dump):
    """Save value at path, replacing the file. Returns the inode of the file
    it wrote, so a caller can tell it from one another writer put there
    after; None when the file already held exactly this and was left."""
    with write_scope(path):
        return _write_json(path, value, durable=durable, mode=mode, **dump)


def _same(path, text):
    """Whether path already holds exactly text. Never follows a link."""
    data = text.encode("utf-8")
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return False
    with os.fdopen(descriptor, "rb") as handle:
        if os.fstat(handle.fileno()).st_size != len(data):
            return False
        return handle.read(len(data) + 1) == data


def _write_json(path, value, *, durable=False, mode=None, **dump):
    if WRITE_GUARD is not None:
        WRITE_GUARD(path)
    text = json.dumps(value, **dump)
    if not durable and mode is None and _same(path, text):
        return None                  # loops save each round; what has not changed is not written again
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    tmp = temporary(path)
    descriptor = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), mode if mode is not None else 0o666)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
        if mode is not None:
            os.chmod(tmp, mode)
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
        inode = os.fstat(handle.fileno()).st_ino
    os.replace(tmp, path)
    if durable and os.name == "posix":
        # fsync(file) alone does not make the replacement directory entry
        # durable across a host crash. Failure must prevent the next mutation.
        descriptor = os.open(folder or ".", os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    return inode
