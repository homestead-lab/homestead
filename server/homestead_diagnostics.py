"""Private, bounded diagnostic recordings and explicitly selected exports.

Originals never enter ordinary settings, backups or public issue drafts. Each
export is derived from the same snapshot; an anonymisation failure cannot fall
back to an original. No Kubernetes Secret or workload specification is read.
"""
import copy
import io
import ipaddress
import json
import os
import re
import secrets
import time
import zipfile
import urllib.parse

import homestead_shared as SHARED

DIRECTORY = "/data/diagnostics"
TTL = 86400
MAX_SECONDS = 600
MAX_EVENTS = 10000
MAX_EVENT_BYTES = 2 * 1024 * 1024
MAX_SOURCE_BYTES = 1024 * 1024
MAX_RECORDS = 20
MAX_BATCH = 100
READ = JOBS = None
VERSION = "unknown"
NAMESPACE = "lab"
POD = ""
LOCK = SHARED.SharedLock("diagnostics", strict=True, directory=lambda: DIRECTORY)
KINDS = {"click", "change", "focus", "toggle", "scroll", "key", "navigation",
         "viewport", "error", "rejection", "request", "server", "lifecycle", "ui"}
FIELDS = {"kind", "at", "target", "action", "page", "method", "path", "status",
          "duration", "request", "operation", "message", "width", "height", "scale",
          "theme", "x", "y", "checked", "expanded", "key", "line", "column"}


def bind(directory, read, jobs, version, namespace, pod):
    global DIRECTORY, READ, JOBS, VERSION, NAMESPACE, POD
    DIRECTORY = os.path.join(directory, "diagnostics")
    READ, JOBS, VERSION, NAMESPACE, POD = read, jobs, version, namespace, pod


def _path(report_id):
    if not re.fullmatch(r"[a-f0-9]{32}", str(report_id)):
        raise ValueError("invalid diagnostic report")
    return os.path.join(DIRECTORY, report_id + ".json")


def _save(record):
    os.makedirs(DIRECTORY, mode=0o700, exist_ok=True)
    SHARED.write_json(_path(record["id"]), record, mode=0o600, separators=(",", ":"))


def _load(report_id, owner):
    try:
        with open(_path(report_id), encoding="utf-8") as handle:
            record = json.load(handle)
    except FileNotFoundError:
        raise ValueError("report not found or expired") from None
    if record["owner"] != owner:
        raise PermissionError("this report belongs to another administrator")
    if time.time() >= record["expires"]:
        raise ValueError("report expired")
    if record["status"] == "recording" and time.time() - record["updated"] > 90:
        record["status"] = "interrupted"
    return record


def cleanup():
    with LOCK:
        if not os.path.isdir(DIRECTORY):
            return
        for name in os.listdir(DIRECTORY):
            if not re.fullmatch(r"[a-f0-9]{32}\.json", name):
                continue
            path = os.path.join(DIRECTORY, name)
            try:
                with open(path, encoding="utf-8") as handle:
                    expired = json.load(handle)["expires"] <= time.time()
                if expired:
                    os.remove(path)
            except (ValueError, KeyError):
                # A corrupt record must not retain private diagnostics forever.
                if os.path.getmtime(path) + TTL <= time.time():
                    os.remove(path)


def run():
    while True:
        try:
            cleanup()
        except Exception:
            pass  # A data handoff can temporarily fence this directory.
        time.sleep(300)


def _summary(record):
    return {key: record[key] for key in ("id", "title", "status", "created", "expires", "updated", "truncated")} | {
        "events": len(record["events"]), "sources": record["manifest"]}


def listing(owner):
    cleanup()
    with LOCK:
        rows = []
        for name in os.listdir(DIRECTORY):
            if re.fullmatch(r"[a-f0-9]{32}\.json", name):
                try:
                    rows.append(_summary(_load(name[:-5], owner)))
                except (PermissionError, ValueError):
                    continue
        return sorted(rows, key=lambda row: row["created"], reverse=True)


def create(owner, package=False):
    cleanup()
    with LOCK:
        if len([name for name in os.listdir(DIRECTORY) if name.endswith(".json")]) >= MAX_RECORDS:
            raise ValueError("diagnostics storage is full; delete an old report first")
        now = time.time()
        record = {"id": secrets.token_hex(16), "owner": owner, "created": now,
                  "updated": now, "expires": now + TTL, "status": "draft" if package else "recording",
                  "title": "Logs package" if package else "Bug report", "comment": "", "version": VERSION,
                  "events": [], "event_bytes": 0, "last_batch": 0, "truncated": False,
                  "sources": {}, "manifest": [], "identifiers": [owner], "collected": False}
        _save(record)
        return _summary(record)


def _event(value):
    if not isinstance(value, dict) or not isinstance(value.get("kind"), str) or value["kind"] not in KINDS:
        raise ValueError("invalid diagnostic event")
    if "at" in value and (type(value["at"]) not in (int, float) or not 0 <= value["at"] < 1e13):
        raise ValueError("invalid diagnostic event time")
    result = {}
    for key, item in value.items():
        if key not in FIELDS:
            continue
        if isinstance(item, str):
            result[key] = item[:500 if key == "message" else 180]
        elif isinstance(item, (int, float, bool)) and abs(item) < 1e13:
            result[key] = item
    # Queries may carry typed searches, credentials or file contents.
    for key in ("path", "page"):
        if key in result:
            result[key] = str(result[key]).split("?", 1)[0].split("#", 1)[0]
    return result


def _append(record, events):
    for event in events:
        size = len(json.dumps(event).encode())
        if len(record["events"]) >= MAX_EVENTS or record["event_bytes"] + size > MAX_EVENT_BYTES:
            record["truncated"] = True
            break
        record["events"].append(event)
        record["event_bytes"] += size


def append(report_id, owner, batch, events):
    if type(batch) is not int or batch < 1 or not isinstance(events, list) or len(events) > MAX_BATCH:
        raise ValueError("invalid diagnostic batch")
    validated = [_event(event) for event in events]
    with LOCK:
        record = _load(report_id, owner)
        if batch <= record["last_batch"]:
            return {"batch": record["last_batch"], "status": record["status"]}
        if batch != record["last_batch"] + 1:
            raise ValueError("diagnostic batch out of order")
        if record["status"] not in ("recording", "interrupted"):
            raise ValueError("recording has stopped")
        _append(record, [event for event in validated if 0 <= event.get("at", 0) <= MAX_SECONDS * 1000])
        record.update(last_batch=batch, updated=time.time(),
                      status="interrupted" if time.time() - record["created"] > MAX_SECONDS else "recording")
        _save(record)
        return {"batch": batch, "status": record["status"], "truncated": record["truncated"]}


def server_event(report_id, owner, event):
    with LOCK:
        record = _load(report_id, owner)
        if record["status"] != "recording" or time.time() - record["created"] > MAX_SECONDS:
            return
        _append(record, [_event({**event, "at": round((time.time() - record["created"]) * 1000)})])
        _save(record)


def stop(report_id, owner):
    with LOCK:
        record = _load(report_id, owner)
        if record["status"] in ("recording", "interrupted"):
            record.update(status="draft", updated=time.time())
            _save(record)
        return _summary(record)


def delete(report_id, owner):
    with LOCK:
        _load(report_id, owner)
        os.remove(_path(report_id))
        return {"ok": True}


def draft(report_id, owner, title, comment):
    if not isinstance(title, str) or not isinstance(comment, str) or len(title) > 120 or len(comment) > 4000:
        raise ValueError("summary or comment is too long")
    with LOCK:
        record = _load(report_id, owner)
        record.update(title=title.strip() or "Bug report", comment=comment, updated=time.time())
        _save(record)
        return _summary(record)


def _collect(record, selected, seconds):
    sources, manifest, identifiers = {}, [], list(record["identifiers"])

    def source(name, call):
        try:
            value = call()
            text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
            raw = text.encode("utf-8")
            if not isinstance(value, str) and len(raw) >= MAX_SOURCE_BYTES:
                sources[name] = json.dumps({"truncated": True, "preview": text[:MAX_SOURCE_BYTES // 4]})
            else:
                sources[name] = raw[:MAX_SOURCE_BYTES].decode("utf-8", "ignore")
            manifest.append({"source": name, "state": "truncated" if len(raw) >= MAX_SOURCE_BYTES else "included"})
        except Exception:
            manifest.append({"source": name, "state": "unavailable"})

    def health():
        nodes = READ("/api/v1/nodes?limit=200", timeout=5).get("items", [])
        out = []
        for node in nodes[:200]:
            meta, status = node.get("metadata", {}), node.get("status", {})
            identifiers.append(meta.get("name", ""))
            out.append({"name": meta.get("name"), "conditions": [
                {"type": row.get("type"), "status": row.get("status")} for row in status.get("conditions", [])],
                "kubelet": status.get("nodeInfo", {}).get("kubeletVersion")})
        return {"homestead": VERSION, "nodes": out}

    source("platform.json", health)
    if "service" in selected:
        def service(previous=False):
            if not POD:
                raise ValueError("pod unavailable")
            query = urllib.parse.urlencode({"timestamps": "true", "sinceSeconds": seconds,
                                           "limitBytes": MAX_SOURCE_BYTES, "tailLines": 5000,
                                           "previous": str(previous).lower()})
            return READ(f"/api/v1/namespaces/{urllib.parse.quote(NAMESPACE, safe='')}/pods/{urllib.parse.quote(POD, safe='')}/log?{query}", timeout=5)
        identifiers.extend([NAMESPACE, POD])
        source("homestead.log", service)
        source("homestead-previous.log", lambda: service(True))
    if "cluster" in selected:
        def events():
            rows = READ("/api/v1/events?limit=500", timeout=5)
            out = []
            for row in rows.get("items", [])[:500]:
                obj = row.get("involvedObject", {})
                identifiers.extend([obj.get("name", ""), obj.get("namespace", "")])
                out.append({"time": row.get("lastTimestamp") or row.get("eventTime"), "type": row.get("type"),
                            "reason": row.get("reason"), "message": row.get("message"),
                            "object": {key: obj.get(key) for key in ("kind", "name", "namespace")}})
            return {"events": out, "limited": bool(rows.get("metadata", {}).get("continue")),
                    "note": "Recent available Kubernetes events; timestamps may predate the selected log window."}
        source("cluster-events.json", events)
    if "jobs" in selected:
        def jobs():
            # Job summaries and history only: log readers may open VM consoles.
            ids = {event.get("operation") for event in record["events"] if event.get("operation")}
            return JOBS.diagnostic_summaries(ids) if JOBS else []
        source("related-jobs.json", jobs)
    return sources, manifest, list(set(filter(None, identifiers)))


def prepare(report_id, owner, title, comment, selected, seconds=900):
    if not isinstance(selected, list) or len(selected) > 3 or any(value not in ("service", "cluster", "jobs") for value in selected):
        raise ValueError("invalid diagnostic sources")
    if seconds not in (900, 3600, 86400):
        raise ValueError("invalid log time range")
    if not isinstance(title, str) or not isinstance(comment, str) or len(title) > 120 or len(comment) > 4000:
        raise ValueError("summary or comment is too long")
    with LOCK:
        record = _load(report_id, owner)
        if record["status"] == "recording":
            raise ValueError("stop recording before preparing the report")
    # Network collection never holds the shared storage lock.
    sources, manifest, identifiers = _collect(record, selected, seconds)
    with LOCK:
        record = _load(report_id, owner)
        record.update(title=title.strip() or "Bug report", comment=comment, sources=sources,
                      manifest=manifest, identifiers=identifiers, collected=True, status="ready", updated=time.time())
        _save(record)
        return _summary(record)


class Anonymiser:
    def __init__(self, identifiers):
        self.aliases = {}
        self.identifiers = sorted(set(filter(None, identifiers)), key=len, reverse=True)

    def alias(self, value):
        if value not in self.aliases:
            self.aliases[value] = f"<identifier-{len(self.aliases) + 1}>"
        return self.aliases[value]

    def text(self, value):
        text = str(value)
        text = re.sub(r"-----BEGIN [^-]*PRIVATE KEY-----[\s\S]*?(?:-----END [^-]*PRIVATE KEY-----|$)", "[secret removed]", text)
        text = re.sub(r"(?im)(authorization|proxy-authorization|cookie|set-cookie)\s*[:=][^\r\n]*", r"\1: [secret removed]", text)
        text = re.sub(r'''(?ix)(["']?(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|client[_-]?secret|credential)["']?\s*[:=]\s*)(?:"[^"\r\n]*"|'[^'\r\n]*'|[^\s,;}]+)''', r"\1[secret removed]", text)
        text = re.sub(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]+", "[secret removed]", text)
        text = re.sub(r"\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|AKIA[A-Z0-9]{16}|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)\b", "[secret removed]", text)
        text = re.sub(r"\b(?:hsk_[A-Za-z0-9_-]+|[A-Za-z0-9_+/=-]{40,})\b", "[secret removed]", text)
        text = re.sub(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s<>\"']+", lambda m: self.alias(m[0]), text)
        text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", lambda m: self.alias(m[0]), text)
        def address(match):
            try:
                ipaddress.ip_address(match[0])
                return self.alias(match[0])
            except ValueError:
                return match[0]  # A timestamp such as 12:30:40 is not an IPv6 address.
        text = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}\b|(?<!\w)(?:[a-fA-F0-9]{0,4}:){2,}[a-fA-F0-9:]{0,39}(?!\w)", address, text)
        text = re.sub(r"(?i)\b(?:[a-f0-9]{2}:){5}[a-f0-9]{2}\b", lambda m: self.alias(m[0]), text)
        text = re.sub(r"(?i)\b(node|namespace|pod|workload|host|user)(\s*[=:]\s*)([^\s,;]+)", lambda m: m[1] + m[2] + self.alias(m[3]), text)
        for name in self.identifiers:
            text = re.sub(r"(?<![\w-])" + re.escape(name) + r"(?![\w-])", lambda m: self.alias(m[0]), text)
        text = re.sub(r"\b[a-f0-9]{8}-[a-f0-9-]{27,}\b|\b(?:[a-zA-Z0-9-]+\.)+(?:local|lan|internal|home|com|net|org)\b", lambda m: self.alias(m[0]), text)
        return text

    def value(self, value):
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self.value(item) for item in value]
        if isinstance(value, dict):
            return {key: self.value(item) for key, item in value.items()}
        return value


def snapshot(report_id, owner, format="anonymised"):
    if format not in ("anonymised", "full"):
        raise ValueError("choose anonymised or full logs")
    with LOCK:
        record = copy.deepcopy(_load(report_id, owner))
    result = {key: record[key] for key in ("id", "version", "title", "comment", "created", "expires",
                                         "status", "events", "sources", "manifest", "truncated")}
    result["format"] = format
    result["events"].sort(key=lambda event: event.get("at", 0))
    if format == "anonymised":
        anonymiser = Anonymiser(record["identifiers"])
        sources = result.pop("sources")
        result = anonymiser.value(result)
        result["sources"] = {name: json.dumps(anonymiser.value(json.loads(value)), ensure_ascii=False, indent=2)
                             if name.endswith(".json") else anonymiser.text(value) for name, value in sources.items()}
    return result


def report_text(record):
    lines = ["Homestead bug report", f"Format: {record['format']}", f"Homestead: {record['version']}",
             f"Summary: {record['title']}", "", record["comment"], "", "Timeline (milliseconds since recording started)"]
    lines.extend(json.dumps(event, ensure_ascii=False) for event in record["events"])
    lines.extend(["", "Collection results", json.dumps(record["manifest"], ensure_ascii=False, indent=2)])
    if record["truncated"]:
        lines.append("Timeline truncated: recording reached its event or size limit.")
    for name, value in record["sources"].items():
        lines.extend(["", "--- " + name + " ---", value])
    return "\n".join(lines) + "\n"


def export(report_id, owner, format, package=False):
    record = snapshot(report_id, owner, format)
    basename = f"homestead-diagnostics-{report_id[:8]}-{format}"
    log = report_text(record).encode("utf-8")
    if not package:
        return basename + ".log", "text/plain; charset=utf-8", log
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("report.log", log)
        archive.writestr("events.json", json.dumps(record["events"], ensure_ascii=False))
        archive.writestr("manifest.json", json.dumps({"format": format, "sources": record["manifest"],
                                                      "truncated": record["truncated"], "version": VERSION}))
        for name, value in record["sources"].items():
            archive.writestr(name, value)
    return basename + ".zip", "application/zip", output.getvalue()


def issue(report_id, owner):
    # A compact, independently anonymised draft; never an arbitrary attachment
    # or a format supplied by the caller. The browser shows these exact strings.
    record = snapshot(report_id, owner, "anonymised")
    body = ("### What happened\n" + record["comment"][:180] + "\n\n### Diagnostics\nHomestead " +
            record["version"] + f"\n{len(record['events'])} UI/request events captured.\n" +
            "Anonymised diagnostics available separately. No logs attached.\n")
    title = record["title"][:90]
    prefix = "https://github.com/wjcloudy/homestead/issues/new?"
    original_body = body
    while len(prefix + urllib.parse.urlencode({"title": title, "body": body})) > 1800:
        body = body[:-20]
    return {"title": title, "body": body, "url": prefix + urllib.parse.urlencode({"title": title, "body": body}),
            "comment_shortened": len(record["comment"]) > 180 or body != original_body}
