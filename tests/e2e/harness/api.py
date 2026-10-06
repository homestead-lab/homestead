"""Homestead, driven as the browser drives it: a session cookie, the
X-Homestead-Auth header on changes, and patience while Homestead itself is
away - a reboot or shutdown takes it down with the host."""
import http.cookiejar
import json
import time
import urllib.error
import urllib.request

from . import log

# A data move's own progress server answers every change with this.
PROGRESS_ONLY = "This endpoint only reports progress"


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
                try:
                    return response.status, json.loads(raw) if raw else {}
                except ValueError:            # a file, a page, a metrics text
                    return response.status, raw.decode(errors="replace")
        except urllib.error.HTTPError as error:
            raw = error.read()
            try:
                return error.code, json.loads(raw)
            except ValueError:
                return error.code, raw.decode(errors="replace")

    def request(self, method, path, body=None, ok=(200, 202), wait=300, timeout=120):
        """One call, trying each address; while Homestead is away (connection
        refused, 429/502/503) it keeps asking for up to `wait` seconds."""
        deadline, last = time.time() + wait, None
        while True:
            for base in self.urls:
                try:
                    status, answer = self._once(base, method, path, body, timeout)
                except (OSError, urllib.error.URLError) as error:
                    last = error
                    continue
                if status == 401 and path not in ("/api/auth/login", "/api/auth/setup", "/api/auth/state"):
                    # A session belongs to the address it was made on: sign in there.
                    self.sign_in(base)
                    status, answer = self._once(base, method, path, body, timeout)
                # Away, or asking to come back shortly ("storage is initializing").
                # Right after a data move the move's progress server can still
                # hold the address for a moment, refusing anything but its own
                # status: Homestead is not back yet, so ask again.
                if status in (429, 502, 503, 504) or (status == 405 and PROGRESS_ONLY in str(answer)):
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

    def sign_in(self, base=None):
        """Sign in - on one address, or the first that answers."""
        if base is None:
            # While Homestead hands its data over it answers only the
            # handoff's own routes: anything else is 404 for a while.
            deadline = time.time() + 900
            while True:
                try:
                    state = self.get("/api/auth/state", wait=900)
                    break
                except HomesteadError as error:
                    if error.status != 404 or time.time() > deadline:
                        raise
                    time.sleep(5)
        else:
            _, state = self._once(base, "GET", "/api/auth/state", None, 30)
        creds = {"username": self.username, "password": self.password, "remember": True}
        if isinstance(state, dict) and state.get("setup"):
            path = "/api/auth/setup"
            log.info("Homestead: first administrator created")
        else:
            path = "/api/auth/login"
        try:
            if base is None:
                self.post(path, creds)
            else:
                self._once(base, "POST", path, creds, 30)
        except HomesteadError as error:
            if error.status == 400 and "changed elsewhere" in str(error) and getattr(self, "_busy", 0) < 5:
                # Homestead wrote its account store at the same moment (its
                # own start-up does); the answer says to try again.
                log.info("Homestead: the account store was busy; asking again")
                self._busy = getattr(self, "_busy", 0) + 1
                time.sleep(3)
                return self.sign_in(base)
            # The setup went through but its answer was lost (the pod moved
            # or the address changed hands mid-request), so the retry was
            # refused: the administrator exists - sign in as it.
            if path != "/api/auth/setup" or error.status != 403 or "already been completed" not in str(error):
                raise
            log.info("Homestead: setup had already gone through; signing in")
            if base is None:
                self.post("/api/auth/login", creds)
            else:
                self._once(base, "POST", "/api/auth/login", creds, 30)

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
