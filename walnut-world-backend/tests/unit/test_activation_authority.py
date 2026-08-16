"""Full-scope Activation/RegistryEntry validation is shared by every Turn boundary."""

from __future__ import annotations

import copy
import inspect
from datetime import UTC, datetime

import pytest
from yaya_agent_contracts import (
    ActorRef,
    ActorType,
    ContentRef,
    RequestContext,
    canonical_json_sha256,
)

from walnut_backend.adapters.postgres import agent_turns, skill_invocation
from walnut_backend.adapters.postgres.activation_authority import (
    validate_activation_registry_authority,
)
from walnut_backend.adapters.postgres.models import (
    RegistryEntryRow,
    RegistryHeadRow,
    SkillActivationRow,
    request_context_data,
)
from walnut_backend.adapters.postgres.workflow_jobs import WorkflowInvariantError
from walnut_backend.workers import turn_worker


def test_exact_activation_registry_authority_accepts_writer_bytes() -> None:
    head, entry, activation = _authority()
    validate_activation_registry_authority(
        head=head,
        entry=entry,
        activation=activation,
        authority_id=head.authority_id,
    )


def test_coordinated_activation_json_and_hash_tamper_fails_closed() -> None:
    head, entry, activation = _authority()
    value = copy.deepcopy(activation.activation_json)
    value["skill_version_id"] = "skillver_tampered"
    activation.activation_json = value
    activation.activation_sha256 = canonical_json_sha256(value)
    with pytest.raises(WorkflowInvariantError, match="JSON or hash"):
        validate_activation_registry_authority(
            head=head,
            entry=entry,
            activation=activation,
            authority_id=head.authority_id,
        )


def test_coordinated_registry_json_and_hash_tamper_fails_closed() -> None:
    head, entry, activation = _authority()
    value = copy.deepcopy(entry.entry_json)
    value["activation_id"] = "activation_tampered"
    entry.entry_json = value
    entry.entry_sha256 = canonical_json_sha256(value)
    with pytest.raises(WorkflowInvariantError, match="JSON or hash"):
        validate_activation_registry_authority(
            head=head,
            entry=entry,
            activation=activation,
            authority_id=head.authority_id,
        )


def test_all_turn_external_boundaries_use_the_shared_validator() -> None:
    assert "load_current_activation_authority(" in inspect.getsource(
        agent_turns.PostgresAgentTurnStore.accept
    )
    assert "load_current_activation_authority(" in inspect.getsource(
        turn_worker.TurnWorkflowHandler._prepare  # pyright: ignore[reportPrivateUsage]
    )
    source = inspect.getsource(skill_invocation._load_authority)  # pyright: ignore[reportPrivateUsage]
    assert "load_current_activation_authority(" in source
    assert "for_update=for_update" in source


def _authority() -> tuple[RegistryHeadRow, RegistryEntryRow, SkillActivationRow]:
    now = datetime(2026, 8, 12, 1, 2, 3, tzinfo=UTC)
    origin = request_context_data(
        RequestContext(
            request_id="req_activation_unit_01",
            correlation_id="corr_activation_unit_01",
            trace_id="trace_activation_unit_01",
            requested_at=now,
            actor=ActorRef(
                "tenant_activation_unit",
                "student_activation_unit",
                ActorType.STUDENT,
                ("game:player",),
            ),
            content_ref=ContentRef("UNIT_ACTIVATION", "1.0.0", "a" * 64),
        )
    )
    activated_at = now.isoformat().replace("+00:00", "Z")
    activation_wire = {
        "request_context": origin,
        "activation_id": "activation_unit_01",
        "skill_id": "skill_unit_01",
        "skill_version_id": "skillver_unit_01",
        "certification_id": "cert_unit_01",
        "artifact_sha256": "b" * 64,
        "activation_scope": {
            "world_id": "world_unit_01",
            "agent_profile_id": "agent_unit_01",
        },
        "previous_registry_revision": 0,
        "registry_revision": 1,
        "activated_at": activated_at,
    }
    entry_wire = {
        "authority_id": "authority_unit_01",
        "activation_id": "activation_unit_01",
        "actor_id": "student_activation_unit",
        "content_hash": "a" * 64,
        "world_id": "world_unit_01",
        "agent_profile_id": "agent_unit_01",
        "skill_id": "skill_unit_01",
        "skill_version_id": "skillver_unit_01",
        "certification_id": "cert_unit_01",
        "artifact_sha256": "b" * 64,
        "previous_revision": 0,
        "revision": 1,
        "activated_at": activated_at,
    }
    scope = {
        "tenant_id": "tenant_activation_unit",
        "actor_id": "student_activation_unit",
        "content_hash": "a" * 64,
        "world_id": "world_unit_01",
        "agent_profile_id": "agent_unit_01",
    }
    head = RegistryHeadRow(
        **scope,
        authority_id="authority_unit_01",
        revision=1,
        updated_at=now,
    )
    entry = RegistryEntryRow(
        **scope,
        revision=1,
        skill_id="skill_unit_01",
        skill_version_id="skillver_unit_01",
        certification_id="cert_unit_01",
        artifact_sha256="b" * 64,
        previous_revision=0,
        entry_sha256=canonical_json_sha256(entry_wire),
        entry_json=entry_wire,
        activated_at=now,
    )
    activation = SkillActivationRow(
        activation_id="activation_unit_01",
        **scope,
        skill_id="skill_unit_01",
        skill_version_id="skillver_unit_01",
        certification_id="cert_unit_01",
        artifact_sha256="b" * 64,
        previous_registry_revision=0,
        registry_revision=1,
        activation_sha256=canonical_json_sha256(activation_wire),
        activation_json=activation_wire,
        activated_at=now,
    )
    return head, entry, activation
