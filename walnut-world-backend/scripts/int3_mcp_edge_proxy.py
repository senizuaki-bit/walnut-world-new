"""Repository entrypoint for the loopback-only INT3 MCP edge proxy."""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = BACKEND_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from walnut_backend.int3_mcp_edge_proxy import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
