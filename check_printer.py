"""Run the printer check without installing the package (used by Drucker-Test.bat)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from bambu_mcp.check import run  # noqa: E402

run()
