"""Run the backend tests: one share of them, or the ones a change touches.

  python scripts/run_tests.py --shard 2/4     CI: the second of four shares
  python scripts/run_tests.py --changed       locally: what this branch touches
  python scripts/run_tests.py --changed dev   ... against another base
  python scripts/run_tests.py --record-durations   refresh tests/durations.json

A share is a fixed set of whole test files, so four jobs run every test
between them and each file runs as it does in one process. Shares are
balanced by the seconds each file took in tests/durations.json; a file not
in it is weighed by its size, at the recorded seconds per byte.

--changed is a quick check, not a gate: it runs the test files that name a
changed server module or web file, the changed test files themselves, and
every test that scans the whole tree (source scans, ratchets, module maps),
since a change anywhere can fail those. A change to server.py, or to
anything the tests share, runs everything. CI still runs every test.
"""
import argparse
import io
import json
import pathlib
import re
import subprocess
import sys
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
DURATIONS = TESTS / "durations.json"
# A test that reads the whole tree, not one module: always run by --changed.
SCANS = re.compile(r"rglob\(|os\.walk\(|\.glob\(|listdir\(|iterdir\(|git ls-files")
# Files every test leans on: a change to one (or to any helper module in
# tests/) runs everything.
SHARED = {"server/server.py", "requirements-test.txt", "requirements-topology.txt", ".gitattributes"}


def test_files():
    return sorted(TESTS.glob("test_*.py"))


def load_durations(path=DURATIONS):
    """Seconds per test file name, as --record-durations wrote them. A file
    that is missing or does not read (conflict markers, a cut-short write)
    gives none, so every file is weighed by its size; anything that is not
    a number of seconds is left out."""
    try:
        durations = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(durations, dict):
        return {}
    return {name: seconds for name, seconds in durations.items()
            if type(seconds) in (int, float) and seconds >= 0}


def shard(files, index, count, durations=None):
    """The files of share `index` (1-based) of `count`: the heaviest first,
    each to the lightest share so far. Every file lands in exactly one.

    A file weighs its recorded seconds. One with no record (a new test file)
    weighs its size times the recorded seconds per byte, so the two compare."""
    durations = load_durations() if durations is None else durations
    sizes = {path: path.stat().st_size for path in files}
    timed = [path for path in files if path.name in durations]
    timed_bytes = sum(sizes[path] for path in timed)
    rate = sum(durations[path.name] for path in sorted(timed)) / timed_bytes if timed_bytes else 1

    def weight(path):
        return durations[path.name] if path.name in durations else sizes[path] * rate

    loads, shares = [0] * count, [[] for _ in range(count)]
    for path in sorted(files, key=lambda p: (-weight(p), p.name)):
        lightest = loads.index(min(loads))
        shares[lightest].append(path)
        loads[lightest] += weight(path)
    return sorted(shares[index - 1])


def record_durations(files, clock=time.perf_counter, previous=None):
    """Run each file once, in this process and in name order, and return its
    seconds to a hundredth (at a tenth most files read 0, and files that
    weigh nothing would all pile onto one share). A file that fails is often
    cut short, so it keeps its time from `previous` or gets none (it is then
    weighed by its size); it is named."""
    previous = previous or {}
    sys.path.insert(0, str(TESTS))
    seconds = {}
    for path in sorted(files):
        start = clock()
        suite = unittest.defaultTestLoader.loadTestsFromName(path.stem)
        result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)
        took = round(clock() - start, 2)
        if result.wasSuccessful():
            seconds[path.name] = took
        else:
            if path.name in previous:
                seconds[path.name] = previous[path.name]
            print(f"{path.name}: failed, " + ("kept its last time" if path.name in previous else "no time recorded"), flush=True)
    return seconds


def changed_paths(base):
    def git(*args):
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout.split()
    merge_base = git("merge-base", base, "HEAD")[0]
    return set(git("diff", "--name-only", merge_base)) | set(git("ls-files", "--others", "--exclude-standard"))


def aliases(server=ROOT / "server"):
    """Each server module's names in the server code (`import homestead_x as
    X`): tests mostly reach a module as server.X, not by its file name."""
    found = {}
    for path in server.glob("*.py"):
        for module, alias in re.findall(r"import (homestead_\w+) as (\w+)", path.read_text(encoding="utf-8", errors="replace")):
            found.setdefault(module, set()).add(alias)
    return found


def affected(files, changed, known=None):
    """The test files a change touches, or None for all of them."""
    if changed & SHARED or any(p.startswith("tests/") and p.endswith(".py") and not p.startswith("tests/test_") for p in changed):
        return None
    known = aliases() if known is None else known
    names = set()
    for path in changed:
        p = pathlib.PurePosixPath(path)
        if p.parent.name == "tests" and p.name.startswith("test_") and p.suffix == ".py":
            names.add(p.name)
        elif p.suffix in (".py", ".js", ".mjs", ".html", ".css"):
            names.add(p.stem if p.suffix == ".py" else p.name)
            names |= known.get(p.stem, set())
    picked = []
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        if path.name in names or SCANS.search(text) or any(
                re.search(r"(?<![\w-])" + re.escape(name) + r"(?![\w-])", text) for name in names if not name.startswith("test_")):
            picked.append(path)
    return picked


def run(files):
    sys.path.insert(0, str(TESTS))
    suite = unittest.defaultTestLoader.loadTestsFromNames([path.stem for path in files])
    print(f"{len(files)} test files", flush=True)
    return unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful()


def main(argv=None):
    if not sys.flags.utf8_mode and argv is None:
        # Sources are UTF-8. Windows reads text as its own code page unless
        # Python's UTF-8 mode is on, as it always is on Linux and macOS.
        return subprocess.run([sys.executable, "-X", "utf8", *sys.argv]).returncode
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--shard", metavar="K/N", help="run share K of N")
    group.add_argument("--changed", nargs="?", const="origin/dev", metavar="BASE",
                       help="run what differs from BASE (default origin/dev)")
    group.add_argument("--record-durations", action="store_true",
                       help=f"time every test file once and write {DURATIONS.relative_to(ROOT)}")
    args = parser.parse_args(argv)
    files = test_files()
    if args.record_durations:
        seconds = record_durations(files, previous=load_durations())
        DURATIONS.write_text(json.dumps(seconds, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        print(f"{len(seconds)} test files, {sum(seconds.values()):.0f} s: {DURATIONS.relative_to(ROOT)}")
        return 0
    if args.shard:
        match = re.fullmatch(r"(\d+)/(\d+)", args.shard)
        if not match or not 1 <= int(match[1]) <= int(match[2]):
            parser.error("--shard is K/N, with 1 <= K <= N")
        files = shard(files, int(match[1]), int(match[2]))
    else:
        picked = affected(files, changed_paths(args.changed))
        if picked is None:
            print("A shared file changed: running every test", flush=True)
        else:
            files = picked
    return 0 if run(files) else 1


if __name__ == "__main__":
    sys.exit(main())
