"""Authorized, gap-free reads for Agent interaction projections."""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from yaya_agent_contracts import (
    ActorType,
    CommandRecord,
    CommandStatus,
    ContentRef,
    EvidenceRef,
    Failure,
    FrozenJsonObject,
    OperationContext,
    Result,
    RuntimeEvent,
    RuntimeEventType,
    SkillRef,
    Success,
    canonical_json_sha256,
)
from yaya_agent_runtime import side_effect_execution_id, skill_invocation_request_sha256

from walnut_backend.adapters.postgres.command_store import validated_command_record
from walnut_backend.adapters.postgres.models import (
    domain_event_data,
    public_domain_event_data,
    request_context_data,
)
from walnut_backend.adapters.postgres.product_drafts import (
    _draft_authority_matches,
    append_draft_revision_in_session,
    draft_resource,
)
from walnut_backend.adapters.postgres.product_workspaces import (
    refresh_workspace_in_session,
)
from walnut_backend.adapters.postgres.workflow_jobs import (
    WorkflowInvariantError,
    workflow_receipt_sha256,
    workflow_step_receipt_id,
)
from walnut_backend.application.game.skill_builds import (
    InvalidSkillBuildRequest,
    validate_source_bundle,
)
from walnut_backend.domain.canonical_json import canonical_payload

from .models import (
    AgentSessionRow,
    AgentTurnRow,
    CommandRow,
    EventRow,
    EvidenceRow,
    IdempotencyReceiptRow,
    JobStepReceiptRow,
    LearnerProjectionJobRow,
    ProductDraftRevisionAssistanceRow,
    ProductDraftRevisionRow,
    ProductDraftRow,
    ProductInteractionRow,
    ProductPatchDecisionReceiptRow,
    ProductSkillPatchDecisionRow,
    ProductSkillPatchEvidenceRow,
    ProductSkillPatchProposalRow,
    ProductSkillPatchRequestRow,
    ProductWorkspaceRow,
    RunRow,
    SkillBuildProvenanceRow,
    SkillBuildRow,
    SkillRunProvenanceRow,
    WorkflowJobRow,
    WorldPresentationEventRow,
)
from .run_outcomes import (
    load_hint_provider_receipts,
    load_patch_provider_receipts,
    load_validated_run,
    validate_provider_decision_wire,
)
from .skill_provenance import validate_run_provenance


class PostgresProductInteractionStore:
    """Projection storage written by the Agent Turn worker and read by Product APIs."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def get(
        self, session_id: str, interaction_id: str, context: OperationContext
    ) -> Result[dict[str, Any]]:
        async with self._sessions() as session, session.begin():
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
            row = await session.scalar(
                select(ProductInteractionRow).where(
                    ProductInteractionRow.tenant_id == context.actor.tenant_id,
                    ProductInteractionRow.actor_id == context.actor.actor_id,
                    ProductInteractionRow.session_id == session_id,
                    ProductInteractionRow.interaction_id == interaction_id,
                )
            )
            owner = (
                await session.scalar(
                    select(AgentSessionRow).where(
                        AgentSessionRow.tenant_id == row.tenant_id,
                        AgentSessionRow.actor_id == row.actor_id,
                        AgentSessionRow.session_id == row.session_id,
                    )
                )
                if row is not None
                else None
            )
            if row is None:
                return Failure(_error("NOT_FOUND", "READ", "agent interaction not found"))
            if owner is None or not await _interactions_have_authority(
                session, [row], owner
            ):
                return Failure(
                    _error(
                        "INVARIANT_VIOLATION",
                        "READ",
                        "agent interaction durable authority drifted",
                    )
                )
            return Success(row.interaction_json)

    async def list(
        self, session_id: str, after_sequence: int, limit: int, context: OperationContext
    ) -> Result[dict[str, Any]]:
        """Read one page against one repeatable-read high watermark."""
        async with self._sessions() as session, session.begin():
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
            agent_session = await session.scalar(
                select(AgentSessionRow).where(
                    AgentSessionRow.tenant_id == context.actor.tenant_id,
                    AgentSessionRow.actor_id == context.actor.actor_id,
                    AgentSessionRow.session_id == session_id,
                )
            )
            if agent_session is None:
                return Failure(_error("NOT_FOUND", "READ", "agent session not found"))
            workspace = await session.scalar(
                select(ProductWorkspaceRow).where(
                    ProductWorkspaceRow.tenant_id == context.actor.tenant_id,
                    ProductWorkspaceRow.actor_id == context.actor.actor_id,
                    ProductWorkspaceRow.session_id == session_id,
                )
            )
            if workspace is None:
                return Failure(
                    _error(
                        "INVARIANT_VIOLATION",
                        "READ",
                        "agent session has no recoverable Product workspace",
                    )
                )
            high_watermark = await session.scalar(
                select(func.max(ProductInteractionRow.sequence)).where(
                    ProductInteractionRow.tenant_id == context.actor.tenant_id,
                    ProductInteractionRow.actor_id == context.actor.actor_id,
                    ProductInteractionRow.session_id == session_id,
                )
            )
            high = int(high_watermark or 0)
            durable_high = workspace.workspace_json.get("last_interaction_sequence")
            if (
                isinstance(durable_high, bool)
                or not isinstance(durable_high, int)
                or durable_high != high
            ):
                return Failure(
                    _error(
                        "EVENT_SEQUENCE_GAP",
                        "READ",
                        "interaction projection differs from Workspace high-watermark",
                    )
                )
            if after_sequence > high:
                return Failure(_error("INVALID_REQUEST", "VALIDATE", "after_sequence is above the high watermark"))
            rows = list(
                (
                    await session.scalars(
                        select(ProductInteractionRow)
                        .where(
                            ProductInteractionRow.tenant_id == context.actor.tenant_id,
                            ProductInteractionRow.actor_id == context.actor.actor_id,
                            ProductInteractionRow.session_id == session_id,
                            ProductInteractionRow.sequence > after_sequence,
                            ProductInteractionRow.sequence <= high,
                        )
                        .order_by(ProductInteractionRow.sequence)
                        .limit(limit)
                    )
                ).all()
            )
            expected = after_sequence + 1
            if any(row.sequence != expected + index for index, row in enumerate(rows)):
                return Failure(_error("EVENT_SEQUENCE_GAP", "READ", "interaction projection sequence is not gap-free"))
            if rows and rows[-1].sequence < high and len(rows) < limit:
                return Failure(_error("EVENT_SEQUENCE_GAP", "READ", "interaction projection has a sequence gap"))
            if not await _interactions_have_authority(session, rows, agent_session):
                return Failure(
                    _error(
                        "INVARIANT_VIOLATION",
                        "READ",
                        "agent interaction durable authority drifted",
                    )
                )

            interactions = [row.interaction_json for row in rows]
            first = rows[0].sequence if rows else None
            last = rows[-1].sequence if rows else None
            page_context = agent_session.session_json["request_context"]
            return Success(
                {
                    "request_context": page_context,
                    "session_id": session_id,
                    "requested_after_sequence": after_sequence,
                    "requested_limit": limit,
                    "high_watermark_sequence": high if rows else after_sequence,
                    "from_sequence": first,
                    "to_sequence": last,
                    "has_more": bool(last is not None and last < high),
                    "next_after_sequence": last if last is not None else after_sequence,
                    "interactions": interactions,
                }
            )

    async def record(self, interaction: Mapping[str, Any], context: OperationContext) -> Result[None]:
        """Internal Agent Turn projection writer; public transport never calls this method."""
        request_context = interaction.get("request_context")
        if not isinstance(request_context, Mapping) or request_context.get("actor") != {
            "tenant_id": context.actor.tenant_id,
            "actor_id": context.actor.actor_id,
            "actor_type": context.actor.actor_type.value,
            "roles": list(context.actor.roles),
        }:
            return Failure(_error("INVARIANT_VIOLATION", "PROJECT", "interaction actor differs from context"))
        try:
            session_id = _text_value(interaction, "session_id")
            interaction_id = _text_value(interaction, "interaction_id")
            turn_id = _text_value(interaction, "turn_id")
            sequence = _integer_value(interaction, "sequence")
            revision = _integer_value(interaction, "interaction_revision")
            created_at = _time_value(interaction, "created_at")
            updated_at = _time_value(interaction, "updated_at")
        except (KeyError, TypeError, ValueError) as error:
            return Failure(_error("INVARIANT_VIOLATION", "PROJECT", str(error)))
        async with self._sessions() as session, session.begin():
            agent_session = await session.scalar(
                select(AgentSessionRow).where(
                    AgentSessionRow.tenant_id == context.actor.tenant_id,
                    AgentSessionRow.actor_id == context.actor.actor_id,
                    AgentSessionRow.session_id == session_id,
                ).with_for_update()
            )
            if agent_session is None:
                return Failure(_error("NOT_FOUND", "PROJECT", "agent session not found"))
            if agent_session.session_json["request_context"]["content_ref"] != request_context.get("content_ref"):
                return Failure(_error("CONTENT_VERSION_MISMATCH", "PROJECT", "interaction content differs from session"))
            existing = await session.scalar(
                select(ProductInteractionRow).where(
                    ProductInteractionRow.tenant_id == context.actor.tenant_id,
                    ProductInteractionRow.session_id == session_id,
                    ProductInteractionRow.interaction_id == interaction_id,
                ).with_for_update()
            )
            if existing is not None:
                if existing.interaction_json != dict(interaction):
                    return Failure(_error("INVARIANT_VIOLATION", "PROJECT", "immutable interaction changed"))
                return Success(None)
            high = await session.scalar(
                select(func.max(ProductInteractionRow.sequence)).where(
                    ProductInteractionRow.tenant_id == context.actor.tenant_id,
                    ProductInteractionRow.session_id == session_id,
                )
            )
            if sequence != int(high or 0) + 1:
                return Failure(_error("EVENT_SEQUENCE_GAP", "PROJECT", "interaction sequence is not next"))
            session.add(
                ProductInteractionRow(
                    tenant_id=context.actor.tenant_id,
                    actor_id=context.actor.actor_id,
                    session_id=session_id,
                    interaction_id=interaction_id,
                    turn_id=turn_id,
                    sequence=sequence,
                    interaction_revision=revision,
                    created_at=created_at,
                    updated_at=updated_at,
                    interaction_json=dict(interaction),
                )
            )
        return Success(None)

    async def decide_patch(
        self,
        session_id: str,
        interaction_id: str,
        patch_id: str,
        request_body: Mapping[str, Any],
        raw_body: bytes,
        idempotency_key: str,
        context: OperationContext,
    ) -> Result[DecisionWrite]:
        """Record one strict terminal decision and at most one Draft revision."""
        if (
            context.actor.actor_type is not ActorType.STUDENT
            or "game:player" not in context.actor.roles
        ):
            return Failure(
                _error(
                    "AUTHORIZATION_DENIED",
                    "AUTHORITY",
                    "Patch decision requires the student game:player authority",
                )
            )
        canonical_path = (
            f"/product-experience/v1/sessions/{session_id}/agent-interactions/"
            f"{interaction_id}/patches/{patch_id}/decision"
        )
        request_hash = hashlib.sha256(raw_body).hexdigest()
        async with self._sessions() as session, session.begin():
            # The immutable Interaction row is the per-proposal serialization
            # point.  A waiter must re-read the receipt after acquiring this
            # lock so concurrent double-misses cannot apply a Patch twice.
            interaction_row = await session.scalar(
                select(ProductInteractionRow).where(
                    ProductInteractionRow.tenant_id == context.actor.tenant_id,
                    ProductInteractionRow.actor_id == context.actor.actor_id,
                    ProductInteractionRow.session_id == session_id,
                    ProductInteractionRow.interaction_id == interaction_id,
                ).with_for_update()
            )
            if interaction_row is None:
                return Failure(_error("NOT_FOUND", "PATCH_DECISION", "agent interaction not found"))
            owner = await session.scalar(
                select(AgentSessionRow).where(
                    AgentSessionRow.tenant_id == context.actor.tenant_id,
                    AgentSessionRow.actor_id == context.actor.actor_id,
                    AgentSessionRow.session_id == session_id,
                )
            )
            if owner is None or not await _skill_patch_interaction_has_authority(
                session, interaction_row, owner
            ):
                return Failure(
                    _error(
                        "INVARIANT_VIOLATION",
                        "PATCH_DECISION",
                        "Patch proposal Interaction authority drifted",
                    )
                )
            receipt = await session.scalar(
                select(ProductPatchDecisionReceiptRow).where(
                    ProductPatchDecisionReceiptRow.tenant_id == context.actor.tenant_id,
                    ProductPatchDecisionReceiptRow.actor_id == context.actor.actor_id,
                    ProductPatchDecisionReceiptRow.canonical_path == canonical_path,
                    ProductPatchDecisionReceiptRow.idempotency_key == idempotency_key,
                ).with_for_update()
            )
            if receipt is not None:
                if receipt.request_sha256 != request_hash:
                    return Failure(
                        _error(
                            "IDEMPOTENCY_KEY_REUSED",
                            "PATCH_DECISION",
                            "idempotency key was reused",
                        )
                    )
                if not await _patch_decision_receipt_has_authority(
                    session,
                    interaction_row=interaction_row,
                    receipt=receipt,
                    request_hash=request_hash,
                ):
                    return Failure(
                        _error(
                            "INVARIANT_VIOLATION",
                            "PATCH_DECISION",
                            "Patch decision receipt authority drifted",
                        )
                    )
                return Success(
                    DecisionWrite(
                        receipt.receipt_json,
                        receipt.interaction_revision,
                        True,
                    )
                )

            interaction = interaction_row.interaction_json
            patch = interaction.get("skill_patch")
            if not isinstance(patch, Mapping) or interaction.get("patch_decision") is not None:
                return Failure(_error("CONTENT_VERSION_MISMATCH", "PATCH_DECISION", "interaction has no undecided patch"))
            if not _decision_identities_match(request_body, session_id, interaction_id, patch_id, interaction, patch):
                return Failure(_error("INVALID_REQUEST", "VALIDATE", "patch decision does not match interaction"))
            if _integer_value(request_body, "expected_interaction_revision") != interaction_row.interaction_revision:
                return Failure(_error("CONTENT_VERSION_MISMATCH", "PATCH_DECISION", "interaction revision is stale"))
            authority = await _load_patch_decision_authority(
                session,
                tenant_id=context.actor.tenant_id,
                actor_id=context.actor.actor_id,
                session_id=session_id,
                interaction_id=interaction_id,
                patch_id=patch_id,
                interaction=interaction,
                patch=patch,
                request_body=request_body,
            )
            if isinstance(authority, Failure):
                return authority
            proposal, draft_row, base_revision, operation = authority.value

            try:
                decided_at = _time_value(request_body, "decided_at")
            except (KeyError, TypeError, ValueError) as error:
                return Failure(_error("INVALID_REQUEST", "VALIDATE", str(error)))
            if decided_at < interaction_row.created_at or decided_at < draft_row.created_at:
                return Failure(
                    _error(
                        "INVALID_REQUEST",
                        "VALIDATE",
                        "decision predates interaction or draft",
                    )
                )
            decision = _text_value(request_body, "decision")
            updated_draft = draft_row.draft_json
            accepted_revision: ProductDraftRevisionRow | None = None
            if decision == "ACCEPT":
                try:
                    updated_draft = _apply_entrypoint_upsert(
                        draft_row.draft_json,
                        patch,
                        operation,
                        decided_at,
                    )
                except InvalidSkillBuildRequest as error:
                    return Failure(_error("INVALID_REQUEST", "PATCH_DECISION", str(error)))
                if updated_draft["draft_sha256"] != request_body["result_draft_sha256"]:
                    return Failure(_error("CONTENT_VERSION_MISMATCH", "PATCH_DECISION", "patch result hash differs"))
                draft_row.revision = updated_draft["revision"]
                draft_row.draft_sha256 = updated_draft["draft_sha256"]
                draft_row.updated_at = decided_at
                draft_row.draft_json = updated_draft
                accepted_revision = append_draft_revision_in_session(
                    session,
                    tenant_id=context.actor.tenant_id,
                    actor_id=context.actor.actor_id,
                    draft=updated_draft,
                    source_kind="SKILL_PATCH",
                    patch_id=patch_id,
                    created_at=decided_at,
                    parent_revision_row_id=base_revision.draft_revision_row_id,
                )
                await session.flush()
            elif decision != "REJECT":
                return Failure(_error("INVALID_REQUEST", "VALIDATE", "unsupported patch decision"))

            interaction_after = copy.deepcopy(interaction)
            revision_after = interaction_row.interaction_revision + 1
            receipt_data = _decision_receipt(
                request_body,
                interaction,
                draft_row.draft_json if decision == "REJECT" else updated_draft,
                interaction_row.interaction_revision,
                revision_after,
                context,
            )
            decision_row = ProductSkillPatchDecisionRow(
                decision_id=_text_value(request_body, "decision_id"),
                tenant_id=context.actor.tenant_id,
                actor_id=context.actor.actor_id,
                patch_id=patch_id,
                interaction_id=interaction_id,
                session_id=session_id,
                draft_id=draft_row.draft_id,
                base_draft_revision_row_id=base_revision.draft_revision_row_id,
                accepted_draft_revision_row_id=(
                    accepted_revision.draft_revision_row_id
                    if accepted_revision is not None
                    else None
                ),
                decision=decision,
                reason_code=cast(str | None, request_body.get("reason_code")),
                request_sha256=request_hash,
                receipt_json=receipt_data,
                decided_at=decided_at,
            )
            session.add(decision_row)
            await session.flush()
            if accepted_revision is not None:
                session.add(
                    ProductDraftRevisionAssistanceRow(
                        draft_revision_row_id=accepted_revision.draft_revision_row_id,
                        origin_accepted_revision_row_id=(
                            accepted_revision.draft_revision_row_id
                        ),
                        patch_id=patch_id,
                        patch_decision_id=decision_row.decision_id,
                        inherited=False,
                        created_at=decided_at,
                    )
                )
                await refresh_workspace_in_session(
                    session,
                    tenant_id=context.actor.tenant_id,
                    actor_id=context.actor.actor_id,
                    session_id=session_id,
                    updated_at=decided_at,
                )
            interaction_after["patch_decision"] = receipt_data
            interaction_after["interaction_revision"] = revision_after
            interaction_after["updated_at"] = request_body["decided_at"]
            interaction_row.interaction_revision = revision_after
            interaction_row.updated_at = decided_at
            interaction_row.interaction_json = interaction_after
            session.add(
                ProductPatchDecisionReceiptRow(
                    tenant_id=context.actor.tenant_id,
                    actor_id=context.actor.actor_id,
                    canonical_path=canonical_path,
                    idempotency_key=idempotency_key,
                    request_sha256=request_hash,
                    decision_id=decision_row.decision_id,
                    patch_id=proposal.patch_id,
                    draft_revision_row_id=(
                        accepted_revision.draft_revision_row_id
                        if accepted_revision is not None
                        else None
                    ),
                    interaction_id=interaction_id,
                    interaction_revision=revision_after,
                    receipt_json=receipt_data,
                    created_at=decided_at,
                )
            )
            return Success(DecisionWrite(receipt_data, revision_after, False))


@dataclass(frozen=True, slots=True)
class _InteractionAuthorityKeys:
    turn_id: str
    command_id: str
    run_id: str | None
    event_id: str
    receipt_id: str
    draft_id: str | None


@dataclass(frozen=True, slots=True)
class _InteractionAuthority:
    turn: AgentTurnRow
    command: CommandRecord
    run: RunRow
    event: EventRow
    job: WorkflowJobRow
    receipt: JobStepReceiptRow
    invocation_receipt: JobStepReceiptRow
    command_receipt: IdempotencyReceiptRow
    draft: ProductDraftRow | None
    decision_receipt: ProductPatchDecisionReceiptRow | None


async def _interactions_have_authority(
    session: AsyncSession,
    rows: list[ProductInteractionRow],
    owner: AgentSessionRow,
) -> bool:
    """Validate ordinary Run projections and no-Run Patch projections separately."""

    ordinary: list[ProductInteractionRow] = []
    proposals: list[ProductInteractionRow] = []
    hints: list[ProductInteractionRow] = []
    for row in rows:
        kind = _interaction_projection_kind(row.interaction_json)
        if kind == "RUN":
            ordinary.append(row)
        elif kind == "SKILL_PATCH_NO_RUN":
            proposals.append(row)
        elif kind == "HINT_NO_RUN":
            hints.append(row)
        else:
            return False
    if ordinary and not await _run_interactions_have_authority(session, ordinary, owner):
        return False
    return all(
        [
            *[
                await _skill_patch_interaction_has_authority(session, row, owner)
                for row in proposals
            ],
            *[await _hint_interaction_has_authority(session, row, owner) for row in hints],
        ]
    )


async def _run_interactions_have_authority(
    session: AsyncSession,
    rows: list[ProductInteractionRow],
    owner: AgentSessionRow,
) -> bool:
    """Resolve every immutable source in one database snapshot, then compare bytes."""

    keyed: list[tuple[ProductInteractionRow, _InteractionAuthorityKeys]] = []
    for row in rows:
        keys = _interaction_authority_keys(row.interaction_json)
        if keys is None or keys.run_id is None:
            return False
        keyed.append((row, keys))
    if not keyed:
        return True

    turn_ids = {keys.turn_id for _, keys in keyed}
    turns = list(
        (
            await session.scalars(
                select(AgentTurnRow).where(
                    AgentTurnRow.tenant_id == owner.tenant_id,
                    AgentTurnRow.actor_id == owner.actor_id,
                    AgentTurnRow.session_id == owner.session_id,
                    AgentTurnRow.turn_id.in_(turn_ids),
                )
            )
        ).all()
    )
    turns_by_id = {turn.turn_id: turn for turn in turns}
    if len(turns_by_id) != len(turn_ids):
        return False

    command_ids = {turn.command_id for turn in turns}
    commands = list(
        (
            await session.scalars(
                select(CommandRow).where(
                    CommandRow.tenant_id == owner.tenant_id,
                    CommandRow.actor_id == owner.actor_id,
                    CommandRow.command_id.in_(command_ids),
                )
            )
        ).all()
    )
    commands_by_id = {command.command_id: command for command in commands}
    if len(commands_by_id) != len(command_ids):
        return False
    validated_commands: dict[str, CommandRecord] = {}
    for command_row in commands:
        command = await validated_command_record(session, command_row)
        if command is None:
            return False
        validated_commands[command.command_id] = command

    run_ids = {cast(str, keys.run_id) for _, keys in keyed}
    runs = list(
        (
            await session.scalars(
                select(RunRow).where(
                    RunRow.tenant_id == owner.tenant_id,
                    RunRow.actor_id == owner.actor_id,
                    RunRow.session_id == owner.session_id,
                    RunRow.run_id.in_(run_ids),
                )
            )
        ).all()
    )
    runs_by_id = {run.run_id: run for run in runs}
    if len(runs_by_id) != len(run_ids):
        return False
    run_provenances = list(
        (
            await session.scalars(
                select(SkillRunProvenanceRow).where(
                    SkillRunProvenanceRow.tenant_id == owner.tenant_id,
                    SkillRunProvenanceRow.actor_id == owner.actor_id,
                    SkillRunProvenanceRow.session_id == owner.session_id,
                    SkillRunProvenanceRow.run_id.in_(run_ids),
                )
            )
        ).all()
    )
    run_provenance_by_id = {item.run_id: item for item in run_provenances}
    if len(run_provenance_by_id) != len(run_ids):
        return False
    for run_id in run_ids:
        if await validate_run_provenance(
            session, run_provenance_by_id[run_id]
        ) is None:
            return False

    event_ids = {keys.event_id for _, keys in keyed}
    events = list(
        (
            await session.scalars(
                select(EventRow).where(
                    EventRow.tenant_id == owner.tenant_id,
                    EventRow.event_id.in_(event_ids),
                )
            )
        ).all()
    )
    events_by_id = {event.event_id: event for event in events}
    if len(events_by_id) != len(event_ids):
        return False

    receipt_ids = {keys.receipt_id for _, keys in keyed}
    receipts = list(
        (
            await session.scalars(
                select(JobStepReceiptRow).where(
                    JobStepReceiptRow.tenant_id == owner.tenant_id,
                    JobStepReceiptRow.receipt_id.in_(receipt_ids),
                )
            )
        ).all()
    )
    receipts_by_id = {receipt.receipt_id: receipt for receipt in receipts}
    if len(receipts_by_id) != len(receipt_ids):
        return False

    job_ids = {receipt.job_id for receipt in receipts}
    jobs = list(
        (
            await session.scalars(
                select(WorkflowJobRow).where(
                    WorkflowJobRow.tenant_id == owner.tenant_id,
                    WorkflowJobRow.job_id.in_(job_ids),
                )
            )
        ).all()
    )
    jobs_by_id = {job.job_id: job for job in jobs}
    if len(jobs_by_id) != len(job_ids):
        return False

    invocation_receipts = list(
        (
            await session.scalars(
                select(JobStepReceiptRow).where(
                    JobStepReceiptRow.tenant_id == owner.tenant_id,
                    JobStepReceiptRow.job_id.in_(job_ids),
                    JobStepReceiptRow.step_name == "SKILL_INVOKED",
                )
            )
        ).all()
    )
    invocation_receipts_by_job: dict[str, JobStepReceiptRow] = {}
    for invocation_receipt in invocation_receipts:
        if invocation_receipt.job_id in invocation_receipts_by_job:
            return False
        invocation_receipts_by_job[invocation_receipt.job_id] = invocation_receipt
    if len(invocation_receipts_by_job) != len(job_ids):
        return False

    command_receipts = list(
        (
            await session.scalars(
                select(IdempotencyReceiptRow).where(
                    IdempotencyReceiptRow.tenant_id == owner.tenant_id,
                    IdempotencyReceiptRow.actor_id == owner.actor_id,
                    IdempotencyReceiptRow.operation == "EXECUTE_AGENT_TURN",
                    IdempotencyReceiptRow.command_id.in_(command_ids),
                )
            )
        ).all()
    )
    command_receipts_by_command = {
        command_receipt.command_id: command_receipt for command_receipt in command_receipts
    }
    if len(command_receipts_by_command) != len(command_ids):
        return False

    draft_ids = {keys.draft_id for _, keys in keyed if keys.draft_id is not None}
    drafts = (
        list(
            (
                await session.scalars(
                    select(ProductDraftRow).where(
                        ProductDraftRow.tenant_id == owner.tenant_id,
                        ProductDraftRow.actor_id == owner.actor_id,
                        ProductDraftRow.session_id == owner.session_id,
                        ProductDraftRow.draft_id.in_(draft_ids),
                    )
                )
            ).all()
        )
        if draft_ids
        else []
    )
    drafts_by_id = {draft.draft_id: draft for draft in drafts}
    if len(drafts_by_id) != len(draft_ids):
        return False

    interaction_ids = {row.interaction_id for row, _ in keyed}
    decision_receipts = list(
        (
            await session.scalars(
                select(ProductPatchDecisionReceiptRow).where(
                    ProductPatchDecisionReceiptRow.tenant_id == owner.tenant_id,
                    ProductPatchDecisionReceiptRow.actor_id == owner.actor_id,
                    ProductPatchDecisionReceiptRow.interaction_id.in_(interaction_ids),
                )
            )
        ).all()
    )
    decisions_by_interaction: dict[str, ProductPatchDecisionReceiptRow] = {}
    for decision_receipt in decision_receipts:
        if decision_receipt.interaction_id in decisions_by_interaction:
            return False
        decisions_by_interaction[decision_receipt.interaction_id] = decision_receipt

    for row, keys in keyed:
        turn = turns_by_id.get(keys.turn_id)
        run = runs_by_id.get(cast(str, keys.run_id))
        event = events_by_id.get(keys.event_id)
        receipt = receipts_by_id.get(keys.receipt_id)
        if turn is None or run is None or event is None or receipt is None:
            return False
        command_row = commands_by_id.get(turn.command_id)
        job = jobs_by_id.get(receipt.job_id)
        command = validated_commands.get(turn.command_id)
        invocation_receipt = (
            invocation_receipts_by_job.get(job.job_id) if job is not None else None
        )
        command_receipt = command_receipts_by_command.get(turn.command_id)
        if (
            command_row is None
            or command is None
            or job is None
            or invocation_receipt is None
            or command_receipt is None
        ):
            return False
        authority = _InteractionAuthority(
            turn=turn,
            command=command,
            run=run,
            event=event,
            job=job,
            receipt=receipt,
            invocation_receipt=invocation_receipt,
            command_receipt=command_receipt,
            draft=drafts_by_id.get(keys.draft_id) if keys.draft_id is not None else None,
            decision_receipt=decisions_by_interaction.get(row.interaction_id),
        )
        if not _interaction_authority_matches(row, owner, authority):
            return False
    return True


def _interaction_authority_keys(
    value: Mapping[str, Any],
) -> _InteractionAuthorityKeys | None:
    feedback = value.get("feedback")
    feedback_event = value.get("feedback_event")
    source = value.get("projection_source")
    patch = value.get("skill_patch")
    if (
        not isinstance(feedback, Mapping)
        or not isinstance(feedback_event, Mapping)
        or not isinstance(source, Mapping)
        or (patch is not None and not isinstance(patch, Mapping))
    ):
        return None
    fields = {
        "turn_id": value.get("turn_id"),
        "command_id": feedback.get("command_id"),
        "run_id": feedback.get("run_id"),
        "event_id": feedback_event.get("event_id"),
        "receipt_id": source.get("receipt_id"),
        "draft_id": patch.get("draft_id") if isinstance(patch, Mapping) else None,
    }
    if any(item is not None and not isinstance(item, str) for item in fields.values()):
        return None
    if any(
        not isinstance(fields[name], str)
        for name in fields
        if name not in {"draft_id", "run_id"}
    ):
        return None
    return _InteractionAuthorityKeys(
        turn_id=cast(str, fields["turn_id"]),
        command_id=cast(str, fields["command_id"]),
        run_id=cast(str | None, fields["run_id"]),
        event_id=cast(str, fields["event_id"]),
        receipt_id=cast(str, fields["receipt_id"]),
        draft_id=cast(str | None, fields["draft_id"]),
    )


def _interaction_projection_kind(value: Mapping[str, Any]) -> str | None:
    """Select the immutable authority graph without treating a prior Run as new."""

    feedback = value.get("feedback")
    if not isinstance(feedback, Mapping):
        return None
    patch = value.get("skill_patch")
    if value.get("response_type") == "skill_patch" or patch is not None:
        if (
            value.get("role") == "teaching_agent"
            and value.get("response_type") == "skill_patch"
            and value.get("question") is None
            and value.get("hint_level") == 4
            and isinstance(patch, Mapping)
            and feedback.get("run_id") is None
        ):
            return "SKILL_PATCH_NO_RUN"
        return None
    if isinstance(feedback.get("run_id"), str):
        return "RUN"
    if (
        feedback.get("run_id") is None
        and value.get("role") in {"teaching_agent", "bug_agent"}
        and value.get("response_type") in {"question", "hint"}
    ):
        return "HINT_NO_RUN"
    return None


async def _skill_patch_interaction_has_authority(
    session: AsyncSession,
    row: ProductInteractionRow,
    owner: AgentSessionRow,
) -> bool:
    """Close a Patch proposal to its new no-Run request and selected prior Run."""

    value = row.interaction_json
    try:
        origin = _object_value(value, "request_context")
        actor = _object_value(origin, "actor")
        content = _object_value(origin, "content_ref")
        feedback = _object_value(value, "feedback")
        feedback_event = _object_value(value, "feedback_event")
        source = _object_value(value, "projection_source")
        links = _object_value(value, "links")
        patch = _object_value(value, "skill_patch")
        command_id = _text_value(feedback, "command_id")
        event_id = _text_value(feedback_event, "event_id")
        receipt_id = _text_value(source, "receipt_id")
        created_at = _time_value(value, "created_at")
        updated_at = _time_value(value, "updated_at")
        completed_at = _time_value(feedback, "completed_at")
    except (KeyError, TypeError, ValueError):
        return False
    if (
        feedback.get("run_id") is not None
        or value.get("role") != "teaching_agent"
        or value.get("response_type") != "skill_patch"
        or value.get("question") is not None
        or value.get("hint_level") != 4
        or value.get("session_id") != row.session_id
        or value.get("interaction_id") != row.interaction_id
        or value.get("turn_id") != row.turn_id
        or value.get("sequence") != row.sequence
        or value.get("interaction_revision") != row.interaction_revision
        or created_at != row.created_at
        or updated_at != row.updated_at
        or actor.get("tenant_id") != row.tenant_id
        or actor.get("actor_id") != row.actor_id
        or owner.tenant_id != row.tenant_id
        or owner.actor_id != row.actor_id
        or owner.session_id != row.session_id
        or content != owner.session_json.get("content")
        or feedback.get("session_id") != row.session_id
        or feedback.get("turn_id") != row.turn_id
        or feedback.get("source") != "provider"
        or feedback.get("degraded") is not False
        or feedback.get("fallback_reason") is not None
    ):
        return False

    turn = await session.scalar(
        select(AgentTurnRow).where(
            AgentTurnRow.tenant_id == row.tenant_id,
            AgentTurnRow.actor_id == row.actor_id,
            AgentTurnRow.session_id == row.session_id,
            AgentTurnRow.turn_id == row.turn_id,
            AgentTurnRow.command_id == command_id,
        )
    )
    command_row = await session.scalar(
        select(CommandRow).where(
            CommandRow.tenant_id == row.tenant_id,
            CommandRow.actor_id == row.actor_id,
            CommandRow.command_id == command_id,
        )
    )
    command = (
        await validated_command_record(session, command_row)
        if command_row is not None
        else None
    )
    job = await session.scalar(
        select(WorkflowJobRow).where(
            WorkflowJobRow.tenant_id == row.tenant_id,
            WorkflowJobRow.command_id == command_id,
            WorkflowJobRow.subject_type == "AGENT_TURN",
            WorkflowJobRow.subject_id == row.turn_id,
        )
    )
    command_receipt = await session.scalar(
        select(IdempotencyReceiptRow).where(
            IdempotencyReceiptRow.tenant_id == row.tenant_id,
            IdempotencyReceiptRow.actor_id == row.actor_id,
            IdempotencyReceiptRow.operation == "EXECUTE_AGENT_TURN",
            IdempotencyReceiptRow.command_id == command_id,
        )
    )
    proposal = await session.scalar(
        select(ProductSkillPatchProposalRow).where(
            ProductSkillPatchProposalRow.tenant_id == row.tenant_id,
            ProductSkillPatchProposalRow.actor_id == row.actor_id,
            ProductSkillPatchProposalRow.session_id == row.session_id,
            ProductSkillPatchProposalRow.interaction_id == row.interaction_id,
            ProductSkillPatchProposalRow.patch_id == patch.get("patch_id"),
        )
    )
    reservation = (
        await session.scalar(
            select(ProductSkillPatchRequestRow).where(
                ProductSkillPatchRequestRow.tenant_id == row.tenant_id,
                ProductSkillPatchRequestRow.actor_id == row.actor_id,
                ProductSkillPatchRequestRow.session_id == row.session_id,
                ProductSkillPatchRequestRow.command_id == command_id,
            )
        )
        if proposal is not None
        else None
    )
    draft = (
        await session.scalar(
            select(ProductDraftRow).where(
                ProductDraftRow.tenant_id == row.tenant_id,
                ProductDraftRow.actor_id == row.actor_id,
                ProductDraftRow.session_id == row.session_id,
                ProductDraftRow.draft_id == proposal.draft_id,
            )
        )
        if proposal is not None
        else None
    )
    forbidden_runs = list(
        (
            await session.scalars(
                select(RunRow).where(
                    RunRow.tenant_id == row.tenant_id,
                    RunRow.actor_id == row.actor_id,
                    RunRow.session_id == row.session_id,
                    RunRow.command_id == command_id,
                )
            )
        ).all()
    )
    forbidden_evidence = list(
        (
            await session.scalars(
                select(EvidenceRow).where(
                    EvidenceRow.tenant_id == row.tenant_id,
                    EvidenceRow.actor_id == row.actor_id,
                    EvidenceRow.command_id == command_id,
                )
            )
        ).all()
    )
    request_events = list(
        (
            await session.scalars(
                select(EventRow).where(
                    EventRow.tenant_id == row.tenant_id,
                    EventRow.event_json["command_id"].astext == command_id,
                )
            )
        ).all()
    )
    forbidden_learner_jobs = list(
        (
            await session.scalars(
                select(LearnerProjectionJobRow).where(
                    LearnerProjectionJobRow.tenant_id == row.tenant_id,
                    LearnerProjectionJobRow.command_id == command_id,
                )
            )
        ).all()
    )
    forbidden_world_events = list(
        (
            await session.scalars(
                select(WorldPresentationEventRow).where(
                    WorldPresentationEventRow.tenant_id == row.tenant_id,
                    WorldPresentationEventRow.command_id == command_id,
                )
            )
        ).all()
    )
    if (
        turn is None
        or command is None
        or job is None
        or command_receipt is None
        or proposal is None
        or reservation is None
        or draft is None
        or forbidden_runs
        or forbidden_evidence
        or forbidden_learner_jobs
        or forbidden_world_events
    ):
        return False
    turn_input = turn.request_json.get("input")
    expected_context = request_context_data(command.request_context)
    if (
        origin != expected_context
        or turn.tenant_id != row.tenant_id
        or turn.actor_id != row.actor_id
        or turn.session_id != row.session_id
        or not isinstance(turn_input, Mapping)
        or dict(turn_input)
        != {
            "type": "UI_ACTION",
            "action_id": "request_ai_patch",
            "selection_id": proposal.requested_interaction_id,
        }
        or command.command_type != "EXECUTE_AGENT_TURN"
        or command.status is not CommandStatus.APPLIED
        or command.stage != "COMPLETE"
        or not command.terminal
        or command.updated_at != created_at
        or command.result
        != {"result_type": "NO_EFFECT", "reason_code": "SKILL_PATCH_PROPOSED"}
        or command.error is not None
        or command.links != {"self": f"/v1/commands/{command_id}"}
        or command_receipt.request_sha256 != job.request_sha256
        or command_receipt.accepted_at != command.accepted_at
        or reservation.status != "PROPOSED"
        or reservation.proposal_id != proposal.patch_id
        or reservation.requested_interaction_id != proposal.requested_interaction_id
        or reservation.turn_id != row.turn_id
        or reservation.command_id != command_id
        or proposal.turn_id != row.turn_id
        or proposal.request_command_id != command_id
        or proposal.proposal_json != dict(patch)
        or not _proposal_agent_authority_matches(proposal, owner, turn, patch)
    ):
        return False

    receipts = list(
        (
            await session.scalars(
                select(JobStepReceiptRow).where(
                    JobStepReceiptRow.tenant_id == row.tenant_id,
                    JobStepReceiptRow.job_id == job.job_id,
                )
            )
        ).all()
    )
    try:
        provider_results = await load_patch_provider_receipts(session, job)
    except WorkflowInvariantError:
        return False
    provider_names = {
        name
        for result in provider_results
        for name in (
            result.step_name,
            result.step_name.replace("_RESULT_", "_DISPATCH_"),
        )
    }
    required_receipts = {
        *provider_names,
        "PATCH_PROPOSAL_DERIVED",
        "TURN_COMPLETED",
    }
    if not _patch_job_receipts_have_authority(receipts, job, required_receipts):
        return False
    receipts_by_name = {item.step_name: item for item in receipts}
    terminal = receipts_by_name["TURN_COMPLETED"]
    derived = receipts_by_name["PATCH_PROPOSAL_DERIVED"]
    provider_result = provider_results[-1]
    provider_dispatch = receipts_by_name[
        provider_result.step_name.replace("_RESULT_", "_DISPATCH_")
    ]
    provider_draft = _patch_provider_decision_draft(value, proposal)
    if provider_draft is None:
        return False
    try:
        provider_result_authority = _object_value(
            provider_result.receipt_json, "dispatch"
        )
        validate_provider_decision_wire(
            provider_results,
            decision_draft=provider_draft,
            evidence_refs=(),
        )
    except (KeyError, TypeError, WorkflowInvariantError):
        return False
    if (
        receipt_id != terminal.receipt_id
        or terminal.receipt_id
        != workflow_step_receipt_id(row.tenant_id, job.job_id, "TURN_COMPLETED")
        or terminal.receipt_json != dict(source)
        or terminal.output_sha256 != workflow_receipt_sha256(dict(source))
        or derived.receipt_json != proposal.agent_proposal_json
        or derived.input_sha256 != proposal.agent_proposal_sha256
        or provider_dispatch.input_sha256 != provider_result.input_sha256
        or provider_dispatch.receipt_json.get("command_id") != command_id
        or provider_dispatch.receipt_json.get("turn_id") != row.turn_id
        or provider_result_authority.get("request_sha256")
        != provider_result.input_sha256
        or provider_result_authority.get("generation_count") != 1
        or provider_result_authority.get("state") != "SUCCEEDED"
        or job.status != "SUCCEEDED"
        or job.phase != "COMPLETE"
        or job.lease_owner is not None
        or job.lease_expires_at is not None
        or job.operation != "EXECUTE_AGENT_TURN"
        or job.job_json.get("request_context") != origin
        or job.job_json.get("session_id") != row.session_id
        or job.job_json.get("turn_id") != row.turn_id
    ):
        return False

    runtime_event_row = await session.scalar(
        select(EventRow).where(
            EventRow.tenant_id == row.tenant_id,
            EventRow.event_id == event_id,
        )
    )
    runtime_event = _runtime_event(runtime_event_row) if runtime_event_row is not None else None
    feedback_sha256 = canonical_json_sha256(dict(feedback))
    source_projection = dict(source)
    retained_source_sha256 = source_projection.pop("source_sha256", None)
    retained_event = (
        public_domain_event_data(runtime_event) if runtime_event is not None else None
    )
    event_payload = retained_event.pop("payload", None) if retained_event is not None else None
    if retained_event is not None:
        retained_event["feedback_sha256"] = feedback_sha256
    if (
        runtime_event is None
        or len(request_events) != 1
        or request_events[0].event_id != event_id
        or runtime_event.event_type
        != RuntimeEventType.AGENT_TURN_FEEDBACK_READY.value
        or runtime_event.command_id != command_id
        or runtime_event.stream_id != f"agent-session:{row.session_id}"
        or runtime_event.occurred_at != completed_at
        or event_payload != feedback
        or retained_event != feedback_event
        or source.get("source_type") != "AGENT_TURN_PRODUCT_PROJECTION"
        or source.get("source_revision") != 1
        or source.get("actor") != actor
        or source.get("content_ref") != content
        or source.get("interaction_id") != row.interaction_id
        or source.get("session_id") != row.session_id
        or source.get("turn_id") != row.turn_id
        or source.get("sequence") != row.sequence
        or source.get("command_id") != command_id
        or source.get("feedback_event_id") != event_id
        or source.get("feedback_sha256") != feedback_sha256
        or source.get("skill_patch_sha256") != proposal.patch_sha256
        or source.get("committed_at") != value.get("created_at")
        or retained_source_sha256 != canonical_json_sha256(source_projection)
        or any(
            source.get(field) != value.get(field)
            for field in ("role", "response_type", "question", "hint_level")
        )
    ):
        return False

    selected = await session.scalar(
        select(ProductInteractionRow).where(
            ProductInteractionRow.tenant_id == row.tenant_id,
            ProductInteractionRow.actor_id == row.actor_id,
            ProductInteractionRow.session_id == row.session_id,
            ProductInteractionRow.interaction_id == proposal.requested_interaction_id,
            ProductInteractionRow.interaction_revision
            == proposal.requested_interaction_revision,
            ProductInteractionRow.sequence == proposal.requested_interaction_sequence,
        )
    )
    if (
        selected is None
        or selected.sequence != proposal.requested_failure_suffix_end_sequence
        or selected.sequence + 1 != row.sequence
        or not await _run_interactions_have_authority(session, [selected], owner)
        or not await _proposal_failure_authority_matches(
            session, proposal, selected, feedback, command
        )
    ):
        return False
    if links != {
        "self": (
            f"/product-experience/v1/sessions/{row.session_id}/"
            f"agent-interactions/{row.interaction_id}"
        ),
        "session_workspace": f"/product-experience/v1/sessions/{row.session_id}/workspace",
        "skill_draft": (
            f"/product-experience/v1/sessions/{row.session_id}/skill-drafts/"
            f"{proposal.draft_id}"
        ),
    } or not _draft_authority_matches(draft, owner):
        return False
    decision_receipt = await session.scalar(
        select(ProductPatchDecisionReceiptRow).where(
            ProductPatchDecisionReceiptRow.tenant_id == row.tenant_id,
            ProductPatchDecisionReceiptRow.actor_id == row.actor_id,
            ProductPatchDecisionReceiptRow.interaction_id == row.interaction_id,
        )
    )
    immutable_decision = await session.scalar(
        select(ProductSkillPatchDecisionRow).where(
            ProductSkillPatchDecisionRow.tenant_id == row.tenant_id,
            ProductSkillPatchDecisionRow.actor_id == row.actor_id,
            ProductSkillPatchDecisionRow.session_id == row.session_id,
            ProductSkillPatchDecisionRow.interaction_id == row.interaction_id,
            ProductSkillPatchDecisionRow.patch_id == proposal.patch_id,
        )
    )
    decision_value = value.get("patch_decision")
    if not (
        (decision_value is None and decision_receipt is None and immutable_decision is None)
        or (
            decision_value is not None
            and decision_receipt is not None
            and immutable_decision is not None
        )
    ):
        return False
    if decision_receipt is not None and not await _patch_decision_receipt_has_authority(
        session,
        interaction_row=row,
        receipt=decision_receipt,
        request_hash=decision_receipt.request_sha256,
    ):
        return False
    authority = _InteractionAuthority(
        turn=turn,
        command=command,
        run=cast(RunRow, None),
        event=cast(EventRow, runtime_event_row),
        job=job,
        receipt=terminal,
        invocation_receipt=cast(JobStepReceiptRow, None),
        command_receipt=command_receipt,
        draft=draft,
        decision_receipt=decision_receipt,
    )
    return _patch_authority_matches(
        row,
        origin=origin,
        patch=patch,
        decision=value.get("patch_decision"),
        authority=authority,
        created_at=created_at,
        updated_at=updated_at,
    )


def _evidence_refs_from_wire(value: object) -> tuple[EvidenceRef, ...]:
    """Read back the Evidence a durable decision cited, or nothing if malformed.

    A malformed reference yields an empty tuple rather than an exception: the
    caller is a read-side authority check, and "cited something unreadable" has
    to fail the check, not the request.
    """

    if not isinstance(value, list):
        return ()
    refs: list[EvidenceRef] = []
    for item in value:
        if not isinstance(item, Mapping):
            return ()
        try:
            refs.append(
                EvidenceRef(
                    evidence_id=str(item["evidence_id"]),
                    evidence_type=str(item["evidence_type"]),
                    created_at=datetime.fromisoformat(str(item["created_at"]).replace("Z", "+00:00")),
                    sha256=item.get("sha256"),
                    uri=item.get("uri"),
                )
            )
        except (KeyError, TypeError, ValueError):
            return ()
    return tuple(refs)


async def _hint_interaction_has_authority(
    session: AsyncSession,
    row: ProductInteractionRow,
    owner: AgentSessionRow,
) -> bool:
    """Close a hint projection to its no-Run Turn and forbid every side effect."""

    value = row.interaction_json
    try:
        origin = _object_value(value, "request_context")
        actor = _object_value(origin, "actor")
        content = _object_value(origin, "content_ref")
        feedback = _object_value(value, "feedback")
        feedback_event = _object_value(value, "feedback_event")
        source = _object_value(value, "projection_source")
        links = _object_value(value, "links")
        command_id = _text_value(feedback, "command_id")
        event_id = _text_value(feedback_event, "event_id")
        receipt_id = _text_value(source, "receipt_id")
        created_at = _time_value(value, "created_at")
        updated_at = _time_value(value, "updated_at")
        completed_at = _time_value(feedback, "completed_at")
    except (KeyError, TypeError, ValueError):
        return False
    role = value.get("role")
    response_type = value.get("response_type")
    question = value.get("question")
    hint_level = value.get("hint_level")
    if (
        feedback.get("run_id") is not None
        or role not in {"teaching_agent", "bug_agent"}
        or response_type not in {"question", "hint"}
        or value.get("skill_patch") is not None
        or value.get("patch_decision") is not None
        or (response_type == "question" and (question is None or hint_level is not None))
        or (
            response_type == "hint"
            and (
                question is not None
                or isinstance(hint_level, bool)
                or not isinstance(hint_level, int)
                or not 0 <= hint_level <= 3
            )
        )
        or value.get("session_id") != row.session_id
        or value.get("interaction_id") != row.interaction_id
        or value.get("turn_id") != row.turn_id
        or value.get("sequence") != row.sequence
        or value.get("interaction_revision") != row.interaction_revision
        or row.interaction_revision != 1
        or created_at != row.created_at
        or updated_at != row.updated_at
        or created_at != updated_at
        or actor.get("tenant_id") != row.tenant_id
        or actor.get("actor_id") != row.actor_id
        or owner.tenant_id != row.tenant_id
        or owner.actor_id != row.actor_id
        or owner.session_id != row.session_id
        or content != owner.session_json.get("content")
        or feedback.get("session_id") != row.session_id
        or feedback.get("turn_id") != row.turn_id
        or feedback.get("source") != "provider"
        or feedback.get("degraded") is not False
        or feedback.get("fallback_reason") is not None
        or links
        != {
            "self": (
                f"/product-experience/v1/sessions/{row.session_id}/"
                f"agent-interactions/{row.interaction_id}"
            ),
            "session_workspace": f"/product-experience/v1/sessions/{row.session_id}/workspace",
            "skill_draft": None,
        }
    ):
        return False

    turn = await session.scalar(
        select(AgentTurnRow).where(
            AgentTurnRow.tenant_id == row.tenant_id,
            AgentTurnRow.actor_id == row.actor_id,
            AgentTurnRow.session_id == row.session_id,
            AgentTurnRow.turn_id == row.turn_id,
            AgentTurnRow.command_id == command_id,
        )
    )
    command_row = await session.scalar(
        select(CommandRow).where(
            CommandRow.tenant_id == row.tenant_id,
            CommandRow.actor_id == row.actor_id,
            CommandRow.command_id == command_id,
        )
    )
    command = (
        await validated_command_record(session, command_row) if command_row is not None else None
    )
    job = await session.scalar(
        select(WorkflowJobRow).where(
            WorkflowJobRow.tenant_id == row.tenant_id,
            WorkflowJobRow.command_id == command_id,
            WorkflowJobRow.subject_type == "AGENT_TURN",
            WorkflowJobRow.subject_id == row.turn_id,
        )
    )
    command_receipt = await session.scalar(
        select(IdempotencyReceiptRow).where(
            IdempotencyReceiptRow.tenant_id == row.tenant_id,
            IdempotencyReceiptRow.actor_id == row.actor_id,
            IdempotencyReceiptRow.operation == "EXECUTE_AGENT_TURN",
            IdempotencyReceiptRow.command_id == command_id,
        )
    )
    forbidden_runs = list(
        (
            await session.scalars(
                select(RunRow).where(
                    RunRow.tenant_id == row.tenant_id,
                    RunRow.actor_id == row.actor_id,
                    RunRow.session_id == row.session_id,
                    RunRow.command_id == command_id,
                )
            )
        ).all()
    )
    forbidden_evidence = list(
        (
            await session.scalars(
                select(EvidenceRow).where(
                    EvidenceRow.tenant_id == row.tenant_id,
                    EvidenceRow.actor_id == row.actor_id,
                    EvidenceRow.command_id == command_id,
                )
            )
        ).all()
    )
    request_events = list(
        (
            await session.scalars(
                select(EventRow).where(
                    EventRow.tenant_id == row.tenant_id,
                    EventRow.event_json["command_id"].astext == command_id,
                )
            )
        ).all()
    )
    forbidden_learner_jobs = list(
        (
            await session.scalars(
                select(LearnerProjectionJobRow).where(
                    LearnerProjectionJobRow.tenant_id == row.tenant_id,
                    LearnerProjectionJobRow.command_id == command_id,
                )
            )
        ).all()
    )
    forbidden_world_events = list(
        (
            await session.scalars(
                select(WorldPresentationEventRow).where(
                    WorldPresentationEventRow.tenant_id == row.tenant_id,
                    WorldPresentationEventRow.command_id == command_id,
                )
            )
        ).all()
    )
    if (
        turn is None
        or command is None
        or job is None
        or command_receipt is None
        or forbidden_runs
        or forbidden_evidence
        or forbidden_learner_jobs
        or forbidden_world_events
    ):
        return False
    turn_input = turn.request_json.get("input")
    expected_context = request_context_data(command.request_context)
    if (
        origin != expected_context
        or turn.tenant_id != row.tenant_id
        or turn.actor_id != row.actor_id
        or turn.session_id != row.session_id
        or not isinstance(turn_input, Mapping)
        or turn_input.get("type") != "MESSAGE"
        or turn.request_json.get("skill_bindings") != []
        or command.command_type != "EXECUTE_AGENT_TURN"
        or command.status is not CommandStatus.APPLIED
        or command.stage != "COMPLETE"
        or not command.terminal
        or command.updated_at != created_at
        or command.result != {"result_type": "NO_EFFECT", "reason_code": "HINT_DELIVERED"}
        or command.error is not None
        or command.evidence_refs != ()
        or command.links != {"self": f"/v1/commands/{command_id}"}
        or command_receipt.request_sha256 != job.request_sha256
        or command_receipt.accepted_at != command.accepted_at
    ):
        return False

    receipts = list(
        (
            await session.scalars(
                select(JobStepReceiptRow).where(
                    JobStepReceiptRow.tenant_id == row.tenant_id,
                    JobStepReceiptRow.job_id == job.job_id,
                )
            )
        ).all()
    )
    try:
        provider_results = await load_hint_provider_receipts(session, job)
    except WorkflowInvariantError:
        return False
    provider_names = {
        name
        for result in provider_results
        for name in (result.step_name, result.step_name.replace("_RESULT_", "_DISPATCH_"))
    }
    required_receipts = {*provider_names, "HINT_DECISION_DERIVED", "TURN_COMPLETED"}
    if not _patch_job_receipts_have_authority(receipts, job, required_receipts):
        return False
    receipts_by_name = {item.step_name: item for item in receipts}
    terminal = receipts_by_name["TURN_COMPLETED"]
    derived = receipts_by_name["HINT_DECISION_DERIVED"]
    provider_result = provider_results[-1]
    provider_dispatch = receipts_by_name[
        provider_result.step_name.replace("_RESULT_", "_DISPATCH_")
    ]
    decision = derived.receipt_json.get("decision")
    if set(derived.receipt_json) != {"decision"} or not isinstance(decision, Mapping):
        return False
    durable_draft = decision.get("draft")
    directive = decision.get("teaching_directive")
    try:
        # The frozen decision renders its instant with an explicit UTC offset
        # while the public feedback renders the same instant with "Z"; compare
        # the instants, never the two spellings.
        decision_completed_at = _time_value(decision, "completed_at")
    except (KeyError, TypeError, ValueError):
        return False
    if (
        not isinstance(durable_draft, Mapping)
        or not isinstance(directive, Mapping)
        or derived.input_sha256 != terminal.input_sha256
        or decision.get("source") != "provider"
        or decision.get("degraded") is not False
        or decision.get("fallback_reason") is not None
        # A hint owns no Evidence -- `forbidden_evidence` below still proves no
        # Evidence row was written under this Command -- but it may cite the
        # compile rejection it was answering. The citation has to be identical
        # everywhere it is repeated, or the record would disagree with itself.
        or decision.get("evidence_refs") != feedback.get("evidence_refs")
        or decision.get("message_key") != feedback.get("message_key")
        or decision_completed_at != completed_at
        or directive.get("patch_eligible") is not False
        or directive.get("full_solution_eligible") is not False
        or durable_draft.get("role") != role
        or durable_draft.get("response_type") != response_type
        or durable_draft.get("question") != question
        or durable_draft.get("hint_level") != hint_level
        or durable_draft.get("skill_patch") is not None
        or durable_draft.get("requires_student_confirmation") is not False
        or durable_draft.get("message") != feedback.get("message")
        or any(
            not isinstance(item, Mapping) or item.get("name") == "invoke_skill"
            for item in cast(list[Any], decision.get("tool_calls") or [])
        )
    ):
        return False
    try:
        provider_result_authority = _object_value(provider_result.receipt_json, "dispatch")
        validate_provider_decision_wire(
            provider_results,
            decision_draft=dict(durable_draft),
            # The Evidence this hint is allowed to cite is the Evidence it did
            # cite, which the checks above already pinned to the feedback. It was
            # hard-coded empty when a hint could only ever ask a question; now a
            # hint that answers a compile rejection names it, and declaring none
            # allowed would reject exactly the hints that talk about a real
            # failure -- the ones this whole path exists to deliver.
            evidence_refs=_evidence_refs_from_wire(decision.get("evidence_refs")),
            decision=dict(decision),
        )
    except (KeyError, TypeError, WorkflowInvariantError):
        return False
    if (
        receipt_id != terminal.receipt_id
        or terminal.receipt_id
        != workflow_step_receipt_id(row.tenant_id, job.job_id, "TURN_COMPLETED")
        or terminal.receipt_json != dict(source)
        or terminal.output_sha256 != workflow_receipt_sha256(dict(source))
        or provider_dispatch.input_sha256 != provider_result.input_sha256
        or provider_dispatch.receipt_json.get("command_id") != command_id
        or provider_dispatch.receipt_json.get("turn_id") != row.turn_id
        or provider_result_authority.get("request_sha256") != provider_result.input_sha256
        or provider_result_authority.get("generation_count") != 1
        or provider_result_authority.get("state") != "SUCCEEDED"
        or job.status != "SUCCEEDED"
        or job.phase != "COMPLETE"
        or job.lease_owner is not None
        or job.lease_expires_at is not None
        or job.operation != "EXECUTE_AGENT_TURN"
        or job.job_json.get("request_context") != origin
        or job.job_json.get("session_id") != row.session_id
        or job.job_json.get("turn_id") != row.turn_id
    ):
        return False

    runtime_event_row = await session.scalar(
        select(EventRow).where(
            EventRow.tenant_id == row.tenant_id,
            EventRow.event_id == event_id,
        )
    )
    runtime_event = _runtime_event(runtime_event_row) if runtime_event_row is not None else None
    feedback_sha256 = canonical_json_sha256(dict(feedback))
    source_projection = dict(source)
    retained_source_sha256 = source_projection.pop("source_sha256", None)
    retained_event = public_domain_event_data(runtime_event) if runtime_event is not None else None
    event_payload = retained_event.pop("payload", None) if retained_event is not None else None
    if retained_event is not None:
        retained_event["feedback_sha256"] = feedback_sha256
    return not (
        runtime_event is None
        or len(request_events) != 1
        or request_events[0].event_id != event_id
        or runtime_event.event_type != RuntimeEventType.AGENT_TURN_FEEDBACK_READY.value
        or runtime_event.command_id != command_id
        or runtime_event.stream_id != f"agent-session:{row.session_id}"
        or runtime_event.occurred_at != completed_at
        or event_payload != feedback
        or retained_event != feedback_event
        or source.get("source_type") != "AGENT_TURN_PRODUCT_PROJECTION"
        or source.get("source_revision") != 1
        or source.get("actor") != actor
        or source.get("content_ref") != content
        or source.get("interaction_id") != row.interaction_id
        or source.get("session_id") != row.session_id
        or source.get("turn_id") != row.turn_id
        or source.get("sequence") != row.sequence
        or source.get("command_id") != command_id
        or source.get("feedback_event_id") != event_id
        or source.get("feedback_sha256") != feedback_sha256
        or source.get("skill_patch_sha256") is not None
        or source.get("committed_at") != value.get("created_at")
        or retained_source_sha256 != canonical_json_sha256(source_projection)
        or any(
            source.get(field) != value.get(field)
            for field in ("role", "response_type", "question", "hint_level")
        )
    )


def _patch_job_receipts_have_authority(
    receipts: Sequence[JobStepReceiptRow],
    job: WorkflowJobRow,
    required_receipts: set[str],
) -> bool:
    """Allow only one no-Run projection's terminal receipts plus closed waits."""

    receipts_by_name = {item.step_name: item for item in receipts}
    if (
        len(receipts_by_name) != len(receipts)
        or not required_receipts.issubset(receipts_by_name)
    ):
        return False
    for item in receipts:
        if (
            item.output_sha256 != workflow_receipt_sha256(item.receipt_json)
            or item.fencing_token <= 0
            or item.fencing_token > job.fencing_token
        ):
            return False
        if item.step_name in required_receipts:
            continue
        if not _patch_reconciliation_receipt_has_authority(item, job):
            return False
    return True


def _patch_reconciliation_receipt_has_authority(
    receipt: JobStepReceiptRow, job: WorkflowJobRow
) -> bool:
    reachable_exception_types = {
        "DurableLlmDispatchPending",
        "DurableLlmDispatchUnknown",
        "DurableLlmReceiptCommitUnknown",
    }
    prefix = "WORKER_RECONCILE_"
    if not receipt.step_name.startswith(prefix):
        return False
    token_text = receipt.step_name.removeprefix(prefix)
    if not token_text.isdecimal() or token_text != str(receipt.fencing_token):
        return False
    payload = receipt.receipt_json
    if not isinstance(payload, Mapping):
        return False
    required_keys = {"code", "exception_type", "attempt"}
    if set(payload) not in (required_keys, required_keys | {"retry_after_seconds"}):
        return False
    attempt = payload.get("attempt")
    retry_after_seconds = payload.get("retry_after_seconds")
    return (
        receipt.input_sha256 == job.request_sha256
        and payload.get("code") == "WORKFLOW_EXECUTION_FAILED"
        and payload.get("exception_type") in reachable_exception_types
        and not isinstance(attempt, bool)
        and isinstance(attempt, int)
        and attempt == receipt.fencing_token
        and (
            retry_after_seconds is None
            or (
                not isinstance(retry_after_seconds, bool)
                and isinstance(retry_after_seconds, int)
                and 1 <= retry_after_seconds <= 86_400
            )
        )
    )


def _proposal_agent_authority_matches(
    proposal: ProductSkillPatchProposalRow,
    owner: AgentSessionRow,
    turn: AgentTurnRow,
    patch: Mapping[str, Any],
) -> bool:
    value = proposal.agent_proposal_json
    try:
        target = _object_value(value, "target")
        request = _object_value(value, "request")
        failed = _object_value(value, "failed")
        skill_ref = _object_value(failed, "skill_ref")
        operation = _object_value(value, "operation")
    except (KeyError, TypeError):
        return False
    digest = _agent_proposal_sha256(value)
    public_operation = {
        "operation": "UPSERT_FILE",
        "path": operation.get("path"),
        "previous_content_sha256": operation.get("previous_content_sha256"),
        "content": operation.get("content"),
        "content_sha256": operation.get("content_sha256"),
    }
    return (
        digest == proposal.agent_proposal_sha256
        and proposal.agent_proposal_id == f"patch_{digest[:32]}"
        and value.get("proposal_id") == proposal.agent_proposal_id
        and value.get("proposal_sha256") == digest
        and proposal.patch_sha256 == patch.get("patch_sha256")
        and _patch_hash(patch) == proposal.patch_sha256
        and patch.get("interaction_id") == proposal.interaction_id
        and patch.get("session_id") == proposal.session_id
        and patch.get("turn_id") == proposal.turn_id
        and patch.get("draft_id") == proposal.draft_id
        and patch.get("skill_id") == proposal.skill_id
        and patch.get("base_draft_revision") == proposal.base_draft_revision
        and patch.get("base_draft_sha256") == proposal.base_draft_sha256
        and patch.get("result_draft_sha256") == proposal.result_draft_sha256
        and patch.get("operations") == [public_operation]
        and patch.get("rationale") == value.get("rationale")
        and _agent_evidence_refs_match_projection(
            failed.get("evidence_refs"), patch.get("evidence_refs")
        )
        and patch.get("requires_student_confirmation") is True
        and target.get("draft_id") == proposal.draft_id
        and target.get("session_id") == proposal.session_id
        and target.get("skill_id") == proposal.skill_id
        and target.get("draft_revision") == proposal.base_draft_revision
        and target.get("draft_sha256") == proposal.base_draft_sha256
        and target.get("source_bundle_sha256") == proposal.source_bundle_sha256
        and target.get("entrypoint") == proposal.entrypoint
        and target.get("entrypoint_sha256") == proposal.entrypoint_sha256
        and request.get("tenant_id") == proposal.tenant_id
        and request.get("actor_id") == proposal.actor_id
        and request.get("actor_type") == ActorType.STUDENT.value
        and request.get("session_id") == proposal.session_id
        and request.get("task_id") == proposal.task_id
        and request.get("turn_id") == proposal.turn_id == turn.turn_id
        and request.get("command_id") == proposal.request_command_id == turn.command_id
        and request.get("requested_interaction_id")
        == proposal.requested_interaction_id
        and failed.get("tenant_id") == proposal.tenant_id
        and failed.get("actor_id") == proposal.actor_id
        and failed.get("session_id") == proposal.session_id
        and failed.get("interaction_id") == proposal.requested_interaction_id
        and failed.get("interaction_revision")
        == proposal.requested_interaction_revision
        and failed.get("interaction_sequence")
        == proposal.requested_interaction_sequence
        and failed.get("same_failure_suffix_end_sequence")
        == proposal.requested_failure_suffix_end_sequence
        and failed.get("turn_id") == proposal.failed_turn_id
        and failed.get("command_id") == proposal.failed_command_id
        and failed.get("task_id") == proposal.task_id
        and failed.get("world_id") == proposal.world_id == owner.world_id
        and skill_ref.get("skill_id") == proposal.skill_id
        and failed.get("failure_count") == proposal.failure_count
        and proposal.failure_count >= 4
        and failed.get("failure_key") == proposal.failure_key
        and failed.get("build_id") == proposal.failed_build_id
        and failed.get("run_id") == proposal.failed_run_id
        and failed.get("feedback_event_id") == proposal.feedback_event_id
        and failed.get("projection_receipt_id") == proposal.projection_receipt_id
        and operation.get("operation_type") == "UPSERT_FILE"
        and operation.get("path") == proposal.entrypoint
        and operation.get("previous_content_sha256")
        == proposal.previous_content_sha256
        and operation.get("content_sha256") == proposal.content_sha256
        and isinstance(operation.get("content"), str)
        and hashlib.sha256(operation["content"].encode("utf-8")).hexdigest()
        == proposal.content_sha256
    )


def _patch_provider_decision_draft(
    interaction: Mapping[str, Any],
    proposal: ProductSkillPatchProposalRow,
) -> dict[str, Any] | None:
    """Rebuild the raw Patch draft fields retained by the trusted projection."""

    feedback = interaction.get("feedback")
    patch = interaction.get("skill_patch")
    operation = proposal.agent_proposal_json.get("operation")
    if (
        not isinstance(feedback, Mapping)
        or not isinstance(patch, Mapping)
        or not isinstance(operation, Mapping)
    ):
        return None
    return {
        "role": interaction.get("role"),
        "response_type": interaction.get("response_type"),
        "message": feedback.get("message"),
        "question": interaction.get("question"),
        "hint_level": interaction.get("hint_level"),
        "learner_inference": None,
        "skill_patch": {
            "replacement_content": operation.get("content"),
            "rationale": proposal.agent_proposal_json.get("rationale"),
        },
        "requires_student_confirmation": patch.get(
            "requires_student_confirmation"
        ),
    }


async def _failed_run_has_full_authority(
    session: AsyncSession,
    proposal: ProductSkillPatchProposalRow,
    evidence_refs: object,
) -> bool:
    """Close a Proposal's selected failure through the public Run/Evidence path."""

    command_row = await session.scalar(
        select(CommandRow).where(
            CommandRow.tenant_id == proposal.tenant_id,
            CommandRow.actor_id == proposal.actor_id,
            CommandRow.command_id == proposal.failed_command_id,
        )
    )
    if command_row is None:
        return False
    command = await validated_command_record(session, command_row)
    if command is None:
        return False
    origin = command.request_context
    context = OperationContext(
        request_id=origin.request_id,
        correlation_id=origin.correlation_id,
        trace_id=origin.trace_id,
        requested_at=origin.requested_at,
        actor=origin.actor,
        content_ref=origin.content_ref,
        schema_version=origin.schema_version,
        command_id=command.command_id,
        causation_id=None,
        deadline_at=None,
    )
    try:
        authority = await load_validated_run(
            session,
            tenant_id=proposal.tenant_id,
            actor_id=proposal.actor_id,
            content_hash=origin.content_ref.content_hash,
            command_id=proposal.failed_command_id,
            expected_context=context,
            require_current_world=False,
        )
    except WorkflowInvariantError:
        return False
    run = authority.run
    return (
        run.run_id == proposal.failed_run_id
        and run.session_id == proposal.session_id
        and run.turn_id == proposal.failed_turn_id
        and run.command_id == proposal.failed_command_id
        and run.world_id == proposal.world_id
        and run.skill_ref.skill_id == proposal.skill_id
        and run.task_success is False
        and run.failure_key == proposal.failure_key
        and _evidence_refs_match(run.evidence_refs, evidence_refs)
    )


async def _proposal_failure_authority_matches(
    session: AsyncSession,
    proposal: ProductSkillPatchProposalRow,
    selected: ProductInteractionRow,
    feedback: Mapping[str, Any],
    command: CommandRecord,
) -> bool:
    selected_value = selected.interaction_json
    selected_feedback = selected_value.get("feedback")
    selected_source = selected_value.get("projection_source")
    failed = proposal.agent_proposal_json.get("failed")
    if (
        not isinstance(selected_feedback, Mapping)
        or not isinstance(selected_source, Mapping)
        or not isinstance(failed, Mapping)
        or selected.turn_id != proposal.failed_turn_id
        or selected_feedback.get("command_id") != proposal.failed_command_id
        or selected_feedback.get("run_id") != proposal.failed_run_id
        or selected_source.get("feedback_event_id") != proposal.feedback_event_id
        or selected_source.get("receipt_id") != proposal.projection_receipt_id
        or feedback.get("evidence_refs") != selected_feedback.get("evidence_refs")
        or not _evidence_refs_match(command.evidence_refs, feedback.get("evidence_refs"))
        or not _agent_evidence_refs_match_projection(
            failed.get("evidence_refs"), feedback.get("evidence_refs")
        )
    ):
        return False
    build = await session.scalar(
        select(SkillBuildRow).where(
            SkillBuildRow.tenant_id == proposal.tenant_id,
            SkillBuildRow.actor_id == proposal.actor_id,
            SkillBuildRow.build_id == proposal.failed_build_id,
        )
    )
    build_provenance = await session.scalar(
        select(SkillBuildProvenanceRow).where(
            SkillBuildProvenanceRow.build_id == proposal.failed_build_id,
            SkillBuildProvenanceRow.tenant_id == proposal.tenant_id,
            SkillBuildProvenanceRow.actor_id == proposal.actor_id,
            SkillBuildProvenanceRow.session_id == proposal.session_id,
            SkillBuildProvenanceRow.draft_revision_row_id
            == proposal.base_draft_revision_row_id,
        )
    )
    run = await session.scalar(
        select(RunRow).where(
            RunRow.tenant_id == proposal.tenant_id,
            RunRow.actor_id == proposal.actor_id,
            RunRow.session_id == proposal.session_id,
            RunRow.run_id == proposal.failed_run_id,
            RunRow.command_id == proposal.failed_command_id,
        )
    )
    run_provenance = await session.scalar(
        select(SkillRunProvenanceRow).where(
            SkillRunProvenanceRow.run_id == proposal.failed_run_id,
            SkillRunProvenanceRow.build_id == proposal.failed_build_id,
            SkillRunProvenanceRow.tenant_id == proposal.tenant_id,
            SkillRunProvenanceRow.actor_id == proposal.actor_id,
            SkillRunProvenanceRow.session_id == proposal.session_id,
            SkillRunProvenanceRow.draft_revision_row_id
            == proposal.base_draft_revision_row_id,
        )
    )
    validated_failed_build = (
        await validate_run_provenance(
            session, run_provenance, require_immutable=True
        )
        if run_provenance is not None
        else None
    )
    evidence_refs = feedback.get("evidence_refs")
    if (
        build is None
        or build_provenance is None
        or run is None
        or run_provenance is None
        or validated_failed_build is None
        or validated_failed_build.build_id != build_provenance.build_id
        or validated_failed_build.authority_sha256
        != build_provenance.authority_sha256
        or build.status != "CERTIFIED"
        or not build.terminal
        or run.run_json.get("status") not in {"FAILED", "REJECTED"}
        or build_provenance.draft_sha256 != proposal.base_draft_sha256
        or build_provenance.source_bundle_sha256 != proposal.source_bundle_sha256
        or run_provenance.draft_sha256 != proposal.base_draft_sha256
        or run_provenance.assistance_authority
        != build_provenance.assistance_authority
        or not isinstance(evidence_refs, list)
        or not evidence_refs
        or not await _failed_run_has_full_authority(
            session, proposal, evidence_refs
        )
    ):
        return False
    links = list(
        (
            await session.scalars(
                select(ProductSkillPatchEvidenceRow).where(
                    ProductSkillPatchEvidenceRow.patch_id == proposal.patch_id
                )
            )
        ).all()
    )
    evidence_ids = [
        item.get("evidence_id") for item in evidence_refs if isinstance(item, Mapping)
    ]
    evidence_rows = list(
        (
            await session.scalars(
                select(EvidenceRow).where(
                    EvidenceRow.tenant_id == proposal.tenant_id,
                    EvidenceRow.actor_id == proposal.actor_id,
                    EvidenceRow.evidence_id.in_(evidence_ids),
                )
            )
        ).all()
    )
    by_link = {item.evidence_id: item for item in links}
    by_row = {item.evidence_id: item for item in evidence_rows}
    refs = {
        cast(str, item.get("evidence_id")): item
        for item in evidence_refs
        if isinstance(item, Mapping) and isinstance(item.get("evidence_id"), str)
    }
    return (
        len(refs) == len(evidence_refs)
        and set(refs) == set(by_link) == set(by_row)
        and all(
            by_link[evidence_id].evidence_ref_json == dict(item)
            and by_link[evidence_id].evidence_sha256 == item.get("sha256")
            and by_row[evidence_id].evidence_json.get("evidence_ref") == dict(item)
            for evidence_id, item in refs.items()
        )
    )


def _interaction_authority_matches(
    row: ProductInteractionRow,
    owner: AgentSessionRow,
    authority: _InteractionAuthority,
) -> bool:
    value = row.interaction_json
    try:
        origin = _object_value(value, "request_context")
        actor = _object_value(origin, "actor")
        content = _object_value(origin, "content_ref")
        source = _object_value(value, "projection_source")
        feedback = _object_value(value, "feedback")
        feedback_event = _object_value(value, "feedback_event")
        links = _object_value(value, "links")
        created_at = _time_value(value, "created_at")
        updated_at = _time_value(value, "updated_at")
        feedback_completed_at = _time_value(feedback, "completed_at")
        runtime_event = _runtime_event(authority.event)
        command = authority.command
        if runtime_event is None:
            return False
        feedback_sha256 = canonical_json_sha256(dict(feedback))
        source_projection = dict(source)
        retained_source_sha256 = source_projection.pop("source_sha256", None)
        source_sha256 = canonical_json_sha256(source_projection)
        retained_event = public_domain_event_data(runtime_event)
        event_payload = retained_event.pop("payload")
        retained_event["feedback_sha256"] = feedback_sha256
        run_origin = _object_value(authority.run.run_json, "request_context")
        patch = value.get("skill_patch")
        patch_sha256 = (
            _text_value(cast(Mapping[str, Any], patch), "patch_sha256")
            if isinstance(patch, Mapping)
            else None
        )
    except (KeyError, TypeError, ValueError):
        return False

    expected_interaction_id = _scoped_identifier(
        "interaction", row.tenant_id, authority.job.job_id
    )
    run_status = authority.run.run_json.get("status")
    expected_command_status = (
        CommandStatus.APPLIED
        if run_status == "SUCCEEDED"
        else (
            CommandStatus.REJECTED
            if run_status in {"REJECTED", "FAILED"}
            else None
        )
    )
    expected_command_stage = _expected_terminal_command_stage(authority.run.run_json)
    if (
        value.get("session_id") != row.session_id
        or row.session_id != owner.session_id
        or value.get("interaction_id") != row.interaction_id
        or row.interaction_id != expected_interaction_id
        or value.get("turn_id") != row.turn_id
        or row.turn_id != authority.turn.turn_id
        or value.get("sequence") != row.sequence
        or value.get("interaction_revision") != row.interaction_revision
        or created_at != row.created_at
        or updated_at != row.updated_at
        or actor.get("tenant_id") != row.tenant_id
        or actor.get("actor_id") != row.actor_id
        or owner.tenant_id != row.tenant_id
        or owner.actor_id != row.actor_id
        or content != owner.session_json.get("content")
        or origin != request_context_data(command.request_context)
    ):
        return False

    if (
        authority.turn.tenant_id != row.tenant_id
        or authority.turn.actor_id != row.actor_id
        or authority.turn.session_id != row.session_id
        or authority.turn.command_id != command.command_id
        or command.command_id != authority.turn.command_id
        or command.command_type != "EXECUTE_AGENT_TURN"
        or expected_command_status is None
        or expected_command_stage is None
        or command.status is not expected_command_status
        or not command.terminal
        or command.stage != expected_command_stage
        or command.updated_at != created_at
        or command.links.get("run") != f"/v1/runs/{authority.run.run_id}"
    ):
        return False

    run = authority.run
    run_value = run.run_json
    if (
        run.tenant_id != row.tenant_id
        or run.actor_id != row.actor_id
        or run.content_hash != content.get("content_hash")
        or run.session_id != row.session_id
        or run.turn_id != row.turn_id
        or run.command_id != command.command_id
        or run_value.get("run_id") != run.run_id
        or run_value.get("session_id") != row.session_id
        or run_value.get("turn_id") != row.turn_id
        or run_value.get("command_id") != command.command_id
        or run_origin != origin
        or run_value.get("agent_feedback") != feedback
        or run_value.get("evidence_refs") != feedback.get("evidence_refs")
        or not _evidence_refs_match(command.evidence_refs, feedback.get("evidence_refs"))
        or feedback.get("source") != "provider"
        or feedback.get("degraded") is not False
        or feedback.get("fallback_reason") is not None
        or feedback.get("session_id") != row.session_id
        or feedback.get("turn_id") != row.turn_id
        or feedback.get("command_id") != command.command_id
        or feedback.get("run_id") != run.run_id
    ):
        return False

    if (
        runtime_event.event_type != RuntimeEventType.AGENT_TURN_FEEDBACK_READY.value
        or runtime_event.event_version != 1
        or runtime_event.stream_id != f"agent-session:{row.session_id}"
        or runtime_event.command_id != command.command_id
        or runtime_event.content_ref.unit_id != content.get("unit_id")
        or runtime_event.content_ref.version != content.get("version")
        or runtime_event.content_ref.content_hash != content.get("content_hash")
        or runtime_event.occurred_at != feedback_completed_at
        or event_payload != feedback
        or feedback_event != retained_event
    ):
        return False

    if (
        source.get("source_type") != "AGENT_TURN_PRODUCT_PROJECTION"
        or source.get("source_revision") != 1
        or source.get("actor") != actor
        or source.get("content_ref") != content
        or source.get("interaction_id") != row.interaction_id
        or source.get("session_id") != row.session_id
        or source.get("turn_id") != row.turn_id
        or source.get("sequence") != row.sequence
        or source.get("command_id") != command.command_id
        or source.get("feedback_event_id") != runtime_event.event_id
        or source.get("feedback_sha256") != feedback_sha256
        or retained_source_sha256 != source_sha256
        or source.get("committed_at") != value.get("created_at")
        or any(
            source.get(field) != value.get(field)
            for field in ("role", "response_type", "question", "hint_level")
        )
        or source.get("skill_patch_sha256") != patch_sha256
    ):
        return False

    receipt = authority.receipt
    job = authority.job
    if (
        source.get("receipt_id") != receipt.receipt_id
        or receipt.receipt_id
        != workflow_step_receipt_id(row.tenant_id, job.job_id, "TURN_COMPLETED")
        or receipt.tenant_id != row.tenant_id
        or receipt.job_id != job.job_id
        or receipt.step_name != "TURN_COMPLETED"
        or receipt.fencing_token != job.fencing_token
        or receipt.output_sha256 != workflow_receipt_sha256(dict(source))
        or receipt.receipt_json != dict(source)
        or receipt.completed_at > job.updated_at
        or job.command_id != command.command_id
        or job.operation != "EXECUTE_AGENT_TURN"
        or job.subject_type != "AGENT_TURN"
        or job.subject_id != row.turn_id
        or job.status != "SUCCEEDED"
        or job.phase != "COMPLETE"
        or job.lease_owner is not None
        or job.lease_expires_at is not None
        or job.job_json.get("request_context") != origin
        or job.job_json.get("session_id") != row.session_id
        or job.job_json.get("turn_id") != row.turn_id
    ):
        return False
    if not _job_and_invocation_authority_matches(
        row,
        owner=owner,
        origin=origin,
        run_value=run_value,
        authority=authority,
    ):
        return False

    if links != {
        "self": (
            f"/product-experience/v1/sessions/{row.session_id}/"
            f"agent-interactions/{row.interaction_id}"
        ),
        "session_workspace": f"/product-experience/v1/sessions/{row.session_id}/workspace",
        "skill_draft": (
            f"/product-experience/v1/sessions/{row.session_id}/skill-drafts/"
            f"{authority.draft.draft_id}"
            if authority.draft is not None
            else None
        ),
    }:
        return False
    if authority.draft is not None and not _draft_authority_matches(authority.draft, owner):
        return False
    return _patch_authority_matches(
        row,
        origin=origin,
        patch=value.get("skill_patch"),
        decision=value.get("patch_decision"),
        authority=authority,
        created_at=created_at,
        updated_at=updated_at,
    )


def _runtime_event(row: EventRow) -> RuntimeEvent | None:
    value = row.event_json
    try:
        content = _object_value(value, "content_ref")
        event = RuntimeEvent(
            event_id=_text_value(value, "event_id"),
            event_type=_text_value(value, "event_type"),
            event_version=_integer_value(value, "event_version"),
            stream_id=_text_value(value, "stream_id"),
            sequence=_integer_value(value, "sequence"),
            occurred_at=_time_value(value, "occurred_at"),
            producer=_text_value(value, "producer"),
            trace_id=_text_value(value, "trace_id"),
            command_id=_text_value(value, "command_id"),
            correlation_id=_text_value(value, "correlation_id"),
            causation_id=(
                _text_value(value, "causation_id")
                if value.get("causation_id") is not None
                else None
            ),
            content_ref=ContentRef(
                unit_id=_text_value(content, "unit_id"),
                version=_text_value(content, "version"),
                content_hash=_text_value(content, "content_hash"),
            ),
            payload=cast(FrozenJsonObject, value["payload"]),
            schema_version=_text_value(value, "schema_version"),
        )
    except (KeyError, TypeError, ValueError):
        return None
    if (
        event.event_id != row.event_id
        or event.stream_id != row.stream_id
        or event.sequence != row.sequence
        or event.occurred_at != row.occurred_at
        or domain_event_data(event) != value
    ):
        return None
    return event


def _evidence_refs_match(
    authoritative: Sequence[EvidenceRef], projected: object
) -> bool:
    if not isinstance(projected, list) or len(projected) != len(authoritative):
        return False
    for reference, item in zip(authoritative, projected, strict=True):
        if not isinstance(item, Mapping):
            return False
        expected_keys = {"evidence_id", "evidence_type", "created_at"}
        if reference.sha256 is not None:
            expected_keys.add("sha256")
        if reference.uri is not None:
            expected_keys.add("uri")
        try:
            created_at = _time_value(item, "created_at")
        except (KeyError, TypeError, ValueError):
            return False
        if (
            set(item) != expected_keys
            or item.get("evidence_id") != reference.evidence_id
            or item.get("evidence_type") != reference.evidence_type.value
            or created_at != reference.created_at
            or item.get("sha256") != reference.sha256
            or item.get("uri") != reference.uri
        ):
            return False
    return True


def _agent_evidence_refs_match_projection(
    authoritative: object, projected: object
) -> bool:
    """Compare Agent dataclass JSON with the frozen Product Evidence wire.

    Agent's internal JSON retains nullable ``uri`` and emits an ISO ``+00:00``
    timestamp.  The frozen Product Evidence wire (the v0.4-compatible shape)
    omits null optionals and canonicalizes UTC as ``Z``.  Both representations
    still have to close field-for-field.
    """

    if (
        not isinstance(authoritative, list)
        or not isinstance(projected, list)
        or len(authoritative) != len(projected)
    ):
        return False
    for source, item in zip(authoritative, projected, strict=True):
        if not isinstance(source, Mapping) or not isinstance(item, Mapping):
            return False
        if set(source) != {
            "evidence_id",
            "evidence_type",
            "created_at",
            "sha256",
            "uri",
        }:
            return False
        expected_keys = {"evidence_id", "evidence_type", "created_at"}
        if source.get("sha256") is not None:
            expected_keys.add("sha256")
        if source.get("uri") is not None:
            expected_keys.add("uri")
        try:
            source_created_at = _time_value(source, "created_at")
            projected_created_at = _time_value(item, "created_at")
        except (KeyError, TypeError, ValueError):
            return False
        if (
            set(item) != expected_keys
            or item.get("evidence_id") != source.get("evidence_id")
            or item.get("evidence_type") != source.get("evidence_type")
            or projected_created_at != source_created_at
            or item.get("sha256") != source.get("sha256")
            or item.get("uri") != source.get("uri")
        ):
            return False
    return True


def _job_and_invocation_authority_matches(
    row: ProductInteractionRow,
    *,
    owner: AgentSessionRow,
    origin: Mapping[str, Any],
    run_value: Mapping[str, Any],
    authority: _InteractionAuthority,
) -> bool:
    job = authority.job
    terminal_receipt = authority.receipt
    invocation_receipt = authority.invocation_receipt
    command_receipt = authority.command_receipt
    command = authority.command
    turn = authority.turn
    job_value = job.job_json
    turn_request = turn.request_json
    invocation_value = invocation_receipt.receipt_json
    try:
        invocation_run = _object_value(invocation_value, "run")
        job_request = _object_value(job_value, "request")
        client_state = _object_value(job_request, "client_state")
        sandbox = _object_value(run_value, "sandbox")
        world_application = _object_value(run_value, "world_application")
    except (KeyError, TypeError):
        return False
    bindings = turn_request.get("skill_bindings")
    if (
        not isinstance(bindings, list)
        or len(bindings) != 1
        or not isinstance(bindings[0], Mapping)
    ):
        return False
    binding = dict(bindings[0])
    turn_projection = {
        key: job_request.get(key)
        for key in ("expected_world_revision", "input", "skill_bindings")
    }
    invocation_id = invocation_value.get("invocation_id")
    expected_invocation_id = side_effect_execution_id(command.command_id, turn.turn_id)
    arguments = invocation_value.get("arguments")
    try:
        expected_invocation_request_sha256 = skill_invocation_request_sha256(
            tenant_id=row.tenant_id,
            invocation_id=_text_value(invocation_value, "invocation_id"),
            session_id=row.session_id,
            turn_id=row.turn_id,
            command_id=command.command_id,
            world_id=owner.world_id,
            expected_world_revision=_integer_value(turn_request, "expected_world_revision"),
            skill_ref=SkillRef(
                skill_id=_text_value(binding, "skill_id"),
                skill_version_id=_text_value(binding, "skill_version_id"),
                artifact_sha256=_text_value(binding, "artifact_sha256"),
                certification_id=_text_value(binding, "certification_id"),
            ),
            arguments=cast(FrozenJsonObject, dict(_object_value(invocation_value, "arguments"))),
        )
    except (KeyError, TypeError, ValueError):
        return False
    expected_run_id = (
        f"run_{hashlib.sha256(invocation_id.encode('utf-8')).hexdigest()[:24]}"
        if isinstance(invocation_id, str)
        else None
    )
    task_success = invocation_run.get("task_success")
    run_status = run_value.get("status")
    world_commit = invocation_run.get("world_commit")
    world_receipt = world_application.get("receipt")
    if (
        set(job_value)
        != {
            "schema_version",
            "turn_id",
            "session_id",
            "turn_sequence",
            "request",
            "request_context",
        }
        or job_value.get("schema_version") != "1.0.0"
        or job_value.get("turn_id") != row.turn_id
        or job_value.get("session_id") != row.session_id
        or job_value.get("turn_sequence") != turn.turn_sequence
        or job_value.get("request_context") != origin
        or set(job_request)
        != {
            "turn_id",
            "expected_world_revision",
            "input",
            "skill_bindings",
            "client_state",
        }
        or job_request.get("turn_id") != row.turn_id
        or job_request.get("expected_world_revision")
        != turn_request.get("expected_world_revision")
        or job_request.get("input") != turn_request.get("input")
        or job_request.get("skill_bindings") != bindings
        or turn_request not in (job_request, turn_projection)
        or set(client_state) != {"last_event_sequence", "client_turn_sequence"}
        or client_state.get("client_turn_sequence") != turn.turn_sequence
        or command_receipt.tenant_id != row.tenant_id
        or command_receipt.actor_id != row.actor_id
        or command_receipt.operation != "EXECUTE_AGENT_TURN"
        or command_receipt.command_id != command.command_id
        or command_receipt.accepted_at != command.accepted_at
        or command_receipt.request_sha256 != job.request_sha256
    ):
        return False
    if (
        invocation_receipt.receipt_id
        != workflow_step_receipt_id(row.tenant_id, job.job_id, "SKILL_INVOKED")
        or invocation_receipt.tenant_id != row.tenant_id
        or invocation_receipt.job_id != job.job_id
        or invocation_receipt.step_name != "SKILL_INVOKED"
        or invocation_receipt.fencing_token <= 0
        or invocation_receipt.fencing_token > terminal_receipt.fencing_token
        or invocation_receipt.completed_at > terminal_receipt.completed_at
        or invocation_receipt.input_sha256 != invocation_value.get("request_sha256")
        or invocation_receipt.input_sha256 != terminal_receipt.input_sha256
        or invocation_receipt.input_sha256 != expected_invocation_request_sha256
        or invocation_receipt.output_sha256
        != workflow_receipt_sha256(dict(invocation_value))
        or set(invocation_value)
        != {
            "schema_version",
            "invocation_id",
            "tenant_id",
            "request_sha256",
            "arguments",
            "run",
        }
        or invocation_value.get("schema_version") != "1.0.0"
        or invocation_value.get("tenant_id") != row.tenant_id
        or not isinstance(arguments, Mapping)
        or invocation_id != expected_invocation_id
        or sandbox.get("invocation_id") != invocation_id
        or expected_run_id != authority.run.run_id
    ):
        return False
    if (
        set(invocation_run)
        != {
            "run_id",
            "session_id",
            "turn_id",
            "command_id",
            "world_id",
            "skill_ref",
            "task_success",
            "world_revision_before",
            "world_revision_after",
            "world_difference",
            "failed_actions",
            "failure_key",
            "evidence_refs",
            "world_commit",
            "request_context",
        }
        or invocation_run.get("run_id") != authority.run.run_id
        or invocation_run.get("session_id") != row.session_id
        or invocation_run.get("turn_id") != row.turn_id
        or invocation_run.get("command_id") != command.command_id
        or invocation_run.get("world_id") != owner.world_id
        or invocation_run.get("skill_ref") != binding
        or invocation_run.get("skill_ref") != run_value.get("skill")
        or invocation_run.get("world_revision_before")
        != turn_request.get("expected_world_revision")
        or invocation_run.get("evidence_refs") != run_value.get("evidence_refs")
        or invocation_run.get("request_context") != origin
        or world_commit != world_receipt
        or command.links.get("world_snapshot")
        != f"/v1/worlds/{owner.world_id}/snapshot"
        or not isinstance(task_success, bool)
        or (task_success and run_status != "SUCCEEDED")
        or (not task_success and run_status not in {"REJECTED", "FAILED"})
    ):
        return False
    if world_commit is None:
        return world_application.get("status") != "COMMITTED"
    if not isinstance(world_commit, Mapping):
        return False
    return (
        world_application.get("status") == "COMMITTED"
        and world_commit.get("world_id") == owner.world_id
        and world_commit.get("previous_revision")
        == invocation_run.get("world_revision_before")
        and world_commit.get("world_revision")
        == invocation_run.get("world_revision_after")
    )


def _patch_authority_matches(
    row: ProductInteractionRow,
    *,
    origin: Mapping[str, Any],
    patch: object,
    decision: object,
    authority: _InteractionAuthority,
    created_at: datetime,
    updated_at: datetime,
) -> bool:
    receipt = authority.decision_receipt
    if patch is None:
        return (
            decision is None
            and authority.draft is None
            and receipt is None
            and row.interaction_revision == 1
            and updated_at == created_at
        )
    if not isinstance(patch, Mapping) or authority.draft is None:
        return False
    draft = authority.draft
    draft_value = draft.draft_json
    try:
        patch_id = _text_value(patch, "patch_id")
        draft_id = _text_value(patch, "draft_id")
        patch_sha256 = _text_value(patch, "patch_sha256")
        base_revision = _integer_value(patch, "base_draft_revision")
        base_sha256 = _text_value(patch, "base_draft_sha256")
        result_sha256 = _text_value(patch, "result_draft_sha256")
    except (KeyError, TypeError, ValueError):
        return False
    if (
        _patch_hash(patch) != patch_sha256
        or patch.get("interaction_id") != row.interaction_id
        or patch.get("session_id") != row.session_id
        or patch.get("turn_id") != row.turn_id
        or draft_id != draft.draft_id
        or patch.get("skill_id") != draft.skill_id
        or draft.tenant_id != row.tenant_id
        or draft.actor_id != row.actor_id
        or draft.session_id != row.session_id
        or draft_value.get("session_id") != draft.session_id
        or draft_value.get("draft_id") != draft.draft_id
        or draft_value.get("skill_id") != draft.skill_id
        or draft_value.get("content_ref") != origin.get("content_ref")
        or draft_value.get("revision") != draft.revision
        or draft_value.get("draft_sha256") != draft.draft_sha256
        or draft.revision < base_revision
        or (draft.revision == base_revision and draft.draft_sha256 != base_sha256)
    ):
        return False
    if decision is None:
        return receipt is None and row.interaction_revision == 1 and updated_at == created_at
    if not isinstance(decision, Mapping) or receipt is None:
        return False
    try:
        decided_at = _time_value(decision, "decided_at")
        before = _integer_value(decision, "interaction_revision_before")
        after = _integer_value(decision, "interaction_revision_after")
        draft_before = _integer_value(decision, "draft_revision_before")
        draft_after = _integer_value(decision, "draft_revision_after")
        receipt_actor = _object_value(_object_value(decision, "request_context"), "actor")
        receipt_content = _object_value(
            _object_value(decision, "request_context"), "content_ref"
        )
    except (KeyError, TypeError, ValueError):
        return False
    expected_path = (
        f"/product-experience/v1/sessions/{row.session_id}/agent-interactions/"
        f"{row.interaction_id}/patches/{patch_id}/decision"
    )
    if (
        decision.get("session_id") != row.session_id
        or decision.get("turn_id") != row.turn_id
        or decision.get("interaction_id") != row.interaction_id
        or decision.get("patch_id") != patch_id
        or decision.get("patch_sha256") != patch_sha256
        or decision.get("draft_id") != draft_id
        or decision.get("skill_id") != draft.skill_id
        or before != 1
        or after != before + 1
        or after != row.interaction_revision
        or draft_before != base_revision
        or decision.get("draft_sha256_before") != base_sha256
        or receipt_actor != origin.get("actor")
        or receipt_content != origin.get("content_ref")
        or decided_at != updated_at
        or receipt.tenant_id != row.tenant_id
        or receipt.actor_id != row.actor_id
        or receipt.canonical_path != expected_path
        or receipt.interaction_id != row.interaction_id
        or receipt.interaction_revision != row.interaction_revision
        or receipt.receipt_json != dict(decision)
        or receipt.created_at != decided_at
        or decision.get("links")
        != {
            "interaction": (
                f"/product-experience/v1/sessions/{row.session_id}/"
                f"agent-interactions/{row.interaction_id}"
            ),
            "skill_draft": (
                f"/product-experience/v1/sessions/{row.session_id}/skill-drafts/{draft_id}"
            ),
        }
    ):
        return False
    if decision.get("decision") == "ACCEPT":
        return (
            decision.get("reason_code") is None
            and decision.get("draft_updated") is True
            and draft_after == draft_before + 1
            and decision.get("draft_sha256_after") == result_sha256
            and draft.revision >= draft_after
            and (draft.revision != draft_after or draft.draft_sha256 == result_sha256)
        )
    if decision.get("decision") == "REJECT":
        return (
            isinstance(decision.get("reason_code"), str)
            and decision.get("draft_updated") is False
            and draft_after == draft_before
            and decision.get("draft_sha256_after") == base_sha256
            and draft.revision >= draft_after
            and (draft.revision != draft_after or draft.draft_sha256 == base_sha256)
        )
    return False


def _object_value(value: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    item = value[key]
    if not isinstance(item, Mapping):
        raise TypeError(f"{key} must be an object")
    return item


def _scoped_identifier(prefix: str, *parts: str) -> str:
    framed = "\x00".join((prefix, *parts)).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(framed).hexdigest()[:24]}"


class DecisionWrite:
    def __init__(self, receipt: dict[str, Any], interaction_revision: int, replayed: bool) -> None:
        self.receipt = receipt
        self.interaction_revision = interaction_revision
        self.replayed = replayed


async def _patch_decision_receipt_has_authority(
    session: AsyncSession,
    *,
    interaction_row: ProductInteractionRow,
    receipt: ProductPatchDecisionReceiptRow,
    request_hash: str,
) -> bool:
    """Re-close an ACK-loss replay to Decision, Draft, lineage and Workspace."""

    interaction = interaction_row.interaction_json
    decision_value = interaction.get("patch_decision")
    patch = interaction.get("skill_patch")
    if not isinstance(decision_value, Mapping) or not isinstance(patch, Mapping):
        return False
    decision = await session.scalar(
        select(ProductSkillPatchDecisionRow).where(
            ProductSkillPatchDecisionRow.decision_id == receipt.decision_id,
            ProductSkillPatchDecisionRow.tenant_id == interaction_row.tenant_id,
            ProductSkillPatchDecisionRow.actor_id == interaction_row.actor_id,
            ProductSkillPatchDecisionRow.session_id == interaction_row.session_id,
            ProductSkillPatchDecisionRow.interaction_id == interaction_row.interaction_id,
            ProductSkillPatchDecisionRow.patch_id == receipt.patch_id,
        )
    )
    proposal = await session.scalar(
        select(ProductSkillPatchProposalRow).where(
            ProductSkillPatchProposalRow.tenant_id == interaction_row.tenant_id,
            ProductSkillPatchProposalRow.actor_id == interaction_row.actor_id,
            ProductSkillPatchProposalRow.session_id == interaction_row.session_id,
            ProductSkillPatchProposalRow.interaction_id
            == interaction_row.interaction_id,
            ProductSkillPatchProposalRow.patch_id == receipt.patch_id,
        )
    )
    draft = (
        await session.scalar(
            select(ProductDraftRow).where(
                ProductDraftRow.tenant_id == interaction_row.tenant_id,
                ProductDraftRow.actor_id == interaction_row.actor_id,
                ProductDraftRow.session_id == interaction_row.session_id,
                ProductDraftRow.draft_id == proposal.draft_id,
            )
        )
        if proposal is not None
        else None
    )
    if (
        decision is None
        or proposal is None
        or draft is None
        or receipt.request_sha256 != request_hash
        or receipt.decision_id != decision.decision_id
        or receipt.patch_id != proposal.patch_id
        or receipt.interaction_id != interaction_row.interaction_id
        or receipt.interaction_revision != interaction_row.interaction_revision
        or receipt.receipt_json != dict(decision_value)
        or decision.receipt_json != dict(decision_value)
        or decision.request_sha256 != request_hash
        or decision.patch_id != patch.get("patch_id")
        or decision.base_draft_revision_row_id
        != proposal.base_draft_revision_row_id
        or decision.draft_id != proposal.draft_id
        or decision.decided_at != receipt.created_at
        or decision_value.get("decision_id") != decision.decision_id
        or decision_value.get("patch_id") != proposal.patch_id
        or decision_value.get("interaction_revision_after")
        != interaction_row.interaction_revision
        or interaction_row.updated_at != decision.decided_at
    ):
        return False
    workspace = await session.scalar(
        select(ProductWorkspaceRow).where(
            ProductWorkspaceRow.tenant_id == interaction_row.tenant_id,
            ProductWorkspaceRow.actor_id == interaction_row.actor_id,
            ProductWorkspaceRow.session_id == interaction_row.session_id,
        )
    )
    if workspace is None:
        return False
    refs = workspace.workspace_json.get("skill_draft_refs")
    interaction_high_watermark = await session.scalar(
        select(func.max(ProductInteractionRow.sequence)).where(
            ProductInteractionRow.tenant_id == interaction_row.tenant_id,
            ProductInteractionRow.actor_id == interaction_row.actor_id,
            ProductInteractionRow.session_id == interaction_row.session_id,
        )
    )
    expected_ref = {
        "draft_id": draft.draft_id,
        "skill_id": draft.skill_id,
        "revision": draft.revision,
        "draft_sha256": draft.draft_sha256,
        "url": (
            f"/product-experience/v1/sessions/{draft.session_id}/"
            f"skill-drafts/{draft.draft_id}"
        ),
    }
    if (
        not isinstance(refs, list)
        or expected_ref not in refs
        or workspace.workspace_json.get("last_interaction_sequence")
        != interaction_high_watermark
    ):
        return False
    if decision.decision == "REJECT":
        assistance = await session.scalar(
            select(ProductDraftRevisionAssistanceRow).where(
                ProductDraftRevisionAssistanceRow.patch_decision_id
                == decision.decision_id
            )
        )
        return (
            decision.reason_code is not None
            and decision.accepted_draft_revision_row_id is None
            and receipt.draft_revision_row_id is None
            and assistance is None
            and decision_value.get("decision") == "REJECT"
            and decision_value.get("draft_updated") is False
        )
    if decision.decision != "ACCEPT" or decision.reason_code is not None:
        return False
    accepted = await session.scalar(
        select(ProductDraftRevisionRow).where(
            ProductDraftRevisionRow.draft_revision_row_id
            == decision.accepted_draft_revision_row_id,
            ProductDraftRevisionRow.tenant_id == interaction_row.tenant_id,
            ProductDraftRevisionRow.actor_id == interaction_row.actor_id,
            ProductDraftRevisionRow.session_id == interaction_row.session_id,
            ProductDraftRevisionRow.draft_id == proposal.draft_id,
            ProductDraftRevisionRow.patch_id == proposal.patch_id,
        )
    )
    assistance = (
        await session.scalar(
            select(ProductDraftRevisionAssistanceRow).where(
                ProductDraftRevisionAssistanceRow.draft_revision_row_id
                == accepted.draft_revision_row_id,
                ProductDraftRevisionAssistanceRow.origin_accepted_revision_row_id
                == accepted.draft_revision_row_id,
                ProductDraftRevisionAssistanceRow.patch_id == proposal.patch_id,
                ProductDraftRevisionAssistanceRow.patch_decision_id
                == decision.decision_id,
                ProductDraftRevisionAssistanceRow.inherited.is_(False),
            )
        )
        if accepted is not None
        else None
    )
    return (
        accepted is not None
        and assistance is not None
        and receipt.draft_revision_row_id == accepted.draft_revision_row_id
        and accepted.parent_revision_row_id
        == proposal.base_draft_revision_row_id
        and accepted.revision == proposal.base_draft_revision + 1
        and accepted.draft_sha256 == proposal.result_draft_sha256
        and accepted.source_kind == "SKILL_PATCH"
        and decision_value.get("decision") == "ACCEPT"
        and decision_value.get("draft_updated") is True
        and decision_value.get("draft_sha256_after") == accepted.draft_sha256
        and draft.revision >= accepted.revision
    )


async def _load_patch_decision_authority(
    session: AsyncSession,
    *,
    tenant_id: str,
    actor_id: str,
    session_id: str,
    interaction_id: str,
    patch_id: str,
    interaction: Mapping[str, Any],
    patch: Mapping[str, Any],
    request_body: Mapping[str, Any],
) -> Result[
    tuple[
        ProductSkillPatchProposalRow,
        ProductDraftRow,
        ProductDraftRevisionRow,
        dict[str, Any],
    ]
]:
    """Re-close every persisted Patch/Draft/failed-Run identity before decision."""

    proposal = await session.scalar(
        select(ProductSkillPatchProposalRow).where(
            ProductSkillPatchProposalRow.tenant_id == tenant_id,
            ProductSkillPatchProposalRow.actor_id == actor_id,
            ProductSkillPatchProposalRow.session_id == session_id,
            ProductSkillPatchProposalRow.interaction_id == interaction_id,
            ProductSkillPatchProposalRow.patch_id == patch_id,
        )
    )
    if proposal is None:
        return Failure(
            _error(
                "INVARIANT_VIOLATION",
                "PATCH_DECISION",
                "Patch has no persisted proposal authority",
            )
        )
    agent_request = proposal.agent_proposal_json.get("request")
    agent_failed = proposal.agent_proposal_json.get("failed")
    if (
        not isinstance(agent_request, Mapping)
        or not isinstance(agent_failed, Mapping)
        or proposal.proposal_json != dict(patch)
        or _patch_hash(patch) != patch.get("patch_sha256")
        or proposal.patch_sha256 != patch.get("patch_sha256")
        or proposal.patch_id != patch.get("patch_id")
        or proposal.requested_interaction_id
        == proposal.interaction_id
        or proposal.base_draft_revision != patch.get("base_draft_revision")
        or proposal.base_draft_sha256 != patch.get("base_draft_sha256")
        or proposal.result_draft_sha256 != patch.get("result_draft_sha256")
        or proposal.draft_id != patch.get("draft_id")
        or proposal.skill_id != patch.get("skill_id")
        or proposal.turn_id != interaction.get("turn_id")
        or proposal.turn_id != agent_request.get("turn_id")
        or proposal.request_command_id != agent_request.get("command_id")
        or proposal.requested_interaction_id
        != agent_request.get("requested_interaction_id")
        or proposal.requested_interaction_id != agent_failed.get("interaction_id")
        or proposal.requested_interaction_revision
        != agent_failed.get("interaction_revision")
        or proposal.requested_interaction_sequence
        != agent_failed.get("interaction_sequence")
        or proposal.requested_failure_suffix_end_sequence
        != agent_failed.get("same_failure_suffix_end_sequence")
        or proposal.requested_interaction_sequence
        != proposal.requested_failure_suffix_end_sequence
        or proposal.failed_turn_id != agent_failed.get("turn_id")
        or proposal.failed_command_id != agent_failed.get("command_id")
        or proposal.task_id != agent_failed.get("task_id")
        or proposal.world_id != agent_failed.get("world_id")
        or proposal.failure_count != agent_failed.get("failure_count")
        or proposal.failure_count < 4
        or proposal.failure_key != agent_failed.get("failure_key")
        or proposal.failed_build_id != agent_failed.get("build_id")
        or proposal.failed_run_id != agent_failed.get("run_id")
        or proposal.feedback_event_id != agent_failed.get("feedback_event_id")
        or proposal.projection_receipt_id
        != agent_failed.get("projection_receipt_id")
        or proposal.agent_proposal_json.get("proposal_id")
        != proposal.agent_proposal_id
        or proposal.agent_proposal_json.get("proposal_sha256")
        != proposal.agent_proposal_sha256
        or _agent_proposal_sha256(proposal.agent_proposal_json)
        != proposal.agent_proposal_sha256
    ):
        return Failure(
            _error(
                "INVARIANT_VIOLATION",
                "PATCH_DECISION",
                "persisted Patch proposal bytes or hashes drifted",
            )
        )

    draft = await session.scalar(
        select(ProductDraftRow).where(
            ProductDraftRow.tenant_id == tenant_id,
            ProductDraftRow.actor_id == actor_id,
            ProductDraftRow.session_id == session_id,
            ProductDraftRow.draft_id == proposal.draft_id,
        ).with_for_update()
    )
    base_revision = await session.scalar(
        select(ProductDraftRevisionRow).where(
            ProductDraftRevisionRow.draft_revision_row_id
            == proposal.base_draft_revision_row_id,
            ProductDraftRevisionRow.tenant_id == tenant_id,
            ProductDraftRevisionRow.actor_id == actor_id,
            ProductDraftRevisionRow.session_id == session_id,
            ProductDraftRevisionRow.draft_id == proposal.draft_id,
            ProductDraftRevisionRow.skill_id == proposal.skill_id,
        )
    )
    if draft is None or base_revision is None:
        return Failure(
            _error(
                "INVARIANT_VIOLATION",
                "PATCH_DECISION",
                "Patch base Draft authority disappeared",
            )
        )
    if (
        draft.skill_id != proposal.skill_id
        or draft.revision != proposal.base_draft_revision
        or draft.draft_sha256 != proposal.base_draft_sha256
        or base_revision.revision != proposal.base_draft_revision
        or base_revision.draft_sha256 != proposal.base_draft_sha256
        or base_revision.source_bundle_sha256 != proposal.source_bundle_sha256
        or base_revision.entrypoint != proposal.entrypoint
        or base_revision.draft_json != draft.draft_json
        or request_body.get("base_draft_revision") != draft.revision
        or request_body.get("base_draft_sha256") != draft.draft_sha256
    ):
        return Failure(
            _error(
                "CONTENT_VERSION_MISMATCH",
                "PATCH_DECISION",
                "Draft revision, hash, entrypoint, or source authority is stale",
            )
        )
    try:
        operation = _validated_entrypoint_operation(draft.draft_json, patch)
    except InvalidSkillBuildRequest as error:
        return Failure(_error("INVALID_REQUEST", "PATCH_DECISION", str(error)))
    if (
        operation.get("path") != proposal.entrypoint
        or operation.get("previous_content_sha256")
        != proposal.previous_content_sha256
        or operation.get("content_sha256") != proposal.content_sha256
    ):
        return Failure(
            _error(
                "INVARIANT_VIOLATION",
                "PATCH_DECISION",
                "Patch operation mirrors differ from proposal authority",
            )
        )

    build = await session.scalar(
        select(SkillBuildRow).where(
            SkillBuildRow.tenant_id == tenant_id,
            SkillBuildRow.actor_id == actor_id,
            SkillBuildRow.build_id == proposal.failed_build_id,
        )
    )
    build_provenance = await session.scalar(
        select(SkillBuildProvenanceRow).where(
            SkillBuildProvenanceRow.build_id == proposal.failed_build_id,
            SkillBuildProvenanceRow.tenant_id == tenant_id,
            SkillBuildProvenanceRow.actor_id == actor_id,
            SkillBuildProvenanceRow.session_id == session_id,
        )
    )
    run = await session.scalar(
        select(RunRow).where(
            RunRow.tenant_id == tenant_id,
            RunRow.actor_id == actor_id,
            RunRow.session_id == session_id,
            RunRow.run_id == proposal.failed_run_id,
        )
    )
    run_provenance = await session.scalar(
        select(SkillRunProvenanceRow).where(
            SkillRunProvenanceRow.run_id == proposal.failed_run_id,
            SkillRunProvenanceRow.tenant_id == tenant_id,
            SkillRunProvenanceRow.actor_id == actor_id,
            SkillRunProvenanceRow.session_id == session_id,
        )
    )
    if (
        build is None
        or build_provenance is None
        or run is None
        or run_provenance is None
        or build.status != "CERTIFIED"
        or not build.terminal
        or run.run_json.get("status") not in {"FAILED", "REJECTED"}
        or run_provenance.build_id != build.build_id
        or run_provenance.draft_revision_row_id
        != build_provenance.draft_revision_row_id
        or run_provenance.draft_sha256 != build_provenance.draft_sha256
        or build_provenance.draft_revision_row_id
        != base_revision.draft_revision_row_id
        or build_provenance.draft_sha256 != base_revision.draft_sha256
        or build_provenance.source_bundle_sha256
        != base_revision.source_bundle_sha256
    ):
        return Failure(
            _error(
                "INVARIANT_VIOLATION",
                "PATCH_DECISION",
                "failed Build, Run, and Draft provenance is not one exact chain",
            )
        )

    evidence_refs = patch.get("evidence_refs")
    if not isinstance(evidence_refs, list) or not evidence_refs:
        return Failure(
            _error("INVARIANT_VIOLATION", "PATCH_DECISION", "Patch Evidence is missing")
        )
    if not await _failed_run_has_full_authority(session, proposal, evidence_refs):
        return Failure(
            _error(
                "INVARIANT_VIOLATION",
                "PATCH_DECISION",
                "failed Run or Evidence authority drifted",
            )
        )
    links = list(
        (
            await session.scalars(
                select(ProductSkillPatchEvidenceRow).where(
                    ProductSkillPatchEvidenceRow.patch_id == proposal.patch_id
                )
            )
        ).all()
    )
    evidence_rows = list(
        (
            await session.scalars(
                select(EvidenceRow).where(
                    EvidenceRow.tenant_id == tenant_id,
                    EvidenceRow.actor_id == actor_id,
                    EvidenceRow.evidence_id.in_(
                        [item.get("evidence_id") for item in evidence_refs]
                    ),
                )
            )
        ).all()
    )
    refs_by_id = {
        cast(str, item.get("evidence_id")): item
        for item in evidence_refs
        if isinstance(item, Mapping) and isinstance(item.get("evidence_id"), str)
    }
    links_by_id = {item.evidence_id: item for item in links}
    rows_by_id = {item.evidence_id: item for item in evidence_rows}
    if (
        len(refs_by_id) != len(evidence_refs)
        or set(refs_by_id) != set(links_by_id)
        or set(refs_by_id) != set(rows_by_id)
        or any(
            link.evidence_ref_json != dict(refs_by_id[evidence_id])
            or link.evidence_sha256 != refs_by_id[evidence_id].get("sha256")
            or rows_by_id[evidence_id].evidence_json.get("evidence_ref")
            != dict(refs_by_id[evidence_id])
            for evidence_id, link in links_by_id.items()
        )
    ):
        return Failure(
            _error(
                "INVARIANT_VIOLATION",
                "PATCH_DECISION",
                "Patch Evidence references or immutable rows drifted",
            )
        )
    return Success((proposal, draft, base_revision, operation))


def _text_value(value: Mapping[str, Any], key: str) -> str:
    item = value[key]
    if not isinstance(item, str):
        raise TypeError(f"{key} must be a string")
    return item


def _integer_value(value: Mapping[str, Any], key: str) -> int:
    item = value[key]
    if not isinstance(item, int) or isinstance(item, bool):
        raise TypeError(f"{key} must be an integer")
    return item


def _time_value(value: Mapping[str, Any], key: str) -> datetime:
    return datetime.fromisoformat(_text_value(value, key).replace("Z", "+00:00"))


def _decision_identities_match(
    body: Mapping[str, Any],
    session_id: str,
    interaction_id: str,
    patch_id: str,
    interaction: Mapping[str, Any],
    patch: Mapping[str, Any],
) -> bool:
    expected = {
        "session_id": session_id,
        "interaction_id": interaction_id,
        "patch_id": patch_id,
        "turn_id": interaction.get("turn_id"),
        "patch_sha256": patch.get("patch_sha256"),
        "draft_id": patch.get("draft_id"),
        "skill_id": patch.get("skill_id"),
        "base_draft_revision": patch.get("base_draft_revision"),
        "base_draft_sha256": patch.get("base_draft_sha256"),
        "result_draft_sha256": patch.get("result_draft_sha256"),
    }
    return all(body.get(key) == value for key, value in expected.items())


def _patch_hash(patch: Mapping[str, Any]) -> str:
    projection = dict(patch)
    projection.pop("patch_sha256", None)
    return hashlib.sha256(canonical_payload(projection)).hexdigest()


def _agent_proposal_sha256(value: Mapping[str, Any]) -> str:
    """Recompute the Agent's richer trusted proposal identity framing."""

    try:
        target = _object_value(value, "target")
        request = _object_value(value, "request")
        failed = _object_value(value, "failed")
        skill_ref = _object_value(failed, "skill_ref")
        operation = _object_value(value, "operation")
        raw_evidence = failed.get("evidence_refs")
        if not isinstance(raw_evidence, list):
            return ""
        evidence: list[list[object]] = []
        for raw in raw_evidence:
            if not isinstance(raw, Mapping):
                return ""
            created_at = _time_value(raw, "created_at").astimezone(UTC)
            evidence.append(
                [
                    raw.get("evidence_id"),
                    raw.get("evidence_type"),
                    created_at.isoformat().replace("+00:00", "Z"),
                    raw.get("sha256"),
                    raw.get("uri"),
                ]
            )
        vector: list[object] = [
            "skill_patch_proposal",
            "1.0.0",
            target.get("draft_id"),
            target.get("session_id"),
            target.get("skill_id"),
            target.get("draft_revision"),
            target.get("draft_sha256"),
            target.get("source_bundle_sha256"),
            target.get("entrypoint"),
            target.get("entrypoint_sha256"),
            request.get("tenant_id"),
            request.get("actor_id"),
            request.get("actor_type"),
            request.get("session_id"),
            request.get("task_id"),
            request.get("turn_id"),
            request.get("command_id"),
            request.get("requested_interaction_id"),
            failed.get("tenant_id"),
            failed.get("actor_id"),
            failed.get("session_id"),
            failed.get("interaction_id"),
            failed.get("interaction_revision"),
            failed.get("interaction_sequence"),
            failed.get("same_failure_suffix_end_sequence"),
            failed.get("turn_id"),
            failed.get("command_id"),
            failed.get("task_id"),
            failed.get("world_id"),
            skill_ref.get("skill_id"),
            skill_ref.get("skill_version_id"),
            skill_ref.get("artifact_sha256"),
            skill_ref.get("certification_id"),
            failed.get("failure_count"),
            failed.get("failure_key"),
            failed.get("build_id"),
            failed.get("run_id"),
            failed.get("feedback_event_id"),
            failed.get("projection_receipt_id"),
            evidence,
            operation.get("operation_type"),
            operation.get("path"),
            operation.get("previous_content_sha256"),
            operation.get("content_sha256"),
            value.get("rationale"),
        ]
    except (KeyError, TypeError, ValueError):
        return ""
    encoded = json.dumps(
        vector,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validated_entrypoint_operation(
    draft: Mapping[str, Any], patch: Mapping[str, Any]
) -> dict[str, Any]:
    """Return the sole full-entrypoint UPSERT or reject the proposal."""

    operations = patch.get("operations")
    source = draft.get("source_bundle")
    if (
        not isinstance(operations, list)
        or len(operations) != 1
        or not isinstance(operations[0], Mapping)
        or not isinstance(source, Mapping)
    ):
        raise InvalidSkillBuildRequest(
            "Skill Patch requires exactly one UPSERT_FILE operation"
        )
    operation = dict(operations[0])
    entrypoint = source.get("entrypoint")
    files = source.get("files")
    if (
        operation.get("operation") != "UPSERT_FILE"
        or not isinstance(entrypoint, str)
        or operation.get("path") != entrypoint
        or not isinstance(files, list)
    ):
        raise InvalidSkillBuildRequest(
            "Skill Patch must UPSERT the current canonical entrypoint"
        )
    matches = [
        item
        for item in files
        if isinstance(item, Mapping) and item.get("path") == entrypoint
    ]
    if len(matches) != 1:
        raise InvalidSkillBuildRequest("Draft entrypoint is not a unique source file")
    current = matches[0]
    content = operation.get("content")
    content_sha256 = operation.get("content_sha256")
    if (
        set(operation)
        != {
            "operation",
            "path",
            "previous_content_sha256",
            "content",
            "content_sha256",
        }
        or operation.get("previous_content_sha256") != current.get("content_sha256")
        or not isinstance(content, str)
        or not content
        or not isinstance(content_sha256, str)
        or hashlib.sha256(content.encode("utf-8")).hexdigest() != content_sha256
        or content_sha256 == current.get("content_sha256")
    ):
        raise InvalidSkillBuildRequest(
            "Skill Patch entrypoint precondition or content hash differs"
        )
    return operation


def _apply_entrypoint_upsert(
    draft: Mapping[str, Any],
    patch: Mapping[str, Any],
    operation: Mapping[str, Any],
    decided_at: datetime,
) -> dict[str, Any]:
    source = copy.deepcopy(draft.get("source_bundle"))
    if not isinstance(source, dict) or not isinstance(source.get("files"), list):
        raise InvalidSkillBuildRequest("Draft source bundle is malformed")
    entrypoint = source.get("entrypoint")
    matches = [
        (index, item)
        for index, item in enumerate(source["files"])
        if isinstance(item, dict) and item.get("path") == entrypoint
    ]
    if len(matches) != 1 or operation.get("path") != entrypoint:
        raise InvalidSkillBuildRequest("Draft entrypoint is not uniquely patchable")
    index, _ = matches[0]
    source["files"][index] = {
        "path": operation["path"],
        "content": operation["content"],
        "content_sha256": operation["content_sha256"],
    }
    validate_source_bundle({"source_bundle": source})
    body = {
        "session_id": draft["session_id"],
        "draft_id": draft["draft_id"],
        "skill_id": draft["skill_id"],
        "content_ref": draft["content_ref"],
        "display_name": draft["display_name"],
        "source_bundle": source,
    }
    return draft_resource(
        body,
        cast(Mapping[str, Any], draft["request_context"]),
        _integer_value(draft, "revision") + 1,
        _time_value(draft, "created_at"),
        decided_at,
        _text_value(patch, "patch_id"),
    )


def _apply_patch(draft: Mapping[str, Any], patch: Mapping[str, Any], decided_at: datetime) -> dict[str, Any]:
    """Apply the released declarative operations without filesystem normalization."""
    source = copy.deepcopy(draft["source_bundle"])
    if not isinstance(source, dict) or not isinstance(source.get("files"), list):
        raise InvalidSkillBuildRequest("draft source bundle is malformed")
    files = source["files"]
    file_by_path: dict[str, dict[str, Any]] = {}
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise InvalidSkillBuildRequest("draft source file is malformed")
        key = item["path"].lower()
        if key in file_by_path:
            raise InvalidSkillBuildRequest("draft has case-insensitive path collision")
        file_by_path[key] = item
    targeted: set[str] = set()
    for operation in patch.get("operations", []):
        if not isinstance(operation, Mapping):
            raise InvalidSkillBuildRequest("patch operation is malformed")
        kind = operation.get("operation")
        if kind in {"UPSERT_FILE", "DELETE_FILE"}:
            path = operation.get("path")
            if not isinstance(path, str) or not _canonical_source_path(path):
                raise InvalidSkillBuildRequest("patch path is not canonical")
            key = path.lower()
            if key in targeted:
                raise InvalidSkillBuildRequest("patch targets one path more than once")
            targeted.add(key)
            existing = file_by_path.get(key)
            previous = operation.get("previous_content_sha256")
            if kind == "UPSERT_FILE":
                content = operation.get("content")
                content_hash = operation.get("content_sha256")
                if not isinstance(content, str) or not isinstance(content_hash, str):
                    raise InvalidSkillBuildRequest("upsert payload is malformed")
                if hashlib.sha256(content.encode("utf-8")).hexdigest() != content_hash:
                    raise InvalidSkillBuildRequest("upsert content hash differs")
                if (existing is None and previous is not None) or (
                    existing is not None and existing.get("content_sha256") != previous
                ):
                    raise InvalidSkillBuildRequest("upsert precondition differs")
                replacement = {"path": path, "content": content, "content_sha256": content_hash}
                if existing is None:
                    files.append(replacement)
                else:
                    files[files.index(existing)] = replacement
                file_by_path[key] = replacement
            else:
                if existing is None or existing.get("content_sha256") != previous:
                    raise InvalidSkillBuildRequest("delete precondition differs")
                if len(files) == 1:
                    raise InvalidSkillBuildRequest("patch cannot remove the final source file")
                files.remove(existing)
                del file_by_path[key]
        elif kind == "SET_ENTRYPOINT":
            path = operation.get("path")
            if not isinstance(path, str) or path not in {item["path"] for item in files}:
                raise InvalidSkillBuildRequest("entrypoint must identify one source file")
            source["entrypoint"] = path
        elif kind == "SET_DISPLAY_NAME":
            display_name = operation.get("display_name")
            if not isinstance(display_name, str) or not display_name:
                raise InvalidSkillBuildRequest("display name is invalid")
            draft = {**draft, "display_name": display_name}
        else:
            raise InvalidSkillBuildRequest("unsupported patch operation")
    validate_source_bundle({"source_bundle": source})
    body = {
        "session_id": draft["session_id"],
        "draft_id": draft["draft_id"],
        "skill_id": draft["skill_id"],
        "content_ref": draft["content_ref"],
        "display_name": draft["display_name"],
        "source_bundle": source,
    }
    return draft_resource(
        body,
        draft["request_context"],
        int(draft["revision"]) + 1,
        _time_value(draft, "created_at"),
        decided_at,
        _text_value(patch, "patch_id"),
    )


def _canonical_source_path(path: str) -> bool:
    import re

    return re.fullmatch(
        r"(?=.{1,240}$)[A-Za-z0-9_](?:[A-Za-z0-9_.-]*[A-Za-z0-9_-])?(?:/[A-Za-z0-9_](?:[A-Za-z0-9_.-]*[A-Za-z0-9_-])?)*",
        path,
    ) is not None


def _expected_terminal_command_stage(run: Mapping[str, Any]) -> str | None:
    """Derive the Command stage from fields that exist in the canonical Run wire."""

    status = run.get("status")
    if status == "SUCCEEDED":
        return "COMPLETE"
    if status not in {"REJECTED", "FAILED"}:
        return None
    sandbox = run.get("sandbox")
    sandbox_status = sandbox.get("status") if isinstance(sandbox, Mapping) else None
    return "SANDBOX" if sandbox_status in {"FAILED", "TIMED_OUT"} else "WORLD_VALIDATE"


def _decision_receipt(
    body: Mapping[str, Any],
    interaction: Mapping[str, Any],
    draft: Mapping[str, Any],
    revision_before: int,
    revision_after: int,
    context: OperationContext,
) -> dict[str, Any]:
    content = interaction["request_context"]["content_ref"]
    origin = request_context_data(replace(context, content_ref=ContentRef(**content)))
    session_id = _text_value(body, "session_id")
    interaction_id = _text_value(body, "interaction_id")
    draft_id = _text_value(body, "draft_id")
    accepted = body["decision"] == "ACCEPT"
    return {
        "request_context": origin,
        "decision_id": body["decision_id"],
        "session_id": session_id,
        "turn_id": body["turn_id"],
        "interaction_id": interaction_id,
        "interaction_revision_before": revision_before,
        "interaction_revision_after": revision_after,
        "patch_id": body["patch_id"],
        "patch_sha256": body["patch_sha256"],
        "draft_id": draft_id,
        "skill_id": body["skill_id"],
        "decision": body["decision"],
        "reason_code": body["reason_code"],
        "draft_updated": accepted,
        "draft_revision_before": body["base_draft_revision"],
        "draft_sha256_before": body["base_draft_sha256"],
        "draft_revision_after": draft["revision"],
        "draft_sha256_after": draft["draft_sha256"],
        "decided_at": body["decided_at"],
        "links": {
            "interaction": f"/product-experience/v1/sessions/{session_id}/agent-interactions/{interaction_id}",
            "skill_draft": f"/product-experience/v1/sessions/{session_id}/skill-drafts/{draft_id}",
        },
    }


def _error(code: str, stage: str, message: str) -> Any:
    from yaya_agent_contracts import ContractError, ErrorCategory

    metadata = {
        "NOT_FOUND": (ErrorCategory.VALIDATION, False, "resource.not_found"),
        "INVALID_REQUEST": (ErrorCategory.VALIDATION, False, "request.invalid"),
        "CONTENT_VERSION_MISMATCH": (ErrorCategory.VALIDATION, False, "content.version_mismatch"),
        "EVENT_SEQUENCE_GAP": (ErrorCategory.CONCURRENCY, True, "event.resync_required"),
        "IDEMPOTENCY_KEY_REUSED": (ErrorCategory.CONCURRENCY, False, "request.idempotency_conflict"),
        "INVARIANT_VIOLATION": (ErrorCategory.INVARIANT, False, "system.invariant_violation"),
    }[code]
    return ContractError(
        code=code,
        category=metadata[0],
        retryable=metadata[1],
        user_message_key=metadata[2],
        stage=stage,
        message=message,
    )
