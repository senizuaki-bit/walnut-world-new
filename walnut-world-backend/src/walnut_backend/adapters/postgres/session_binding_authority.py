"""Deterministic and causal authority checks for the selected Agent Session."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import AgentSessionRow, CurrentSessionBindingRow, LaunchAuthorityRow


def current_session_binding_id(
    tenant_id: str,
    authority_id: str,
    session_id: str,
) -> str:
    digest = hashlib.sha256(
        "\x00".join(("binding", tenant_id, authority_id, session_id)).encode("utf-8")
    ).hexdigest()
    return f"binding_{digest[:24]}"


def current_session_binding_matches(
    binding: CurrentSessionBindingRow | None,
    *,
    owner: AgentSessionRow,
    authority: LaunchAuthorityRow,
    observed_at: datetime,
) -> bool:
    """Close identity and time against server-authenticated durable authorities."""

    if binding is None:
        return False
    content = owner.session_json.get("content")
    if not isinstance(content, Mapping):
        return False
    timeline = _causal_timeline(
        authority.created_at,
        owner.created_at,
        binding.bound_at,
        observed_at,
    )
    if timeline is None:
        return False
    authority_created_at, session_created_at, bound_at, observed = timeline
    return (
        binding.binding_id
        == current_session_binding_id(
            owner.tenant_id,
            authority.authority_id,
            owner.session_id,
        )
        and binding.tenant_id == owner.tenant_id == authority.tenant_id
        and binding.authority_id == authority.authority_id
        and binding.session_id == owner.session_id
        and binding.actor_id == owner.actor_id == authority.actor_id
        and binding.content_hash == content.get("content_hash") == authority.content_hash
        and content.get("unit_id") == authority.content_unit_id
        and content.get("version") == authority.content_version
        and binding.world_id == owner.world_id == authority.world_id
        and binding.learner_id == owner.session_json.get("learner_id") == authority.learner_id
        and binding.agent_profile_id
        == owner.session_json.get("agent_profile_id")
        == authority.agent_profile_id
        and owner.session_json.get("channel") == authority.channel
        and owner.status == "ACTIVE"
        and authority.active is True
        and authority_created_at <= session_created_at <= bound_at <= observed
    )


async def current_session_binding_observed_at(session: AsyncSession) -> datetime | None:
    """Capture the binding check's upper bound from PostgreSQL's clock."""

    value = await session.scalar(select(func.clock_timestamp()))
    if not isinstance(value, datetime) or value.tzinfo is None:
        return None
    try:
        if value.utcoffset() is None:
            return None
        return value.astimezone(UTC)
    except (OverflowError, ValueError):
        return None


def _causal_timeline(*values: datetime) -> tuple[datetime, ...] | None:
    normalized: list[datetime] = []
    for value in values:
        if not isinstance(value, datetime) or value.tzinfo is None:
            return None
        try:
            if value.utcoffset() is None:
                return None
            normalized.append(value.astimezone(UTC))
        except (OverflowError, ValueError):
            return None
    return tuple(normalized)


__all__ = [
    "current_session_binding_id",
    "current_session_binding_matches",
    "current_session_binding_observed_at",
]
