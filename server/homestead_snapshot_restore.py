"""Restore files from a snapshot's browsing copy into a live volume.

The review (restore_plan) says what a restore adds and which live files it
replaces; the run (restore) plans again and refuses unless the review is
unchanged. A restore adds and replaces, it never deletes: folders merge, and
the tar it sends holds exactly the reviewed entries. Links are restored as
links only when they point inside the volume; links in the live volume are
never written through.

One restore runs into a volume at a time, across every copy of Homestead:
the run holds a short Lease for the volume from before its review check until
the copy ends, renewed while it runs (_claim, _keep).
"""
import datetime
import hashlib
import json
import posixpath
import secrets
import threading
import time
import urllib.error

import homestead_files as FILES
import homestead_kube as KUBE
import homestead_leader as LEADER
import homestead_names as NAMES
import homestead_pod_exec as POD_EXEC
import homestead_routes as ROUTER
import homestead_snapshot_files as SNAP

ENDED = "This snapshot's browsing session ended. Open it again."
IN_USE = ("{app} is running and using this volume, so it sees these files change while it runs. "
          "For a database, stop it first.")
# The browsing pod stops at its deadline, restore or not, and a file cut off
# mid-copy is left short: a restore must fit in the time left, at a rate
# well below what a homelab network and Longhorn manage.
MIN_SECONDS_LEFT = 300
SAFE_RATE = 10 * 1024 * 1024    # bytes a second
_RUNNING, _LOCK = {}, threading.Lock()   # (namespace, pvc) -> job id, or "planning"
_HOLDERS = {}   # (namespace, pvc) -> this copy's hold on the volume's restore Lease (_hold)
ELSEWHERE = ("Another copy of Homestead is restoring files into this volume. Try again when it finishes. "
             "If that copy stopped, the volume is free again within a minute.")
NOT_HELD = "Homestead could not keep this volume for the restore, so it did not start. Try again."
LOST = "Another restore may have written into this volume at the same time. Check the restored files."
# The Lease is short and renewed while the restore runs: a copy of Homestead
# that stopped mid-restore frees the volume within CLAIM_SECONDS. It is lost
# only when no renewal has reached the API server for CLAIM_SECONDS, or
# another copy holds it (a 409).
CLAIM_SECONDS = 60
RENEW_EVERY = 15
RENEW_GRACE = 5   # a renewal later than RENEW_EVERY + this has failed at least once
_clock = time.monotonic


def _claim_path(name=""):
    base = f"/apis/coordination.k8s.io/v1/namespaces/{LEADER.NS}/leases"
    return f"{base}/{name}" if name else base


def _claim_name(namespace, pvc):
    return "homestead-restore-" + hashlib.sha256(f"{namespace}/{pvc}".encode()).hexdigest()[:20]


def _when(value):
    try:
        return datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00")) if value else None
    except ValueError:
        return None


class _Held(Exception):
    pass


def _stamp(now):
    return now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _claim(namespace, pvc):
    """Take the volume's restore Lease for CLAIM_SECONDS. Returns the
    holder; raises _Held while another copy of Homestead holds it, or wins
    it at the same moment (a 409)."""
    holder = f"{LEADER.IDENTITY}/{secrets.token_hex(4)}"
    now = datetime.datetime.now(datetime.timezone.utc)
    stamp = _stamp(now)
    name = _claim_name(namespace, pvc)
    spec = {"holderIdentity": holder, "leaseDurationSeconds": CLAIM_SECONDS, "acquireTime": stamp, "renewTime": stamp}
    lease = KUBE.get_optional(_claim_path(name), read=SNAP.kget)
    try:
        if lease is None:
            SNAP.ksend("POST", _claim_path(), {
                "apiVersion": "coordination.k8s.io/v1", "kind": "Lease",
                "metadata": {"name": name, "namespace": LEADER.NS, "labels": NAMES.labels("snapshot-restore"),
                             "annotations": {NAMES.key("restore-into"): f"{namespace}/{pvc}"}},
                "spec": dict(spec, leaseTransitions=0)})
            return holder
        held = lease.get("spec") or {}
        renewed = _when(held.get("renewTime")) or _when(held.get("acquireTime"))
        left = renewed and (renewed - now).total_seconds() + int(held.get("leaseDurationSeconds") or 0)
        if held.get("holderIdentity") and left and left > 0:
            raise _Held(ELSEWHERE)
        lease["spec"] = dict(held, **spec, leaseTransitions=int(held.get("leaseTransitions") or 0) + 1)
        # resourceVersion is kept: of two taking it at once, one gets a 409.
        SNAP.ksend("PUT", _claim_path(name), lease)
        return holder
    except urllib.error.HTTPError as error:
        if error.code == 409:
            raise _Held(ELSEWHERE) from None
        raise


def _renew(namespace, pvc, holder):
    """True when renewed, False when the Lease is no longer holder's (another
    copy has it, or a 409), None when the API server did not answer."""
    path = _claim_path(_claim_name(namespace, pvc))
    try:
        lease = KUBE.get_optional(path, read=SNAP.kget)
        if not lease or (lease.get("spec") or {}).get("holderIdentity") != holder:
            return False
        lease["spec"].update(renewTime=_stamp(datetime.datetime.now(datetime.timezone.utc)),
                             leaseDurationSeconds=CLAIM_SECONDS)
        SNAP.ksend("PUT", path, lease)
        return True
    except urllib.error.HTTPError as error:
        return False if error.code == 409 else None
    except Exception:
        return None


def _keep(namespace, pvc, hold):
    """Renew the Lease every RENEW_EVERY seconds until hold["stop"]. When it
    is lost, mark the hold and stop renewing: the restore says so."""
    while not hold["stop"].wait(RENEW_EVERY):
        renewed = _renew(namespace, pvc, hold["holder"])
        if renewed:
            hold["renewed"] = _clock()
        elif renewed is False or _clock() - hold["renewed"] >= CLAIM_SECONDS:
            hold["lost"] = True
            return


def _hold(namespace, pvc, holder):
    """Keep holder's Lease renewed from now until _let_go."""
    hold = {"holder": holder, "stop": threading.Event(), "lost": False, "renewed": _clock()}
    hold["thread"] = threading.Thread(target=_keep, args=(namespace, pvc, hold), name="snapshot-restore-claim", daemon=True)
    hold["thread"].start()
    return hold


def _let_go(namespace, pvc, hold):
    """Stop renewing, then give the Lease back (unless it was lost)."""
    hold["stop"].set()
    hold["thread"].join(5)
    if not hold["lost"]:
        _release(namespace, pvc, hold["holder"])


def _release(namespace, pvc, holder):
    """Give the Lease back if holder still has it. Never raises: one not
    given back runs out after CLAIM_SECONDS."""
    try:
        path = _claim_path(_claim_name(namespace, pvc))
        lease = KUBE.get_optional(path, read=SNAP.kget)
        if lease and (lease.get("spec") or {}).get("holderIdentity") == holder:
            lease["spec"]["holderIdentity"] = ""
            SNAP.ksend("PUT", path, lease)
    except Exception:
        pass


def _session_ready(namespace, session):
    state = SNAP.status(namespace, session)
    if state["state"] in ("closed", "closing"):
        raise ValueError(ENDED)
    if state["state"] != "ready":
        raise ValueError(state.get("message") or "the snapshot is still being prepared")
    SNAP._verified(namespace, session)
    return state


def _request(body):
    """The checked request: snapshot session, its folder and entry names,
    and where they go. Raises on anything that is not plain input."""
    namespace = SNAP._name(body.get("namespace"))
    session = body.get("session")
    SNAP._paths(namespace, session)
    folder = SNAP._relative(body.get("folder"))
    paths = body.get("paths")
    if not isinstance(paths, list) or not paths:
        raise ValueError("choose what to restore")
    names = []
    for path in paths:
        relative = SNAP._relative(path)
        parent, _, name = relative.rpartition("/")
        if not name or parent != folder:
            raise ValueError("choose entries from one folder")
        names.append(name)
    target = body.get("target") or {}
    target_ns = FILES._name(target.get("namespace"), "namespace")
    pvc = FILES._name(target.get("pvc"), "volume name")
    if pvc.startswith(SNAP.PREFIX):
        raise PermissionError("Snapshot copies are read-only")
    return namespace, session, folder, sorted(set(names)), (target_ns, pvc, FILES.safe_path(target.get("folder")))


def _walk(namespace, session, folder, names):
    """Every entry under the chosen ones in the snapshot, links not followed."""
    answer = POD_EXEC.run(namespace, session, ["python3", "-c", SNAP.READER, "/data", "walk", folder, json.dumps(names)],
                          timeout=120)
    out, err = answer
    if getattr(answer, "cut", False):
        raise ValueError("The list of what to restore is larger than Homestead reads at once (4 MiB). Choose fewer entries.")
    if err or not out:
        raise ValueError("the snapshot could not be read there: " + (err or "no answer")[:200])
    value = json.loads(out)
    return value["entries"], value["truncated"]


def _inspect_script(base):
    """For each path on stdin: what the live volume has there, and whether a
    link sits anywhere on the way to it (then nothing is written there)."""
    return (f"base={FILES._quote(base)}; "
            '[ -d "$base" ] && [ ! -L "$base" ] && [ "$(realpath "$base")" = "$base" ] || '
            "{ echo 'no such folder on the live volume' >&2; exit 3; }; "
            'while IFS= read -r rel; do p="$base/$rel"; '
            'if [ -L "$p" ]; then printf "link|0|0|%s\\n" "$rel"; continue; fi; '
            'd="$p"; while [ ! -e "$d" ] && [ ! -L "$d" ]; do d="${d%/*}"; done; '
            'if [ -L "$d" ] || [ "$(realpath "$d")" != "$d" ]; then printf "via-link|0|0|%s\\n" "$rel"; '
            'elif [ "$d" != "$p" ]; then printf "missing|0|0|%s\\n" "$rel"; '
            'else printf "%s|%s\\n" "$(stat -c "%F|%s|%Y" "$p")" "$rel"; fi; done')


def _parse_inspect(out):
    """Records end at a newline and nowhere else (names may hold \\r or
    U+2028, which splitlines() would split on); names keep their bytes."""
    rows = {}
    for line in out.split(b"\n"):
        parts = line.decode("utf-8", "surrogateescape").split("|", 3)
        if len(parts) != 4:
            continue
        kind, size, modified, rel = parts
        kind = kind if kind in ("link", "via-link", "missing") else FILES.TYPES.get(kind, "other")
        rows[rel] = {"kind": kind, "size": int(size) if size.isdigit() else 0,
                     "modified": int(modified) if modified.isdigit() else 0}
    return rows


def _inspect(namespace, pvc, folder, rels):
    ns, pod = FILES._session(namespace, pvc)
    out, err = FILES._sh(ns, pod, _inspect_script(FILES._full(folder)), stdin="\n".join(rels).encode("utf-8", "surrogateescape") + b"\n",
                         timeout=120)
    if err and not out:
        raise ValueError(err[:200])
    return _parse_inspect(out)


def _users(namespace, pvc):
    """The apps running with this claim mounted, and the claim's access modes."""
    claim = SNAP.kget(f"/api/v1/namespaces/{namespace}/persistentvolumeclaims/{pvc}")
    apps = set()
    for pod in SNAP.kget(f"/api/v1/namespaces/{namespace}/pods").get("items", []):
        meta = pod.get("metadata", {})
        name = meta.get("name", "")
        if (name.startswith((FILES.POD_PREFIX, SNAP.PREFIX)) or meta.get("deletionTimestamp")
                or pod.get("status", {}).get("phase") != "Running"
                or not any((v.get("persistentVolumeClaim") or {}).get("claimName") == pvc
                           for v in pod.get("spec", {}).get("volumes", []))):
            continue
        owner = next(iter(meta.get("ownerReferences") or []), {})
        app = owner.get("name") or name
        if owner.get("kind") == "ReplicaSet" and "-" in app:
            app = app.rsplit("-", 1)[0]
        apps.add(app)
    return sorted(apps), (claim.get("spec") or {}).get("accessModes") or []


def _outside(target_folder, rel, link):
    if link.startswith("/"):
        return True
    where = posixpath.normpath(posixpath.join(posixpath.dirname(posixpath.join(target_folder, rel)), link))
    return where == ".." or where.startswith("../")


WORDS = {"dir": "a folder", "file": "a file", "link": "a link", "other": "a special file"}


def _plan(body):
    """The review, and the entries a run would send."""
    namespace, session, folder, names, (target_ns, pvc, target_folder) = _request(body)
    state = _session_ready(namespace, session)
    blockers = []
    rows, truncated = _walk(namespace, session, folder, names)
    if truncated:
        blockers.append("More than 20000 entries. Restore a smaller folder.")
    live = _inspect(target_ns, pvc, target_folder, [r["path"] for r in rows if "\n" not in r["path"]])
    adds, replaces, skipped, send, held = [], [], [], [], []
    for row in rows:
        rel = row["path"]
        if any(rel.startswith(p + "/") for p in held):
            continue
        there = live.get(rel)
        if "\n" in rel:
            why = "its name has a line break"
        elif there is None:
            why = "the live volume could not be checked there"
        elif row["kind"] == "other":
            why = "it is not a file, folder or link"
        elif there["kind"] == "via-link":
            why = "a link in the live volume is on the way to it"
        elif row["kind"] == "link" and _outside(target_folder, rel, row.get("target", "")):
            why = "it is a link to somewhere outside the volume"
        elif there["kind"] not in ("missing", row["kind"]) or (row["kind"] == "link" and there["kind"] != "missing"):
            why = f"the live volume has {WORDS.get(there['kind'], 'something')} there"
        else:
            why = ""
        if why:
            skipped.append({"path": rel, "why": why})
            if row["kind"] == "dir":
                held.append(rel)
            continue
        if there["kind"] == "missing":
            adds.append(rel)
            send.append(rel)
        elif row["kind"] == "file":
            replaces.append({"path": rel, "live": {"size": there["size"], "modified": there["modified"]},
                             "snapshot": {"size": row["size"], "modified": row["modified"]}})
            send.append(rel)
    size = sum(row["size"] for row in rows if row["kind"] == "file" and row["path"] in set(send))
    left = state["expires"] - time.time()
    if left < MIN_SECONDS_LEFT + size / SAFE_RATE:
        blockers.insert(0, f"this snapshot's browsing session ends in {max(0, int(left // 60))} minutes, "
                           f"too soon to copy {_size(size)}. Open it again to restore.")
    if not send and not truncated:
        blockers.append("nothing to restore: the live folder already has everything chosen")
    apps, modes = _users(target_ns, pvc)
    if apps and "ReadWriteOncePod" in modes:
        blockers.append(f"{pvc} can be mounted by one pod only, and an app has it. Stop the app first.")
    review = {"session": session, "namespace": namespace, "sources": [posixpath.join(folder, n) for n in names],
              "target": {"namespace": target_ns, "pvc": pvc, "folder": target_folder},
              "replace": sorted([r["path"], r["live"]["size"], r["live"]["modified"]] for r in replaces)}
    plan = {"ready": not blockers, "blockers": blockers, "adds": adds, "replaces": replaces, "skipped": skipped,
            "in_use": apps, "warning": IN_USE.format(app=", ".join(apps)) if apps else "",
            "session_ends_at": state["expires"], "snapshot": state.get("source", {}).get("snapshot", ""),
            "review_token": hashlib.sha256(json.dumps(review, sort_keys=True).encode()).hexdigest()[:20]}
    return plan, send


def _size(n):
    for unit, step in (("GiB", 1024 ** 3), ("MiB", 1024 ** 2), ("KiB", 1024)):
        if n >= step:
            return f"{n / step:.1f} {unit}"
    return f"{n} bytes"


def restore_plan(body):
    return _plan(body)[0]


def restore(body):
    namespace, session, folder, _, (target_ns, pvc, target_folder) = _request(body)
    key = (target_ns, pvc)
    with _LOCK:
        if key in _RUNNING:
            return ROUTER.Reply(409, {"error": f"A restore into {pvc} is running. Wait for it to finish."})
        _RUNNING[key] = "planning"
    job = None
    try:
        # The claim is taken first, so nothing can change the volume between
        # the review check and the copy.
        try:
            holder = _claim(target_ns, pvc)
        except _Held as held:
            return _refuse(str(held), _plan(body)[0], key)
        with _LOCK:
            _HOLDERS[key] = _hold(target_ns, pvc, holder)
        plan, send = _plan(body)
        if body.get("review_token") != plan["review_token"]:
            return _refuse("What the restore would change has changed. Review it again.", plan, key)
        if not plan["ready"]:
            return _refuse(plan["blockers"][0], plan, key)
        if _HOLDERS[key]["lost"]:
            return _refuse(NOT_HELD, plan, key)
        dst_ns, dst_pod = FILES._session(target_ns, pvc)
        count = len(send)
        job = {"id": secrets.token_hex(8), "status": "running", "bytes": 0, "error": "", "move": False,
               "from": f"snapshot {plan['snapshot']}", "to": pvc, "names": send[:50], "started": time.time(),
               "title": f"Restore {count} file{'s' if count != 1 else ''} from snapshot {plan['snapshot']}"}
        with FILES._JOBS_LOCK:
            FILES._JOBS[job["id"]] = job
        # Held together from here until _finished: the claim (no second
        # restore, no helper stop), and the session (no close, no cleanup).
        with _LOCK:
            _RUNNING[key] = job["id"]
            SNAP.ACTIVE_RESTORES[(namespace, session)] += 1
            FILES.BUSY_CLAIMS.add(key)
        reader = ["python3", "-c", SNAP.READER, "/data", "tarlist", folder, json.dumps(send)]
        _launch(job, namespace, session, dst_ns, dst_pod, target_folder, reader)
        return ROUTER.Reply(202, {"ok": True, "job": dict(job)})
    except Exception as error:
        if job and _RUNNING.get(key) == job["id"]:
            job.update(status="failed", error=str(error)[:300], finished=time.time())
            _finished(job, target_ns, pvc, namespace, session)
        with _LOCK:
            planning = _RUNNING.get(key) == "planning"
            if planning:
                del _RUNNING[key]
                hold = _HOLDERS.pop(key, None)
        if planning and hold:
            _let_go(target_ns, pvc, hold)
        raise


def _refuse(error, plan, key):
    with _LOCK:
        _RUNNING.pop(key, None)
        hold = _HOLDERS.pop(key, None)
    if hold:
        _let_go(*key, hold)
    return ROUTER.Reply(409, {"error": error, "plan": plan})


def _launch(job, namespace, session, dst_ns, dst_pod, folder, reader):
    def run():
        try:
            FILES._transfer(job, namespace, session, [], dst_ns, dst_pod, folder, reader_argv=reader, touch_source=False)
            # A copy cut off mid-file is worse than one that finishes, so a
            # lost claim does not stop the copy: the job says what happened.
            hold = _HOLDERS.get((dst_ns, job["to"]))
            if hold and not hold["lost"] and _clock() - hold["renewed"] > RENEW_EVERY + RENEW_GRACE:
                # Renewals were failing: another copy may have taken the claim
                # since the last one. One more look before calling it done.
                hold["lost"] = _renew(dst_ns, job["to"], hold["holder"]) is not True
            if hold and hold["lost"]:
                job.update(status="failed", error=f"{LOST} {job['error']}".strip())
        finally:
            _finished(job, dst_ns, job["to"], namespace, session)
    threading.Thread(target=run, name="snapshot-restore", daemon=True).start()


def _finished(job, target_ns, pvc, namespace, session):
    """The claim is free for another restore, and the session for cleanup,
    which closes it now if it ran out while the restore ran."""
    with _LOCK:
        if _RUNNING.get((target_ns, pvc)) != job["id"]:
            return
        del _RUNNING[(target_ns, pvc)]
        hold = _HOLDERS.pop((target_ns, pvc), None)
        FILES.BUSY_CLAIMS.discard((target_ns, pvc))
        SNAP.ACTIVE_RESTORES[(namespace, session)] -= 1
        last = SNAP.ACTIVE_RESTORES[(namespace, session)] <= 0
        if last:
            del SNAP.ACTIVE_RESTORES[(namespace, session)]
        close = last and (namespace, session) in SNAP.CLOSE_AFTER_RESTORE
        if close:
            SNAP.CLOSE_AFTER_RESTORE.discard((namespace, session))
    if hold:
        _let_go(target_ns, pvc, hold)
    if close:
        try:
            SNAP.close(namespace, session)
        except Exception:
            pass   # the session expires and cleanup removes it
