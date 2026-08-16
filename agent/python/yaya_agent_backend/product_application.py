"""Product AgentInteraction read application boundary."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

from yaya_agent_contracts import ActorRef, FrozenJsonObject, canonical_json_sha256

from .product_repositories import (
    ProductInteractionReadRepository,
    ProductReadCursorError,
    ProductReadDependencyError,
    ProductReadInvariantError,
    ProductReadNotFoundError,
)
from .product_semantics import (
    ProductProjectionSemanticError,
    validate_interaction_semantics,
    validate_page_semantics,
)
from .wire import ContractSchemaValidator

_MAX_SAFE_SEQUENCE = 9_007_199_254_740_991


class ProductApplicationError(RuntimeError):
    def __init__(
        self,
        code: str,
        http_status: int,
        stage: str,
        message: str,
        details: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.stage = stage
        self.details = cast(FrozenJsonObject, dict(details or {}))


@dataclass(frozen=True, slots=True)
class ProductReadResult:
    payload: Mapping[str, object]
    headers: Mapping[str, str]


def _integer(value: object, field_name: str, *, minimum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= _MAX_SAFE_SEQUENCE
    ):
        raise ProductApplicationError(
            "INVARIANT_VIOLATION",
            500,
            "PRODUCT_READ",
            f"{field_name} is outside its contract range",
        )
    return value


class ProductInteractionReadApplication:
    def __init__(
        self,
        repository: ProductInteractionReadRepository,
        validator: ContractSchemaValidator,
    ) -> None:
        self._repository = repository
        self._validator = validator

    async def list_interactions(
        self,
        actor: ActorRef,
        session_id: str,
        *,
        after_sequence: int,
        limit: int = 50,
    ) -> ProductReadResult:
        if (
            isinstance(after_sequence, bool)
            or not isinstance(after_sequence, int)
            or not 0 <= after_sequence <= _MAX_SAFE_SEQUENCE
            or isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 100
        ):
            raise ProductApplicationError(
                "INVALID_REQUEST",
                400,
                "PRODUCT_VALIDATE",
                "Product interaction cursor is invalid",
            )
        try:
            snapshot = await self._repository.list_interactions(
                actor,
                session_id,
                after_sequence=after_sequence,
                limit=limit,
            )
        except ProductReadCursorError as error:
            raise ProductApplicationError(
                "INVALID_REQUEST",
                400,
                "PRODUCT_VALIDATE",
                str(error),
            ) from error
        except ProductReadNotFoundError as error:
            raise ProductApplicationError(
                "NOT_FOUND",
                404,
                "PRODUCT_READ",
                "Product session was not found",
            ) from error
        except ProductReadDependencyError as error:
            raise ProductApplicationError(
                "DEPENDENCY_UNAVAILABLE",
                503,
                "PRODUCT_READ",
                "Product interaction storage is unavailable",
            ) from error
        except ProductReadInvariantError as error:
            raise ProductApplicationError(
                "INVARIANT_VIOLATION",
                500,
                "PRODUCT_READ",
                str(error),
            ) from error

        interactions = [dict(item.interaction) for item in snapshot.interactions]
        high_watermark = snapshot.high_watermark_sequence
        if interactions:
            from_sequence = _integer(
                interactions[0].get("sequence"),
                "first interaction sequence",
                minimum=1,
            )
            to_sequence = _integer(
                interactions[-1].get("sequence"),
                "last interaction sequence",
                minimum=1,
            )
            next_after_sequence = to_sequence
            has_more = to_sequence < high_watermark
        else:
            from_sequence = None
            to_sequence = None
            next_after_sequence = after_sequence
            has_more = False
        payload: dict[str, object] = {
            "request_context": dict(snapshot.request_context),
            "session_id": snapshot.session_id,
            "requested_after_sequence": after_sequence,
            "requested_limit": limit,
            "high_watermark_sequence": high_watermark,
            "from_sequence": from_sequence,
            "to_sequence": to_sequence,
            "has_more": has_more,
            "next_after_sequence": next_after_sequence,
            "interactions": interactions,
        }
        self._validate_page(
            payload,
            expected_session_id=session_id,
            authenticated_actor=actor,
        )
        return ProductReadResult(
            payload,
            {"X-Interaction-High-Watermark": str(high_watermark)},
        )

    async def get_interaction(
        self,
        actor: ActorRef,
        session_id: str,
        interaction_id: str,
    ) -> ProductReadResult:
        try:
            snapshot = await self._repository.get_interaction(
                actor,
                session_id,
                interaction_id,
            )
        except ProductReadNotFoundError as error:
            raise ProductApplicationError(
                "NOT_FOUND",
                404,
                "PRODUCT_READ",
                "Product interaction was not found",
            ) from error
        except ProductReadDependencyError as error:
            raise ProductApplicationError(
                "DEPENDENCY_UNAVAILABLE",
                503,
                "PRODUCT_READ",
                "Product interaction storage is unavailable",
            ) from error
        except ProductReadInvariantError as error:
            raise ProductApplicationError(
                "INVARIANT_VIOLATION",
                500,
                "PRODUCT_READ",
                str(error),
            ) from error
        payload = dict(snapshot.interaction)
        try:
            self._validator.validate(
                "schemas/product-experience/agent-interaction.schema.json",
                payload,
            )
        except (TypeError, ValueError, RuntimeError) as error:
            raise ProductApplicationError(
                "INVARIANT_VIOLATION",
                500,
                "PRODUCT_READ",
                "Product interaction violates its outbound schema",
            ) from error
        try:
            validate_interaction_semantics(
                payload,
                authenticated_actor=actor,
                expected_session_id=session_id,
                expected_interaction_id=interaction_id,
            )
        except ProductProjectionSemanticError as error:
            raise ProductApplicationError(
                "INVARIANT_VIOLATION",
                500,
                "PRODUCT_READ",
                str(error),
            ) from error
        revision = _integer(
            payload.get("interaction_revision"),
            "interaction_revision",
            minimum=1,
        )
        etag = f'"interaction:{revision}:{canonical_json_sha256(payload)}"'
        return ProductReadResult(
            payload,
            {
                "ETag": etag,
                "X-Interaction-Revision": str(revision),
            },
        )

    def _validate_page(
        self,
        payload: Mapping[str, object],
        *,
        expected_session_id: str,
        authenticated_actor: ActorRef,
    ) -> None:
        try:
            self._validator.validate(
                "schemas/product-experience/agent-interaction-page.schema.json",
                payload,
            )
        except (TypeError, ValueError, RuntimeError) as error:
            raise ProductApplicationError(
                "INVARIANT_VIOLATION",
                500,
                "PRODUCT_READ",
                "Product interaction page violates its outbound schema",
            ) from error
        try:
            validate_page_semantics(
                payload,
                authenticated_actor=authenticated_actor,
                expected_session_id=expected_session_id,
                expected_after_sequence=_integer(
                    payload.get("requested_after_sequence"),
                    "requested_after_sequence",
                    minimum=0,
                ),
                expected_limit=_integer(
                    payload.get("requested_limit"),
                    "requested_limit",
                    minimum=1,
                ),
            )
        except ProductProjectionSemanticError as error:
            raise ProductApplicationError(
                "INVARIANT_VIOLATION",
                500,
                "PRODUCT_READ",
                str(error),
            ) from error


__all__ = [
    "ProductApplicationError",
    "ProductInteractionReadApplication",
    "ProductReadResult",
]
