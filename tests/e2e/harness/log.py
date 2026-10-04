"""Timestamped output a person can follow in the Actions log, with each
scenario's steps grouped, and a verbose file kept for the artifacts."""
import os
import sys
import time
from pathlib import Path

STARTED = time.monotonic()
_file = None


def to(directory):
    global _file
    Path(directory).mkdir(parents=True, exist_ok=True)
    _file = open(Path(directory) / "e2e.log", "a", buffering=1)


def _stamp():
    return f"[{time.monotonic() - STARTED:7.1f}s]"


def _write(line, console=True):
    if console:
        print(line, flush=True)
    if _file:
        _file.write(line + "\n")


def info(message):
    _write(f"{_stamp()} {message}")


def debug(message):
    _write(f"{_stamp()}   {message}", console=bool(os.environ.get("E2E_VERBOSE")))


def group(title):
    """A collapsible section in the GitHub Actions log."""
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::group::{title}", flush=True)
    _write(f"{_stamp()} == {title}", console=not os.environ.get("GITHUB_ACTIONS"))


def end_group():
    if os.environ.get("GITHUB_ACTIONS"):
        print("::endgroup::", flush=True)


def error(message):
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::error::{message.splitlines()[0][:300]}", flush=True)
    _write(f"{_stamp()} FAILED: {message}", console=True)
    sys.stdout.flush()
