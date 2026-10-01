"""Bounded console lifetimes and local/cross-replica credential revocation."""
import socket
import threading
import time

CHECK_SECONDS = 5
MAX_SECONDS = 3600
_lock = threading.Lock()
_active = {}


def revoke(user):
    with _lock:
        callbacks = list(_active.get(user, ()))
    for callback in callbacks:
        callback()


def watch(user, authorize, stopped, *connections):
    """Close both sides even when a reader is idle or blocked in a partial frame."""
    def close():
        stopped.set()
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    with _lock:
        _active.setdefault(user, set()).add(close)

    def monitor():
        deadline = time.monotonic() + MAX_SECONDS
        try:
            while not stopped.wait(CHECK_SECONDS):
                try:
                    valid = time.monotonic() < deadline and authorize()
                except Exception:
                    valid = False
                if not valid:
                    close()
                    return
        finally:
            with _lock:
                callbacks = _active.get(user, set())
                callbacks.discard(close)
                if not callbacks:
                    _active.pop(user, None)

    thread = threading.Thread(target=monitor, daemon=True)
    thread.start()
    return close
