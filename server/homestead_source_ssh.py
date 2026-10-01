"""Pinned import-source SSH. A scan is a candidate, never automatic trust."""
import base64
import hashlib
import ipaddress
import re
import shlex

ALIAS = "homestead-import-source"
KNOWN = "/tmp/homestead-known-hosts"
ALGORITHMS = ("ssh-ed25519", "ecdsa-sha2-nistp256", "ssh-rsa")


def connection(src):
    host, user = str(src.get("host") or "").strip(), str(src.get("user") or "").strip()
    try:
        host = str(ipaddress.ip_address(host))
    except ValueError:
        if len(host) > 253 or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", host):
            raise ValueError("Enter a host IP or DNS name, without a URL, username or port")
        host = host.lower()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}", user):
        raise ValueError("Enter a valid SSH username")
    try:
        raw_port = src.get("port", 22)
        if not re.fullmatch(r"[0-9]{1,5}", str(raw_port)):
            raise ValueError()
        port = int(raw_port)
        if not 1 <= port <= 65535:
            raise ValueError()
    except (TypeError, ValueError):
        raise ValueError("SSH port must be between 1 and 65535") from None
    return {"host": host, "user": user, "port": port}


def key(value):
    parts = str(value or "").split()
    if len(parts) != 2 or parts[0] not in ALGORITHMS or len(parts[1]) > 8192:
        raise ValueError("Unsupported SSH host key")
    try:
        blob = base64.b64decode(parts[1], validate=True)
        fields, offset = [], 0
        while offset < len(blob):
            if offset + 4 > len(blob):
                raise ValueError()
            size = int.from_bytes(blob[offset:offset + 4], "big")
            offset += 4
            if not size or offset + size > len(blob):
                raise ValueError()
            fields.append(blob[offset:offset + size]); offset += size
        if fields[0].decode() != parts[0]:
            raise ValueError()
        if parts[0] == "ssh-ed25519" and (len(fields) != 2 or len(fields[1]) != 32):
            raise ValueError()
        if parts[0] == "ecdsa-sha2-nistp256" and (len(fields) != 3 or fields[1] != b"nistp256" or len(fields[2]) != 65):
            raise ValueError()
        if parts[0] == "ssh-rsa" and (len(fields) != 3 or int.from_bytes(fields[2], "big").bit_length() < 2048):
            raise ValueError()
    except (ValueError, IndexError, UnicodeError):
        raise ValueError("Invalid SSH host key") from None
    encoded = base64.b64encode(blob).decode()
    return {"key": f"{parts[0]} {encoded}", "algorithm": parts[0],
            "fingerprint": "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")}


def candidate(lines):
    keys = {}
    for line in lines:
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) == 3 and parts[1] in ALGORITHMS:
            found = key(" ".join(parts[1:]))
            keys.setdefault(found["algorithm"], set()).add(found["key"])
    for algorithm in ALGORITHMS:
        if algorithm in keys:
            if len(keys[algorithm]) != 1:
                raise ValueError("The address returned different host keys. Resolve that ambiguity before trusting it.")
            return key(next(iter(keys[algorithm])))
    raise ValueError("No supported SSH host key was returned. Check the address, port and source SSH service.")


def verified(src):
    endpoint = connection(src)
    trust = src.get("ssh_trust") or {}
    if trust.get("connection") != endpoint:
        raise ValueError("Verify this source's SSH fingerprint in Import → Verify source before connecting")
    return key(trust.get("key"))


def setup(src):
    trusted = verified(src)
    return f"umask 077\nprintf '%s\\n' {shlex.quote(ALIAS + ' ' + trusted['key'])} > {KNOWN}\n"


def transport(src):
    verified(src)
    port = connection(src)["port"]
    return (f"ssh -F /dev/null -p {port} -o StrictHostKeyChecking=yes -o HostKeyAlias={ALIAS} "
            f"-o UserKnownHostsFile={KNOWN} -o GlobalKnownHostsFile=/dev/null -o UpdateHostKeys=no "
            "-o CheckHostIP=no -o ConnectTimeout=10 -o ConnectionAttempts=1 "
            "-o ServerAliveInterval=10 -o ServerAliveCountMax=3 -o LogLevel=ERROR "
            "-o PreferredAuthentications=password -o PubkeyAuthentication=no -o NumberOfPasswordPrompts=1")


def command(src, remote, compress=False):
    """compress: SSH's own compression, for a stream with much to gain - a VM
    disk's empty space crosses as almost nothing."""
    endpoint = connection(src)
    return ("sshpass -e " + transport(src) + (" -o Compression=yes" if compress else "") + " "
            + shlex.quote(endpoint["user"] + "@" + endpoint["host"]) + " " + shlex.quote(remote))


def scan_script(src):
    endpoint = connection(src)
    return f"ssh-keyscan -T 8 -p {endpoint['port']} -t ed25519,ecdsa,rsa {shlex.quote(endpoint['host'])} 2>/dev/null"


def source_check(src, cfg):
    """Observe a discovered Docker container; never stop or alter the source."""
    ident = cfg.get("source_container_id")
    if not ident or cfg.get("source_consistency") == "snapshot":
        return ""
    if not re.fullmatch(r"[a-f0-9]{64}", str(ident)):
        raise ValueError("Source container identity is invalid; inspect the source again")
    remote = "docker inspect --format '{{.Id}} {{.State.Running}} {{.State.Paused}} {{.State.Restarting}}' " + ident
    expected = ident + " false false false"
    return "source_state=$(" + command(src, remote) + ")\n" + f"[ \"$source_state\" = {shlex.quote(expected)} ] || {{ echo 'Source container is not stopped or its identity changed; copy stopped'; exit 5; }}"
