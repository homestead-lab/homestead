"""Search the logs of every app at once, without running Loki.

Each running pod of the apps chosen - every app when none is - is asked for
its log lines of the last while (Kubernetes keeps what kubelet still holds:
the current container's, and the one before a restart), and the lines that
match are returned newest first, with the app, pod and container they came
from. Several pods are asked at once; everything is capped - pods asked,
bytes per container, line length, matches returned - and the answer says when
a cap was reached, so a search is never quietly incomplete.

Plain text matching by default; a regular expression on request, refused when
it could take ages to run (a repeated group holding a repeat or a choice).
That check is by shape and lets some slow ones through (`^(x?){40}x{40}$`),
and Python cannot stop a regular expression mid-match in a thread, so an
expression is matched in a child process (run_regex), one at a time, within
REGEX_SECONDS of matching for the whole search (a child's start-up is free).
Each container's newest lines go first; when the time runs out the child is killed, what it matched so far is kept,
and the answer names the apps that were only partly searched ("cut").
"""
import contextlib
import json
import re
import select
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

MAX_PODS = 60
MAX_BYTES = 2 * 1024 * 1024        # per container
MAX_LINE = 2000
MAX_MATCHES = 500
REGEX_SECONDS = 10                 # matching time an expression may take, per search
READY_SECONDS = 5                  # a child not up by then could not be run
STARTUP_FREE = 2                   # seconds of a child's start-up not charged to the search; the rest is
REGEX_READERS = 2                  # containers read at once for an expression: matching is one at a time
WINDOWS = {"15m": 900, "1h": 3600, "6h": 21600, "24h": 86400}
_SLOW = re.compile(r"\([^)]*[*+|][^)]*\)\s*[*+{]")   # (a+)+, (a|aa)* and the like
_ONE_AT_A_TIME = threading.Lock()   # each search reads up to MAX_PODS x MAX_BYTES
_STAMP = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z) (.*)$")


def matcher(query, regex=False, case=False):
    """A function telling whether a line matches; ValueError for a bad query."""
    query = str(query or "")
    if not query.strip():
        raise ValueError("type something to search for")
    if len(query) > 200:
        raise ValueError("a search is at most 200 characters")
    if regex:
        if _SLOW.search(query):
            raise ValueError("that expression repeats a group holding a repeat or a choice, which can take ages to run; simplify it")
        try:
            pattern = re.compile(query, 0 if case else re.I)
        except re.error as error:
            raise ValueError(f"that is not a valid regular expression: {error}") from None
        return lambda line: bool(pattern.search(line))
    needle = query if case else query.lower()
    return (lambda line: needle in line) if case else (lambda line: needle in line.lower())


# The child that matches an expression: the query and lines on stdin; on
# stdout "r" once it is up (the time limit starts then, not before), then
# "m <i>" for each matching line and "c <n>" once the first n lines are
# checked, flushed every 256 lines or 0.1 s, so a kill loses at most those.
# Run with -I: no site, no cwd.
_CHILD = ("import json,re,sys,time\n"
          "sys.stdout.write('r\\n');sys.stdout.flush()\n"
          "d=json.load(sys.stdin)\n"
          "p=re.compile(d['q'],0 if d['c'] else re.I)\n"
          "o=sys.stdout;t=time.monotonic();k=0\n"
          "for i,l in enumerate(d['l']):\n"
          " if i-k>=256 or time.monotonic()-t>=0.1:\n"
          "  o.write('c %d\\n'%i);o.flush();k=i;t=time.monotonic()\n"
          " if p.search(l):o.write('m %d\\n'%i)\n"
          "o.write('c %d\\n'%len(d['l']));o.flush()\n")


class NotUp(ValueError):
    """The child did not come up; .spent is the time waited for it."""

    def __init__(self, spent=0.0):
        super().__init__("the expression could not be run")
        self.spent = spent


def run_regex(lines, query, case, limit):
    """(indexes of the lines that match, how many lines were checked), the
    expression run in a child process killed after `limit` seconds of
    matching (its start-up, about 0.1 s, is not counted): fewer checked than
    given means the time ran out. ValueError when it cannot run."""
    hits, checked, _spent = _run(lines, query, case, limit)
    return hits, checked


def _run(lines, query, case, limit):
    """run_regex, and the seconds the matching took, start-up left out."""
    if not lines or limit <= 0:
        return [], 0, 0.0
    payload = json.dumps({"q": query, "c": bool(case), "l": list(lines)})
    try:
        child = subprocess.Popen([sys.executable, "-I", "-c", _CHILD], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    except OSError:
        raise ValueError("the expression could not be run") from None
    killed = False
    try:
        asked = time.monotonic()
        if not select.select([child.stdout], [], [], READY_SECONDS)[0] or child.stdout.readline() != "r\n":
            raise NotUp(time.monotonic() - asked)
        began = time.monotonic()           # the child is up: the limit starts here
        late = max(0.0, began - asked - STARTUP_FREE)   # a start-up this slow is charged
        try:
            out, _ = child.communicate(payload, timeout=limit)
        except subprocess.TimeoutExpired:
            child.kill()
            out, _ = child.communicate()   # what it wrote before the kill
            killed = True
        spent = time.monotonic() - began
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
        for pipe in (child.stdin, child.stdout):
            with contextlib.suppress(OSError, ValueError):
                pipe.close()
    if not killed and child.returncode != 0:
        raise ValueError("the expression could not be run")
    hits, checked = _child_rows(out)
    return hits, min(checked, len(lines)), spent + late


def _child_rows(out):
    """(matching indexes, lines checked) from what the child wrote: whole rows
    only, as a kill can cut the last one; no match past the last check."""
    hits, checked = [], 0
    rows = out.split("\n")[:-1]        # the part after the last newline is cut, or empty
    for row in rows:
        kind, _, n = row.partition(" ")
        if not n.isdigit():
            break
        if kind == "c":
            checked = int(n)
        elif kind == "m":
            hits.append(int(n))
    return [i for i in hits if i < checked], checked


def targets(workloads, apps=None):
    """(ns, app, pod, container) for each running app container of the chosen apps."""
    chosen = {tuple(a.split("/", 1)) for a in apps or [] if "/" in a}
    out = []
    for w in workloads or []:
        if chosen and (w.get("ns"), w.get("name")) not in chosen:
            continue
        if not chosen and w.get("platform"):
            continue
        for pod in w.get("pods") or []:
            if pod.get("phase") != "Running":
                continue
            names = [c.get("name") for c in pod.get("containers") or [] if c.get("kind", "app") == "app" and c.get("name")]
            for name in names or [""]:
                out.append((w["ns"], w["name"], pod["name"], name))
    return out


def search(workloads, query, read, window="1h", apps=None, regex=False, case=False, now=None):
    """Matching lines, newest first. read(ns, pod, container, since_seconds,
    limit_bytes) returns that container's log text with timestamps."""
    match = matcher(query, regex, case)
    since = WINDOWS.get(window)
    if not since:
        raise ValueError("choose 15m, 1h, 6h or 24h")
    with exclusive():
        return _search(workloads, match, since, read, window, apps, now, _Budget(query, case) if regex else None)


class _Budget:
    """One search's REGEX_SECONDS of expression matching, one child at a
    time: a thread waiting for its turn spends none of it, and once it is
    spent nothing more is started."""

    def __init__(self, query, case):
        self.query, self.case, self.left, self.lane = query, case, REGEX_SECONDS, threading.Lock()

    def run(self, lines):
        with self.lane:
            try:
                hits, checked, spent = _run(lines, self.query, self.case, self.left)
            except NotUp as error:
                self.left -= error.spent   # waiting for a child that never came up is spent time
                raise
            self.left -= spent             # matching time, and start-up beyond STARTUP_FREE
            return hits, checked


@contextlib.contextmanager
def exclusive():
    """One log search at a time, shared with whatever else reads logs on
    someone's request (a log alert's preview): it refuses at once rather
    than queueing behind a running search."""
    if not _ONE_AT_A_TIME.acquire(blocking=False):
        raise ValueError("another log search is running; try again in a moment")
    try:
        yield
    finally:
        _ONE_AT_A_TIME.release()


def _search(workloads, match, since, read, window, apps, now, budget=None):
    every = targets(workloads, apps)
    asked, skipped = every[:MAX_PODS], max(0, len(every) - MAX_PODS)
    errors, full, cut = [], [], []

    def one(target):
        ns, app, pod, container = target
        try:
            text = read(ns, pod, container, since, MAX_BYTES)
        except Exception as error:
            errors.append(f"{app} ({pod}): {str(error)[:120]}")
            return []
        if len(text) >= MAX_BYTES:
            full.append(app)
        lines = []
        for raw in text.splitlines():
            stamp = _STAMP.match(raw)
            at, line = (stamp.group(1), stamp.group(2)) if stamp else ("", raw)
            lines.append((at, line[:MAX_LINE]))
        if budget is None:
            hits = [i for i, (_at, line) in enumerate(lines) if match(line)]
        else:
            lines.reverse()            # newest first: a cut keeps the newest
            try:
                hits, checked = budget.run([line for _at, line in lines])
            except ValueError as error:
                errors.append(f"{app} ({pod}): {error}")
                return []
            if checked < len(lines):
                cut.append(app)
        return [{"at": lines[i][0], "ns": ns, "app": app, "pod": pod, "container": container, "line": lines[i][1]}
                for i in hits]

    with ThreadPoolExecutor(max_workers=8 if budget is None else REGEX_READERS) as pool:
        found = [row for rows in pool.map(one, asked) for row in rows]
    found.sort(key=lambda r: r["at"], reverse=True)
    return {"matches": found[:MAX_MATCHES], "total": len(found), "truncated": len(found) > MAX_MATCHES,
            "asked": len(asked), "skipped_pods": skipped, "errors": errors, "capped": sorted(set(full)),
            "cut": sorted(set(cut)), "cut_seconds": REGEX_SECONDS,
            "window": window, "searched_at": round(now or time.time())}
