"""Write docs/api/openapi.json from the API's own declarations.

    python scripts/build_api_docs.py

The running server serves the same document at /api/v1/openapi.json; this
copy is for GitHub and for tools that read a spec from a repository. A test
fails if the two drift apart, so run this after changing an endpoint.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
import homestead_api_keys as KEYS  # noqa: E402
import homestead_api_v1 as API  # noqa: E402

SCOPES = {scope: text for scope, (_, text) in KEYS.SCOPES.items()}


def document():
    return json.dumps(API.openapi(None, SCOPES), indent=2, ensure_ascii=False) + "\n"


if __name__ == "__main__":
    out = ROOT / "docs" / "api" / "openapi.json"
    out.write_text(document(), encoding="utf-8", newline="\n")
    print(f"wrote {out.relative_to(ROOT)}")
