"""Provider-neutral async ports implemented by independently replaceable adapters."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal, Protocol

from .models import (
    ActivateSkillInput,
    ActiveSkill,
    AuditQuery,
    AuditRecord,
    CertificationEvidence,
    CertifiedSkill,
    CommandCreateReceipt,
    CommandRecord,
    CommandTransition,
    CommandType,
    CompileAndTestRequest,
    ContractError,
    CursorPage,
    DeliveryPayload,
    DeliveryReceipt,
    DomainEvent,
    EventAppendReceipt,
    LearnerModelSnapshot,
    LearnerUpdate,
    LlmReply,
    LlmRequest,
    NewCommand,
    OperationContext,
    OutboxMessage,
    PolicyGrant,
    PolicyInput,
    RegistrySnapshot,
    Result,
    RuntimeEvent,
    SandboxRunRequest,
    SandboxRunResult,
    SkillRef,
    UncommittedEvent,
    WorldAtomicCommit,
    WorldAtomicCommitReceipt,
    WorldSnapshot,
)

type ExpectedStreamSequence = int | Literal["NO_STREAM"]


class AuditPort(Protocol):
    """Append-only redacted access audit boundary scoped by context.actor.tenant_id."""

    async def append(
        self,
        record: AuditRecord,
        context: OperationContext,
    ) -> Result[AuditRecord]: ...

    async def query(
        self,
        query: AuditQuery,
        context: OperationContext,
    ) -> Result[CursorPage[AuditRecord]]: ...


class PolicyPort(Protocol):
    async def authorize(
        self,
        request: PolicyInput,
        context: OperationContext,
    ) -> Result[PolicyGrant]: ...


class RegistryPort(Protocol):
    async def certify(
        self,
        evidence: CertificationEvidence,
        context: OperationContext,
    ) -> Result[CertifiedSkill]: ...

    async def reject_certification(
        self,
        evidence: CertificationEvidence,
        reason: ContractError,
        context: OperationContext,
    ) -> Result[None]: ...

    async def get_certified_version(
        self,
        ref: SkillRef,
        context: OperationContext,
    ) -> Result[CertifiedSkill]: ...

    async def get_active_skill(
        self,
        skill_id: str,
        context: OperationContext,
    ) -> Result[ActiveSkill]: ...

    async def activate(
        self,
        request: ActivateSkillInput,
        context: OperationContext,
    ) -> Result[ActiveSkill]: ...

    async def snapshot(self, context: OperationContext) -> Result[RegistrySnapshot]: ...


# Backwards-compatible name; new code should depend on the capability-oriented name.
SkillRegistryPort = RegistryPort


class SandboxPort(Protocol):
    async def compile_and_test(
        self,
        request: CompileAndTestRequest,
        context: OperationContext,
    ) -> Result[CertificationEvidence]: ...

    async def run(
        self,
        request: SandboxRunRequest,
        context: OperationContext,
    ) -> Result[SandboxRunResult]: ...

    async def cancel(
        self,
        run_id: str,
        reason_code: str,
        context: OperationContext,
    ) -> Result[None]: ...


class WorldPort(Protocol):
    """Read-only world repository. World writes go only through WorldUnitOfWorkPort."""

    async def get_snapshot(
        self,
        world_id: str,
        context: OperationContext,
    ) -> Result[WorldSnapshot]: ...


class WorldUnitOfWorkPort(Protocol):
    """Atomically persist state/events/outbox; receipt.stream_id must equal request.stream_id."""

    async def commit(
        self,
        request: WorldAtomicCommit,
        context: OperationContext,
    ) -> Result[WorldAtomicCommitReceipt]: ...


class EventStorePort(Protocol):
    async def append(
        self,
        stream_id: str,
        expected_sequence: ExpectedStreamSequence,
        events: tuple[UncommittedEvent, ...],
        context: OperationContext,
    ) -> Result[EventAppendReceipt]: ...

    async def read_stream(
        self,
        stream_id: str,
        after_sequence: int,
        limit: int,
        context: OperationContext,
    ) -> Result[CursorPage[DomainEvent[Mapping[str, Any]]]]: ...

    async def get_by_id(
        self,
        event_id: str,
        context: OperationContext,
    ) -> Result[DomainEvent[Mapping[str, Any]]]: ...


class LearnerPort(Protocol):
    async def project(
        self,
        event: RuntimeEvent,
        expected_learner_revision: int,
        context: OperationContext,
    ) -> Result[LearnerUpdate]: ...

    async def get_snapshot(
        self,
        learner_id: str,
        context: OperationContext,
    ) -> Result[LearnerModelSnapshot]: ...

    async def rebuild(
        self,
        learner_id: str,
        through_sequence: int,
        context: OperationContext,
    ) -> Result[LearnerModelSnapshot]: ...


class LlmPort(Protocol):
    async def generate(
        self,
        request: LlmRequest,
        context: OperationContext,
    ) -> Result[LlmReply]:
        """Return success only after output validates against request.output_schema."""

        ...


class DeliveryPort(Protocol):
    """External delivery capability; vendor-specific SDKs remain in adapters."""

    async def deliver(
        self,
        payload: DeliveryPayload,
        context: OperationContext,
    ) -> Result[DeliveryReceipt]: ...


# Compatibility alias for existing imports. It intentionally exposes no vendor SDK types.
FeishuPort = DeliveryPort


class CommandStorePort(Protocol):
    """Atomic command persistence scoped by tenant, actor, operation and key."""

    async def get(
        self,
        command_id: str,
        context: OperationContext,
    ) -> Result[CommandRecord]: ...

    async def get_by_idempotency_key(
        self,
        operation: CommandType,
        idempotency_key: str,
        context: OperationContext,
    ) -> Result[CommandRecord]:
        """Look up within ``(tenant_id, actor_id, operation, key)`` from context."""

        ...

    async def accept_once(
        self,
        command: NewCommand,
        context: OperationContext,
    ) -> Result[CommandCreateReceipt]:
        """Atomically create or replay one command.

        The adapter keys by ``command.idempotency_scope(context)``.  An existing
        equal ``request_sha256`` returns its original record with ``created=False``;
        a different hash returns ``IDEMPOTENCY_KEY_REUSED`` and creates nothing.
        """

        ...

    async def transition(
        self,
        transition: CommandTransition,
        context: OperationContext,
    ) -> Result[CommandRecord]:
        """CAS the previous record's identity/revision/status, then persist next_record."""

        ...

    async def find_non_terminal_before(
        self,
        updated_before: datetime,
        cursor: str | None,
        limit: int,
        context: OperationContext,
    ) -> Result[CursorPage[CommandRecord]]: ...


class OutboxPort(Protocol):
    """Tenant-level service delivery; origin actor is audit data, not dedup scope."""

    async def enqueue(
        self,
        message: OutboxMessage,
        context: OperationContext,
    ) -> Result[OutboxMessage]: ...

    async def claim_ready(
        self,
        worker_id: str,
        limit: int,
        lease_seconds: int,
        context: OperationContext,
    ) -> Result[tuple[OutboxMessage, ...]]: ...

    async def mark_sent(
        self,
        message_id: str,
        lease_id: str,
        receipt: DeliveryReceipt,
        context: OperationContext,
    ) -> Result[OutboxMessage]: ...

    async def mark_retry(
        self,
        message_id: str,
        lease_id: str,
        error: ContractError,
        next_attempt_at: datetime,
        context: OperationContext,
    ) -> Result[OutboxMessage]: ...

    async def mark_dead_letter(
        self,
        message_id: str,
        lease_id: str,
        error: ContractError,
        dead_lettered_at: datetime,
        context: OperationContext,
    ) -> Result[OutboxMessage]: ...


__all__ = [
    "AuditPort",
    "CommandStorePort",
    "DeliveryPort",
    "EventStorePort",
    "ExpectedStreamSequence",
    "FeishuPort",
    "LearnerPort",
    "LlmPort",
    "OutboxPort",
    "PolicyPort",
    "RegistryPort",
    "SandboxPort",
    "SkillRegistryPort",
    "WorldPort",
    "WorldUnitOfWorkPort",
]
