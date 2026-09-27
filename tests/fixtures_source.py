"""Synthetic public host key for manifest tests; never used for a connection."""
import base64
import homestead_source_ssh as ssh

def source(**fields):
    row = {"name": "tower", "host": "192.0.2.10", "user": "root", "port": 22,
           "kind": "unraid", "base_path": "/mnt/user/appdata", **fields}
    blob = b"\0\0\0\x0bssh-ed25519\0\0\0\x20" + bytes(range(32))
    public = ssh.key("ssh-ed25519 " + base64.b64encode(blob).decode())
    row["ssh_trust"] = {**public, "connection": ssh.connection(row)}
    return row
