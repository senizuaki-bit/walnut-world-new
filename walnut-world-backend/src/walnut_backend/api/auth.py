"""Fail-closed HS256 JWT authentication aligned with the current Agent runtime."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import cast

from yaya_agent_contracts import ActorRef, ActorType

from walnut_backend.bootstrap import Settings


class AuthenticationError(ValueError):
    """Raised only inside the authentication boundary; callers deny the request."""


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise AuthenticationError("duplicate JWT field")
        result[key] = value
    return result


def _decode_object(value: str, label: str) -> dict[str, object]:
    if not value or any(character.isspace() for character in value):
        raise AuthenticationError(f"invalid JWT {label}")
    try:
        padded = value + "=" * (-len(value) % 4)
        raw = base64.b64decode(padded, altchars=b"-_", validate=True)
        decoded = json.loads(raw.decode("utf-8", errors="strict"), object_pairs_hook=_strict_object)
    except (UnicodeError, ValueError, json.JSONDecodeError) as error:
        raise AuthenticationError(f"invalid JWT {label}") from error
    if not isinstance(decoded, dict):
        raise AuthenticationError(f"JWT {label} must be an object")
    return decoded


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AuthenticationError(f"JWT {label} must be an integer")
    return value


class JwtAuthenticator:
    """Validate the closed production JWT profile without accepting mock credentials."""

    def __init__(self, settings: Settings) -> None:
        if (
            settings.auth_hmac_secret is None
            or settings.auth_issuer is None
            or settings.auth_audience is None
        ):
            raise ValueError("production JWT settings are incomplete")
        self._secret = settings.auth_hmac_secret.encode("utf-8")
        self._issuer = settings.auth_issuer
        self._audience = settings.auth_audience
        self._clock_skew_seconds = settings.auth_clock_skew_seconds
        self._maximum_lifetime_seconds = settings.auth_maximum_lifetime_seconds

    def authenticate(self, authorization: str, *, now: datetime | None = None) -> ActorRef:
        if not authorization.startswith("Bearer "):
            raise AuthenticationError("Authorization must use Bearer JWT")
        token = authorization.removeprefix("Bearer ")
        if token.count(".") != 2:
            raise AuthenticationError("Bearer credential is not a JWT")
        encoded_header, encoded_claims, encoded_signature = token.split(".")
        header = _decode_object(encoded_header, "header")
        claims = _decode_object(encoded_claims, "claims")
        if header != {"alg": "HS256", "typ": "JWT"}:
            raise AuthenticationError("JWT header must select only HS256")
        expected = hmac.new(
            self._secret,
            f"{encoded_header}.{encoded_claims}".encode("ascii"),
            hashlib.sha256,
        ).digest()
        try:
            padded = encoded_signature + "=" * (-len(encoded_signature) % 4)
            supplied = base64.b64decode(padded, altchars=b"-_", validate=True)
        except ValueError as error:
            raise AuthenticationError("invalid JWT signature") from error
        if not hmac.compare_digest(expected, supplied):
            raise AuthenticationError("JWT signature mismatch")

        required = {
            "iss", "aud", "sub", "tenant_id", "actor_id", "actor_type", "roles", "iat", "nbf", "exp"
        }
        if set(claims) != required:
            raise AuthenticationError("JWT claims must use the closed production claim set")
        if claims["iss"] != self._issuer or claims["aud"] != self._audience:
            raise AuthenticationError("JWT issuer or audience mismatch")
        if claims["sub"] != claims["actor_id"]:
            raise AuthenticationError("JWT subject must equal actor_id")
        current = (datetime.now(UTC) if now is None else now).astimezone(UTC)
        timestamp = int(current.timestamp())
        issued, not_before, expires = (
            _integer(claims["iat"], "iat"),
            _integer(claims["nbf"], "nbf"),
            _integer(claims["exp"], "exp"),
        )
        if issued > timestamp + self._clock_skew_seconds or not_before > timestamp + self._clock_skew_seconds:
            raise AuthenticationError("JWT is not active")
        if expires <= timestamp - self._clock_skew_seconds:
            raise AuthenticationError("JWT has expired")
        if expires <= issued or expires - issued > self._maximum_lifetime_seconds:
            raise AuthenticationError("JWT lifetime is invalid")
        roles_value = claims["roles"]
        if isinstance(roles_value, str | bytes | bytearray) or not isinstance(
            roles_value, Sequence
        ):
            raise AuthenticationError("JWT roles must be an array")
        roles = tuple(cast(str, value) for value in roles_value)
        if any(not isinstance(value, str) for value in roles):
            raise AuthenticationError("JWT roles must contain strings")
        try:
            return ActorRef(
                tenant_id=cast(str, claims["tenant_id"]),
                actor_id=cast(str, claims["actor_id"]),
                actor_type=ActorType(cast(str, claims["actor_type"])),
                roles=roles,
            )
        except (TypeError, ValueError) as error:
            raise AuthenticationError("JWT actor claims are invalid") from error
