"""Every page's data answers: each GET route in Homestead's route policy,
as an administrator, on a real cluster. A route may refuse a request without
its parameters (4xx); it may not fail (5xx) or hang. Then the installer's
node doctor reports on each host."""
import sys
from pathlib import Path

from harness import log
from harness.api import HomesteadError

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "server"))
import homestead_route_policy as POLICY  # noqa: E402

# Streams, consoles and downloads hold a connection open or need a target.
SKIP = ("stream", "/console", "/terminal", "download", "/watch", "/logs/follow", "/vnc", "/exec")


def run(ctx):
    nodes = ctx.api.get("/api/nodes")
    names = sorted(n.get("name") for n in (nodes if isinstance(nodes, list) else nodes.get("nodes", [])))
    assert names == [n.name for n in ctx.lab.nodes], f"Homestead lists {names}"

    routes = sorted({path for (method, path) in POLICY.POLICY if method == "GET" and not any(s in path for s in SKIP)})
    failed = []
    for path in routes:
        try:
            ctx.api.request("GET", path, ok=tuple(range(200, 500)), wait=60, timeout=60)
        except (HomesteadError, TimeoutError) as error:
            failed.append(f"{path}: {str(error)[:200]}")
    log.info(f"{len(routes) - len(failed)} of {len(routes)} GET routes answered without a server error")
    assert not failed, "routes that failed:\n  " + "\n  ".join(failed)


def doctor(ctx):
    """The node doctor's report on each host: 0 healthy, 1 warnings, 2 failures."""
    for node in ctx.lab.nodes:
        out = node.ssh(f"curl -sfL https://raw.githubusercontent.com/homestead-lab/homestead/v{ctx.version}/scripts/install.sh"
                       f" -o /tmp/install.sh && sudo sh /tmp/install.sh --report < /dev/null; echo EXIT=$?", check=False, timeout=600)
        code = out.strip().rsplit("EXIT=", 1)[-1].strip()
        log.info(f"{node.name}: doctor exit {code}")
        assert code in ("0", "1"), f"{node.name}: the doctor reports failures:\n{out[-3000:]}"
