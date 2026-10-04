"""Homestead, driven as the browser drives it: a session cookie, the
X-Homestead-Auth header on changes, and patience while Homestead itself is
away - a reboot or shutdown takes it down with the host."""
import http.cookiejar
import json
import time
import urllib.error
import urllib.request

from . import log


class HomesteadError(RuntimeError):
    def __init__(self, status, body, path):
        self.status, self.body, self.path = status, body, path
        super().__init__(f"{path}: HTTP {status}: {str(body)[:400]}")


class Homestead:
    def __init__(self, urls, username="admin", password="e2e-Password-1"):
        self.urls = list(urls)
        self.username, self.password = username, password
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def _once(self, base, method, path, body, timeout):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json", "X-Homestead-Auth": "1"})
        try:
            with self.opener.open(req, timeout=timeout) as response:
                raw = response.read()
                return response.status, json.loads(raw) if raw else {}
        except urllib.error.HTTPError as error:
            raw = error.read()
            try:
                return error.code, json.loads(raw)
            except ValueError:
                return error.code, raw.decode(errors="replace")

    def request(self, method, path, body=None, ok=(200, 202), wait=300, timeout=30):
        """One call, trying each address; while Homestead is away (connection
        refused, 502/503) it keeps asking for up to `wait` seconds."""
        deadline, last = time.time() + wait, None
        while True:
            for base in self.urls:
                try:
                    status, answer = self._once(base, method, path, body, timeout)
                except (OSError, urllib.error.URLError) as error:
                    last = error
                    continue
                if status == 401 and path not in ("/api/auth/login", "/api/auth/setup", "/api/auth/state"):
                    self.sign_in()
                    status, answer = self._once(base, method, path, body, timeout)
                if status in (502, 503, 504):
                    last = HomesteadError(status, answer, path)
                    continue
                if status not in ok:
                    raise HomesteadError(status, answer, path)
                log.debug(f"{method} {path} -> {status}")
                return answer
            if time.time() > deadline:
                raise TimeoutError(f"{method} {path}: Homestead did not answer within {wait}s ({last})")
            time.sleep(5)

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self.request("POST", path, body if body is not None else {}, **kw)

    def sign_in(self):
        state = self.get("/api/auth/state", wait=900)
        if state.get("setup") or state.get("needs_setup"):
            self.post("/api/auth/setup", {"username": self.username, "password": self.password, "remember": True})
            log.info("Homestead: first administrator created")
        else:
            self.post("/api/auth/login", {"username": self.username, "password": self.password, "remember": True})

    # ------------------------------------------------------------ jobs
    def job(self, operation_id):
        return next((o for o in self.get("/api/operations", wait=900) if o.get("id") == operation_id), None)

    def wait_job(self, operation_id, timeout=3600, until=("succeeded",)):
        """Until the job ends; a job ending otherwise than `until` fails the test
        with its last message."""
        deadline, last = time.time() + timeout, None
        while time.time() < deadline:
            job = self.job(operation_id)
            if job:
                if job.get("message") != (last or {}).get("message"):
                    log.info(f"  job {operation_id[:8]}: {job.get('status')} {job.get('progress')}% · {job.get('message', '')[:160]}")
                last = job
                if job["status"] in ("succeeded", "failed", "cancelled"):
                    if job["status"] not in until:
                        raise AssertionError(f"job {job.get('title')} {job['status']}: {job.get('message')}")
                    return job
            time.sleep(10)
        raise TimeoutError(f"job {operation_id} still {(last or {}).get('status')} after {timeout}s: {(last or {}).get('message')}")
