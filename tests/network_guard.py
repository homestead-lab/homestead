"""Tests reach no real network (#TBD-tests-refuse-real-network).

A test that does not fake every Kubernetes or HTTP call falls through to the real
client, which looks up kubernetes.default.svc or another real host. On a machine
without that name the lookup waits for the resolver (about 5 s on GitHub's runner),
and a test timed against it fails now and then (#1123, #1135). install() makes
every lookup (getaddrinfo, gethostbyname, gethostbyaddr, getfqdn) and connect to
anything but this machine fail at once, as a failed
lookup does, and records which test made it and from where.

Who is blamed: the test that started the calling thread, inherited down the
thread tree (a loop's worker carries the loop's test), not whichever test runs
when the call lands. A thread started in setUpClass or setUpModule is blamed on
its file. A call made after its test ended is marked late, naming that test.
tests/fakes/kube.py no_cluster() names its turned-away reads the same way (blame()).

What stays allowed: loopback (127.0.0.0/8, ::1, "localhost"), the unspecified
addresses (0.0.0.0, ::), Unix sockets, and any test that patches socket itself.
The shared Kubernetes fake (tests/fakes/kube.py) replaces homestead_kube.transport,
so a test on it never gets this far.

Who checks: scripts/run_tests.py installs it before the tests load and reports the
tests that reached the network (with ENFORCE, a file not in network_offenders.txt
fails the run);
tests/test_zz_network_refused.py does the same for a plain `unittest discover` run.
network_offenders.txt is today's list, which may only shrink.
"""
import errno
import ipaddress
import socket
import sys
import threading
import traceback
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
OFFENDERS_FILE = HERE / "network_offenders.txt"
# Advisory for now: which tests reach the network depends on what ran before them
# in the same process (a module left bound to a fake, or not), so a list measured
# in one order is not complete in another. A refused call still fails at once, as
# a failed lookup does; the run reports who made it. Set True once network_offenders.txt
# holds steady across CI's splits: then a file not on it fails the run.
ENFORCE = False

_installed = {}
_own = []          # this machine's own name, once read
_lock = threading.Lock()
_current = {"test": None}   # the test running now; its module while setUpClass/setUpModule runs
_ended = set()              # tests that have finished: a call from their threads after this is late
seen = {}          # test id -> [(what, where), ...]: every refused lookup or connect


def allowed_files():
    return {line.split("\t", 1)[0] for line in OFFENDERS_FILE.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")}


LOCAL_NAMES = {"", "localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}


def _text(host):
    if isinstance(host, bytes):
        host = host.decode(errors="replace")
    return str(host).strip("[]").split("%", 1)[0].rstrip(".").lower()


def _own_name():
    """This machine's own name (what getfqdn() with no argument asks about): not the network."""
    if not _own:
        _own.append(_text(_installed.get("gethostname", socket.gethostname)()))
    return _own[0]


def _literal(host):
    """The address, when host is an IP literal (no lookup needed to use it)."""
    try:
        return ipaddress.ip_address(_text(host))
    except ValueError:
        return None


def _local(host):
    if host is None or _text(host) in LOCAL_NAMES or _text(host) == _own_name():
        return True
    address = _literal(host)
    return address is not None and (address.is_loopback or address.is_unspecified)


# The cluster client itself: the caller worth naming is the module that asked it.
TRANSPORT = {"network_guard.py", "homestead_kube.py", "homestead_read_deadline.py"}


def _where():
    """The first frame in server/ or tests/ that made the call (not the transport)."""
    for frame in reversed(traceback.extract_stack()[:-3]):
        path = Path(frame.filename)
        if path.name in TRANSPORT:
            continue
        if ROOT in path.parents and path.parent.name in ("server", "tests", "fakes"):
            return f"{path.parent.name}/{path.name}:{frame.lineno} ({frame.name})"
    return "outside server/ and tests/"


def origin():
    """Who this thread speaks for: the test (or test module) that started it, or,
    in a thread not started under the guard (the main one), the test running now."""
    stamp = getattr(threading.current_thread(), "_network_origin", None)
    return stamp[0] if stamp is not None else _current["test"]


def blame():
    """(name to blame, late): the starter of this thread, and whether it had already
    ended when the call was made. None when no test or module started it."""
    name = origin()
    return name, name in _ended


def _refuse(what):
    where = _where()
    name, late = blame()
    test = name or "(no test running)"
    if late:
        what = f"late (thread {threading.current_thread().name} outlived its test): {what}"
        when = f"late: {test} had ended; stop or join its thread in a cleanup, after its patches"
    else:
        when = f"during {test}"
    with _lock:
        seen.setdefault(test, []).append((what, where))
    return f"refused in tests: {what} from {where} {when} (tests/network_guard.py)"


def _getaddrinfo(host, *args, **kwargs):
    # An IP literal needs no lookup: let it through, and the connect is refused,
    # as a real one would fail (ENETUNREACH, not "no such name").
    if _local(host) or _literal(host) is not None:
        return _installed["getaddrinfo"](host, *args, **kwargs)
    what = "Kubernetes API (no fake active)" if host == "kubernetes.default.svc" else f"lookup of {host}"
    raise socket.gaierror(socket.EAI_NONAME, "Name or service not known; " + _refuse(what))


def _gethostbyname(host):
    if _local(host) or _literal(host) is not None:
        return _installed["gethostbyname"](host)
    raise socket.gaierror(socket.EAI_NONAME, "Name or service not known; " + _refuse(f"lookup of {host}"))


def _gethostbyaddr(host):
    if _local(host):
        return _installed["gethostbyaddr"](host)
    raise socket.herror(1, "Unknown host; " + _refuse(f"reverse lookup of {host}"))


def _getfqdn(name=""):
    if _local(name):
        return _installed["getfqdn"](name)      # this machine's own name: no network
    _refuse(f"getfqdn of {name}")
    return name                                  # what getfqdn answers when the lookup fails


def _address_host(address):
    if isinstance(address, (str, bytes)):          # a Unix socket's path
        return None
    if isinstance(address, tuple) and address:
        return address[0]
    return None


def _connect(self, address):
    if self.family == getattr(socket, "AF_UNIX", None) or _local(_address_host(address)):
        return _installed["connect"](self, address)
    raise OSError(errno.ENETUNREACH, "Network is unreachable; " + _refuse(f"connect to {address!r}"))


def _connect_ex(self, address):
    if self.family == getattr(socket, "AF_UNIX", None) or _local(_address_host(address)):
        return _installed["connect_ex"](self, address)
    _refuse(f"connect to {address!r}")
    return errno.ENETUNREACH


def _run(self, result=None):
    test, before = unittest.TestCase.id(self), _current["test"]
    _current["test"] = test
    try:
        return _installed["run"](self, result)
    finally:
        _ended.add(test)
        _current["test"] = before       # a test run inside another hands back to it


def _fixture(name):
    """setUpClass/setUpModule run outside any test: what they start is blamed on their file."""
    def handle(self, test, result):
        before = _current["test"]
        _current["test"] = type(test).__module__
        try:
            return _installed[name](self, test, result)
        finally:
            _current["test"] = before
    return handle


def _start(self):
    # Kept as a 1-tuple: a thread started outside any test is stamped None, not
    # read as "the test running now" when its call lands.
    self._network_origin = (origin(),)
    return _installed["start"](self)


def install():
    """Refuse the real network for the rest of this process (once)."""
    if _installed:
        return
    _installed.update(getaddrinfo=socket.getaddrinfo, gethostbyname=socket.gethostbyname,
                      gethostbyaddr=socket.gethostbyaddr, getfqdn=socket.getfqdn,
                      connect=socket.socket.connect, connect_ex=socket.socket.connect_ex, run=unittest.TestCase.run,
                      start=threading.Thread.start,
                      _handleClassSetUp=unittest.suite.TestSuite._handleClassSetUp,
                      _handleModuleFixture=unittest.suite.TestSuite._handleModuleFixture)
    socket.getaddrinfo = _getaddrinfo
    socket.gethostbyname = _gethostbyname
    socket.gethostbyaddr = _gethostbyaddr
    socket.getfqdn = _getfqdn
    socket.socket.connect = _connect
    socket.socket.connect_ex = _connect_ex
    unittest.TestCase.run = _run
    threading.Thread.start = _start
    for name in ("_handleClassSetUp", "_handleModuleFixture"):
        setattr(unittest.suite.TestSuite, name, _fixture(name))


def offenders(allowed=None):
    """{test id: [(what, where)]} for the tests whose file is not in network_offenders.txt."""
    allowed = allowed_files() if allowed is None else allowed
    return {test: calls for test, calls in sorted(seen.items())
            if test.split(".", 1)[0] not in allowed}


def report(stream=None):
    """Print what was refused; False when a file not on the list reached the network."""
    stream = stream or sys.stdout
    if not seen:
        return True
    bad = offenders()
    print(f"network: {len(seen)} test(s) tried the real network, {len(bad)} outside network_offenders.txt"
          f"{'' if ENFORCE else ' (advisory: tests/network_guard.py ENFORCE)'}", file=stream)
    for test, calls in (bad or {}).items():
        for what, where in calls[:3]:
            print(f"network: REFUSED {test}: {what} from {where}", file=stream)
    return not bad or not ENFORCE
