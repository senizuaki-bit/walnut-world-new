"""Read-only local launcher checks. Never opens a provider or database connection."""

import argparse
import hashlib
import json
import sys
from pathlib import Path

from walnut_backend.llm_relay.config import RelaySettings, _read_secret_file


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--key-file", required=True)
    parser.add_argument("--endpoint", default="https://api.deepseek.com/chat/completions")
    parser.add_argument("--model", default="deepseek-v4-flash")
    parser.add_argument("--provider", default="deepseek")
    args = parser.parse_args(argv)
    if sys.version_info[:2] != (3, 12):
        print(json.dumps({"status": "INVALID", "code": "PYTHON_312_REQUIRED"}))
        return 1
    try:
        key = _read_secret_file(Path(args.key_file), "upstream_api_key")
        RelaySettings(
            database_url="postgresql+asyncpg://localhost/preflight",
            relay_api_key="local-preflight-unused-internal-key",
            upstream_api_key=key,
            provider=args.provider,
            model=args.model,
            upstream_endpoint=args.endpoint,
        )
    except (ValueError, OSError):
        print(json.dumps({"status": "INVALID", "code": "LLM_CONFIGURATION_INVALID"}))
        return 1
    # Import delayed dependencies now, before accepting student submissions.
    try:
        import yaya_agent_runtime

        import walnut_backend.learner_worker_main  # noqa: F401
        import walnut_backend.worker_main  # noqa: F401
        import walnut_backend.workers.turn_projection  # noqa: F401
    except ImportError:
        print(json.dumps({"status": "INVALID", "code": "BACKEND_DEPENDENCIES_MISSING"}))
        return 1
    digest = hashlib.sha256(json.dumps([key, args.endpoint, args.model, args.provider]).encode())
    # A running Python process does not reload code after git pull.
    for root in (Path(__file__).parent, Path(yaya_agent_runtime.__file__).parent.parent):
        for path in sorted(root.rglob("*.py")):
            digest.update(str(path.relative_to(root)).encode())
            digest.update(path.read_bytes())
    print(
        json.dumps(
            {
                "status": "CONFIGURED",
                "llm": "CONFIGURED",
                "provider_access": "NOT_CHECKED",
                "configuration_sha256": digest.hexdigest(),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
