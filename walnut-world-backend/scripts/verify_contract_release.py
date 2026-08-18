"""Verify the byte-pinned Agent contract release consumed by this backend."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_AGENT_ROOT = (
    BACKEND_ROOT / "agent"
    if (BACKEND_ROOT / "agent" / "contracts" / "manifest.json").is_file()
    else BACKEND_ROOT.parent / "agent"
)
sys.path.insert(0, str(BACKEND_ROOT / "src"))

from walnut_backend.contract_release import (  # noqa: E402, I001
    ContractReleaseVerificationError,
    DEFAULT_RELEASE,
    verify_agent_contract_release,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--agent-repo",
        type=Path,
        default=DEFAULT_AGENT_ROOT,
        help="Agent workspace or installed release containing contracts/manifest.json",
    )
    parser.add_argument(
        "--release",
        type=Path,
        default=DEFAULT_RELEASE,
        help="backend-owned immutable release descriptor",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        count = verify_agent_contract_release(args.agent_repo, args.release)
    except ContractReleaseVerificationError as error:
        print(f"Agent contract release verification failed: {error}", file=sys.stderr)
        return 1
    print(f"Agent contract release verification passed: {count} byte-pinned wire files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
