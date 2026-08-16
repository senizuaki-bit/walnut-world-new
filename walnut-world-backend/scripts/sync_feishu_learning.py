"""Project authoritative INT3 learning data into the configured Feishu assets."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = BACKEND_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from walnut_backend.adapters.lark_cli.feishu_learning import (  # noqa: E402
    LarkCliError,
    LarkCliFeishuSyncPort,
)
from walnut_backend.adapters.postgres.feishu_learning import (  # noqa: E402
    PostgresFeishuLearningSyncReader,
)
from walnut_backend.adapters.postgres.session import create_session_factory  # noqa: E402
from walnut_backend.application.feishu.learning_sync import (  # noqa: E402
    AmbiguousSyncState,
    FeishuAssets,
    FeishuLearningSynchronizer,
    snapshot_report,
)

DEFAULT_ASSETS = BACKEND_ROOT / "config" / "int3_feishu_assets.json"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read Learner Profile / SUCCEEDED Run projection / redacted Evidence from "
            "PostgreSQL and idempotently synchronize the INT3 Feishu Base and growth documents."
        )
    )
    parser.add_argument("--tenant-id", required=True, help="single tenant authority to synchronize")
    parser.add_argument(
        "--assets", type=Path, default=DEFAULT_ASSETS, help="non-sensitive Feishu asset config"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="perform Feishu writes; omitted means a PostgreSQL-only dry run",
    )
    parser.add_argument(
        "--lark-cli",
        default=os.getenv("WALNUT_LARK_CLI", "lark-cli"),
        help="lark-cli executable name/path (never an auth token)",
    )
    parser.add_argument("--identity", choices=("user", "bot"), default="user")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        database_url = _required_environment("WALNUT_DATABASE_URL")
        pseudonym_secret = _required_environment("WALNUT_FEISHU_PSEUDONYM_SECRET")
        assets = FeishuAssets.from_mapping(_load_json(args.assets))
        # Fail before reading authority data or constructing a write adapter.  The
        # competition asset set represents exactly one class, hence exactly one tenant.
        assets.assert_binding(args.tenant_id, pseudonym_secret)
        sessions = create_session_factory(database_url)
        reader = PostgresFeishuLearningSyncReader(
            sessions, pseudonym_secret=pseudonym_secret
        )
        snapshot = asyncio.run(reader.load_tenant(args.tenant_id))
        if args.apply:
            if args.identity != "user":
                raise ValueError("Miaoda apply requires lark-cli user identity")
            port = LarkCliFeishuSyncPort(
                assets, executable=args.lark_cli, identity=args.identity
            )
            report = FeishuLearningSynchronizer(
                port, assets, pseudonym_secret=pseudonym_secret
            ).sync(snapshot)
            mode = "applied"
        else:
            report = snapshot_report(snapshot)
            mode = "dry-run"
        print(json.dumps({"mode": mode, "report": asdict(report)}, ensure_ascii=False))
        return 0
    except (AmbiguousSyncState, LarkCliError, OSError, ValueError) as error:
        print(f"Feishu learning sync stopped safely: {error}", file=sys.stderr)
        return 2
    except Exception:
        # Database drivers may include connection details in exception text.  Never echo them.
        print("Feishu learning sync failed before a safe completion.", file=sys.stderr)
        return 1


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError("asset config is not valid JSON") from error
    if not isinstance(payload, dict):
        raise ValueError("asset config must be a JSON object")
    return payload


def _required_environment(name: str) -> str:
    value = os.getenv(name)
    if value is None or not value.strip():
        raise ValueError(f"{name} is required")
    return value


if __name__ == "__main__":
    raise SystemExit(main())
