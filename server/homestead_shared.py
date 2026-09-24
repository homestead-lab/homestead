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

try:
    import fcntl
except ImportError:          # Windows, in tests: one process is all there is
    fcntl = None

DIR = os.environ.get("DATA_DIR", "/data")


def bind(data_dir):
    global DIR
    DIR = data_dir


class SharedLock:
    """Re-entrant within a thread, exclusive across threads and replicas."""

    def __init__(self, name):
        self.name = name
        self._thread = threading.RLock()
        self._depth = 0
        self._handle = None

    def _path(self):
        folder = os.path.join(DIR, ".locks")
        os.makedirs(folder, exist_ok=True)
        return os.path.join(folder, f"{self.name}.lock")

    def __enter__(self):
        self._thread.acquire()
        self._depth += 1
        if self._depth == 1 and fcntl is not None:
            try:
                self._handle = open(self._path(), "a+")
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX)
            except OSError:
                # No shared directory (a read-only test tree): the thread lock
                # still holds within this process.
                self._handle = None
        return self

    def __exit__(self, *exc):
        self._depth -= 1
        if self._depth == 0 and self._handle is not None:
            try:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            finally:
                self._handle.close()
                self._handle = None
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


def write_json(path, value, **dump):
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    tmp = temporary(path)
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(value, handle, **dump)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
