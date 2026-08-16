"""Validated entrypoint for this repository's production Compose topology."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = BACKEND_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from walnut_backend.image_reference import require_digest_pinned_image  # noqa: E402

IMAGE_ENVIRONMENT_NAMES = (
    "WALNUT_BUILD_IMAGE",
    "WALNUT_DIND_IMAGE",
    "WALNUT_POSTGRES_IMAGE",
    "WALNUT_SANDBOX_IMAGE",
)


def main(arguments: list[str] | None = None) -> int:
    requested = list(sys.argv[1:] if arguments is None else arguments)
    if not requested:
        print(
            "COMPOSE_IMAGE_POLICY_REFUSED: pass a Docker Compose command such as config or up",
            file=sys.stderr,
        )
        return 2
    try:
        for name in IMAGE_ENVIRONMENT_NAMES:
            value = os.environ.get(name)
            if value is None or not value.strip():
                raise ValueError(f"{name} is required")
            require_digest_pinned_image(value, name)
    except (TypeError, ValueError) as error:
        print(f"COMPOSE_IMAGE_POLICY_REFUSED: {error}", file=sys.stderr)
        return 2

    command = [
        "docker",
        "compose",
        "--project-directory",
        str(BACKEND_ROOT),
        "--file",
        str(BACKEND_ROOT / "docker-compose.yml"),
        *requested,
    ]
    try:
        return subprocess.run(command, cwd=BACKEND_ROOT, check=False).returncode
    except FileNotFoundError:
        print("COMPOSE_IMAGE_POLICY_REFUSED: Docker CLI is unavailable", file=sys.stderr)
        return 127


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["IMAGE_ENVIRONMENT_NAMES", "main"]
