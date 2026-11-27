"""Fakes the Python tests share (#619).

The fakes import the server's modules, so server/ goes on the path here for a
test file that imports a fake first. It goes last, so a server folder a test
file put first (HOMESTEAD_TEST_SERVER_DIR) still wins."""
import sys
from pathlib import Path

_SERVER = str(Path(__file__).resolve().parents[2] / "server")
if _SERVER not in sys.path:
    sys.path.append(_SERVER)
