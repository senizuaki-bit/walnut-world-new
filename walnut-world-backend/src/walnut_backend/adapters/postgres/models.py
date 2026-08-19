"""Database tables used by the PostgreSQL command/audit/outbox adapters."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class CommandRow(Base):
    __tablename__ = "commands"

    command_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(96), nullable=False, index=True)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    command_type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    terminal: Mapped[bool] = mapped_column(nullable=False)
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    record_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class IdempotencyReceiptRow(Base):
    __tablename__ = "idempotency_receipts"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "actor_id",
            "operation",
            "idempotency_key",
            name="uq_command_idempotency_scope",
        ),
    )

    receipt_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(96), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    operation: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    command_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AuditRow(Base):
    __tablename__ = "audit_records"

    audit_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(96), nullable=False, index=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    operation: Mapped[str] = mapped_column(String(128), nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    record_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class OutboxRow(Base):
    __tablename__ = "outbox_messages"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "destination", "idempotency_key", name="uq_outbox_delivery_scope"
        ),
        Index("ix_outbox_ready", "tenant_id", "status", "next_attempt_at", "lease_expires_at"),
    )

    message_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(96), nullable=False)
    destination: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_id: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    message_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class WorldStreamRow(Base):
    __tablename__ = "world_streams"
    __table_args__ = (
        UniqueConstraint("tenant_id", "world_id", name="uq_world_stream_tenant_world"),
    )

    stream_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    world_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_sequence: Mapped[int] = mapped_column(Integer, nullable=False)


class WorldSnapshotRow(Base):
    __tablename__ = "world_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "world_id",
            "actor_id",
            "content_hash",
            name="uq_world_snapshot_authority",
        ),
    )

    world_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    last_event_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    state_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class WorldPresentationStreamRow(Base):
    """Internal high-watermark for the append-only World presentation stream."""

    __tablename__ = "world_presentation_streams"
    __table_args__ = (
        UniqueConstraint("tenant_id", "world_id", name="uq_world_presentation_tenant_world"),
        ForeignKeyConstraint(
            ["world_id", "tenant_id"],
            ["world_snapshots.world_id", "world_snapshots.tenant_id"],
            name="fk_world_presentation_snapshot",
        ),
        CheckConstraint("last_sequence >= 0", name="ck_world_presentation_last_sequence"),
        CheckConstraint(
            "initial_world_revision >= 0 AND initial_world_event_sequence >= 0 "
            "AND last_world_revision >= initial_world_revision "
            "AND last_world_event_sequence >= initial_world_event_sequence",
            name="ck_world_presentation_world_head",
        ),
        CheckConstraint(
            "gap_world_revision IS NULL OR gap_world_revision >= 1",
            name="ck_world_presentation_gap_revision",
        ),
        CheckConstraint(
            "initial_snapshot_state_hash ~ '^[a-f0-9]{64}$' "
            "AND last_snapshot_state_hash ~ '^[a-f0-9]{64}$'",
            name="ck_world_presentation_stream_hashes",
        ),
    )

    stream_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    world_id: Mapped[str] = mapped_column(String(128), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    initial_world_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    initial_world_event_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    initial_snapshot_state_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    last_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    last_world_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    last_world_event_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    last_snapshot_state_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    gap_world_revision: Mapped[int | None] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class WorldPresentationEventRow(Base):
    """One closed, authoritative HARVEST display action; never a raw sandbox intent."""

    __tablename__ = "world_presentation_events"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "stream_id", "sequence", name="uq_world_presentation_sequence"
        ),
        UniqueConstraint(
            "tenant_id",
            "commit_id",
            "action_index",
            name="uq_world_presentation_commit_action",
        ),
        UniqueConstraint(
            "tenant_id", "commit_id", "intent_id", name="uq_world_presentation_commit_intent"
        ),
        ForeignKeyConstraint(
            ["stream_id", "tenant_id"],
            ["world_presentation_streams.stream_id", "world_presentation_streams.tenant_id"],
            name="fk_world_presentation_event_stream",
        ),
        Index(
            "ix_world_presentation_events_stream_sequence",
            "tenant_id",
            "stream_id",
            "sequence",
        ),
        CheckConstraint("sequence >= 1", name="ck_world_presentation_event_sequence"),
        CheckConstraint(
            "action_count >= 1 AND action_index >= 0 AND action_index < action_count",
            name="ck_world_presentation_action_index",
        ),
        CheckConstraint(
            "event_type = 'world.action.harvested' AND event_version = 1 "
            "AND schema_version = '1.0.0' AND producer = 'walnut_world_engine'",
            name="ck_world_presentation_event_version",
        ),
        CheckConstraint(
            "state_hash_before ~ '^[a-f0-9]{64}$' "
            "AND state_hash_after ~ '^[a-f0-9]{64}$' "
            "AND final_snapshot_state_hash ~ '^[a-f0-9]{64}$' "
            "AND payload_sha256 ~ '^[a-f0-9]{64}$' "
            "AND integrity_sha256 ~ '^[a-f0-9]{64}$'",
            name="ck_world_presentation_event_hashes",
        ),
    )

    event_id: Mapped[str] = mapped_column(String(45), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(96), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    stream_id: Mapped[str] = mapped_column(String(160), nullable=False)
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    event_version: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    producer: Mapped[str] = mapped_column(String(64), nullable=False)
    world_id: Mapped[str] = mapped_column(String(128), nullable=False)
    session_id: Mapped[str] = mapped_column(String(128), nullable=False)
    turn_id: Mapped[str] = mapped_column(String(128), nullable=False)
    command_id: Mapped[str] = mapped_column(String(128), nullable=False)
    run_id: Mapped[str] = mapped_column(String(128), nullable=False)
    commit_id: Mapped[str] = mapped_column(String(128), nullable=False)
    world_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    action_index: Mapped[int] = mapped_column(Integer, nullable=False)
    action_count: Mapped[int] = mapped_column(Integer, nullable=False)
    intent_id: Mapped[str] = mapped_column(String(128), nullable=False)
    state_hash_before: Mapped[str] = mapped_column(String(64), nullable=False)
    state_hash_after: Mapped[str] = mapped_column(String(64), nullable=False)
    final_snapshot_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    final_world_event_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    final_snapshot_state_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    integrity_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    event_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class EventRow(Base):
    __tablename__ = "domain_events"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "stream_id", "sequence", name="uq_domain_event_stream_sequence"
        ),
        Index("ix_domain_events_stream_sequence", "tenant_id", "stream_id", "sequence"),
    )

    event_id: Mapped[str] = mapped_column(String(132), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(96), nullable=False, index=True)
    stream_id: Mapped[str] = mapped_column(String(160), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    event_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class SkillBuildRow(Base):
    __tablename__ = "skill_builds"
    __table_args__ = (
        UniqueConstraint("tenant_id", "command_id", name="uq_skill_build_command"),
        Index("ix_skill_builds_authority", "tenant_id", "actor_id", "updated_at"),
    )

    build_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(96), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    command_id: Mapped[str] = mapped_column(String(128), nullable=False)
    skill_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    terminal: Mapped[bool] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    build_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    request_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class AgentSessionRow(Base):
    __tablename__ = "agent_sessions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "command_id", name="uq_agent_session_command"),
        Index("ix_agent_sessions_authority", "tenant_id", "actor_id", "updated_at"),
    )

    session_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(96), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    command_id: Mapped[str] = mapped_column(String(128), nullable=False)
    world_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    session_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class AgentTurnRow(Base):
    __tablename__ = "agent_turns"
    __table_args__ = (
        UniqueConstraint("tenant_id", "session_id", "turn_id", name="uq_agent_turn_identity"),
        UniqueConstraint("tenant_id", "command_id", name="uq_agent_turn_command"),
        Index("ix_agent_turns_session_sequence", "tenant_id", "session_id", "turn_sequence"),
    )

    turn_row_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(96), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    session_id: Mapped[str] = mapped_column(String(128), nullable=False)
    turn_id: Mapped[str] = mapped_column(String(128), nullable=False)
    command_id: Mapped[str] = mapped_column(String(128), nullable=False)
    turn_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    request_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class ClientEventRow(Base):
    """Durable client outbox event, unique per tenant and device event identity."""

    __tablename__ = "client_events"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "session_id", "sequence", name="uq_client_event_session_sequence"
        ),
        Index("ix_client_events_authority", "tenant_id", "actor_id", "session_id", "sequence"),
    )

    event_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(96), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    session_id: Mapped[str] = mapped_column(String(128), nullable=False)
    world_id: Mapped[str] = mapped_column(String(128), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    event_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class ProductContentUnitRow(Base):
    """Immutable, tenant-authorized published Product content representation."""

    __tablename__ = "product_content_units"
    __table_args__ = (
        UniqueConstraint("tenant_id", "unit_id", "version", name="uq_product_content_version"),
        UniqueConstraint("tenant_id", "unit_id", "content_hash", name="uq_product_content_hash"),
        UniqueConstraint(
            "tenant_id",
            "unit_id",
            "version",
            "content_hash",
            name="uq_product_content_authority",
        ),
        Index("ix_product_content_lookup", "tenant_id", "unit_id", "version", "content_hash"),
    )

    content_row_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(96), nullable=False)
    unit_id: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    audiences: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    content_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class ProductWorkspaceRow(Base):
    __tablename__ = "product_workspaces"
    __table_args__ = (
        UniqueConstraint("tenant_id", "session_id", name="uq_product_workspace_session"),
    )

    workspace_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(96), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    session_id: Mapped[str] = mapped_column(String(128), nullable=False)
    workspace_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    workspace_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class RunRow(Base):
    """Immutable Game Run projection written by the future Agent Turn worker."""

    __tablename__ = "game_runs"
    __table_args__ = (Index("ix_game_runs_authority", "tenant_id", "actor_id", "created_at"),)

    run_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(96), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    session_id: Mapped[str] = mapped_column(String(128), nullable=False)
    turn_id: Mapped[str] = mapped_column(String(128), nullable=False)
    command_id: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    run_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class EvidenceRow(Base):
    """Immutable Evidence projection authorized by its origin actor and content."""

    __tablename__ = "game_evidence"
    __table_args__ = (Index("ix_game_evidence_authority", "tenant_id", "actor_id", "recorded_at"),)

    evidence_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(96), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    command_id: Mapped[str | None] = mapped_column(String(128))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    evidence_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class ProductDraftRow(Base):
    __tablename__ = "product_skill_drafts"
    __table_args__ = (
        UniqueConstraint("tenant_id", "session_id", "draft_id", name="uq_product_draft_identity"),
        UniqueConstraint("tenant_id", "session_id", "skill_id", name="uq_product_draft_skill"),
        Index("ix_product_drafts_authority", "tenant_id", "actor_id", "session_id"),
    )

    draft_row_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(96), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    session_id: Mapped[str] = mapped_column(String(128), nullable=False)
    draft_id: Mapped[str] = mapped_column(String(128), nullable=False)
    skill_id: Mapped[str] = mapped_column(String(128), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    draft_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    draft_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class ProductDraftRevisionRow(Base):
    """Append-only Draft bytes used by Patch CAS and Build provenance."""

    __tablename__ = "product_skill_draft_revisions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "session_id",
            "draft_id",
            "revision",
            name="uq_product_draft_revision_identity",
        ),
        UniqueConstraint(
            "tenant_id",
            "session_id",
            "draft_id",
            "revision",
            "draft_sha256",
            name="uq_product_draft_revision_authority",
        ),
        UniqueConstraint(
            "draft_revision_row_id",
            "patch_id",
            name="uq_product_draft_revision_patch_pair",
        ),
        ForeignKeyConstraint(
            ["parent_revision_row_id"],
            ["product_skill_draft_revisions.draft_revision_row_id"],
            name="fk_product_draft_revision_parent",
        ),
        CheckConstraint(
            "draft_sha256 ~ '^[a-f0-9]{64}$' "
            "AND source_bundle_sha256 ~ '^[a-f0-9]{64}$'",
            name="ck_product_draft_revision_hashes",
        ),
        CheckConstraint(
            "(source_kind = 'STUDENT' AND patch_id IS NULL) OR "
            "(source_kind = 'SKILL_PATCH' AND patch_id IS NOT NULL)",
            name="ck_product_draft_revision_source",
        ),
        Index(
            "ix_product_draft_revision_authority",
            "tenant_id",
            "actor_id",
            "session_id",
            "skill_id",
            "revision",
        ),
    )

    draft_revision_row_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True
    )
    parent_revision_row_id: Mapped[int | None] = mapped_column(BigInteger)
    tenant_id: Mapped[str] = mapped_column(String(96), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    session_id: Mapped[str] = mapped_column(String(128), nullable=False)
    draft_id: Mapped[str] = mapped_column(String(128), nullable=False)
    skill_id: Mapped[str] = mapped_column(String(128), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    draft_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    entrypoint: Mapped[str] = mapped_column(String(240), nullable=False)
    source_bundle_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    patch_id: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    draft_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class ProductSkillPatchProposalRow(Base):
    """Immutable Backend-derived proposal over one exact failed authority chain."""

    __tablename__ = "product_skill_patch_proposals"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "interaction_id", name="uq_product_skill_patch_interaction"
        ),
        UniqueConstraint(
            "tenant_id",
            "requested_interaction_id",
            name="uq_product_skill_patch_selected_failure",
        ),
        UniqueConstraint(
            "tenant_id", "patch_id", "patch_sha256", name="uq_product_skill_patch_authority"
        ),
        UniqueConstraint(
            "patch_id",
            "base_draft_revision_row_id",
            name="uq_product_skill_patch_base_pair",
        ),
        ForeignKeyConstraint(
            ["base_draft_revision_row_id"],
            ["product_skill_draft_revisions.draft_revision_row_id"],
            name="fk_product_skill_patch_base_draft",
        ),
        ForeignKeyConstraint(
            ["failed_build_id"],
            ["skill_builds.build_id"],
            name="fk_product_skill_patch_failed_build",
        ),
        ForeignKeyConstraint(
            ["failed_run_id"],
            ["game_runs.run_id"],
            name="fk_product_skill_patch_failed_run",
        ),
        CheckConstraint(
            "base_draft_revision >= 1 AND requested_interaction_revision >= 1 "
            "AND requested_interaction_sequence >= 1 "
            "AND requested_failure_suffix_end_sequence = requested_interaction_sequence "
            "AND failure_count >= 4 "
            "AND base_draft_sha256 ~ '^[a-f0-9]{64}$' "
            "AND source_bundle_sha256 ~ '^[a-f0-9]{64}$' "
            "AND entrypoint_sha256 ~ '^[a-f0-9]{64}$' "
            "AND previous_content_sha256 ~ '^[a-f0-9]{64}$' "
            "AND content_sha256 ~ '^[a-f0-9]{64}$' "
            "AND result_draft_sha256 ~ '^[a-f0-9]{64}$' "
            "AND patch_sha256 ~ '^[a-f0-9]{64}$' "
            "AND agent_proposal_sha256 ~ '^[a-f0-9]{64}$'",
            name="ck_product_skill_patch_hashes",
        ),
    )

    patch_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(96), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    session_id: Mapped[str] = mapped_column(String(128), nullable=False)
    interaction_id: Mapped[str] = mapped_column(String(128), nullable=False)
    requested_interaction_id: Mapped[str] = mapped_column(String(128), nullable=False)
    turn_id: Mapped[str] = mapped_column(String(128), nullable=False)
    request_command_id: Mapped[str] = mapped_column(String(128), nullable=False)
    requested_interaction_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    requested_interaction_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    requested_failure_suffix_end_sequence: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    failed_turn_id: Mapped[str] = mapped_column(String(128), nullable=False)
    failed_command_id: Mapped[str] = mapped_column(String(128), nullable=False)
    task_id: Mapped[str] = mapped_column(String(128), nullable=False)
    world_id: Mapped[str] = mapped_column(String(128), nullable=False)
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False)
    failure_key: Mapped[str] = mapped_column(String(128), nullable=False)
    feedback_event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    projection_receipt_id: Mapped[str] = mapped_column(String(128), nullable=False)
    draft_id: Mapped[str] = mapped_column(String(128), nullable=False)
    skill_id: Mapped[str] = mapped_column(String(128), nullable=False)
    base_draft_revision_row_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    base_draft_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    base_draft_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_bundle_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    entrypoint: Mapped[str] = mapped_column(String(240), nullable=False)
    entrypoint_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    previous_content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    result_draft_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    patch_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    agent_proposal_id: Mapped[str] = mapped_column(String(128), nullable=False)
    agent_proposal_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    failed_build_id: Mapped[str] = mapped_column(String(128), nullable=False)
    failed_run_id: Mapped[str] = mapped_column(String(128), nullable=False)
    proposal_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    agent_proposal_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ProductSkillPatchRequestRow(Base):
    """Pre-Provider reservation for one explicit selected failure."""

    __tablename__ = "product_skill_patch_requests"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "requested_interaction_id",
            name="uq_product_skill_patch_request_selected_failure",
        ),
        UniqueConstraint(
            "tenant_id", "command_id", name="uq_product_skill_patch_request_command"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "session_id", "requested_interaction_id"],
            [
                "product_agent_interactions.tenant_id",
                "product_agent_interactions.session_id",
                "product_agent_interactions.interaction_id",
            ],
            name="fk_product_skill_patch_request_interaction",
        ),
        CheckConstraint(
            "authority_sha256 ~ '^[a-f0-9]{64}$' AND status IN ('PENDING','PROPOSED')",
            name="ck_product_skill_patch_request_authority",
        ),
    )

    request_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(96), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    session_id: Mapped[str] = mapped_column(String(128), nullable=False)
    turn_id: Mapped[str] = mapped_column(String(128), nullable=False)
    command_id: Mapped[str] = mapped_column(String(128), nullable=False)
    requested_interaction_id: Mapped[str] = mapped_column(String(128), nullable=False)
    authority_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    proposal_id: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ProductSkillPatchEvidenceRow(Base):
    """Exact immutable Evidence references retained by a proposal."""

    __tablename__ = "product_skill_patch_evidence"
    __table_args__ = (
        ForeignKeyConstraint(
            ["patch_id"],
            ["product_skill_patch_proposals.patch_id"],
            name="fk_product_skill_patch_evidence_proposal",
        ),
        ForeignKeyConstraint(
            ["evidence_id"],
            ["game_evidence.evidence_id"],
            name="fk_product_skill_patch_evidence_row",
        ),
        CheckConstraint(
            "evidence_sha256 ~ '^[a-f0-9]{64}$'",
            name="ck_product_skill_patch_evidence_hash",
        ),
    )

    patch_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    evidence_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    evidence_type: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    evidence_ref_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class ProductSkillPatchDecisionRow(Base):
    """One immutable terminal decision for one exact Patch proposal."""

    __tablename__ = "product_skill_patch_decisions"
    __table_args__ = (
        UniqueConstraint("patch_id", name="uq_product_skill_patch_terminal_decision"),
        UniqueConstraint(
            "accepted_draft_revision_row_id",
            name="uq_product_skill_patch_accepted_draft",
        ),
        UniqueConstraint("decision_id", "patch_id", name="uq_product_skill_patch_decision_pair"),
        UniqueConstraint(
            "decision_id",
            "patch_id",
            "accepted_draft_revision_row_id",
            name="uq_product_skill_patch_decision_accepted_triple",
        ),
        ForeignKeyConstraint(
            ["patch_id"],
            ["product_skill_patch_proposals.patch_id"],
            name="fk_product_skill_patch_decision_proposal",
        ),
        ForeignKeyConstraint(
            ["patch_id", "base_draft_revision_row_id"],
            [
                "product_skill_patch_proposals.patch_id",
                "product_skill_patch_proposals.base_draft_revision_row_id",
            ],
            name="fk_product_skill_patch_decision_proposal_base",
        ),
        ForeignKeyConstraint(
            ["base_draft_revision_row_id"],
            ["product_skill_draft_revisions.draft_revision_row_id"],
            name="fk_product_skill_patch_decision_base_draft",
        ),
        ForeignKeyConstraint(
            ["accepted_draft_revision_row_id", "patch_id"],
            [
                "product_skill_draft_revisions.draft_revision_row_id",
                "product_skill_draft_revisions.patch_id",
            ],
            name="fk_product_skill_patch_decision_accepted_draft",
        ),
        CheckConstraint(
            "(decision = 'ACCEPT' AND reason_code IS NULL "
            "AND accepted_draft_revision_row_id IS NOT NULL) OR "
            "(decision = 'REJECT' AND reason_code IS NOT NULL "
            "AND accepted_draft_revision_row_id IS NULL)",
            name="ck_product_skill_patch_decision_terminal",
        ),
        CheckConstraint(
            "request_sha256 ~ '^[a-f0-9]{64}$'",
            name="ck_product_skill_patch_decision_request_hash",
        ),
    )

    decision_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(96), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    patch_id: Mapped[str] = mapped_column(String(128), nullable=False)
    interaction_id: Mapped[str] = mapped_column(String(128), nullable=False)
    session_id: Mapped[str] = mapped_column(String(128), nullable=False)
    draft_id: Mapped[str] = mapped_column(String(128), nullable=False)
    base_draft_revision_row_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    accepted_draft_revision_row_id: Mapped[int | None] = mapped_column(BigInteger)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(96))
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    receipt_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Int2LegacyBuildMarkerRow(Base):
    """Migration-sealed proof that a Build predates immutable Draft provenance."""

    __tablename__ = "int2_legacy_build_markers"
    __table_args__ = (
        UniqueConstraint("build_id", name="uq_int2_legacy_build_marker_build"),
        UniqueConstraint(
            "marker_id",
            "build_id",
            "tenant_id",
            "actor_id",
            "build_authority_sha256",
            name="uq_int2_legacy_build_marker_authority",
        ),
        ForeignKeyConstraint(
            ["build_id"],
            ["skill_builds.build_id"],
            name="fk_int2_legacy_build_marker_build",
        ),
        CheckConstraint(
            "build_authority_sha256 ~ '^[a-f0-9]{64}$' "
            "AND marker_sha256 ~ '^[a-f0-9]{64}$'",
            name="ck_int2_legacy_build_marker_hashes",
        ),
    )

    marker_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    build_id: Mapped[str] = mapped_column(String(128), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(96), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    build_authority_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    marker_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SkillBuildProvenanceRow(Base):
    """Acyclic immutable Draft/Patch authority attached to one Build."""

    __tablename__ = "skill_build_provenance"
    __table_args__ = (
        UniqueConstraint(
            "build_id",
            "authority_sha256",
            name="uq_skill_build_provenance_authority",
        ),
        ForeignKeyConstraint(
            ["build_id"],
            ["skill_builds.build_id"],
            name="fk_skill_build_provenance_build",
        ),
        ForeignKeyConstraint(
            ["command_receipt_id"],
            ["idempotency_receipts.receipt_id"],
            name="fk_skill_build_provenance_command_receipt",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "workflow_job_id"],
            ["workflow_jobs.tenant_id", "workflow_jobs.job_id"],
            name="fk_skill_build_provenance_workflow_job",
        ),
        ForeignKeyConstraint(
            [
                "legacy_marker_id",
                "build_id",
                "tenant_id",
                "actor_id",
                "authority_sha256",
            ],
            [
                "int2_legacy_build_markers.marker_id",
                "int2_legacy_build_markers.build_id",
                "int2_legacy_build_markers.tenant_id",
                "int2_legacy_build_markers.actor_id",
                "int2_legacy_build_markers.build_authority_sha256",
            ],
            name="fk_skill_build_provenance_legacy_marker",
        ),
        ForeignKeyConstraint(
            ["draft_revision_row_id"],
            ["product_skill_draft_revisions.draft_revision_row_id"],
            name="fk_skill_build_provenance_draft",
        ),
        ForeignKeyConstraint(
            ["patch_id"],
            ["product_skill_patch_proposals.patch_id"],
            name="fk_skill_build_provenance_patch",
        ),
        ForeignKeyConstraint(
            ["patch_decision_id", "patch_id", "origin_accepted_revision_row_id"],
            [
                "product_skill_patch_decisions.decision_id",
                "product_skill_patch_decisions.patch_id",
                "product_skill_patch_decisions.accepted_draft_revision_row_id",
            ],
            name="fk_skill_build_provenance_accepted_decision",
        ),
        ForeignKeyConstraint(
            [
                "draft_revision_row_id",
                "origin_accepted_revision_row_id",
                "patch_id",
                "patch_decision_id",
            ],
            [
                "product_draft_revision_assistance.draft_revision_row_id",
                "product_draft_revision_assistance.origin_accepted_revision_row_id",
                "product_draft_revision_assistance.patch_id",
                "product_draft_revision_assistance.patch_decision_id",
            ],
            name="fk_skill_build_provenance_draft_assistance",
        ),
        CheckConstraint(
            "build_request_sha256 ~ '^[a-f0-9]{64}$' "
            "AND command_receipt_authority_sha256 ~ '^[a-f0-9]{64}$' "
            "AND workflow_request_sha256 ~ '^[a-f0-9]{64}$' "
            "AND source_bundle_sha256 ~ '^[a-f0-9]{64}$' "
            "AND authority_sha256 ~ '^[a-f0-9]{64}$'",
            name="ck_skill_build_provenance_hashes",
        ),
        CheckConstraint(
            "(provenance_kind = 'LEGACY_V04' AND assistance_authority = 'NONE' "
            "AND legacy_marker_id IS NOT NULL "
            "AND session_id IS NULL AND draft_id IS NULL "
            "AND draft_revision_row_id IS NULL AND draft_revision IS NULL "
            "AND draft_sha256 IS NULL AND origin_accepted_revision_row_id IS NULL "
            "AND patch_id IS NULL AND patch_decision_id IS NULL) OR "
            "(provenance_kind = 'IMMUTABLE_DRAFT' AND session_id IS NOT NULL "
            "AND legacy_marker_id IS NULL "
            "AND draft_id IS NOT NULL AND draft_revision_row_id IS NOT NULL "
            "AND draft_revision >= 1 AND draft_sha256 ~ '^[a-f0-9]{64}$' AND "
            "((assistance_authority = 'NONE' "
            "AND origin_accepted_revision_row_id IS NULL AND patch_id IS NULL "
            "AND patch_decision_id IS NULL) OR "
            "(assistance_authority = 'SKILL_PATCH' "
            "AND origin_accepted_revision_row_id IS NOT NULL "
            "AND patch_id IS NOT NULL AND patch_decision_id IS NOT NULL)))",
            name="ck_skill_build_provenance_assistance",
        ),
    )

    build_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    provenance_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    legacy_marker_id: Mapped[str | None] = mapped_column(String(128))
    tenant_id: Mapped[str] = mapped_column(String(96), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    build_request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    command_receipt_id: Mapped[int] = mapped_column(Integer, nullable=False)
    command_receipt_authority_sha256: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    workflow_job_id: Mapped[str] = mapped_column(String(128), nullable=False)
    workflow_request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    session_id: Mapped[str | None] = mapped_column(String(128))
    draft_id: Mapped[str | None] = mapped_column(String(128))
    skill_id: Mapped[str] = mapped_column(String(128), nullable=False)
    draft_revision_row_id: Mapped[int | None] = mapped_column(BigInteger)
    draft_revision: Mapped[int | None] = mapped_column(Integer)
    draft_sha256: Mapped[str | None] = mapped_column(String(64))
    source_bundle_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    origin_accepted_revision_row_id: Mapped[int | None] = mapped_column(BigInteger)
    patch_id: Mapped[str | None] = mapped_column(String(128))
    patch_decision_id: Mapped[str | None] = mapped_column(String(128))
    assistance_authority: Mapped[str] = mapped_column(String(32), nullable=False)
    authority_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ProductDraftRevisionAssistanceRow(Base):
    """Explicit assistance lineage; never inferred from a recent Patch."""

    __tablename__ = "product_draft_revision_assistance"
    __table_args__ = (
        UniqueConstraint(
            "draft_revision_row_id",
            "origin_accepted_revision_row_id",
            "patch_id",
            "patch_decision_id",
            name="uq_product_draft_assistance_authority",
        ),
        ForeignKeyConstraint(
            ["draft_revision_row_id"],
            ["product_skill_draft_revisions.draft_revision_row_id"],
            name="fk_product_draft_assistance_revision",
        ),
        ForeignKeyConstraint(
            ["origin_accepted_revision_row_id"],
            ["product_skill_draft_revisions.draft_revision_row_id"],
            name="fk_product_draft_assistance_origin",
        ),
        ForeignKeyConstraint(
            ["patch_id"],
            ["product_skill_patch_proposals.patch_id"],
            name="fk_product_draft_assistance_patch",
        ),
        ForeignKeyConstraint(
            ["patch_decision_id", "patch_id", "origin_accepted_revision_row_id"],
            [
                "product_skill_patch_decisions.decision_id",
                "product_skill_patch_decisions.patch_id",
                "product_skill_patch_decisions.accepted_draft_revision_row_id",
            ],
            name="fk_product_draft_assistance_accepted_decision",
        ),
    )

    draft_revision_row_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    origin_accepted_revision_row_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    patch_id: Mapped[str] = mapped_column(String(128), nullable=False)
    patch_decision_id: Mapped[str] = mapped_column(String(128), nullable=False)
    inherited: Mapped[bool] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SkillCertificationProvenanceRow(Base):
    """Sealed successful Build/Policy/Artifact authority for a Certification."""

    __tablename__ = "skill_certification_provenance"
    __table_args__ = (
        UniqueConstraint(
            "certification_id",
            "authority_sha256",
            name="uq_skill_certification_provenance_authority",
        ),
        ForeignKeyConstraint(
            ["certification_id"],
            ["skill_certifications.certification_id"],
            name="fk_skill_certification_provenance_certification",
        ),
        ForeignKeyConstraint(
            ["build_id", "build_authority_sha256"],
            ["skill_build_provenance.build_id", "skill_build_provenance.authority_sha256"],
            name="fk_skill_certification_provenance_build",
        ),
        ForeignKeyConstraint(
            ["build_receipt_id"],
            ["job_step_receipts.receipt_id"],
            name="fk_skill_certification_provenance_receipt",
        ),
        CheckConstraint(
            "build_authority_sha256 ~ '^[a-f0-9]{64}$' "
            "AND build_request_sha256 ~ '^[a-f0-9]{64}$' "
            "AND workflow_request_sha256 ~ '^[a-f0-9]{64}$' "
            "AND workflow_job_sha256 ~ '^[a-f0-9]{64}$' "
            "AND command_authority_sha256 ~ '^[a-f0-9]{64}$' "
            "AND build_receipt_sha256 ~ '^[a-f0-9]{64}$' "
            "AND build_receipt_authority_sha256 ~ '^[a-f0-9]{64}$' "
            "AND policy_sha256 ~ '^[a-f0-9]{64}$' "
            "AND artifact_sha256 ~ '^[a-f0-9]{64}$' "
            "AND artifact_authority_sha256 ~ '^[a-f0-9]{64}$' "
            "AND certification_sha256 ~ '^[a-f0-9]{64}$' "
            "AND authority_sha256 ~ '^[a-f0-9]{64}$'",
            name="ck_skill_certification_provenance_authority",
        ),
    )

    certification_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(96), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    build_id: Mapped[str] = mapped_column(String(128), nullable=False)
    build_authority_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    build_request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    workflow_job_id: Mapped[str] = mapped_column(String(128), nullable=False)
    workflow_request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    workflow_job_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    command_authority_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    build_receipt_id: Mapped[str] = mapped_column(String(128), nullable=False)
    build_receipt_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    build_receipt_authority_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    artifact_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    artifact_authority_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    certification_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    authority_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SkillBuildTerminalAuthorityRow(Base):
    """Append-only terminal Command/Job/receipt seal for one Build."""

    __tablename__ = "skill_build_terminal_authority"
    __table_args__ = (
        UniqueConstraint(
            "build_id",
            "authority_sha256",
            name="uq_skill_build_terminal_authority",
        ),
        ForeignKeyConstraint(
            ["build_id", "build_authority_sha256"],
            ["skill_build_provenance.build_id", "skill_build_provenance.authority_sha256"],
            name="fk_skill_build_terminal_build",
        ),
        ForeignKeyConstraint(
            ["command_id"],
            ["commands.command_id"],
            name="fk_skill_build_terminal_command",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "workflow_job_id"],
            ["workflow_jobs.tenant_id", "workflow_jobs.job_id"],
            name="fk_skill_build_terminal_workflow",
        ),
        ForeignKeyConstraint(
            ["terminal_receipt_id"],
            ["job_step_receipts.receipt_id"],
            name="fk_skill_build_terminal_receipt",
        ),
        ForeignKeyConstraint(
            ["certification_id", "certification_authority_sha256"],
            [
                "skill_certification_provenance.certification_id",
                "skill_certification_provenance.authority_sha256",
            ],
            name="fk_skill_build_terminal_certification",
        ),
        CheckConstraint(
            "build_authority_sha256 ~ '^[a-f0-9]{64}$' "
            "AND command_authority_sha256 ~ '^[a-f0-9]{64}$' "
            "AND workflow_job_sha256 ~ '^[a-f0-9]{64}$' "
            "AND terminal_receipt_authority_sha256 ~ '^[a-f0-9]{64}$' "
            "AND authority_sha256 ~ '^[a-f0-9]{64}$'",
            name="ck_skill_build_terminal_hashes",
        ),
        CheckConstraint(
            "(terminal_status = 'REJECTED' AND certification_id IS NULL "
            "AND certification_authority_sha256 IS NULL) OR "
            "(terminal_status = 'CERTIFIED' AND certification_id IS NOT NULL "
            "AND certification_authority_sha256 ~ '^[a-f0-9]{64}$')",
            name="ck_skill_build_terminal_status",
        ),
    )

    build_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(96), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    build_authority_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    terminal_status: Mapped[str] = mapped_column(String(32), nullable=False)
    command_id: Mapped[str] = mapped_column(String(128), nullable=False)
    command_authority_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    workflow_job_id: Mapped[str] = mapped_column(String(128), nullable=False)
    workflow_job_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    terminal_receipt_id: Mapped[str] = mapped_column(String(128), nullable=False)
    terminal_receipt_authority_sha256: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    certification_id: Mapped[str | None] = mapped_column(String(128))
    certification_authority_sha256: Mapped[str | None] = mapped_column(String(64))
    authority_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SkillActivationProvenanceRow(Base):
    """Sealed internal Certification/Artifact/Build authority for one Activation."""

    __tablename__ = "skill_activation_provenance"
    __table_args__ = (
        UniqueConstraint(
            "activation_id",
            "authority_sha256",
            name="uq_skill_activation_provenance_authority",
        ),
        ForeignKeyConstraint(
            ["certification_id", "certification_authority_sha256"],
            [
                "skill_certification_provenance.certification_id",
                "skill_certification_provenance.authority_sha256",
            ],
            name="fk_skill_activation_provenance_certification",
        ),
        ForeignKeyConstraint(
            ["activation_id"],
            ["skill_activations.activation_id"],
            name="fk_skill_activation_provenance_activation",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "launch_authority_id"],
            ["launch_authorities.tenant_id", "launch_authorities.authority_id"],
            name="fk_skill_activation_provenance_launch_authority",
        ),
        ForeignKeyConstraint(
            ["workflow_job_id"],
            ["workflow_jobs.job_id"],
            name="fk_skill_activation_provenance_workflow_job",
        ),
        ForeignKeyConstraint(
            ["activation_receipt_id"],
            ["job_step_receipts.receipt_id"],
            name="fk_skill_activation_provenance_receipt",
        ),
        ForeignKeyConstraint(
            ["build_id", "build_authority_sha256"],
            [
                "skill_build_provenance.build_id",
                "skill_build_provenance.authority_sha256",
            ],
            name="fk_skill_activation_provenance_build",
        ),
        CheckConstraint(
            "activation_sha256 ~ '^[a-f0-9]{64}$' "
            "AND certification_sha256 ~ '^[a-f0-9]{64}$' "
            "AND certification_authority_sha256 ~ '^[a-f0-9]{64}$' "
            "AND artifact_sha256 ~ '^[a-f0-9]{64}$' "
            "AND artifact_authority_sha256 ~ '^[a-f0-9]{64}$' "
            "AND build_authority_sha256 ~ '^[a-f0-9]{64}$' "
            "AND entry_sha256 ~ '^[a-f0-9]{64}$' "
            "AND workflow_request_sha256 ~ '^[a-f0-9]{64}$' "
            "AND workflow_job_sha256 ~ '^[a-f0-9]{64}$' "
            "AND activation_receipt_sha256 ~ '^[a-f0-9]{64}$' "
            "AND authority_sha256 ~ '^[a-f0-9]{64}$' "
            "AND registry_revision >= 1",
            name="ck_skill_activation_provenance_authority",
        ),
    )

    activation_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(96), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    build_id: Mapped[str] = mapped_column(String(128), nullable=False)
    build_authority_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    certification_id: Mapped[str] = mapped_column(String(128), nullable=False)
    certification_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    certification_authority_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    artifact_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    artifact_authority_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    registry_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    activation_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    launch_authority_id: Mapped[str] = mapped_column(String(128), nullable=False)
    entry_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    workflow_job_id: Mapped[str] = mapped_column(String(128), nullable=False)
    workflow_request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    workflow_job_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    activation_receipt_id: Mapped[str] = mapped_column(String(128), nullable=False)
    activation_receipt_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    authority_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SkillRunProvenanceRow(Base):
    """Internal immutable Run-to-Build provenance used by Patch and Learner."""

    __tablename__ = "skill_run_provenance"
    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id"],
            ["game_runs.run_id"],
            name="fk_skill_run_provenance_run",
        ),
        ForeignKeyConstraint(
            ["build_id", "build_authority_sha256"],
            [
                "skill_build_provenance.build_id",
                "skill_build_provenance.authority_sha256",
            ],
            name="fk_skill_run_provenance_build",
        ),
        ForeignKeyConstraint(
            ["draft_revision_row_id"],
            ["product_skill_draft_revisions.draft_revision_row_id"],
            name="fk_skill_run_provenance_draft",
        ),
        ForeignKeyConstraint(
            ["activation_id", "activation_authority_sha256"],
            [
                "skill_activation_provenance.activation_id",
                "skill_activation_provenance.authority_sha256",
            ],
            name="fk_skill_run_provenance_activation",
        ),
        CheckConstraint(
            "build_authority_sha256 ~ '^[a-f0-9]{64}$' "
            "AND certification_sha256 ~ '^[a-f0-9]{64}$' "
            "AND certification_authority_sha256 ~ '^[a-f0-9]{64}$' "
            "AND artifact_sha256 ~ '^[a-f0-9]{64}$' "
            "AND artifact_authority_sha256 ~ '^[a-f0-9]{64}$' "
            "AND authority_sha256 ~ '^[a-f0-9]{64}$' AND "
            "((provenance_kind = 'LEGACY_V04' AND draft_revision_row_id IS NULL "
            "AND draft_sha256 IS NULL AND assistance_authority = 'NONE' AND "
            "activation_id IS NULL AND activation_sha256 IS NULL "
            "AND activation_authority_sha256 IS NULL "
            "AND registry_revision IS NULL AND certification_id IS NOT NULL) OR "
            "(provenance_kind = 'LEGACY_V04_ACTIVE' "
            "AND draft_revision_row_id IS NULL AND draft_sha256 IS NULL "
            "AND assistance_authority = 'NONE' AND activation_id IS NOT NULL "
            "AND activation_sha256 ~ '^[a-f0-9]{64}$' "
            "AND activation_authority_sha256 ~ '^[a-f0-9]{64}$' "
            "AND registry_revision >= 1 AND certification_id IS NOT NULL) OR "
            "(provenance_kind = 'IMMUTABLE_DRAFT' "
            "AND draft_revision_row_id IS NOT NULL "
            "AND draft_sha256 ~ '^[a-f0-9]{64}$' "
            "AND activation_id IS NOT NULL "
            "AND activation_sha256 ~ '^[a-f0-9]{64}$' "
            "AND activation_authority_sha256 ~ '^[a-f0-9]{64}$' "
            "AND registry_revision >= 1 AND certification_id IS NOT NULL "
            "AND assistance_authority IN ('NONE','SKILL_PATCH')))",
            name="ck_skill_run_provenance_authority",
        ),
    )

    run_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    build_id: Mapped[str] = mapped_column(String(128), nullable=False)
    provenance_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    build_authority_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(96), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    session_id: Mapped[str] = mapped_column(String(128), nullable=False)
    activation_id: Mapped[str | None] = mapped_column(String(128))
    activation_sha256: Mapped[str | None] = mapped_column(String(64))
    activation_authority_sha256: Mapped[str | None] = mapped_column(String(64))
    registry_revision: Mapped[int | None] = mapped_column(BigInteger)
    certification_id: Mapped[str] = mapped_column(String(128), nullable=False)
    certification_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    certification_authority_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    artifact_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    artifact_authority_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    draft_revision_row_id: Mapped[int | None] = mapped_column(BigInteger)
    draft_sha256: Mapped[str | None] = mapped_column(String(64))
    assistance_authority: Mapped[str] = mapped_column(String(32), nullable=False)
    authority_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ProductInteractionRow(Base):
    """Immutable Product-facing Agent interaction projection per session sequence."""

    __tablename__ = "product_agent_interactions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "session_id", "interaction_id", name="uq_product_interaction_identity"
        ),
        UniqueConstraint(
            "tenant_id", "session_id", "sequence", name="uq_product_interaction_sequence"
        ),
        Index(
            "ix_product_interactions_authority", "tenant_id", "actor_id", "session_id", "sequence"
        ),
    )

    interaction_row_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(96), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    session_id: Mapped[str] = mapped_column(String(128), nullable=False)
    interaction_id: Mapped[str] = mapped_column(String(128), nullable=False)
    turn_id: Mapped[str] = mapped_column(String(128), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    interaction_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    interaction_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class ProductIdempotencyReceiptRow(Base):
    __tablename__ = "product_idempotency_receipts"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "actor_id",
            "operation",
            "canonical_path",
            "idempotency_key",
            name="uq_product_idempotency_scope",
        ),
    )

    receipt_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(96), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    operation: Mapped[str] = mapped_column(String(128), nullable=False)
    canonical_path: Mapped[str] = mapped_column(String(512), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(128), nullable=False)
    http_status: Mapped[int] = mapped_column(Integer, nullable=False)
    original_trace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ProductPatchDecisionReceiptRow(Base):
    """Idempotency receipt retaining the immutable PatchDecision response."""

    __tablename__ = "product_patch_decision_receipts"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "actor_id",
            "canonical_path",
            "idempotency_key",
            name="uq_product_patch_decision_idempotency",
        ),
    )

    receipt_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(96), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    canonical_path: Mapped[str] = mapped_column(String(512), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    decision_id: Mapped[str | None] = mapped_column(
        String(128),
        ForeignKey(
            "product_skill_patch_decisions.decision_id",
            name="fk_product_patch_receipt_decision",
        ),
    )
    patch_id: Mapped[str | None] = mapped_column(
        String(128),
        ForeignKey(
            "product_skill_patch_proposals.patch_id",
            name="fk_product_patch_receipt_proposal",
        ),
    )
    draft_revision_row_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey(
            "product_skill_draft_revisions.draft_revision_row_id",
            name="fk_product_patch_receipt_draft_revision",
        ),
    )
    interaction_id: Mapped[str] = mapped_column(String(128), nullable=False)
    interaction_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    receipt_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class LearnerProfileRow(Base):
    """Published learner authority used by the public student launch closure."""

    __tablename__ = "learner_profiles"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "learner_id",
            "actor_id",
            "content_hash",
            name="uq_learner_profile_authority",
        ),
        CheckConstraint("content_hash ~ '^[a-f0-9]{64}$'", name="ck_learner_profile_content_hash"),
        CheckConstraint("profile_sha256 ~ '^[a-f0-9]{64}$'", name="ck_learner_profile_sha256"),
    )

    tenant_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    learner_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    profile_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    profile_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AgentProfileRow(Base):
    """Published Agent profile pinned by a launch authority."""

    __tablename__ = "agent_profiles"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "agent_profile_id",
            "actor_id",
            "content_hash",
            name="uq_agent_profile_authority",
        ),
        CheckConstraint("content_hash ~ '^[a-f0-9]{64}$'", name="ck_agent_profile_content_hash"),
        CheckConstraint("profile_sha256 ~ '^[a-f0-9]{64}$'", name="ck_agent_profile_sha256"),
    )

    tenant_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    agent_profile_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    profile_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    profile_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class BuildPolicyRow(Base):
    """Server-owned immutable compiler, test, capability, and source limits."""

    __tablename__ = "build_policies"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "build_policy_id",
            "actor_id",
            "content_hash",
            name="uq_build_policy_authority",
        ),
        Index(
            "uq_build_policy_active_scope",
            "tenant_id",
            "actor_id",
            "content_hash",
            unique=True,
            postgresql_where=text("active IS TRUE"),
        ),
        CheckConstraint("content_hash ~ '^[a-f0-9]{64}$'", name="ck_build_policy_content_hash"),
        CheckConstraint("policy_sha256 ~ '^[a-f0-9]{64}$'", name="ck_build_policy_sha256"),
        CheckConstraint(
            "sandbox_image_digest ~ '^sha256:[a-f0-9]{64}$'",
            name="ck_build_policy_sandbox_digest",
        ),
        CheckConstraint("max_source_files > 0", name="ck_build_policy_max_source_files"),
        CheckConstraint("max_source_bytes > 0", name="ck_build_policy_max_source_bytes"),
    )

    tenant_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    build_policy_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    compiler_profile: Mapped[str] = mapped_column(String(128), nullable=False)
    compiler_version: Mapped[str] = mapped_column(String(128), nullable=False)
    sandbox_image_digest: Mapped[str] = mapped_column(String(512), nullable=False)
    test_suite_version: Mapped[str] = mapped_column(String(128), nullable=False)
    allowed_capabilities: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    max_source_files: Mapped[int] = mapped_column(Integer, nullable=False)
    max_source_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    policy_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    policy_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    active: Mapped[bool] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class LaunchAuthorityRow(Base):
    """Exactly one active launch closure selected explicitly for an authenticated actor."""

    __tablename__ = "launch_authorities"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "authority_id",
            "actor_id",
            "content_hash",
            "world_id",
            "learner_id",
            "agent_profile_id",
            name="uq_launch_authority_closure",
        ),
        UniqueConstraint(
            "tenant_id",
            "authority_id",
            "actor_id",
            "content_hash",
            "world_id",
            "agent_profile_id",
            name="uq_launch_authority_registry_scope",
        ),
        Index(
            "uq_launch_authority_active_actor",
            "tenant_id",
            "actor_id",
            unique=True,
            postgresql_where=text("active IS TRUE"),
        ),
        Index(
            "ix_launch_authority_resolution",
            "tenant_id",
            "actor_id",
            "content_hash",
            "world_id",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "content_unit_id", "content_version", "content_hash"],
            [
                "product_content_units.tenant_id",
                "product_content_units.unit_id",
                "product_content_units.version",
                "product_content_units.content_hash",
            ],
            name="fk_launch_authority_content",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "world_id", "actor_id", "content_hash"],
            [
                "world_snapshots.tenant_id",
                "world_snapshots.world_id",
                "world_snapshots.actor_id",
                "world_snapshots.content_hash",
            ],
            name="fk_launch_authority_world",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "learner_id", "actor_id", "content_hash"],
            [
                "learner_profiles.tenant_id",
                "learner_profiles.learner_id",
                "learner_profiles.actor_id",
                "learner_profiles.content_hash",
            ],
            name="fk_launch_authority_learner",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "agent_profile_id", "actor_id", "content_hash"],
            [
                "agent_profiles.tenant_id",
                "agent_profiles.agent_profile_id",
                "agent_profiles.actor_id",
                "agent_profiles.content_hash",
            ],
            name="fk_launch_authority_agent_profile",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "build_policy_id", "actor_id", "content_hash"],
            [
                "build_policies.tenant_id",
                "build_policies.build_policy_id",
                "build_policies.actor_id",
                "build_policies.content_hash",
            ],
            name="fk_launch_authority_build_policy",
        ),
        CheckConstraint("content_hash ~ '^[a-f0-9]{64}$'", name="ck_launch_authority_content_hash"),
        CheckConstraint("authority_sha256 ~ '^[a-f0-9]{64}$'", name="ck_launch_authority_sha256"),
        CheckConstraint("channel IN ('GAME')", name="ck_launch_authority_channel"),
    )

    tenant_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    authority_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    content_unit_id: Mapped[str] = mapped_column(String(128), nullable=False)
    content_version: Mapped[str] = mapped_column(String(64), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    world_id: Mapped[str] = mapped_column(String(128), nullable=False)
    learner_id: Mapped[str] = mapped_column(String(128), nullable=False)
    agent_profile_id: Mapped[str] = mapped_column(String(128), nullable=False)
    build_policy_id: Mapped[str] = mapped_column(String(128), nullable=False)
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    teaching_spec_version: Mapped[str] = mapped_column(String(128), nullable=False)
    authority_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    active: Mapped[bool] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class WorkflowJobRow(Base):
    """Backend-owned durable workflow claim with lease and monotonic fencing."""

    __tablename__ = "workflow_jobs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "job_id", name="uq_workflow_job_tenant"),
        UniqueConstraint("tenant_id", "command_id", name="uq_workflow_job_command"),
        Index(
            "ix_workflow_jobs_ready",
            "status",
            "next_attempt_at",
            "lease_expires_at",
        ),
        CheckConstraint(
            "status IN ('ACCEPTED','READY','CLAIMED','RUNNING','RETRY_WAIT',"
            "'WAITING_PROJECTION','SUCCEEDED','FAILED','CANCELLED','DEAD_LETTER')",
            name="ck_workflow_job_status",
        ),
        CheckConstraint("fencing_token >= 0", name="ck_workflow_job_fencing_token"),
        CheckConstraint("attempt >= 0", name="ck_workflow_job_attempt"),
        CheckConstraint(
            "(lease_owner IS NULL AND lease_expires_at IS NULL) OR "
            "(lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name="ck_workflow_job_lease_pair",
        ),
    )

    job_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(96), nullable=False)
    command_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("commands.command_id", name="fk_workflow_job_command"),
        nullable=False,
    )
    operation: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_type: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(128), nullable=False)
    phase: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    job_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    last_error_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class JobStepReceiptRow(Base):
    """Immutable proof that one fenced external workflow step was materialized."""

    __tablename__ = "job_step_receipts"
    __table_args__ = (
        UniqueConstraint("tenant_id", "job_id", "step_name", name="uq_job_step_once"),
        ForeignKeyConstraint(
            ["tenant_id", "job_id"],
            ["workflow_jobs.tenant_id", "workflow_jobs.job_id"],
            name="fk_job_step_workflow",
        ),
        CheckConstraint("fencing_token > 0", name="ck_job_step_fencing_token"),
        CheckConstraint("input_sha256 ~ '^[a-f0-9]{64}$'", name="ck_job_step_input_sha256"),
        CheckConstraint("output_sha256 ~ '^[a-f0-9]{64}$'", name="ck_job_step_output_sha256"),
    )

    receipt_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(96), nullable=False)
    job_id: Mapped[str] = mapped_column(String(128), nullable=False)
    step_name: Mapped[str] = mapped_column(String(64), nullable=False)
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    input_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    output_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    receipt_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SkillArtifactRow(Base):
    """Immutable metadata for one content-addressed Build artifact."""

    __tablename__ = "skill_artifacts"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "artifact_sha256",
            "build_id",
            "actor_id",
            "content_hash",
            name="uq_skill_artifact_closure",
        ),
        CheckConstraint("artifact_sha256 ~ '^[a-f0-9]{64}$'", name="ck_skill_artifact_sha256"),
        CheckConstraint("source_sha256 ~ '^[a-f0-9]{64}$'", name="ck_skill_artifact_source_sha256"),
    )

    tenant_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    artifact_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    # build_id is part of the identity: the artifact is content-addressed, so a
    # learner who writes the same correct code twice rebuilds to the same sha,
    # and both Builds must be able to record the row that certifies them.
    build_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("skill_builds.build_id", name="fk_skill_artifact_build"),
        nullable=False,
        primary_key=True,
    )
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    skill_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    artifact_uri: Mapped[str] = mapped_column(String(1024), nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SkillCertificationRow(Base):
    """Immutable certification closure for one exact SkillVersion tuple."""

    __tablename__ = "skill_certifications"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "certification_id",
            "skill_id",
            "skill_version_id",
            "artifact_sha256",
            "actor_id",
            "content_hash",
            name="uq_skill_certification_closure",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "artifact_sha256", "build_id", "actor_id", "content_hash"],
            [
                "skill_artifacts.tenant_id",
                "skill_artifacts.artifact_sha256",
                "skill_artifacts.build_id",
                "skill_artifacts.actor_id",
                "skill_artifacts.content_hash",
            ],
            name="fk_skill_certification_artifact",
        ),
        CheckConstraint(
            "certification_sha256 ~ '^[a-f0-9]{64}$'",
            name="ck_skill_certification_sha256",
        ),
    )

    certification_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(96), nullable=False)
    build_id: Mapped[str] = mapped_column(String(128), nullable=False)
    skill_id: Mapped[str] = mapped_column(String(128), nullable=False)
    skill_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    artifact_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    certification_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    certification_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    certified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SkillCertificationRevocationRow(Base):
    """Separate append-only revocation so certification bytes remain immutable."""

    __tablename__ = "skill_certification_revocations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "certification_id", name="uq_certification_revocation"),
        CheckConstraint(
            "revocation_sha256 ~ '^[a-f0-9]{64}$'", name="ck_certification_revocation_sha256"
        ),
    )

    revocation_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(96), nullable=False)
    certification_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("skill_certifications.certification_id", name="fk_revocation_certification"),
        nullable=False,
    )
    revocation_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    revocation_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    revoked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CurrentSessionBindingRow(Base):
    """Explicit current Session selected for one launch authority; never inferred by latest time."""

    __tablename__ = "current_session_bindings"
    __table_args__ = (
        UniqueConstraint("tenant_id", "authority_id", name="uq_current_session_authority"),
        UniqueConstraint("tenant_id", "session_id", name="uq_current_session_identity"),
        ForeignKeyConstraint(
            [
                "tenant_id",
                "authority_id",
                "actor_id",
                "content_hash",
                "world_id",
                "learner_id",
                "agent_profile_id",
            ],
            [
                "launch_authorities.tenant_id",
                "launch_authorities.authority_id",
                "launch_authorities.actor_id",
                "launch_authorities.content_hash",
                "launch_authorities.world_id",
                "launch_authorities.learner_id",
                "launch_authorities.agent_profile_id",
            ],
            name="fk_current_session_launch_authority",
        ),
    )

    binding_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(96), nullable=False)
    authority_id: Mapped[str] = mapped_column(String(128), nullable=False)
    session_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("agent_sessions.session_id", name="fk_current_session_resource"),
        nullable=False,
    )
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    world_id: Mapped[str] = mapped_column(String(128), nullable=False)
    learner_id: Mapped[str] = mapped_column(String(128), nullable=False)
    agent_profile_id: Mapped[str] = mapped_column(String(128), nullable=False)
    bound_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RegistryHeadRow(Base):
    """Full-scope CAS head; revision zero is itself durable authority."""

    __tablename__ = "registry_heads"
    __table_args__ = (
        ForeignKeyConstraint(
            [
                "tenant_id",
                "authority_id",
                "actor_id",
                "content_hash",
                "world_id",
                "agent_profile_id",
            ],
            [
                "launch_authorities.tenant_id",
                "launch_authorities.authority_id",
                "launch_authorities.actor_id",
                "launch_authorities.content_hash",
                "launch_authorities.world_id",
                "launch_authorities.agent_profile_id",
            ],
            name="fk_registry_head_launch_authority",
        ),
        CheckConstraint("revision >= 0", name="ck_registry_head_revision"),
        CheckConstraint("content_hash ~ '^[a-f0-9]{64}$'", name="ck_registry_head_content_hash"),
    )

    tenant_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    actor_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    content_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    world_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    agent_profile_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    authority_id: Mapped[str] = mapped_column(String(128), nullable=False)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RegistryEntryRow(Base):
    """Immutable full-scope registry history entry."""

    __tablename__ = "registry_entries"
    __table_args__ = (
        ForeignKeyConstraint(
            [
                "tenant_id",
                "actor_id",
                "content_hash",
                "world_id",
                "agent_profile_id",
            ],
            [
                "registry_heads.tenant_id",
                "registry_heads.actor_id",
                "registry_heads.content_hash",
                "registry_heads.world_id",
                "registry_heads.agent_profile_id",
            ],
            name="fk_registry_entry_head",
        ),
        UniqueConstraint(
            "tenant_id",
            "actor_id",
            "content_hash",
            "world_id",
            "agent_profile_id",
            "revision",
            "skill_id",
            "skill_version_id",
            "certification_id",
            "artifact_sha256",
            name="uq_registry_entry_activation_closure",
        ),
        ForeignKeyConstraint(
            [
                "tenant_id",
                "certification_id",
                "skill_id",
                "skill_version_id",
                "artifact_sha256",
                "actor_id",
                "content_hash",
            ],
            [
                "skill_certifications.tenant_id",
                "skill_certifications.certification_id",
                "skill_certifications.skill_id",
                "skill_certifications.skill_version_id",
                "skill_certifications.artifact_sha256",
                "skill_certifications.actor_id",
                "skill_certifications.content_hash",
            ],
            name="fk_registry_entry_certification",
        ),
        CheckConstraint("revision >= 1", name="ck_registry_entry_revision"),
        CheckConstraint("previous_revision >= 0", name="ck_registry_entry_previous_revision"),
        CheckConstraint("revision = previous_revision + 1", name="ck_registry_entry_chain"),
        CheckConstraint("entry_sha256 ~ '^[a-f0-9]{64}$'", name="ck_registry_entry_sha256"),
    )

    tenant_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    actor_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    content_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    world_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    agent_profile_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    revision: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    skill_id: Mapped[str] = mapped_column(String(128), nullable=False)
    skill_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    certification_id: Mapped[str] = mapped_column(String(128), nullable=False)
    artifact_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    previous_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    entry_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    entry_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    activated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SkillActivationRow(Base):
    """Immutable public activation resource linked to the exact registry entry."""

    __tablename__ = "skill_activations"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "actor_id",
            "content_hash",
            "world_id",
            "agent_profile_id",
            "skill_id",
            "registry_revision",
            name="uq_skill_activation_registry_revision",
        ),
        ForeignKeyConstraint(
            [
                "tenant_id",
                "actor_id",
                "content_hash",
                "world_id",
                "agent_profile_id",
                "registry_revision",
                "skill_id",
                "skill_version_id",
                "certification_id",
                "artifact_sha256",
            ],
            [
                "registry_entries.tenant_id",
                "registry_entries.actor_id",
                "registry_entries.content_hash",
                "registry_entries.world_id",
                "registry_entries.agent_profile_id",
                "registry_entries.revision",
                "registry_entries.skill_id",
                "registry_entries.skill_version_id",
                "registry_entries.certification_id",
                "registry_entries.artifact_sha256",
            ],
            name="fk_skill_activation_registry_entry",
        ),
        CheckConstraint("previous_registry_revision >= 0", name="ck_activation_previous_revision"),
        CheckConstraint("registry_revision >= 1", name="ck_activation_registry_revision"),
        CheckConstraint(
            "registry_revision = previous_registry_revision + 1", name="ck_activation_chain"
        ),
        CheckConstraint("activation_sha256 ~ '^[a-f0-9]{64}$'", name="ck_activation_sha256"),
    )

    activation_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(96), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    world_id: Mapped[str] = mapped_column(String(128), nullable=False)
    agent_profile_id: Mapped[str] = mapped_column(String(128), nullable=False)
    skill_id: Mapped[str] = mapped_column(String(128), nullable=False)
    skill_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    certification_id: Mapped[str] = mapped_column(String(128), nullable=False)
    artifact_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    previous_registry_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    registry_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    activation_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    activation_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    activated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class LearnerProjectionJobRow(Base):
    """Separately claimed learner/Product projection bound to one Turn hand-off."""

    __tablename__ = "learner_projection_jobs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "job_id", name="uq_learner_projection_job_tenant"),
        UniqueConstraint("tenant_id", "command_id", name="uq_learner_projection_command"),
        UniqueConstraint("tenant_id", "session_id", "turn_id", name="uq_learner_projection_turn"),
        UniqueConstraint("tenant_id", "run_id", name="uq_learner_projection_run"),
        UniqueConstraint(
            "tenant_id",
            "learner_id",
            "source_event_id",
            name="uq_learner_projection_source",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "job_id"],
            ["workflow_jobs.tenant_id", "workflow_jobs.job_id"],
            name="fk_learner_projection_workflow",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "learner_id", "actor_id", "content_hash"],
            [
                "learner_profiles.tenant_id",
                "learner_profiles.learner_id",
                "learner_profiles.actor_id",
                "learner_profiles.content_hash",
            ],
            name="fk_learner_projection_profile",
        ),
        UniqueConstraint(
            "tenant_id",
            "learner_id",
            "actor_id",
            "content_hash",
            "expected_revision",
            name="uq_learner_projection_revision",
        ),
        UniqueConstraint(
            "tenant_id",
            "learner_id",
            "actor_id",
            "content_hash",
            "through_sequence",
            name="uq_learner_projection_sequence",
        ),
        Index(
            "ix_learner_projection_jobs_ready",
            "status",
            "next_attempt_at",
            "lease_expires_at",
            "created_at",
        ),
        CheckConstraint(
            "status IN ('READY','CLAIMED','RUNNING','RETRY_WAIT','SUCCEEDED','DEAD_LETTER')",
            name="ck_learner_projection_status",
        ),
        CheckConstraint("attempt >= 0", name="ck_learner_projection_attempt"),
        CheckConstraint("fencing_token >= 0", name="ck_learner_projection_fencing_token"),
        CheckConstraint(
            "request_sha256 ~ '^[a-f0-9]{64}$'",
            name="ck_learner_projection_request_sha256",
        ),
        CheckConstraint(
            "result_sha256 IS NULL OR result_sha256 ~ '^[a-f0-9]{64}$'",
            name="ck_learner_projection_result_sha256",
        ),
        CheckConstraint(
            "((status IN ('CLAIMED','RUNNING')) = "
            "(lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL))",
            name="ck_learner_projection_lease_state",
        ),
        CheckConstraint(
            "((status IN ('READY','RETRY_WAIT')) = (next_attempt_at IS NOT NULL))",
            name="ck_learner_projection_next_attempt",
        ),
        CheckConstraint(
            "(status = 'SUCCEEDED' AND result_sha256 IS NOT NULL AND result_json IS NOT NULL "
            "AND completed_at IS NOT NULL) OR "
            "(status = 'DEAD_LETTER' AND result_sha256 IS NULL AND result_json IS NULL "
            "AND completed_at IS NOT NULL) OR "
            "(status NOT IN ('SUCCEEDED','DEAD_LETTER') AND result_sha256 IS NULL "
            "AND result_json IS NULL AND completed_at IS NULL)",
            name="ck_learner_projection_terminal_payload",
        ),
        CheckConstraint("expected_revision >= 0", name="ck_learner_projection_revision"),
        CheckConstraint("through_sequence >= 0", name="ck_learner_projection_sequence"),
    )

    job_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(96), nullable=False)
    command_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("commands.command_id", name="fk_learner_projection_command"),
        nullable=False,
    )
    session_id: Mapped[str] = mapped_column(String(128), nullable=False)
    turn_id: Mapped[str] = mapped_column(String(128), nullable=False)
    run_id: Mapped[str] = mapped_column(String(128), nullable=False)
    learner_id: Mapped[str] = mapped_column(String(128), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_event_id: Mapped[str] = mapped_column(
        String(132),
        ForeignKey("domain_events.event_id", name="fk_learner_projection_event"),
        nullable=False,
    )
    expected_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    through_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    projection_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    result_sha256: Mapped[str | None] = mapped_column(String(64))
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    last_error_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RecoverableLlmDispatchRow(Base):
    """One immutable private-relay resource and at most one upstream generation."""

    __tablename__ = "recoverable_llm_dispatches"
    __table_args__ = (
        Index("ix_recoverable_llm_dispatch_ready", "state", "generation_count", "created_at"),
        Index("ix_recoverable_llm_dispatch_expiry", "state", "expires_at"),
        CheckConstraint(
            "state IN ('PENDING','SUCCEEDED','FAILED','EXPIRED')",
            name="ck_recoverable_llm_dispatch_state",
        ),
        CheckConstraint(
            "generation_count IN (0, 1)",
            name="ck_recoverable_llm_generation_count",
        ),
        CheckConstraint(
            "response_http_status IS NULL OR response_http_status BETWEEN 100 AND 599",
            name="ck_recoverable_llm_http_status",
        ),
        CheckConstraint(
            "updated_at >= created_at",
            name="ck_recoverable_llm_updated_order",
        ),
        CheckConstraint(
            "terminal_at IS NULL OR terminal_at >= created_at",
            name="ck_recoverable_llm_terminal_order",
        ),
        CheckConstraint(
            "expires_at IS NULL OR (terminal_at IS NOT NULL AND expires_at >= terminal_at)",
            name="ck_recoverable_llm_expiry_order",
        ),
        CheckConstraint(
            "request_sha256 ~ '^[a-f0-9]{64}$' AND "
            "context_sha256 ~ '^[a-f0-9]{64}$' AND "
            "completion_sha256 ~ '^[a-f0-9]{64}$' AND "
            "request_body_sha256 ~ '^[a-f0-9]{64}$'",
            name="ck_recoverable_llm_request_hashes",
        ),
        CheckConstraint(
            "response_body_sha256 IS NULL OR response_body_sha256 ~ '^[a-f0-9]{64}$'",
            name="ck_recoverable_llm_response_hash",
        ),
        CheckConstraint(
            "(generation_count = 0 AND dispatch_started_at IS NULL "
            "AND upstream_deadline_at IS NULL) OR "
            "(generation_count = 1 AND dispatch_started_at IS NOT NULL "
            "AND upstream_deadline_at IS NOT NULL)",
            name="ck_recoverable_llm_generation_timestamps",
        ),
        CheckConstraint(
            "state <> 'SUCCEEDED' OR "
            "(generation_count = 1 AND response_http_status IS NOT NULL "
            "AND response_content_type IS NOT NULL AND response_body IS NOT NULL "
            "AND response_body_sha256 IS NOT NULL AND failure_code IS NULL "
            "AND failure_retryable IS NULL AND terminal_at IS NOT NULL "
            "AND expires_at IS NOT NULL)",
            name="ck_recoverable_llm_success_shape",
        ),
        CheckConstraint(
            "state <> 'FAILED' OR "
            "(generation_count = 1 AND failure_code IS NOT NULL "
            "AND failure_retryable IS NOT NULL AND response_http_status IS NULL "
            "AND response_content_type IS NULL AND response_body IS NULL "
            "AND response_body_sha256 IS NULL AND terminal_at IS NOT NULL "
            "AND expires_at IS NOT NULL)",
            name="ck_recoverable_llm_failure_shape",
        ),
        CheckConstraint(
            "state <> 'EXPIRED' OR "
            "(generation_count = 1 AND request_body IS NULL AND response_http_status IS NULL "
            "AND response_content_type IS NULL AND response_body IS NULL "
            "AND response_body_sha256 IS NULL AND failure_code IS NULL "
            "AND failure_retryable IS NULL AND terminal_at IS NOT NULL "
            "AND expires_at IS NOT NULL)",
            name="ck_recoverable_llm_expired_shape",
        ),
        CheckConstraint(
            "state <> 'PENDING' OR "
            "(request_body IS NOT NULL AND terminal_at IS NULL AND expires_at IS NULL "
            "AND response_http_status IS NULL AND response_content_type IS NULL "
            "AND response_body IS NULL AND response_body_sha256 IS NULL "
            "AND failure_code IS NULL AND failure_retryable IS NULL)",
            name="ck_recoverable_llm_pending_shape",
        ),
    )

    dispatch_id: Mapped[str] = mapped_column(String(47), primary_key=True)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    context_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    completion_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(128), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    request_body_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    request_body: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    generation_count: Mapped[int] = mapped_column(Integer, nullable=False)
    dispatch_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    upstream_deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    response_http_status: Mapped[int | None] = mapped_column(Integer)
    response_content_type: Mapped[str | None] = mapped_column(String(256))
    response_body_sha256: Mapped[str | None] = mapped_column(String(64))
    response_body: Mapped[bytes | None] = mapped_column(LargeBinary)
    failure_code: Mapped[str | None] = mapped_column(String(96))
    failure_retryable: Mapped[bool | None] = mapped_column()
    terminal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


def json_value(value: Any) -> Any:
    """Convert frozen contract values to JSONB-safe primitives without losing timestamps."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: json_value(getattr(value, field.name))
            for field in fields(value)
            if field.init
        }
    if isinstance(value, Mapping):
        return {key: json_value(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [json_value(item) for item in value]
    return value


def _datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _actor(data: dict[str, Any]) -> Any:
    from yaya_agent_contracts import ActorRef

    return ActorRef(**data)


def _content_ref(data: dict[str, Any]) -> Any:
    from yaya_agent_contracts import ContentRef

    return ContentRef(**data)


def request_context_data(context: Any) -> dict[str, Any]:
    return {
        "request_id": context.request_id,
        "correlation_id": context.correlation_id,
        "trace_id": context.trace_id,
        "requested_at": json_value(context.requested_at),
        "actor": json_value(context.actor),
        "content_ref": json_value(context.content_ref),
        "schema_version": context.schema_version,
    }


def request_context_from_data(data: dict[str, Any]) -> Any:
    from yaya_agent_contracts import RequestContext

    return RequestContext(
        request_id=data["request_id"],
        correlation_id=data["correlation_id"],
        trace_id=data["trace_id"],
        requested_at=_datetime(data["requested_at"]),
        actor=_actor(data["actor"]),
        content_ref=_content_ref(data["content_ref"]),
        schema_version=data["schema_version"],
    )


def operation_context_data(context: Any) -> dict[str, Any]:
    return {
        **request_context_data(context),
        "command_id": context.command_id,
        "causation_id": context.causation_id,
        "deadline_at": json_value(context.deadline_at),
    }


def operation_context_from_data(data: dict[str, Any]) -> Any:
    from yaya_agent_contracts import OperationContext

    return OperationContext(
        request_id=data["request_id"],
        correlation_id=data["correlation_id"],
        trace_id=data["trace_id"],
        requested_at=_datetime(data["requested_at"]),
        actor=_actor(data["actor"]),
        content_ref=_content_ref(data["content_ref"]),
        schema_version=data["schema_version"],
        command_id=data["command_id"],
        causation_id=data["causation_id"],
        deadline_at=_datetime(data["deadline_at"]) if data["deadline_at"] else None,
    )


def world_snapshot_data(snapshot: Any) -> dict[str, Any]:
    return {
        "request_context": request_context_data(snapshot.request_context),
        "world_id": snapshot.world_id,
        "revision": snapshot.revision,
        "last_event_sequence": snapshot.last_event_sequence,
        "state_hash": snapshot.state_hash,
        "generated_at": json_value(snapshot.generated_at),
        "world_rules_version": snapshot.world_rules_version,
        "state": json_value(snapshot.state),
        "state_schema_version": snapshot.state_schema_version,
    }


def world_snapshot_from_data(data: dict[str, Any]) -> Any:
    from yaya_agent_contracts import WorldSnapshot

    return WorldSnapshot(
        request_context=request_context_from_data(data["request_context"]),
        world_id=data["world_id"],
        revision=data["revision"],
        last_event_sequence=data["last_event_sequence"],
        state_hash=data["state_hash"],
        generated_at=_datetime(data["generated_at"]),
        world_rules_version=data["world_rules_version"],
        state=data["state"],
        state_schema_version=data["state_schema_version"],
    )


def domain_event_data(event: Any) -> dict[str, Any]:
    return {
        "event_id": event.event_id,
        "event_type": event.event_type,
        "event_version": event.event_version,
        "stream_id": event.stream_id,
        "sequence": event.sequence,
        "occurred_at": json_value(event.occurred_at),
        "producer": event.producer,
        "trace_id": event.trace_id,
        "command_id": event.command_id,
        "correlation_id": event.correlation_id,
        "causation_id": event.causation_id,
        "content_ref": json_value(event.content_ref),
        "payload": json_value(event.payload),
        "schema_version": event.schema_version,
    }


def public_domain_event_data(event: Any) -> dict[str, Any]:
    """Present one Event with the canonical public UTC timestamp spelling."""

    if event.occurred_at.tzinfo is None:
        raise ValueError("Event.occurred_at must include a timezone")
    value = domain_event_data(event)
    value["occurred_at"] = event.occurred_at.astimezone(UTC).isoformat().replace(
        "+00:00", "Z"
    )
    return value


def domain_event_from_data(data: dict[str, Any]) -> Any:
    from yaya_agent_contracts import ContentRef, DomainEvent

    return DomainEvent(
        event_id=data["event_id"],
        event_type=data["event_type"],
        event_version=data["event_version"],
        stream_id=data["stream_id"],
        sequence=data["sequence"],
        occurred_at=_datetime(data["occurred_at"]),
        producer=data["producer"],
        trace_id=data["trace_id"],
        command_id=data["command_id"],
        correlation_id=data["correlation_id"],
        causation_id=data["causation_id"],
        content_ref=ContentRef(**data["content_ref"]),
        payload=data["payload"],
        schema_version=data["schema_version"],
    )


def error_data(error: Any | None) -> dict[str, Any] | None:
    return json_value(error) if error is not None else None


def error_from_data(data: dict[str, Any] | None) -> Any | None:
    if data is None:
        return None
    from yaya_agent_contracts import ContractError

    return ContractError(**data)


def command_record_data(record: Any) -> dict[str, Any]:
    versions = json_value(record.versions)
    if not isinstance(versions, dict):
        raise TypeError("command versions must serialize as an object")
    return {
        "request_context": request_context_data(record.request_context),
        "command_id": record.command_id,
        "command_type": record.command_type,
        "status": record.status.value,
        "stage": record.stage,
        "terminal": record.terminal,
        "accepted_at": json_value(record.accepted_at),
        "updated_at": json_value(record.updated_at),
        "result": json_value(record.result),
        "error": error_data(record.error),
        "evidence_refs": [_command_evidence_ref_data(item) for item in record.evidence_refs],
        "versions": {key: value for key, value in versions.items() if value is not None},
        "links": json_value(record.links),
        "revision": record.revision,
    }


def _command_evidence_ref_data(reference: Any) -> dict[str, Any]:
    value = {
        "evidence_id": reference.evidence_id,
        "evidence_type": reference.evidence_type.value,
        "created_at": json_value(reference.created_at),
    }
    if reference.sha256 is not None:
        value["sha256"] = reference.sha256
    if reference.uri is not None:
        value["uri"] = reference.uri
    return value


def command_record_from_data(data: dict[str, Any]) -> Any:
    from yaya_agent_contracts import CommandRecord, EvidenceRef, EvidenceType, VersionSet

    return CommandRecord(
        request_context=request_context_from_data(data["request_context"]),
        command_id=data["command_id"],
        command_type=data["command_type"],
        status=data["status"],
        stage=data["stage"],
        terminal=data["terminal"],
        accepted_at=_datetime(data["accepted_at"]),
        updated_at=_datetime(data["updated_at"]),
        result=data["result"],
        error=error_from_data(data["error"]),
        evidence_refs=tuple(
            EvidenceRef(
                evidence_id=item["evidence_id"],
                evidence_type=EvidenceType(item["evidence_type"]),
                created_at=_datetime(item["created_at"]),
                sha256=item.get("sha256"),
                uri=item.get("uri"),
            )
            for item in data["evidence_refs"]
        ),
        versions=VersionSet(**data["versions"]),
        links=data["links"],
        revision=data["revision"],
    )


def outbox_message_data(message: Any) -> dict[str, Any]:
    return {
        "message_id": message.message_id,
        "destination": message.destination,
        "idempotency_key": message.idempotency_key,
        "payload": json_value(message.payload),
        "created_at": json_value(message.created_at),
        "operation_context": operation_context_data(message.operation_context),
        "status": message.status.value,
        "attempt": message.attempt,
        "next_attempt_at": json_value(message.next_attempt_at),
        "lease_id": message.lease_id,
        "lease_expires_at": json_value(message.lease_expires_at),
        "last_error": error_data(message.last_error),
        "delivery_receipt": json_value(message.delivery_receipt),
        "dead_lettered_at": json_value(message.dead_lettered_at),
    }


def outbox_message_from_data(data: dict[str, Any]) -> Any:
    from yaya_agent_contracts import (
        DeliveryPayload,
        DeliveryReceipt,
        FeishuReportDraftBody,
        OutboxMessage,
    )

    payload = data["payload"]
    return OutboxMessage(
        message_id=data["message_id"],
        destination=data["destination"],
        idempotency_key=data["idempotency_key"],
        payload=DeliveryPayload(
            delivery_id=payload["delivery_id"],
            operation=payload["operation"],
            deduplication_key=payload["deduplication_key"],
            attempt=payload["attempt"],
            body=FeishuReportDraftBody(**payload["body"]),
        ),
        created_at=_datetime(data["created_at"]),
        operation_context=operation_context_from_data(data["operation_context"]),
        status=data["status"],
        attempt=data["attempt"],
        next_attempt_at=_datetime(data["next_attempt_at"]) if data["next_attempt_at"] else None,
        lease_id=data["lease_id"],
        lease_expires_at=_datetime(data["lease_expires_at"]) if data["lease_expires_at"] else None,
        last_error=error_from_data(data["last_error"]),
        delivery_receipt=(
            DeliveryReceipt(
                **{
                    **data["delivery_receipt"],
                    "sent_at": _datetime(data["delivery_receipt"]["sent_at"]),
                }
            )
            if data["delivery_receipt"]
            else None
        ),
        dead_lettered_at=_datetime(data["dead_lettered_at"]) if data["dead_lettered_at"] else None,
    )
