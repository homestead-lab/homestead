"""Disposable, network-isolated real SSH/rsync rehearsal; no Kubernetes writes.

Run only in source-copy.Dockerfile with the repository mounted read-only,
--network none and no host ports/volumes other than that read-only repository.
All keys, credentials and source/destination files are generated inside the fixture.
"""
import copy
import hashlib
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import tempfile
import time
from unittest import mock

sys.path.insert(0, "/repo/server")
import homestead_imports as imports
import homestead_source_ssh as ssh


def run(*args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, text=True, **kwargs)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


with tempfile.TemporaryDirectory(prefix="source-copy-") as directory:
    root = Path(directory)
    keypath = root / "host_key"
    run("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(keypath))
    public = " ".join(keypath.with_suffix(".pub").read_text().split()[:2])
    password = secrets.token_urlsafe(32)
    run("chpasswd", input="root:" + password + "\n")
    env = {**os.environ, "SSHPASS": password}
    config = root / "sshd_config"
    config.write_text(f"Port 2222\nListenAddress 127.0.0.1\nHostKey {keypath}\n"
                      "PasswordAuthentication yes\nPermitRootLogin yes\nUsePAM no\n"
                      "PidFile /tmp/fixture-sshd.pid\nLogLevel VERBOSE\n")
    daemon = subprocess.Popen(["/usr/sbin/sshd", "-D", "-e", "-f", str(config)],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        src = {"name": "fixture", "host": "127.0.0.1", "user": "root", "port": 2222}
        src["ssh_trust"] = {**ssh.key(public), "connection": ssh.connection(src)}
        for attempt in range(50):
            scan = subprocess.run(["sh", "-c", ssh.scan_script(src)], capture_output=True, text=True)
            if scan.returncode == 0:
                break
            time.sleep(.1)
        assert ssh.candidate(scan.stdout.splitlines())["key"] == public
        run("sh", "-c", ssh.setup(src) + ssh.command(src, "printf verified"), env=env)

        other = root / "different_key"
        run("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(other))
        wrong = copy.deepcopy(src)
        wrong["ssh_trust"]["key"] = " ".join(other.with_suffix(".pub").read_text().split()[:2])
        marker = root / "must-not-execute"
        result = subprocess.run(["sh", "-c", ssh.setup(wrong) + ssh.command(wrong, f"touch {marker}")],
                                env=env, capture_output=True, text=True)
        assert result.returncode != 0 and not marker.exists(), "wrong key permitted a remote command"
        assert "Host key verification failed" in result.stderr or "REMOTE HOST IDENTIFICATION HAS CHANGED" in result.stderr
        print("PASS: unknown/changed key rejected; verified key connects", flush=True)

        # A discovered source must be the exact stopped container, not merely
        # another container with the same name. The SSH command itself is real.
        state = root / "docker-state"
        docker = Path("/usr/local/bin/docker")
        docker.write_text(f"#!/bin/sh\ncat {state}\n")
        docker.chmod(0o755)
        identity = "a" * 64
        source_dir = root / "source with spaces"
        source_dir.mkdir()
        (source_dir / "a-small").write_text("application configuration\n")
        (source_dir / "z-large").write_bytes(secrets.token_bytes(8 * 1024 * 1024))
        hashes = {p.name: digest(p) for p in source_dir.iterdir()}
        destination = Path("/appdata")
        destination.mkdir()
        (destination / "borrowed-file").write_text("preserve borrowed destination data")
        cfg = {"source": "fixture", "name": "fixture", "image": "example/app:1",
               "create_workload": False, "source_container_id": identity,
               "source_consistency": "stopped", "remote_path": str(source_dir),
               "pvc": "fixture-data", "size_gb": 1, "mount_path": "/config"}
        with mock.patch.object(imports, "_source", return_value=src):
            prepared = imports.prepare_import(cfg)
        job = prepared["job"]
        assert job["spec"]["backoffLimit"] == 0
        assert job["spec"]["template"]["spec"]["restartPolicy"] == "Never"
        script = job["spec"]["template"]["spec"]["containers"][0]["command"][-1]
        script = script.replace("apk add --no-cache rsync openssh-client sshpass >/dev/null 2>&1", ":")
        for status in (identity + " true false false", "b" * 64 + " false false false"):
            state.write_text(status)
            result = subprocess.run(["sh", "-c", script], env=env, capture_output=True, text=True)
            assert result.returncode != 0 and not (destination / "a-small").exists()
        print("PASS: running or replaced source refused before copying", flush=True)

        state.write_text(identity + " false false false")
        logpath = root / "interrupted.log"
        with logpath.open("w") as log:
            process = subprocess.Popen(["sh", "-c", script.replace("rsync -aH", "rsync --bwlimit=256 -aH")],
                                       env=env, stdout=log, stderr=log, start_new_session=True)
            try:
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline and not (destination / "a-small").exists():
                    if process.poll() is not None:
                        raise AssertionError(logpath.read_text())
                    time.sleep(.1)
                assert (destination / "a-small").exists(), "copy never started"
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=10)
        assert process.returncode != 0 and "==> done" not in logpath.read_text()
        assert not (destination / "z-large").exists()
        assert (destination / "borrowed-file").read_text() == "preserve borrowed destination data"
        assert hashes == {p.name: digest(p) for p in source_dir.iterdir()}
        print("PASS: interrupted copy is unsuccessful; partial destination and original source preserved", flush=True)

        # Deliberately launch a fresh copy after inspecting partial data. The
        # product does not automatically replay an uncertain/failed Job.
        result = run("sh", "-c", script, env=env)
        assert "==> done" in result.stdout
        assert all(digest(destination / name) == value for name, value in hashes.items())
        assert (destination / "borrowed-file").exists()
        print("PASS: explicit fresh copy completes with matching hashes and preserves borrowed files", flush=True)

        # Simulate a writer restarting after the initial check: successful file
        # transfer must not emit the workflow's final completion marker.
        counter = root / "checked"
        docker.write_text(f"#!/bin/sh\nif [ -e {counter} ]; then echo '{identity} true false false'; "
                          f"else touch {counter}; echo '{identity} false false false'; fi\n")
        result = subprocess.run(["sh", "-c", script], env=env, capture_output=True, text=True)
        assert result.returncode != 0 and "==> done" not in result.stdout
        print("PASS: source restarting during copy prevents completion", flush=True)

        def real_probe(tag, script, src, **kwargs):
            return run("sh", "-c", ssh.setup(src) + script, env=env).stdout.splitlines()
        with mock.patch.object(imports, "_source", return_value=src), mock.patch.object(imports, "run_probe", side_effect=real_probe):
            measured = imports.measure_source_paths("fixture", [str(source_dir), str(root / "missing")])
            assert measured["paths"][0]["measured"] and not measured["paths"][1]["exists"]
            try:
                imports.browse_source("fixture", str(root / "missing"))
                raise AssertionError("failed browse returned empty success")
            except subprocess.CalledProcessError:
                pass
        print("PASS: missing measurement is explicit; failed browse is not empty success", flush=True)
    finally:
        daemon.terminate()
        daemon.wait(timeout=5)
