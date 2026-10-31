"""Log alerts: a saved log search that raises an alert (spike 12, built).

"More than 10 lines matching 'Unable to read frames' a minute from app X."
Built on homestead_logsearch (no Loki: each running pod's log through the
Kubernetes API) and homestead_alerts (debounced facts, raised and resolved
by the leader).

What shapes it:
- **Counts, never lines, never the query.** An alert carries the rule's
  name, its apps and a number. A log line can hold a password or a token,
  and so can a search for one; an alert's title and body go to alerts.json
  (kept in config backups), to every subscribed device's lock screen and to
  the bell. A name that repeats the query is refused for the same reason.
- **Its own thread, leader only.** The worker reads once a minute; the
  shared alert loop only takes its last facts (alert_facts), so a slow read
  or a slow expression never holds up any other alert source.
- **Bounded work.** At most MAX_RULES rules of MAX_APPS apps; MAX_CONTAINERS
  reads a minute of MAX_BYTES each, only what was written since the last
  read, READ_WORKERS at a time within READ_BUDGET seconds. A regular
  expression is matched in a child process killed after REGEX_SECONDS
  (Python cannot stop a regex mid-match in a thread); plain text, the
  default, is matched here.
- **Not read is not zero.** A rule whose app could not be read, was beyond
  the minute's reads, or whose expression ran out of time says so, keeps a
  null for that minute and is neither raised nor cleared.
- **Ids are the server's.** A client id is accepted only when it is
  well-formed and names an existing rule.
- **Crash loops count.** A container that restarted since the last round
  wrote lines before it died; its previous log is read too (#929).
- **Paused, or removed, is said.** A paused rule is not read. Pausing or
  deleting a rule whose alert is raised ends that alert as "paused" or
  "removed", not as "back under" a minute later. A rule whose expression
  runs out of time AUTO_PAUSE rounds in a row is paused by Homestead.
"""
import calendar
import math
import os
import re
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
import homestead_logsearch as LOGSEARCH
import homestead_shared as SHARED

EVERY = 60                      # seconds between reads
MAX_RULES = 20
MAX_APPS = 5                    # per rule
MAX_CONTAINERS = 40             # reads a minute, across every rule
MAX_BYTES = 256 * 1024          # per read
READ_WORKERS = 4
READ_BUDGET = 40                # seconds a round's reads may take
REGEX_SECONDS = 2               # per rule and round
PREVIEW_BYTES = 512 * 1024      # per container, for the hour a preview reads
PREVIEW_WORKERS = 8
PREVIEW_SECONDS = 10            # the preview matches an hour at once, on request
PREVIEW_BUDGET = 30             # seconds a preview's reads may take, holding log search's lock
MAX_BACK = 300                  # seconds a late round reads back at most
KEEP = 60                       # minutes of counts kept per rule
STALE = 180                     # facts older than this: could not look
WINDOWS = (1, 5, 15)            # minutes a threshold is counted over
SEVERITIES = ("info", "degraded", "critical")
AUTO_PAUSE = 10                 # rounds in a row out of time before Homestead pauses a rule
_ID = re.compile(r"^la-[0-9a-f]{12}$")
_COUNTED = ("apps", "query", "regex", "case")   # an edit to these starts the counts again
_lock = SHARED.SharedLock("logalerts")
_round = threading.Lock()
_starting = threading.Lock()
_cursors = {}                   # (ns, pod, container) -> (last stamp read, when); leader memory only
_restarts = {}                  # (ns, pod, container) -> restarts when last read; leader memory only
_last = {"at": 0.0, "facts": None}
_started = []
_inflight = {"round": 0, "preview": 0}   # reads still running, by who started them
_inflight_lock = threading.Lock()
_bound = {"dir": None, "workloads": None, "read": None, "is_leader": None, "beat": None,
          "withdraw": None, "note": None}


class TooSlow(ValueError):
    pass


def bind(data_dir=None, workloads=None, read=None, is_leader=None, beat=None, withdraw=None, note=None):
    _bound.update(dir=data_dir, workloads=workloads, read=read, is_leader=is_leader, beat=beat,
                  withdraw=withdraw, note=note)


def reset():
    """Forget the leader's memory: cursors, restarts and the last round."""
    _cursors.clear()
    _restarts.clear()
    _last.update(at=0.0, facts=None)


def _withdraw(key, title, body):
    if _bound["withdraw"]:
        return _bound["withdraw"](key, title, body)
    import homestead_alerts as ALERTS
    return ALERTS.withdraw(key, title, body)


def _note(fact):
    if _bound["note"]:
        return _bound["note"](fact)
    import homestead_alerts as ALERTS
    return ALERTS.note(fact)


def _workloads():
    if _bound["workloads"]:
        return _bound["workloads"]()
    import homestead_routes as ROUTER
    import homestead_workloads as WL
    return ROUTER.cached("wl", 5, WL.get_workloads)


def _read(*args):
    if _bound["read"]:
        return _bound["read"](*args)
    import homestead_kube as KUBE
    return KUBE.logsearch_read(*args)


def _is_leader():
    if _bound["is_leader"]:
        return _bound["is_leader"]()
    import homestead_leader as LEADER
    return LEADER.is_leader()


def _beat(error=None):
    if _bound["beat"]:
        return _bound["beat"]("logalerts", EVERY, error, leader_only=True)
    import homestead_heartbeats as HEART
    return HEART.beat("logalerts", EVERY, error, leader_only=True)


def _path():
    return os.path.join(_bound["dir"] or SHARED.DIR, "logalerts.json")


def _load():
    state = SHARED.read_json(_path(), {})
    state.setdefault("rules", [])
    state.setdefault("counts", {})   # rule id -> [[minute, count or None], ...]
    state.setdefault("status", {})   # rule id -> {"state", "detail", "at"}
    return state


def _save(state):
    SHARED.write_json(_path(), state)


def _apps(workloads):
    return {f"{w.get('ns')}/{w.get('name')}" for w in workloads or []}


def validate(body, known_apps=None, existing_ids=(), named=True):
    """A rule from a form, or ValueError. A preview needs no name (named=False)."""
    body = body if isinstance(body, dict) else {}
    rule_id = body.get("id")
    if rule_id is None:
        rule_id = "la-" + secrets.token_hex(6)
    elif not (isinstance(rule_id, str) and _ID.fullmatch(rule_id) and rule_id in existing_ids):
        raise ValueError("that log alert no longer exists; refresh and try again")
    name = str(body.get("name") or "").strip()
    if named and (not name or len(name) > 60):
        raise ValueError("give the alert a name of at most 60 characters")
    apps = body.get("apps") if isinstance(body.get("apps"), list) else []
    apps = [a for a in apps if isinstance(a, str) and "/" in a]
    if not apps:
        raise ValueError("choose the app (or up to five apps) to watch")
    if len(apps) > MAX_APPS:
        raise ValueError(f"a log alert watches at most {MAX_APPS} apps")
    if known_apps is not None and any(a not in known_apps for a in apps):
        raise ValueError("one of those apps is not running here")
    query, regex, case = str(body.get("query") or ""), bool(body.get("regex")), bool(body.get("case"))
    LOGSEARCH.matcher(query, regex, case)
    if named and len(query.strip()) >= 3 and query.strip().lower() in name.lower():
        raise ValueError("the name may not repeat the search text: alert names reach lock screens and config backups")
    try:
        threshold, window = int(body.get("threshold")), int(body.get("window") or 1)
    except (TypeError, ValueError):
        raise ValueError("the threshold is a number of lines, from 1") from None
    if not 1 <= threshold <= 100000:
        raise ValueError("the threshold is a number of lines, from 1")
    if window not in WINDOWS:
        raise ValueError("count over 1, 5 or 15 minutes")
    severity = body.get("severity") if body.get("severity") in SEVERITIES else "degraded"
    return {"id": rule_id, "name": name, "apps": sorted(set(apps)), "query": query, "regex": regex, "case": case,
            "threshold": threshold, "window": window, "severity": severity}


def save(body, known_apps=None):
    known = _apps(_workloads()) if known_apps is None else known_apps
    with _lock:
        state = _load()
        old = {r["id"]: r for r in state["rules"]}
        # An app the rule already watches stays allowed while nothing of it runs.
        same = old.get(body.get("id")) if isinstance(body, dict) and isinstance(body.get("id"), str) else None
        rule = validate(body, set(known) | set((same or {}).get("apps", [])), old)
        if rule["id"] not in old and len(old) >= MAX_RULES:
            raise ValueError(f"at most {MAX_RULES} log alerts")
        before = old.get(rule["id"])
        if before and any(before.get(k) != rule[k] for k in _COUNTED):
            state["counts"].pop(rule["id"], None)
            state["status"].pop(rule["id"], None)
        # An edit keeps a paused rule paused: Resume is its own choice.
        rule.update({k: before[k] for k in ("paused", "paused_note") if before and k in before})
        state["rules"] = [rule if r["id"] == rule["id"] else r for r in state["rules"]] + ([] if before else [rule])
        _save(state)
    return rule


def _existing(state, rule_id):
    rule = next((r for r in state["rules"] if r["id"] == rule_id), None) \
        if isinstance(rule_id, str) and _ID.fullmatch(rule_id) else None
    if rule is None:
        raise ValueError("that log alert no longer exists; refresh and try again")
    return rule


def delete(rule_id):
    with _lock:
        state = _load()
        rule = _existing(state, rule_id)
        state["rules"] = [r for r in state["rules"] if r["id"] != rule_id]
        state["counts"].pop(rule_id, None)
        state["status"].pop(rule_id, None)
        _save(state)
    # Its alert, if raised, ends as removed: not "back under" a minute later.
    _withdraw(f"logs:{rule_id}", f"{rule['name']}: removed", "This log alert was removed, so it no longer counts.")
    return {"ok": True}


def pause(rule_id, paused, note="", now=None):
    """Stop counting a rule, or start again. A raised alert ends as paused.
    On resume the paused minutes are kept as not read: a 5- or 15-minute
    window reaching back into them is never counted as 0 there."""
    if not isinstance(paused, bool):
        raise ValueError("say whether to pause the log alert or resume it")
    minute = int((time.time() if now is None else now) // 60)
    with _lock:
        state = _load()
        rule = _existing(state, rule_id)
        was = bool(rule.get("paused"))
        rule["paused"] = paused
        rule.pop("paused_note", None)
        if paused and note:
            rule["paused_note"] = note
        if paused and not was:
            rule["paused_at"] = minute
        if not paused:
            since = rule.pop("paused_at", minute)
            history = {m: c for m, c in state["counts"].get(rule_id, []) if m > minute - KEEP}
            history.update({m: None for m in range(max(since, minute - KEEP + 1), minute + 1)})
            state["counts"][rule_id] = sorted([m, c] for m, c in history.items())
            state["status"].pop(rule_id, None)
        _save(state)
    if paused and not was:
        _withdraw(f"logs:{rule_id}", f"{rule['name']}: paused" + (" by Homestead" if note else ""),
                  note or "This log alert was paused, so it does not count until it is resumed.")
    return {"ok": True, "paused": paused}


def listing():
    with _lock:
        state = _load()
    return {"rules": [{**r, "paused": bool(r.get("paused")), "counts": state["counts"].get(r["id"], []),
                       "status": {"state": "paused", "detail": r.get("paused_note", "")} if r.get("paused")
                       else state["status"].get(r["id"], {"state": "waiting", "detail": ""})}
                      for r in state["rules"]],
            "every": EVERY, "max_rules": MAX_RULES, "max_apps": MAX_APPS,
            # When the leader last counted: a list that stopped moving says so.
            "counted_at": max((s.get("at", 0) for s in state["status"].values()), default=None)}


def matching(lines, query, regex, case, limit=REGEX_SECONDS):
    """The indexes of the lines that match. TooSlow when an expression runs
    out of time: its child process (log search's run_regex) is killed."""
    if not regex:
        needle = query if case else query.lower()
        return [i for i, line in enumerate(lines) if needle in (line if case else line.lower())]
    lines = list(lines)
    hits, checked = LOGSEARCH.run_regex(lines, query, case, limit)
    if checked < len(lines):
        raise TooSlow(f"the expression took more than {limit:g} seconds")
    return hits


def leftover_reads():
    """Reads still running, left by a round or a preview whose time ran out."""
    with _inflight_lock:
        return _inflight["round"] + _inflight["preview"]


def _submit(pool, kind, fn, *args):
    """Start a read and count it until it ends, or is cancelled before it began:
    a budget that runs out leaves running reads behind, and nothing new of that
    kind starts beside them."""
    with _inflight_lock:
        _inflight[kind] += 1
    future = pool.submit(fn, *args)

    def done(_future):
        with _inflight_lock:
            _inflight[kind] -= 1
    future.add_done_callback(done)
    return future


def _since(key, now):
    cursor = _cursors.get(key)
    if not cursor:
        return EVERY
    return max(EVERY, min(MAX_BACK, math.ceil(now - cursor[1]) + 5))


def _minute(stamp):
    """The minute (since the epoch) a kubelet timestamp falls in."""
    return calendar.timegm(time.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S")) // 60


def _read_new(target, now, previous=False):
    """What a container wrote since its cursor, as (minute written, line)
    pairs (an unstamped line counts in this round's minute), and the last
    minute read when a read was cut (None when none was). With previous, the
    container restarted since the last round: what the one before it wrote
    is read first."""
    ns, _app, pod, container = target
    key = (ns, pod, container)
    since = _since(key, now)
    texts = ([_read(ns, pod, container, since, MAX_BYTES, True)] if previous else []) + \
        [_read(ns, pod, container, since, MAX_BYTES)]
    last = (_cursors.get(key) or ("", 0))[0]
    out, newest, here, reached = [], last, int(now // 60), None
    for text in texts:
        for raw in text.splitlines():
            stamp = LOGSEARCH._STAMP.match(raw)
            at, line = (stamp.group(1), stamp.group(2)) if stamp else ("", raw)
            if at and at <= last:
                continue
            newest = max(newest, at)
            out.append((_minute(at) if at else here, line[:LOGSEARCH.MAX_LINE]))
        if len(text) >= MAX_BYTES and reached is None:
            # limitBytes keeps the oldest lines: what came after the last one read was not.
            reached = _minute(newest) if newest else int((now - since) // 60)
    return key, newest, out, reached, since


def _restart_counts(workloads):
    """(ns, pod, container) -> how often that container has restarted."""
    return {(w.get("ns"), pod.get("name"), c.get("name")): int(c.get("restarts") or 0)
            for w in workloads or [] for pod in w.get("pods") or [] for c in pod.get("containers") or []}


def _plan(rules, workloads, turn=0):
    """Containers to read this round, rule by rule until MAX_CONTAINERS,
    starting at a different rule each round so that no rule is always the
    one left out; the apps left out are named."""
    chosen, skipped = [], set()
    rules = rules[turn % len(rules):] + rules[:turn % len(rules)] if rules else []
    for rule in rules:
        for target in LOGSEARCH.targets(workloads, rule["apps"]):
            if target in chosen:
                continue
            if len(chosen) >= MAX_CONTAINERS:
                skipped.add(f"{target[0]}/{target[1]}")
            else:
                chosen.append(target)
    return chosen, skipped


def _names(apps):
    return ", ".join(a.split("/", 1)[1] for a in apps)


def _count(rule, read, unread, skipped, running, busy=False):
    """(state, detail, {minute: matches} or None, covered from, read up to)
    for one rule. "Read up to" is the last minute a cut read reached (None
    when nothing was cut): the minutes after it were not read."""
    absent = sorted(a for a in rule["apps"] if a not in running)
    missing = sorted(a for a in rule["apps"] if a in unread)
    later = sorted(a for a in rule["apps"] if a in skipped)
    if absent:
        return "absent", f"nothing of {_names(absent)} is running, so there is nothing to read", None, None, None
    if missing:
        return "unread", f"could not read {_names(missing)}" + (
            ": the last round is still reading its logs" if busy else ""), None, None, None
    if later:
        return "skipped", (f"not read this minute: over the {MAX_CONTAINERS} containers a minute "
                           f"Homestead reads for log alerts ({_names(later)})"), None, None, None
    pairs = [pair for app in rule["apps"] for pair in read[app]["lines"]]
    try:
        hits = matching([line for _, line in pairs], rule["query"], rule["regex"], rule["case"])
    except ValueError as error:
        return ("slow" if isinstance(error, TooSlow) else "failed"), str(error), None, None, None
    per = {}
    for i in hits:
        per[pairs[i][0]] = per.get(pairs[i][0], 0) + 1
    cut = [read[app]["upto"] for app in rule["apps"] if read[app]["upto"] is not None]
    return "ok", "", per, max(read[app]["from"] for app in rule["apps"]), min(cut) if cut else None


def _over(history, rule, minute, late=False):
    """The most matching lines in any window of the rule's length ending this
    minute or the one just finished (whose last seconds this round read), and
    the one before when the last round ran late, so no minute goes unchecked."""
    ends = (minute - 2, minute - 1, minute) if late else (minute - 1, minute)
    return max(sum(c for m, c in history.items() if end - rule["window"] < m <= end and c is not None)
               for end in ends)


def evaluate(now=None):
    """One round: read what the rules' apps wrote since the last read, count
    each rule's matches in the minute each line was written, keep the counts
    and return the alert facts. The caller checks leadership."""
    now = time.time() if now is None else now
    minute = int(now // 60)
    with _round:
        late = bool(_last["at"]) and now - _last["at"] > 90
        with _lock:
            rules = [r for r in _load()["rules"] if not r.get("paused")]
        sig = {r["id"]: [r.get(k) for k in _COUNTED] for r in rules}
        workloads = _workloads()
        targets, skipped = _plan(rules, workloads, minute)
        running = {f"{t[0]}/{t[1]}" for t in LOGSEARCH.targets(workloads, sorted({a for r in rules for a in r["apps"]}))} \
            if rules else set()
        # A restart seen since the last round: the container before it wrote
        # lines too (a crash loop's). Restarts from before the first look are not news.
        counts = _restart_counts(workloads)
        restarted = {t for t in targets if counts.get((t[0], t[2], t[3]), 0) > _restarts.get((t[0], t[2], t[3]), math.inf)}
        # Restarts as last seen, kept for containers still here; a read updates its own.
        seen = {k: v for k, v in _restarts.items() if k in counts}
        read, unread, capped, cursors = {}, set(), set(), {}
        busy = bool(_inflight["round"])
        if busy:
            # The last round's reads ran out of time and still run: none beside them.
            pairs, finished = [(None, t) for t in targets], set()
        else:
            pool = ThreadPoolExecutor(max_workers=READ_WORKERS)
            futures = {_submit(pool, "round", _read_new, t, now, t in restarted): t for t in targets}
            finished, _ = wait(futures, timeout=READ_BUDGET)
            pool.shutdown(wait=False, cancel_futures=True)
            pairs = list(futures.items())
        for future, target in pairs:
            app = f"{target[0]}/{target[1]}"
            # The restart is taken as seen even when the read fails: a previous
            # log gone for good (a node rebooted) would otherwise hold the rule
            # unread for ever. The failed round's minutes are not counted
            # (unread, never 0), and they hold what the old container wrote last.
            seen[(target[0], target[2], target[3])] = counts.get((target[0], target[2], target[3]), 0)
            try:
                if future is None or future not in finished:
                    raise TimeoutError("read took too long")
                key, newest, got, reached, since = future.result()
            except Exception:
                unread.add(app)
                continue
            cursors[key] = (newest, now)
            row = read.setdefault(app, {"lines": [], "from": 0, "upto": None})
            row["lines"].extend(got)
            row["from"] = max(row["from"], int((now - since) // 60) + 1)
            if reached is not None:
                capped.add(app)
                row["upto"] = reached if row["upto"] is None else min(row["upto"], reached)
        # A container that is gone, or was not read, starts again from a fresh read.
        _cursors.clear()
        _cursors.update(cursors)
        _restarts.clear()
        _restarts.update(seen)
        counted = {r["id"]: _count(r, read, unread, skipped, running, busy) for r in rules}
        facts, paused = [], []
        with _lock:
            fresh = _load()
            for rule in fresh["rules"]:
                rid = rule["id"]
                if rule.get("paused"):
                    continue   # paused, perhaps during the round: neither raised nor counted
                if rid not in counted:
                    continue   # new during the round: counted from the next one
                if sig[rid] != [rule.get(k) for k in _COUNTED]:
                    # Edited during the round: counted from the next one, and
                    # an alert it raised is held meanwhile, neither raised nor cleared.
                    facts.append({"unknown_key": f"logs:{rid}"})
                    continue
                state, detail, per, start, upto = counted[rid]
                history = {m: c for m, c in fresh["counts"].get(rid, []) if m > minute - KEEP}
                if per is None:
                    # Not counted: this minute, and the one just finished (of which
                    # only its first seconds were counted), are not read; never 0.
                    history[minute - 1] = history[minute] = None
                    facts.append({"unknown_key": f"logs:{rid}"})
                else:
                    # A minute once not read stays so: a later part count is not its count.
                    for m in range(start, minute + 1):
                        if upto is not None and m > upto:
                            history[m] = None
                        elif m not in history:
                            history[m] = 0
                    for m, n in per.items():
                        if m > minute - KEEP and history.get(m, 0) is not None:
                            history[m] = (history.get(m) or 0) + n
                fresh["counts"][rid] = sorted([m, c] for m, c in history.items())
                slow = (fresh["status"].get(rid, {}).get("slow", 0) + 1) if state == "slow" else 0
                fresh["status"][rid] = {"state": state, "detail": detail, "at": int(now), "slow": slow}
                if slow >= AUTO_PAUSE:
                    # Out of time every minute says nothing new: stop, and say so once.
                    rule["paused_at"] = minute
                    rule["paused"], rule["paused_note"] = True, (
                        f"Homestead paused it: its expression ran out of time {AUTO_PAUSE} minutes in a row. "
                        "Make the expression simpler, or use plain text, then resume it.")
                    facts.remove({"unknown_key": f"logs:{rid}"})
                    paused.append(rule)
                if per is not None:
                    total = _over(history, rule, minute, late)
                    if total > rule["threshold"]:
                        facts.append(fact(rule, total, capped & set(rule["apps"])))
            ids = {r["id"] for r in fresh["rules"]}
            fresh["counts"] = {k: v for k, v in fresh["counts"].items() if k in ids}
            fresh["status"] = {k: v for k, v in fresh["status"].items() if k in ids}
            _save(fresh)
        _last.update(at=now, facts=facts)
        for rule in paused:
            title = f"{rule['name']}: paused by Homestead"
            if _withdraw(f"logs:{rule['id']}", title, rule["paused_note"]) is None:
                _note({"key": f"logs:{rule['id']}:paused", "category": "logs", "severity": "info", "event": True,
                       "title": title, "body": rule["paused_note"], "href": "/containers?panel=logalerts"})
        return facts


def fact(rule, total, capped=()):
    """What the bell, push and alerts.json see: the rule's name, its apps and
    a count. No line, and no query: a search for a token is the token.
    Raised on the alert loop's first look: the counting window is the
    debounce, and the next round replaces the fact before a further hold
    would end."""
    span = "a minute" if rule["window"] == 1 else f"{rule['window']} minutes"
    level = max(0, (total // max(1, rule["threshold"])).bit_length() - 1)   # 1x, 2x, 4x... over
    return {"key": f"logs:{rule['id']}", "category": "logs", "severity": rule["severity"], "urgent": True,
            "title": f"{rule['name']}: {total} matching lines in {span}",
            "resolved": f"{rule['name']}: back under {rule['threshold']} in {span}",
            "body": f"{_names(rule['apps'])} wrote more than {rule['threshold']} lines matching this log alert in {span}"
                    + (" (at least: more than Homestead reads at once)" if capped else "")
                    + ". Open Log alerts to search them.",
            "signals": {"level": level}, "href": "/containers?panel=logalerts"}


def alert_facts(now=None):
    """The last round's facts for the shared alert loop, which never reads
    logs itself. None when there is no recent round: could not look. A rule
    deleted or paused since that round is left out: its alert has ended."""
    now = time.time() if now is None else now
    if _last["facts"] is None or now - _last["at"] > STALE:
        return None
    with _lock:
        live = {f"logs:{r['id']}" for r in _load()["rules"] if not r.get("paused")}
    # When it was taken: a withdraw after this wins over it (homestead_alerts.observe).
    taken = time.time()
    return [{**f, "collected": taken} if "key" in f else f
            for f in _last["facts"] if f.get("key", f.get("unknown_key")) in live]


def step(now=None):
    """One turn of the worker: a round on the leader, nothing elsewhere."""
    if not _is_leader():
        reset()
        return None
    try:
        facts = evaluate(now)
        _beat()
        return facts
    except Exception as error:
        _beat(error)
        print(f"logalerts: {str(error)[:160]}", flush=True)
        return None


def run(sleep=time.sleep, clock=time.time):
    """A round two seconds into every minute, so the minute just finished is
    read whole."""
    while True:
        step()
        sleep(EVERY - clock() % EVERY + 2)


def start():
    """Start the worker once; the alert loop calls this before its own loop."""
    with _starting:
        if _started:
            return
        _started.append(threading.Thread(target=run, name="logalerts", daemon=True))
    _started[0].start()


def preview(body, now=None):
    """What a rule would have done over the last hour: counts per minute and
    the minutes over its threshold. No lines. Takes log search's one-at-a-time
    limit; reads PREVIEW_BYTES per container, several at once."""
    body = {k: v for k, v in (body if isinstance(body, dict) else {}).items() if k != "id"}
    workloads = _workloads()
    rule = validate(body, _apps(workloads), named=False)
    now = time.time() if now is None else now

    def one(target):
        ns, _app, pod, container = target
        return _read(ns, pod, container, 3600, PREVIEW_BYTES)

    with LOGSEARCH.exclusive():
        if _inflight["preview"]:
            raise ValueError("an earlier try is still reading logs; try again in a moment")
        stamps, found, unread, capped = [], [], set(), set()
        targets = LOGSEARCH.targets(workloads, rule["apps"])[:MAX_CONTAINERS]
        pool = ThreadPoolExecutor(max_workers=PREVIEW_WORKERS)
        futures = [(t, _submit(pool, "preview", one, t)) for t in targets]
        finished, _ = wait([f for _, f in futures], timeout=PREVIEW_BUDGET)
        pool.shutdown(wait=False, cancel_futures=True)
        for (ns, app, _pod, _container), future in futures:
            try:
                if future not in finished:
                    raise TimeoutError("read took too long")
                text = future.result()
            except Exception:
                unread.add(app)
                continue
            if len(text) >= PREVIEW_BYTES:
                capped.add(app)
            for raw in text.splitlines():
                stamp = LOGSEARCH._STAMP.match(raw)
                if stamp:
                    stamps.append(stamp.group(1))
                    found.append(stamp.group(2)[:LOGSEARCH.MAX_LINE])
        try:
            hits = matching(found, rule["query"], rule["regex"], rule["case"], PREVIEW_SECONDS)
        except TooSlow:
            raise ValueError(f"trying it on the last hour took more than {PREVIEW_SECONDS} seconds: too many lines, "
                             "or too slow an expression. Try plain text, or a simpler expression") from None
    per = {}
    for i in hits:
        per[_minute(stamps[i])] = per.get(_minute(stamps[i]), 0) + 1
    minute = int(now // 60)
    counts = [[m, per.get(m, 0)] for m in range(minute - 59, minute + 1)]
    over = [m for m, _ in counts if sum(c for mm, c in counts if m - rule["window"] < mm <= m) > rule["threshold"]]
    return {"counts": counts, "over_minutes": len(over), "unread": sorted(unread), "capped": sorted(capped)}


def _body(request):
    return request.body if isinstance(request.body, dict) else {}


def _pause(body):
    return pause(body.get("id"), body.get("paused"))


ROUTES = {
    ("GET", "/api/logalerts"): ("viewer", lambda request: listing()),
    ("POST", "/api/logalerts"): ("operator", lambda request: save(_body(request))),
    ("POST", "/api/logalerts/delete"): ("operator", lambda request: delete(_body(request).get("id"))),
    ("POST", "/api/logalerts/pause"): ("operator", lambda request: _pause(_body(request))),
    ("POST", "/api/logalerts/preview"): ("viewer", lambda request: preview(_body(request))),
}
