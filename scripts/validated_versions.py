#!/usr/bin/env python3
"""Make the validated-versions key, and sign or check the list
(server/homestead_validated.py, issue #1348).

    python scripts/validated_versions.py keygen | gh secret set VALIDATED_VERSIONS_KEY
        Makes a new Ed25519 key. The private key goes to standard output only,
        so it can be piped straight into the Actions secret and is never
        written to disk; the public key is printed on standard error, for
        PUBLIC_KEY in server/homestead_validated.py.

    VALIDATED_VERSIONS_KEY=<hex> python scripts/validated_versions.py sign FILE
        Checks FILE's shape and writes its signature into it (CI).

    python scripts/validated_versions.py verify FILE [PUBLIC_KEY_HEX]
        Checks FILE's shape and signature, against Homestead's key by default.
"""
import base64
import json
import os
import pathlib
import secrets
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "server"))
import homestead_ed25519 as ED25519  # noqa: E402
import homestead_validated as VALIDATED  # noqa: E402


def keygen():
    secret = secrets.token_bytes(32)
    print(secret.hex())
    print(f"Public key, for PUBLIC_KEY in server/homestead_validated.py: {ED25519.public_key(secret).hex()}",
          file=sys.stderr)


def sign(path):
    key = os.environ.get("VALIDATED_VERSIONS_KEY", "").strip()
    if not key:
        sys.exit("VALIDATED_VERSIONS_KEY is not set.")
    document = VALIDATED.check(json.loads(pathlib.Path(path).read_text(encoding="utf-8")))
    secret = bytes.fromhex(key)
    document["signature"] = base64.b64encode(ED25519.sign(secret, VALIDATED.canonical(document))).decode()
    if not VALIDATED.verify(document, ED25519.public_key(secret).hex()):
        sys.exit("The new signature does not verify.")
    pathlib.Path(path).write_bytes((json.dumps(document, indent=1, sort_keys=True) + "\n").encode("utf-8"))
    print(f"Signed {path}.")


def verify(path, public=None):
    document = VALIDATED.check(json.loads(pathlib.Path(path).read_text(encoding="utf-8")))
    if not VALIDATED.verify(document, public):
        sys.exit(f"{path}: the signature does not match.")
    print(f"{path}: signed, generated {document['generated']}.")


def main(argv):
    if argv[:1] == ["keygen"] and len(argv) == 1:
        return keygen()
    if argv[:1] == ["sign"] and len(argv) == 2:
        return sign(argv[1])
    if argv[:1] == ["verify"] and len(argv) in (2, 3):
        return verify(*argv[1:])
    sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
