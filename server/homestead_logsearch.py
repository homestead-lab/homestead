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
"""
import re
import time
from concurrent.futures import ThreadPoolExecutor

MAX_PODS = 60
MAX_BYTES = 2 * 1024 * 1024        # per container
MAX_LINE = 2000
MAX_MATCHES = 500
WINDOWS = {"15m": 900, "1h": 3600, "6h": 21600, "24h": 86400}
_SLOW = re.compile(r"\([^)]*[*+|][^)]*\)\s*[*+{]")   # (a+)+, (a|aa)* and the like
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
    every = targets(workloads, apps)
    asked, skipped = every[:MAX_PODS], max(0, len(every) - MAX_PODS)
    errors, full = [], []

    def one(target):
        ns, app, pod, container = target
        try:
            text = read(ns, pod, container, since, MAX_BYTES)
        except Exception as error:
            errors.append(f"{app} ({pod}): {str(error)[:120]}")
            return []
        if len(text) >= MAX_BYTES:
            full.append(app)
        rows = []
        for raw in text.splitlines():
            stamp = _STAMP.match(raw)
            at, line = (stamp.group(1), stamp.group(2)) if stamp else ("", raw)
            line = line[:MAX_LINE]
            if match(line):
                rows.append({"at": at, "ns": ns, "app": app, "pod": pod, "container": container, "line": line})
        return rows

    with ThreadPoolExecutor(max_workers=8) as pool:
        found = [row for rows in pool.map(one, asked) for row in rows]
    found.sort(key=lambda r: r["at"], reverse=True)
    return {"matches": found[:MAX_MATCHES], "total": len(found), "truncated": len(found) > MAX_MATCHES,
            "asked": len(asked), "skipped_pods": skipped, "errors": errors, "capped": sorted(set(full)),
            "window": window, "searched_at": round(now or time.time())}
