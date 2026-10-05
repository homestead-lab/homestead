"""Homestead's data mount gone stale, and the pod replaced to get a fresh one.

Homestead's data can live on a Longhorn RWX volume, served over NFS by a
share-manager. When that share comes back as a different export - the engine
salvaged after its replica faulted, the share-manager moved - every access
through the old mount answers ESTALE, and keeps answering it. Restarting the
container does not help: kubelet cannot even create a new container in the
same pod, because the pod's subPath mount is the stale one. A new pod mounts
the share afresh, so the watchdog deletes its own pod and lets the Deployment
make the next one.
"""
import errno
import os
import time

EVERY = 10      # seconds between looks
NEEDED = 3      # stale this many looks running before the pod is replaced


def stale(path):
    """Whether the mount answers ESTALE. Any other error is not this one."""
    try:
        os.listdir(path)
        return False
    except OSError as error:
        return error.errno == errno.ESTALE


def watch(path, replace, every=EVERY, needed=NEEDED, sleep=time.sleep, log=print, looks=None):
    """Look at the mount every few seconds; replace the pod once it has been
    stale for `needed` looks running. `looks` bounds the loop for tests."""
    running = 0
    count = 0
    while looks is None or count < looks:
        count += 1
        running = running + 1 if stale(path) else 0
        if running >= needed:
            log(f"data: {path} is a stale NFS mount (its share came back as a different export); "
                "replacing this pod so the next one mounts it afresh", flush=True)
            try:
                replace()
                return True
            except Exception as error:
                log(f"data: could not replace this pod ({str(error)[:160]}); trying again", flush=True)
                running = 0
        sleep(every)
    return False
