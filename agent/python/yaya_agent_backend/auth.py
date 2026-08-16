"""Small closed HS256 JWT boundary for the production HTTP adapter."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import cast

from yaya_agent_contracts import ActorRef, ActorType


class AuthenticationError(ValueError):
    """A fail-closed bearer-token validation error."""


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise AuthenticationError(f"duplicate JWT field {key}")
        value[key] = item
    return value


def _decode_segment(segment: str, label: str) -> dict[str, object]:
    if not segment or any(character.isspace() for character in segment):
        raise AuthenticationError(f"invalid JWT {label}")
    try:
        padded = segment + "=" * (-len(segment) % 4)
        raw = base64.b64decode(padded, altchars=b"-_", validate=True)
        parsed = json.loads(raw.decode("utf-8", errors="strict"), object_pairs_hook=_strict_object)
    except (ValueError, UnicodeError, json.JSONDecodeError) as error:
        raise AuthenticationError(f"invalid JWT {label}") from error
    if not isinstance(parsed, Mapping):
        raise AuthenticationError(f"JWT {label} must be an object")
    mapping = cast(Mapping[object, object], parsed)
    if any(not isinstance(key, str) for key in mapping):
        raise AuthenticationError(f"JWT {label} keys must be strings")
    return {cast(str, key): item for key, item in mapping.items()}


def _encode_segment(value: Mapping[str, object]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(encoded).rstrip(b"=").decode("ascii")


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AuthenticationError(f"JWT {name} must be an integer")
    return value


class JwtAuthenticator:
    def __init__(
        self,
        *,
        hmac_secret: str,
        issuer: str,
        audience: str,
        clock_skew_seconds: int = 30,
        maximum_lifetime_seconds: int = 3600,
    ) -> None:
        if not 32 <= len(hmac_secret) <= 4096:
            raise ValueError("JWT HMAC secret must contain 32..4096 characters")
        if not issuer or not audience:
            raise ValueError("JWT issuer and audience are required")
        if not 0 <= clock_skew_seconds <= 300:
            raise ValueError("JWT clock skew must be between 0 and 300 seconds")
        if not 60 <= maximum_lifetime_seconds <= 86_400:
            raise ValueError("JWT maximum lifetime must be between 60 and 86400 seconds")
        self._secret = hmac_secret.encode("utf-8")
        self._issuer = issuer
        self._audience = audience
        self._skew = clock_skew_seconds
        self._maximum_lifetime = maximum_lifetime_seconds

    def authenticate(self, authorization: str, *, now: datetime | None = None) -> ActorRef:
        prefix = "Bearer "
        if not authorization.startswith(prefix):
            raise AuthenticationError("Authorization must use Bearer JWT")
        token = authorization[len(prefix) :]
        if token.count(".") != 2:
            raise AuthenticationError("Bearer credential is not a JWT")
        encoded_header, encoded_claims, encoded_signature = token.split(".")
        header = _decode_segment(encoded_header, "header")
        claims = _decode_segment(encoded_claims, "claims")
        if header != {"alg": "HS256", "typ": "JWT"}:
            raise AuthenticationError("JWT header must select only HS256")
        signing_input = f"{encoded_header}.{encoded_claims}".encode("ascii")
        expected = hmac.new(self._secret, signing_input, hashlib.sha256).digest()
        try:
            padded = encoded_signature + "=" * (-len(encoded_signature) % 4)
            supplied = base64.b64decode(padded, altchars=b"-_", validate=True)
        except ValueError as error:
            raise AuthenticationError("invalid JWT signature encoding") from error
        if not hmac.compare_digest(expected, supplied):
            raise AuthenticationError("JWT signature mismatch")

        required = {
            "iss",
            "aud",
            "sub",
            "tenant_id",
            "actor_id",
            "actor_type",
            "roles",
            "iat",
            "nbf",
            "exp",
        }
        if set(claims) != required:
            raise AuthenticationError("JWT claims must use the closed production claim set")
        if claims["iss"] != self._issuer or claims["aud"] != self._audience:
            raise AuthenticationError("JWT issuer or audience mismatch")
        if claims["sub"] != claims["actor_id"]:
            raise AuthenticationError("JWT subject must equal actor_id")
        current = (datetime.now(UTC) if now is None else now).astimezone(UTC)
        current_timestamp = int(current.timestamp())
        issued = _integer(claims["iat"], "iat")
        not_before = _integer(claims["nbf"], "nbf")
        expires = _integer(claims["exp"], "exp")
        if issued > current_timestamp + self._skew:
            raise AuthenticationError("JWT issued-at is in the future")
        if not_before > current_timestamp + self._skew:
            raise AuthenticationError("JWT is not active yet")
        if expires <= current_timestamp - self._skew:
            raise AuthenticationError("JWT has expired")
        if expires <= issued or expires - issued > self._maximum_lifetime:
            raise AuthenticationError("JWT lifetime is invalid")
        roles_value = claims["roles"]
        if isinstance(roles_value, (str, bytes, bytearray)) or not isinstance(
            roles_value, Sequence
        ):
            raise AuthenticationError("JWT roles must be an array")
        roles_raw = tuple(cast(Sequence[object], roles_value))
        if any(not isinstance(role, str) for role in roles_raw):
            raise AuthenticationError("JWT roles must contain strings")
        roles = cast(tuple[str, ...], roles_raw)
        try:
            return ActorRef(
                tenant_id=cast(str, claims["tenant_id"]),
                actor_id=cast(str, claims["actor_id"]),
                actor_type=ActorType(cast(str, claims["actor_type"])),
                roles=roles,
            )
        except (TypeError, ValueError) as error:
            raise AuthenticationError("JWT actor claims are invalid") from error

    def issue_for_test(
        self,
        actor: ActorRef,
        *,
        now: datetime,
        lifetime: timedelta = timedelta(minutes=10),
    ) -> str:
        """Issue a real signed token for integration tests and local smoke runs."""

        issued = int(now.astimezone(UTC).timestamp())
        lifetime_seconds = int(lifetime.total_seconds())
        if not 1 <= lifetime_seconds <= self._maximum_lifetime:
            raise ValueError("test token lifetime is outside the configured maximum")
        header = _encode_segment({"alg": "HS256", "typ": "JWT"})
        claims = _encode_segment(
            {
                "iss": self._issuer,
                "aud": self._audience,
                "sub": actor.actor_id,
                "tenant_id": actor.tenant_id,
                "actor_id": actor.actor_id,
                "actor_type": actor.actor_type.value,
                "roles": list(actor.roles),
                "iat": issued,
                "nbf": issued,
                "exp": issued + lifetime_seconds,
            }
        )
        signing_input = f"{header}.{claims}".encode("ascii")
        signature = base64.urlsafe_b64encode(
            hmac.new(self._secret, signing_input, hashlib.sha256).digest()
        ).rstrip(b"=")
        return f"{header}.{claims}.{signature.decode('ascii')}"


__all__ = ["AuthenticationError", "JwtAuthenticator"]
