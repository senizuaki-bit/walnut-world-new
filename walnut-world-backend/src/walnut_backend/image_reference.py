"""Fail-closed validation for runtime Docker image authorities."""

from __future__ import annotations

import re

_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


def require_digest_pinned_image(value: str, name: str) -> tuple[str, str]:
    """Return ``(repository, digest)`` only for ``name@sha256:<64 hex>``.

    Docker accepts floating tags such as ``image:latest``.  Runtime and Compose
    configuration must reject those before a daemon pull or a workflow claim.
    The Docker CLI remains responsible for the wider repository-name grammar;
    this boundary owns the immutable digest requirement.
    """

    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    candidate = value.strip()
    repository, separator, digest_hex = candidate.rpartition("@sha256:")
    if (
        not separator
        or not repository
        or "@" in repository
        or any(character.isspace() for character in candidate)
        or _SHA256_HEX.fullmatch(digest_hex) is None
    ):
        raise ValueError(f"{name} must be name@sha256:<64 lowercase hex characters>")
    return repository, f"sha256:{digest_hex}"


__all__ = ["require_digest_pinned_image"]
