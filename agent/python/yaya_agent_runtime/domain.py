"""Immutable internal Agent values.

These are application/domain models, not public Game or Product Experience
wire DTOs.  Public adapters must project them into the frozen schemas under
``agent/contracts`` and must retain the canonical command/run/evidence links.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal, cast

if TYPE_CHECKING:
    from .pedagogy_policy import TeachingDirective

from yaya_agent_contracts import (
    ActorRef,
    ActorType,
    ContentRef,
    EvidenceRef,
    EvidenceType,
    RequestContext,
    SkillRef,
    WorldCommitReceipt,
    canonical_json_sha256,
)

type RoleId = Literal[
    "world_agent",
    "xiaohutao",
    "teaching_agent",
    "bug_agent",
    "book_agent",
]
type GameEventType = Literal[
    "task_started",
    "compile_succeeded",
    "compile_failed",
    "run_skill_requested",
    "run_succeeded",
    "run_failed",
    "task_completed",
    "hint_requested",
    "skill_patch_requested",
    "skill_patch_confirmed",
]
type ResponseType = Literal["message", "question", "hint", "skill_patch", "growth_summary"]
# Public constructors accept ordinary mappings/lists and deep-freeze them at
# runtime.  ``object`` here avoids pretending Python's type system can prove a
# recursively JSON-compatible value supplied by an adapter.
type FrozenValue = object
type FrozenObject = Mapping[str, object]

BUG_FAILURE_THRESHOLD = 3

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$")
_TENANT_ID = re.compile(r"^[A-Za-z0-9_-]{3,96}$")
_COMMAND_ID = re.compile(r"^cmd_[A-Za-z0-9_-]{8,96}$")
_LOWER_KEY = re.compile(r"^[a-z][a-z0-9_.:-]{1,127}$")
_UPPER_CODE = re.compile(r"^[A-Z][A-Z0-9_]{2,95}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_SOURCE_PATH = re.compile(
    r"^(?=.{1,240}$)[A-Za-z0-9_]"
    r"(?:[A-Za-z0-9_.-]*[A-Za-z0-9_-])?"
    r"(?:/[A-Za-z0-9_](?:[A-Za-z0-9_.-]*[A-Za-z0-9_-])?)*$"
)
_MAX_RUN_WORLD_DIFFERENCE_BYTES = 24_576
_MAX_RUN_FAILED_ACTIONS_BYTES = 24_576
_ROLES: frozenset[str] = frozenset(
    {"world_agent", "xiaohutao", "teaching_agent", "bug_agent", "book_agent"}
)
_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "task_started",
        "compile_succeeded",
        "compile_failed",
        "run_skill_requested",
        "run_succeeded",
        "run_failed",
        "task_completed",
        "hint_requested",
        "skill_patch_requested",
        "skill_patch_confirmed",
    }
)
_RESPONSE_TYPES: frozenset[str] = frozenset(
    {"message", "question", "hint", "skill_patch", "growth_summary"}
)


def _require_identifier(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{field_name} does not match the canonical identifier format")
    return value


def _require_text(value: object, field_name: str, minimum: int, maximum: int) -> str:
    if not isinstance(value, str) or not minimum <= len(value) <= maximum:
        raise ValueError(f"{field_name} length must be between {minimum} and {maximum}")
    return value


def _require_datetime(value: object, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


def _require_integer(
    value: object,
    field_name: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{field_name} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{field_name} must be at most {maximum}")
    return value


def freeze_value(value: object, field_name: str = "value") -> FrozenValue:
    """Deep-freeze JSON-compatible data and reject ambiguous Python values."""

    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError(f"{field_name} contains a non-finite number")
        return value
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        frozen: dict[str, FrozenValue] = {}
        for key, item in mapping.items():
            if not isinstance(key, str):
                raise TypeError(f"{field_name} object keys must be strings")
            frozen[key] = freeze_value(item, f"{field_name}.{key}")
        return MappingProxyType(frozen)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        sequence = cast(Sequence[object], value)
        return tuple(freeze_value(item, f"{field_name}[]") for item in sequence)
    raise TypeError(f"{field_name} must contain only JSON-compatible values")


def freeze_object(value: Mapping[str, object], field_name: str = "value") -> FrozenObject:
    frozen = freeze_value(value, field_name)
    if not isinstance(frozen, Mapping):
        raise TypeError(f"{field_name} must be an object")
    return cast(FrozenObject, frozen)


def thaw_value(value: object) -> object:
    """Return ordinary JSON-serializable containers for prompts and adapters."""

    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        return {str(key): thaw_value(item) for key, item in mapping.items()}
    if isinstance(value, tuple):
        return [thaw_value(item) for item in cast(tuple[object, ...], value)]
    return value


def _json_encoded_size(value: object) -> int:
    return len(
        json.dumps(
            thaw_value(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    )


def _freeze_evidence(value: Sequence[EvidenceRef], field_name: str) -> tuple[EvidenceRef, ...]:
    evidence = tuple(value)
    if len(evidence) > 64:
        raise ValueError(f"{field_name} must contain at most 64 items")
    seen: set[str] = set()
    for index, item in enumerate(evidence):
        if not isinstance(item, EvidenceRef):
            raise TypeError(f"{field_name}[{index}] must be an EvidenceRef")
        if item.evidence_id in seen:
            raise ValueError(f"{field_name} must contain unique evidence_id values")
        seen.add(item.evidence_id)
    return evidence


def _require_same_authority(
    snapshot_context: RequestContext,
    authority: RequestContext,
    field_name: str,
) -> None:
    snapshot_actor = snapshot_context.actor
    authority_actor = authority.actor
    if (
        snapshot_actor.tenant_id,
        snapshot_actor.actor_id,
        snapshot_actor.actor_type,
    ) != (
        authority_actor.tenant_id,
        authority_actor.actor_id,
        authority_actor.actor_type,
    ):
        raise ValueError(f"{field_name} was authorized for a different actor")
    if snapshot_context.content_ref != authority.content_ref:
        raise ValueError(f"{field_name} uses a different pinned content version")


def _require_source_path(value: object, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or not _SOURCE_PATH.fullmatch(value)
        or "\\" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise ValueError(f"{field_name} must be a canonical relative source path")
    return value


@dataclass(frozen=True, slots=True)
class DraftAuthority:
    """Immutable identity of the exact Draft source used by one Build."""

    draft_id: str
    session_id: str
    skill_id: str
    draft_revision: int
    draft_sha256: str
    source_bundle_sha256: str
    entrypoint: str
    entrypoint_sha256: str

    def __post_init__(self) -> None:
        for name in ("draft_id", "session_id", "skill_id"):
            _require_identifier(getattr(self, name), name)
        _require_integer(self.draft_revision, "draft_revision", minimum=1)
        for name in ("draft_sha256", "source_bundle_sha256", "entrypoint_sha256"):
            value = getattr(self, name)
            if not isinstance(value, str) or not _SHA256.fullmatch(value):
                raise ValueError(f"{name} must be lowercase SHA-256")
        _require_source_path(self.entrypoint, "entrypoint")


@dataclass(frozen=True, slots=True)
class DraftSnapshot:
    """Read-only current Draft snapshot; the Agent has no corresponding write port."""

    authority: DraftAuthority
    source_code: str
    request_context: RequestContext

    def __post_init__(self) -> None:
        if not isinstance(self.authority, DraftAuthority):
            raise TypeError("authority must be a DraftAuthority")
        _require_text(self.source_code, "source_code", 1, 1_048_576)
        if (
            hashlib.sha256(self.source_code.encode("utf-8")).hexdigest()
            != self.authority.entrypoint_sha256
        ):
            raise ValueError("entrypoint_sha256 does not match source_code UTF-8 bytes")
        if not isinstance(self.request_context, RequestContext):
            raise TypeError("request_context must be a RequestContext")


def _draft_authority_from_mapping(value: object, field_name: str) -> DraftAuthority:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be an object")
    mapping = cast(Mapping[object, object], value)
    expected = {
        "draft_id",
        "session_id",
        "skill_id",
        "draft_revision",
        "draft_sha256",
        "source_bundle_sha256",
        "entrypoint",
        "entrypoint_sha256",
    }
    if set(mapping) != expected or any(not isinstance(key, str) for key in mapping):
        raise ValueError(f"{field_name} must use the exact Draft authority fields")
    return DraftAuthority(
        draft_id=cast(str, mapping["draft_id"]),
        session_id=cast(str, mapping["session_id"]),
        skill_id=cast(str, mapping["skill_id"]),
        draft_revision=cast(int, mapping["draft_revision"]),
        draft_sha256=cast(str, mapping["draft_sha256"]),
        source_bundle_sha256=cast(str, mapping["source_bundle_sha256"]),
        entrypoint=cast(str, mapping["entrypoint"]),
        entrypoint_sha256=cast(str, mapping["entrypoint_sha256"]),
    )


@dataclass(frozen=True, slots=True)
class GameEvent:
    """Trusted internal event bound to one accepted Agent command.

    ``failure_count`` means consecutive failures with the same
    ``failure_key`` in this session.  It is not a lifetime or total count.
    """

    event_id: str
    event_type: GameEventType
    student_id: str
    task_id: str
    session_id: str
    turn_id: str
    command_id: str
    occurred_at: datetime
    expected_world_revision: int
    skill_ref: SkillRef | None = None
    run_id: str | None = None
    build_id: str | None = None
    failure_count: int = 0
    failure_key: str | None = None
    evidence_refs: tuple[EvidenceRef, ...] = ()
    payload: FrozenObject = field(default_factory=lambda: MappingProxyType({}))
    _patch_draft_authority: DraftAuthority | None = field(
        default=None,
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        for name in ("event_id", "student_id", "task_id", "session_id", "turn_id"):
            _require_identifier(getattr(self, name), name)
        if not isinstance(self.command_id, str) or not _COMMAND_ID.fullmatch(self.command_id):
            raise ValueError("command_id does not match the canonical command format")
        if self.event_type not in _EVENT_TYPES:
            raise ValueError("event_type is not supported")
        _require_datetime(self.occurred_at, "occurred_at")
        _require_integer(self.expected_world_revision, "expected_world_revision", minimum=0)
        _require_integer(self.failure_count, "failure_count", minimum=0, maximum=10_000)
        if self.skill_ref is not None and not isinstance(self.skill_ref, SkillRef):
            raise TypeError("skill_ref must be a SkillRef or None")
        if self.run_id is not None:
            _require_identifier(self.run_id, "run_id")
        if self.build_id is not None:
            _require_identifier(self.build_id, "build_id")
        if self.failure_key is not None:
            _require_text(self.failure_key, "failure_key", 1, 128)

        if self.event_type in {"compile_succeeded", "compile_failed"} and self.build_id is None:
            raise ValueError(f"{self.event_type} requires build_id")
        if (
            self.event_type in {"run_succeeded", "run_failed", "task_completed"}
            and self.run_id is None
        ):
            raise ValueError(f"{self.event_type} requires run_id")
        if self.event_type == "run_skill_requested" and self.skill_ref is None:
            raise ValueError("run_skill_requested requires one certified skill_ref")
        if self.event_type == "run_skill_requested" and self.evidence_refs:
            raise ValueError("run_skill_requested cannot carry pre-existing Evidence")
        if (
            self.event_type
            in {
                "compile_failed",
                "run_failed",
                "task_completed",
                "skill_patch_requested",
            }
            and self.skill_ref is None
        ):
            raise ValueError(f"{self.event_type} requires one certified skill_ref")
        # A hint is the one request a learner can make before building anything.
        # At the start of a level the Registry holds no activation, so there is
        # no certified Skill to name -- the teaching roles then advise from the
        # task alone. Once a hint refers to a Run it must name that Run's Skill.
        if (
            self.event_type == "hint_requested"
            and self.skill_ref is None
            and self.run_id is not None
        ):
            raise ValueError("hint_requested about a Run requires its certified skill_ref")
        if self.event_type == "run_failed":
            if self.failure_count < 1 or self.failure_key is None:
                raise ValueError(
                    "run_failed requires a positive same-failure count and failure_key"
                )
        if (
            self.event_type == "hint_requested"
            and self.failure_count > 0
            and self.failure_key is None
        ):
            raise ValueError("hint_requested with failures requires failure_key")
        if self.event_type == "hint_requested" and self.failure_count >= BUG_FAILURE_THRESHOLD:
            if self.run_id is None or not self.evidence_refs:
                raise ValueError(
                    "bug-threshold hint_requested requires the exact failed run and Evidence"
                )

        if self.event_type == "skill_patch_requested":
            if self.run_id is None or self.build_id is None:
                raise ValueError("skill_patch_requested requires exact run_id and build_id")
            if self.failure_count < 1 or self.failure_key is None:
                raise ValueError(
                    "skill_patch_requested requires the selected failed Run and failure identity"
                )

        evidence = _freeze_evidence(self.evidence_refs, "evidence_refs")
        if (
            self.event_type
            in {"compile_failed", "run_failed", "task_completed", "skill_patch_requested"}
            and not evidence
        ):
            raise ValueError(f"{self.event_type} requires immutable evidence_refs")
        object.__setattr__(self, "evidence_refs", evidence)
        payload = freeze_object(self.payload, "payload")
        object.__setattr__(self, "payload", payload)
        if self.event_type == "skill_patch_requested":
            expected_payload = {
                "source_event_type",
                "action_id",
                "requested_interaction_id",
                "feature_enabled",
                "capability_enabled",
                "effective_hint_level",
                "draft_authority",
            }
            if set(payload) != expected_payload:
                raise ValueError(
                    "skill_patch_requested payload must use the exact trusted UI_ACTION fields"
                )
            if payload["source_event_type"] != "UI_ACTION":
                raise ValueError("skill_patch_requested must derive from exact UI_ACTION input")
            if payload["action_id"] != "request_ai_patch":
                raise ValueError("skill_patch_requested action_id must be request_ai_patch")
            _require_identifier(
                payload["requested_interaction_id"],
                "payload.requested_interaction_id",
            )
            if not isinstance(payload["feature_enabled"], bool) or not isinstance(
                payload["capability_enabled"], bool
            ):
                raise ValueError("skill_patch_requested feature/capability gates must be boolean")
            if payload["effective_hint_level"] != 4:
                raise ValueError("skill_patch_requested requires effective_hint_level 4")
            authority = _draft_authority_from_mapping(
                payload["draft_authority"],
                "payload.draft_authority",
            )
            if authority.session_id != self.session_id:
                raise ValueError("skill_patch_requested Draft belongs to another session")
            if self.skill_ref is None or authority.skill_id != self.skill_ref.skill_id:
                raise ValueError("skill_patch_requested Draft belongs to another skill")
            object.__setattr__(self, "_patch_draft_authority", authority)

    @property
    def patch_draft_authority(self) -> DraftAuthority | None:
        return self._patch_draft_authority

    @property
    def skill_patch_feature_enabled(self) -> bool:
        return bool(
            self.event_type == "skill_patch_requested"
            and self.payload.get("feature_enabled") is True
        )

    @property
    def skill_patch_capability_enabled(self) -> bool:
        return bool(
            self.event_type == "skill_patch_requested"
            and self.payload.get("capability_enabled") is True
        )


@dataclass(frozen=True, slots=True)
class TaskSnapshot:
    task_id: str
    title: str
    goal: str
    story: str
    knowledge_points: tuple[str, ...]
    request_context: RequestContext
    max_hint_level: int = 4

    def __post_init__(self) -> None:
        _require_identifier(self.task_id, "task_id")
        _require_text(self.title, "title", 1, 200)
        _require_text(self.goal, "goal", 1, 2000)
        _require_text(self.story, "story", 0, 4000)
        if not isinstance(self.request_context, RequestContext):
            raise TypeError("request_context must be a RequestContext")
        points = tuple(self.knowledge_points)
        if len(points) > 64 or len(points) != len(set(points)):
            raise ValueError("knowledge_points must contain at most 64 unique values")
        for point in points:
            if not isinstance(point, str) or not _LOWER_KEY.fullmatch(point):
                raise ValueError("knowledge_points contains an invalid concept key")
        _require_integer(self.max_hint_level, "max_hint_level", minimum=0, maximum=4)
        object.__setattr__(self, "knowledge_points", points)


@dataclass(frozen=True, slots=True)
class SessionSnapshot:
    session_id: str
    student_id: str
    task_id: str
    world_id: str
    request_context: RequestContext

    def __post_init__(self) -> None:
        for name in ("session_id", "student_id", "task_id", "world_id"):
            _require_identifier(getattr(self, name), name)
        if not isinstance(self.request_context, RequestContext):
            raise TypeError("request_context must be a RequestContext")


@dataclass(frozen=True, slots=True)
class SkillSnapshot:
    ref: SkillRef
    source_code: str
    source_sha256: str
    entrypoint: str
    parameter_schema: FrozenObject
    request_context: RequestContext

    def __post_init__(self) -> None:
        if not isinstance(self.ref, SkillRef):
            raise TypeError("ref must be a SkillRef")
        _require_text(self.source_code, "source_code", 1, 1_048_576)
        if not isinstance(self.source_sha256, str) or not _SHA256.fullmatch(self.source_sha256):
            raise ValueError("source_sha256 must be lowercase SHA-256")
        if hashlib.sha256(self.source_code.encode("utf-8")).hexdigest() != self.source_sha256:
            raise ValueError("source_sha256 does not match source_code UTF-8 bytes")
        _require_text(self.entrypoint, "entrypoint", 1, 240)
        if not isinstance(self.request_context, RequestContext):
            raise TypeError("request_context must be a RequestContext")
        object.__setattr__(
            self,
            "parameter_schema",
            freeze_object(self.parameter_schema, "parameter_schema"),
        )


@dataclass(frozen=True, slots=True)
class CompileResultSnapshot:
    build_id: str
    skill_ref: SkillRef
    succeeded: bool
    diagnostics: tuple[str, ...]
    evidence_refs: tuple[EvidenceRef, ...]
    request_context: RequestContext
    draft_authority: DraftAuthority | None = None

    def __post_init__(self) -> None:
        _require_identifier(self.build_id, "build_id")
        if not isinstance(self.skill_ref, SkillRef):
            raise TypeError("skill_ref must be a SkillRef")
        if not isinstance(self.succeeded, bool):
            raise ValueError("succeeded must be a boolean")
        if not isinstance(self.request_context, RequestContext):
            raise TypeError("request_context must be a RequestContext")
        if self.draft_authority is not None and not isinstance(
            self.draft_authority, DraftAuthority
        ):
            raise TypeError("draft_authority must be a DraftAuthority or None")
        diagnostics = tuple(self.diagnostics)
        if len(diagnostics) > 100:
            raise ValueError("diagnostics must contain at most 100 items")
        for item in diagnostics:
            _require_text(item, "diagnostic", 1, 2000)
        if not self.succeeded and not diagnostics:
            raise ValueError("a failed compile result requires at least one diagnostic")
        object.__setattr__(self, "diagnostics", diagnostics)
        object.__setattr__(
            self, "evidence_refs", _freeze_evidence(self.evidence_refs, "evidence_refs")
        )


@dataclass(frozen=True, slots=True)
class RunResultSnapshot:
    run_id: str
    session_id: str
    turn_id: str
    command_id: str
    world_id: str
    skill_ref: SkillRef
    task_success: bool
    world_revision_before: int
    world_revision_after: int
    world_difference: FrozenObject
    failed_actions: tuple[FrozenObject, ...]
    failure_key: str | None
    evidence_refs: tuple[EvidenceRef, ...]
    world_commit: WorldCommitReceipt | None
    request_context: RequestContext
    build_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("run_id", "session_id", "turn_id", "world_id"):
            _require_identifier(getattr(self, name), name)
        if not isinstance(self.command_id, str) or not _COMMAND_ID.fullmatch(self.command_id):
            raise ValueError("command_id does not match the canonical command format")
        if not isinstance(self.skill_ref, SkillRef):
            raise TypeError("skill_ref must be a SkillRef")
        if not isinstance(self.task_success, bool):
            raise ValueError("task_success must be a boolean")
        if not isinstance(self.request_context, RequestContext):
            raise TypeError("request_context must be a RequestContext")
        if self.build_id is not None:
            _require_identifier(self.build_id, "build_id")
        if self.world_commit is not None and not isinstance(self.world_commit, WorldCommitReceipt):
            raise TypeError("world_commit must be a WorldCommitReceipt or None")
        _require_integer(self.world_revision_before, "world_revision_before", minimum=0)
        _require_integer(self.world_revision_after, "world_revision_after", minimum=0)
        if self.world_revision_after < self.world_revision_before:
            raise ValueError("world_revision_after must not precede world_revision_before")
        if self.failure_key is not None:
            _require_text(self.failure_key, "failure_key", 1, 128)
        if not self.task_success and self.failure_key is None:
            raise ValueError("an unsuccessful run requires failure_key")
        world_difference = freeze_object(self.world_difference, "world_difference")
        if _json_encoded_size(world_difference) > _MAX_RUN_WORLD_DIFFERENCE_BYTES:
            raise ValueError("world_difference exceeds its 24576-byte canonical bound")
        object.__setattr__(self, "world_difference", world_difference)
        failed_actions = tuple(self.failed_actions)
        if len(failed_actions) > 20:
            raise ValueError("failed_actions must contain at most 20 summarized actions")
        frozen_failed_actions = tuple(
            freeze_object(item, "failed_actions item") for item in failed_actions
        )
        if _json_encoded_size(frozen_failed_actions) > _MAX_RUN_FAILED_ACTIONS_BYTES:
            raise ValueError("failed_actions exceeds its 24576-byte canonical bound")
        object.__setattr__(self, "failed_actions", frozen_failed_actions)
        if self.task_success and (self.failure_key is not None or failed_actions):
            raise ValueError("a successful run cannot contain failure_key or failed_actions")
        evidence = _freeze_evidence(self.evidence_refs, "evidence_refs")
        if not evidence:
            raise ValueError("run results require immutable evidence_refs")
        revision_delta = self.world_revision_after - self.world_revision_before
        world_commits = tuple(
            item for item in evidence if item.evidence_type is EvidenceType.WORLD_COMMIT
        )
        if revision_delta not in {0, 1}:
            raise ValueError("one run may advance the world by at most one revision")
        if (revision_delta == 1) != (len(world_commits) == 1):
            raise ValueError("world revision and WORLD_COMMIT Evidence must describe one commit")
        if (revision_delta == 1) != (self.world_commit is not None):
            raise ValueError(
                "world revision and typed World commit receipt must describe one commit"
            )
        if self.world_commit is not None:
            receipt = self.world_commit
            if (
                receipt.world_id != self.world_id
                or receipt.previous_revision != self.world_revision_before
                or receipt.world_revision != self.world_revision_after
            ):
                raise ValueError(
                    "World commit receipt does not match the Run identity or revisions"
                )
            if world_commits[0].sha256 != world_commit_receipt_sha256(receipt):
                raise ValueError("WORLD_COMMIT Evidence hash does not match the typed receipt")
        if self.task_success and revision_delta != 1:
            raise ValueError("a successful world task requires an exact +1 World commit")
        object.__setattr__(self, "evidence_refs", evidence)


@dataclass(frozen=True, slots=True)
class WorldSummary:
    world_id: str
    revision: int
    last_event_sequence: int
    state_hash: str
    visible_state: FrozenObject

    def __post_init__(self) -> None:
        _require_identifier(self.world_id, "world_id")
        _require_integer(self.revision, "revision", minimum=0)
        _require_integer(self.last_event_sequence, "last_event_sequence", minimum=0)
        if not isinstance(self.state_hash, str) or not _SHA256.fullmatch(self.state_hash):
            raise ValueError("state_hash must be lowercase SHA-256")
        object.__setattr__(
            self, "visible_state", freeze_object(self.visible_state, "visible_state")
        )


@dataclass(frozen=True, slots=True)
class LearnerProfileSnapshot:
    student_id: str
    revision: int
    competencies: FrozenObject
    request_context: RequestContext
    evidence_refs: tuple[EvidenceRef, ...] = ()

    def __post_init__(self) -> None:
        _require_identifier(self.student_id, "student_id")
        _require_integer(self.revision, "revision", minimum=0)
        if not isinstance(self.request_context, RequestContext):
            raise TypeError("request_context must be a RequestContext")
        object.__setattr__(self, "competencies", freeze_object(self.competencies, "competencies"))
        object.__setattr__(
            self, "evidence_refs", _freeze_evidence(self.evidence_refs, "evidence_refs")
        )


@dataclass(frozen=True, slots=True)
class MessageSnapshot:
    message_id: str
    session_id: str
    role: RoleId
    message: str
    request_context: RequestContext

    def __post_init__(self) -> None:
        _require_identifier(self.message_id, "message_id")
        _require_identifier(self.session_id, "session_id")
        if self.role not in _ROLES:
            raise ValueError("role is not supported")
        if not isinstance(self.request_context, RequestContext):
            raise TypeError("request_context must be a RequestContext")
        _require_text(self.message, "message", 1, 4000)


@dataclass(frozen=True, slots=True)
class SkillVersionSummary:
    session_id: str
    skill_id: str
    skill_version_id: str
    source_sha256: str
    change_summary: str
    request_context: RequestContext

    def __post_init__(self) -> None:
        _require_identifier(self.session_id, "session_id")
        _require_identifier(self.skill_id, "skill_id")
        _require_identifier(self.skill_version_id, "skill_version_id")
        if not isinstance(self.request_context, RequestContext):
            raise TypeError("request_context must be a RequestContext")
        if not isinstance(self.source_sha256, str) or not _SHA256.fullmatch(self.source_sha256):
            raise ValueError("source_sha256 must be lowercase SHA-256")
        _require_text(self.change_summary, "change_summary", 1, 1000)


@dataclass(frozen=True, slots=True)
class CounterexampleSnapshot:
    case_id: str
    task_id: str
    failure_key: str
    title: str
    input: FrozenObject
    observed: FrozenObject
    evidence_refs: tuple[EvidenceRef, ...]
    request_context: RequestContext

    def __post_init__(self) -> None:
        _require_identifier(self.case_id, "case_id")
        _require_identifier(self.task_id, "task_id")
        _require_text(self.failure_key, "failure_key", 1, 128)
        _require_text(self.title, "title", 1, 300)
        if not isinstance(self.request_context, RequestContext):
            raise TypeError("request_context must be a RequestContext")
        object.__setattr__(self, "input", freeze_object(self.input, "input"))
        object.__setattr__(self, "observed", freeze_object(self.observed, "observed"))
        evidence = _freeze_evidence(self.evidence_refs, "evidence_refs")
        if not evidence:
            raise ValueError("counterexamples require immutable evidence_refs")
        object.__setattr__(self, "evidence_refs", evidence)


@dataclass(frozen=True, slots=True)
class FailedInteractionSnapshot:
    """Current contract-validated failed interaction selected by the student.

    The read adapter must derive this value from the canonical Product
    Interaction projection and its retained feedback event/receipt authority.
    It must return no value outside the latest same-failure suffix.
    """

    interaction_id: str
    interaction_revision: int
    interaction_sequence: int
    same_failure_suffix_end_sequence: int
    session_id: str
    turn_id: str
    command_id: str
    run_id: str
    build_id: str
    task_id: str
    world_id: str
    skill_ref: SkillRef
    failure_count: int
    failure_key: str
    evidence_refs: tuple[EvidenceRef, ...]
    feedback_event_id: str
    projection_receipt_id: str
    request_context: RequestContext

    def __post_init__(self) -> None:
        for name in (
            "interaction_id",
            "session_id",
            "turn_id",
            "run_id",
            "build_id",
            "task_id",
            "world_id",
            "feedback_event_id",
            "projection_receipt_id",
        ):
            _require_identifier(getattr(self, name), name)
        if not isinstance(self.command_id, str) or not _COMMAND_ID.fullmatch(self.command_id):
            raise ValueError("command_id does not match the canonical command format")
        _require_integer(self.interaction_revision, "interaction_revision", minimum=1)
        _require_integer(self.interaction_sequence, "interaction_sequence", minimum=1)
        _require_integer(
            self.same_failure_suffix_end_sequence,
            "same_failure_suffix_end_sequence",
            minimum=1,
        )
        if self.interaction_sequence != self.same_failure_suffix_end_sequence:
            raise ValueError(
                "selected interaction must be the latest current same-failure suffix item"
            )
        _require_integer(self.failure_count, "failure_count", minimum=1)
        _require_text(self.failure_key, "failure_key", 1, 128)
        if not isinstance(self.skill_ref, SkillRef):
            raise TypeError("skill_ref must be a SkillRef")
        if not isinstance(self.request_context, RequestContext):
            raise TypeError("request_context must be a RequestContext")
        evidence = _freeze_evidence(self.evidence_refs, "evidence_refs")
        if not evidence or any(item.sha256 is None for item in evidence):
            raise ValueError("failed interaction Evidence requires immutable SHA-256 identity")
        object.__setattr__(self, "evidence_refs", evidence)


@dataclass(frozen=True, slots=True)
class SkillPatchRequestAuthority:
    """New explicit UI-action request scope, distinct from the failed Run."""

    tenant_id: str
    actor_id: str
    actor_type: ActorType
    session_id: str
    task_id: str
    turn_id: str
    command_id: str
    requested_interaction_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.tenant_id, str) or not _TENANT_ID.fullmatch(self.tenant_id):
            raise ValueError("tenant_id must be a canonical tenant identifier")
        for name in (
            "actor_id",
            "session_id",
            "task_id",
            "turn_id",
            "requested_interaction_id",
        ):
            _require_identifier(getattr(self, name), name)
        if self.actor_type is not ActorType.STUDENT:
            raise ValueError("Skill Patch request authority requires a student actor")
        if not isinstance(self.command_id, str) or not _COMMAND_ID.fullmatch(self.command_id):
            raise ValueError("command_id does not match the canonical command format")


@dataclass(frozen=True, slots=True)
class SkillPatchFailureAuthority:
    """Exact failed Build/Run/Evidence authority referenced by a proposal."""

    tenant_id: str
    actor_id: str
    session_id: str
    interaction_id: str
    interaction_revision: int
    interaction_sequence: int
    same_failure_suffix_end_sequence: int
    turn_id: str
    command_id: str
    task_id: str
    world_id: str
    skill_ref: SkillRef
    failure_count: int
    failure_key: str
    build_id: str
    run_id: str
    evidence_refs: tuple[EvidenceRef, ...]
    feedback_event_id: str
    projection_receipt_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.tenant_id, str) or not _TENANT_ID.fullmatch(self.tenant_id):
            raise ValueError("tenant_id must be a canonical tenant identifier")
        for name in (
            "actor_id",
            "session_id",
            "interaction_id",
            "turn_id",
            "task_id",
            "world_id",
            "build_id",
            "run_id",
            "feedback_event_id",
            "projection_receipt_id",
        ):
            _require_identifier(getattr(self, name), name)
        if not isinstance(self.command_id, str) or not _COMMAND_ID.fullmatch(self.command_id):
            raise ValueError("command_id does not match the canonical command format")
        _require_integer(self.interaction_revision, "interaction_revision", minimum=1)
        _require_integer(self.interaction_sequence, "interaction_sequence", minimum=1)
        _require_integer(
            self.same_failure_suffix_end_sequence,
            "same_failure_suffix_end_sequence",
            minimum=1,
        )
        if self.interaction_sequence != self.same_failure_suffix_end_sequence:
            raise ValueError("Skill Patch failure must be the latest same-failure interaction")
        if not isinstance(self.skill_ref, SkillRef):
            raise TypeError("skill_ref must be a SkillRef")
        _require_integer(self.failure_count, "failure_count", minimum=1)
        _require_text(self.failure_key, "failure_key", 1, 128)
        evidence = _freeze_evidence(self.evidence_refs, "evidence_refs")
        if not evidence or any(item.sha256 is None for item in evidence):
            raise ValueError("Skill Patch Evidence requires immutable SHA-256 identity")
        object.__setattr__(self, "evidence_refs", evidence)


@dataclass(frozen=True, slots=True)
class SkillPatchAuthority:
    """Runtime-owned authority supplied to the closed model-output parser."""

    draft: DraftSnapshot
    request: SkillPatchRequestAuthority
    failed: SkillPatchFailureAuthority

    def __post_init__(self) -> None:
        if not isinstance(self.draft, DraftSnapshot):
            raise TypeError("draft must be a DraftSnapshot")
        if not isinstance(self.request, SkillPatchRequestAuthority):
            raise TypeError("request must be a SkillPatchRequestAuthority")
        if not isinstance(self.failed, SkillPatchFailureAuthority):
            raise TypeError("failed must be a SkillPatchFailureAuthority")
        if self.request.requested_interaction_id != self.failed.interaction_id:
            raise ValueError("Patch request does not select the failed interaction authority")

    @property
    def target(self) -> DraftAuthority:
        return self.draft.authority


@dataclass(frozen=True, slots=True)
class TurnContext:
    role: RoleId
    event: GameEvent
    task: TaskSnapshot
    session: SessionSnapshot
    hint_level: int
    world: WorldSummary | None = None
    skill: SkillSnapshot | None = None
    available_skills: tuple[SkillSnapshot, ...] = ()
    compile_result: CompileResultSnapshot | None = None
    run_result: RunResultSnapshot | None = None
    failure_history: tuple[RunResultSnapshot, ...] = ()
    counterexamples: tuple[CounterexampleSnapshot, ...] = ()
    learner_profile: LearnerProfileSnapshot | None = None
    recent_messages: tuple[MessageSnapshot, ...] = ()
    session_runs: tuple[RunResultSnapshot, ...] = ()
    skill_history: tuple[SkillVersionSummary, ...] = ()
    teaching_directive: TeachingDirective | None = None
    patch_authority: SkillPatchAuthority | None = None

    def __post_init__(self) -> None:
        if self.role not in _ROLES:
            raise ValueError("role is not supported")
        if not isinstance(self.event, GameEvent):
            raise TypeError("event must be a GameEvent")
        if not isinstance(self.task, TaskSnapshot) or not isinstance(self.session, SessionSnapshot):
            raise TypeError("task and session must be typed snapshots")
        if self.event.task_id != self.task.task_id or self.session.task_id != self.task.task_id:
            raise ValueError("task identity is not closed across event, session and context")
        if self.event.session_id != self.session.session_id:
            raise ValueError("session identity is not closed across event and context")
        if self.event.student_id != self.session.student_id:
            raise ValueError("student identity is not closed across event and session")
        authority = self.task.request_context
        _require_same_authority(self.session.request_context, authority, "session")
        if authority.actor.actor_id != self.event.student_id:
            raise ValueError("task provenance actor does not own the Agent event")
        _require_integer(self.hint_level, "hint_level", minimum=0, maximum=self.task.max_hint_level)
        for name, expected_type in (
            ("world", WorldSummary),
            ("skill", SkillSnapshot),
            ("compile_result", CompileResultSnapshot),
            ("run_result", RunResultSnapshot),
            ("learner_profile", LearnerProfileSnapshot),
            ("patch_authority", SkillPatchAuthority),
        ):
            value = getattr(self, name)
            if value is not None and not isinstance(value, expected_type):
                raise TypeError(f"{name} has an invalid snapshot type")
        for name, expected_type in (
            ("available_skills", SkillSnapshot),
            ("failure_history", RunResultSnapshot),
            ("counterexamples", CounterexampleSnapshot),
            ("recent_messages", MessageSnapshot),
            ("session_runs", RunResultSnapshot),
            ("skill_history", SkillVersionSummary),
        ):
            values = tuple(getattr(self, name))
            if any(not isinstance(item, expected_type) for item in values):
                raise TypeError(f"{name} contains an invalid snapshot type")
            object.__setattr__(self, name, values)
        for name in ("skill", "compile_result", "run_result", "learner_profile"):
            value = getattr(self, name)
            if value is not None:
                _require_same_authority(value.request_context, authority, name)
        if self.patch_authority is not None:
            _require_same_authority(
                self.patch_authority.draft.request_context,
                authority,
                "patch Draft",
            )
        for name in (
            "available_skills",
            "failure_history",
            "counterexamples",
            "recent_messages",
            "session_runs",
            "skill_history",
        ):
            for value in getattr(self, name):
                _require_same_authority(value.request_context, authority, name)

        # Import locally so the pure policy can depend on the immutable domain
        # vocabulary without creating an import cycle during module loading.
        from .pedagogy_policy import TeachingDirective

        directive = self.teaching_directive
        if self.role == "xiaohutao":
            if directive is not None:
                raise ValueError("xiaohutao must not carry a TeachingDirective")
        elif not isinstance(directive, TeachingDirective):
            raise TypeError("directive-bearing roles require one typed TeachingDirective")
        else:
            if directive.hint_level != self.hint_level:
                raise ValueError("TeachingDirective owns the context hint_level")
            if self.learner_profile is None:
                raise ValueError("TeachingDirective requires the latest learner profile")
            if directive.learner_revision != self.learner_profile.revision:
                raise ValueError("TeachingDirective learner revision is not pinned to context")
            if directive.target_concept not in self.task.knowledge_points:
                raise ValueError("TeachingDirective target concept is outside the task")

        patch_authority = self.patch_authority
        if patch_authority is None:
            if self.event.event_type == "skill_patch_requested":
                raise ValueError("skill_patch_requested requires validated Patch authority")
        else:
            if self.role != "teaching_agent" or self.event.event_type != "skill_patch_requested":
                raise ValueError("Patch authority is only valid for teaching skill_patch_requested")
            if self.compile_result is None or self.run_result is None or self.skill is None:
                raise ValueError("Patch authority requires exact Draft, Build, Run and Skill facts")
            target = patch_authority.target
            request = patch_authority.request
            failed = patch_authority.failed
            draft_actor = patch_authority.draft.request_context.actor
            if (
                target != self.event.patch_draft_authority
                or self.compile_result.draft_authority != target
                or self.compile_result.build_id != failed.build_id
                or self.run_result.build_id != failed.build_id
                or self.run_result.run_id != failed.run_id
                or self.run_result.evidence_refs != failed.evidence_refs
                or self.event.evidence_refs != failed.evidence_refs
                or self.event.build_id != failed.build_id
                or self.event.run_id != failed.run_id
                or failed.tenant_id != draft_actor.tenant_id
                or failed.actor_id != draft_actor.actor_id
                or failed.session_id != self.event.session_id
                or failed.interaction_id != request.requested_interaction_id
                or failed.turn_id != self.run_result.turn_id
                or failed.command_id != self.run_result.command_id
                or failed.task_id != self.event.task_id
                or failed.world_id != self.session.world_id
                or failed.skill_ref != self.event.skill_ref
                or failed.failure_count != self.event.failure_count
                or failed.failure_key != self.event.failure_key
                or request.tenant_id != draft_actor.tenant_id
                or request.actor_id != draft_actor.actor_id
                or request.actor_type is not ActorType.STUDENT
                or request.session_id != self.event.session_id
                or request.task_id != self.event.task_id
                or request.turn_id != self.event.turn_id
                or request.command_id != self.event.command_id
                or draft_actor.actor_type is not ActorType.STUDENT
                or self.event.student_id != draft_actor.actor_id
            ):
                raise ValueError(
                    "Patch authority is not closed across Draft, Build, Run and Evidence"
                )
            if (
                self.run_result.task_success
                or self.run_result.failure_key != self.event.failure_key
                or not self.compile_result.succeeded
            ):
                raise ValueError(
                    "Patch authority must reference one successful Build and failed Run"
                )
            if (
                self.skill.ref != self.event.skill_ref
                or self.skill.entrypoint != target.entrypoint
                or self.skill.source_sha256 != target.entrypoint_sha256
                or self.skill.source_code != patch_authority.draft.source_code
            ):
                raise ValueError("Patch Draft source is not the exact failed Build/Run entrypoint")

    @property
    def draft(self) -> DraftSnapshot | None:
        return None if self.patch_authority is None else self.patch_authority.draft


@dataclass(frozen=True, slots=True)
class SkillRecoveryContext:
    """Minimal immutable scope for reconciling an already committed invocation."""

    event: GameEvent
    task: TaskSnapshot
    session: SessionSnapshot
    skill: SkillSnapshot

    def __post_init__(self) -> None:
        if self.event.event_type != "run_skill_requested" or self.event.skill_ref is None:
            raise ValueError("Skill recovery requires one run_skill_requested event binding")
        if self.task.task_id != self.event.task_id:
            raise ValueError("Skill recovery task does not match the event")
        if (
            self.session.session_id,
            self.session.student_id,
            self.session.task_id,
        ) != (
            self.event.session_id,
            self.event.student_id,
            self.event.task_id,
        ):
            raise ValueError("Skill recovery session does not match the event")
        if self.skill.ref != self.event.skill_ref:
            raise ValueError("Skill recovery snapshot does not match the certified binding")
        authority = self.task.request_context
        _require_same_authority(self.session.request_context, authority, "session")
        _require_same_authority(self.skill.request_context, authority, "skill")
        if authority.actor.actor_id != self.event.student_id:
            raise ValueError("Skill recovery actor does not own the event")


@dataclass(frozen=True, slots=True)
class LearnerInference:
    concept: str
    score_delta: float
    confidence: float
    reason: str
    evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.concept, str) or not _LOWER_KEY.fullmatch(self.concept):
            raise ValueError("concept is not a valid concept key")
        for name in ("score_delta", "confidence"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} must be a number")
            try:
                exact = Decimal(str(value))
            except InvalidOperation as error:
                raise ValueError(f"{name} must be a finite number") from error
            scaled = exact * Decimal(1_000_000)
            if not exact.is_finite() or scaled != scaled.to_integral_value():
                raise ValueError(f"{name} must have at most six decimal places")
        if not -0.3 <= self.score_delta <= 0.3:
            raise ValueError("score_delta must be between -0.3 and 0.3")
        if not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")
        _require_text(self.reason, "reason", 1, 1000)
        evidence_ids = tuple(self.evidence_ids)
        if (
            not evidence_ids
            or len(evidence_ids) > 16
            or len(set(evidence_ids)) != len(evidence_ids)
        ):
            raise ValueError("evidence_ids must contain 1..16 unique identifiers")
        for evidence_id in evidence_ids:
            _require_identifier(evidence_id, "evidence_ids item")
        object.__setattr__(self, "evidence_ids", evidence_ids)


@dataclass(frozen=True, slots=True)
class SkillPatchOperation:
    """Exactly one full-entrypoint UPSERT; no delete, rename, or multi-file form."""

    operation_type: Literal["UPSERT_FILE"]
    path: str
    previous_content_sha256: str
    content: str
    content_sha256: str

    def __post_init__(self) -> None:
        if self.operation_type != "UPSERT_FILE":
            raise ValueError("Skill Patch operation must be UPSERT_FILE")
        _require_source_path(self.path, "path")
        for name in ("previous_content_sha256", "content_sha256"):
            value = getattr(self, name)
            if not isinstance(value, str) or not _SHA256.fullmatch(value):
                raise ValueError(f"{name} must be lowercase SHA-256")
        _require_text(self.content, "content", 1, 1_048_576)
        if hashlib.sha256(self.content.encode("utf-8")).hexdigest() != self.content_sha256:
            raise ValueError("content_sha256 does not match replacement UTF-8 bytes")
        if self.previous_content_sha256 == self.content_sha256:
            raise ValueError("Skill Patch replacement must change the entrypoint content")


def _skill_patch_proposal_sha256(
    target: DraftAuthority,
    request: SkillPatchRequestAuthority,
    failed: SkillPatchFailureAuthority,
    operation: SkillPatchOperation,
    rationale: str,
) -> str:
    evidence = [
        [
            item.evidence_id,
            item.evidence_type.value,
            item.created_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            item.sha256,
            item.uri,
        ]
        for item in failed.evidence_refs
    ]
    canonical_array: list[object] = [
        "skill_patch_proposal",
        "1.0.0",
        target.draft_id,
        target.session_id,
        target.skill_id,
        target.draft_revision,
        target.draft_sha256,
        target.source_bundle_sha256,
        target.entrypoint,
        target.entrypoint_sha256,
        request.tenant_id,
        request.actor_id,
        request.actor_type.value,
        request.session_id,
        request.task_id,
        request.turn_id,
        request.command_id,
        request.requested_interaction_id,
        failed.tenant_id,
        failed.actor_id,
        failed.session_id,
        failed.interaction_id,
        failed.interaction_revision,
        failed.interaction_sequence,
        failed.same_failure_suffix_end_sequence,
        failed.turn_id,
        failed.command_id,
        failed.task_id,
        failed.world_id,
        failed.skill_ref.skill_id,
        failed.skill_ref.skill_version_id,
        failed.skill_ref.artifact_sha256,
        failed.skill_ref.certification_id,
        failed.failure_count,
        failed.failure_key,
        failed.build_id,
        failed.run_id,
        failed.feedback_event_id,
        failed.projection_receipt_id,
        evidence,
        operation.operation_type,
        operation.path,
        operation.previous_content_sha256,
        operation.content_sha256,
        rationale,
    ]
    encoded = json.dumps(
        canonical_array,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class SkillPatchProposal:
    """Trusted internal proposal; Backend owns decision/CAS/Draft persistence."""

    proposal_id: str
    proposal_sha256: str
    target: DraftAuthority
    request: SkillPatchRequestAuthority
    failed: SkillPatchFailureAuthority
    operation: SkillPatchOperation
    rationale: str

    @classmethod
    def create(
        cls,
        authority: SkillPatchAuthority,
        *,
        replacement_content: str,
        rationale: str,
    ) -> SkillPatchProposal:
        if not isinstance(authority, SkillPatchAuthority):
            raise TypeError("authority must be a SkillPatchAuthority")
        _require_text(replacement_content, "replacement_content", 1, 1_048_576)
        _require_text(rationale, "rationale", 1, 2000)
        target = authority.target
        operation = SkillPatchOperation(
            operation_type="UPSERT_FILE",
            path=target.entrypoint,
            previous_content_sha256=target.entrypoint_sha256,
            content=replacement_content,
            content_sha256=hashlib.sha256(replacement_content.encode("utf-8")).hexdigest(),
        )
        digest = _skill_patch_proposal_sha256(
            target,
            authority.request,
            authority.failed,
            operation,
            rationale,
        )
        return cls(
            proposal_id=f"patch_{digest[:32]}",
            proposal_sha256=digest,
            target=target,
            request=authority.request,
            failed=authority.failed,
            operation=operation,
            rationale=rationale,
        )

    def __post_init__(self) -> None:
        _require_identifier(self.proposal_id, "proposal_id")
        if not isinstance(self.proposal_sha256, str) or not _SHA256.fullmatch(self.proposal_sha256):
            raise ValueError("proposal_sha256 must be lowercase SHA-256")
        if not isinstance(self.target, DraftAuthority):
            raise TypeError("target must be a DraftAuthority")
        if not isinstance(self.request, SkillPatchRequestAuthority):
            raise TypeError("request must be a SkillPatchRequestAuthority")
        if not isinstance(self.failed, SkillPatchFailureAuthority):
            raise TypeError("failed must be a SkillPatchFailureAuthority")
        if not isinstance(self.operation, SkillPatchOperation):
            raise TypeError("operation must be a SkillPatchOperation")
        _require_text(self.rationale, "rationale", 1, 2000)
        if (
            self.request.requested_interaction_id != self.failed.interaction_id
            or self.request.tenant_id != self.failed.tenant_id
            or self.request.actor_id != self.failed.actor_id
            or self.request.session_id != self.failed.session_id
            or self.request.task_id != self.failed.task_id
            or self.failed.session_id != self.target.session_id
            or self.failed.skill_ref.skill_id != self.target.skill_id
        ):
            raise ValueError("Skill Patch request, failure and Draft scopes are not closed")
        if (
            self.operation.path != self.target.entrypoint
            or self.operation.previous_content_sha256 != self.target.entrypoint_sha256
        ):
            raise ValueError("Skill Patch operation is not closed to the target entrypoint")
        expected = _skill_patch_proposal_sha256(
            self.target,
            self.request,
            self.failed,
            self.operation,
            self.rationale,
        )
        if self.proposal_sha256 != expected or self.proposal_id != f"patch_{expected[:32]}":
            raise ValueError("Skill Patch stable proposal identity does not match its content")


@dataclass(frozen=True, slots=True)
class DecisionDraft:
    role: RoleId
    response_type: ResponseType
    message: str
    question: str | None
    hint_level: int | None
    learner_inference: LearnerInference | None
    skill_patch: SkillPatchProposal | None
    requires_student_confirmation: bool

    def __post_init__(self) -> None:
        if self.role not in _ROLES:
            raise ValueError("role is not supported")
        if self.response_type not in _RESPONSE_TYPES:
            raise ValueError("response_type is not supported")
        _require_text(self.message, "message", 1, 4000)
        if self.question is not None:
            _require_text(self.question, "question", 1, 1000)
        if self.hint_level is not None:
            _require_integer(self.hint_level, "hint_level", minimum=0, maximum=4)
        if self.learner_inference is not None and not isinstance(
            self.learner_inference, LearnerInference
        ):
            raise TypeError("learner_inference must be a LearnerInference or None")
        if self.skill_patch is not None and not isinstance(self.skill_patch, SkillPatchProposal):
            raise TypeError("skill_patch must be a SkillPatchProposal or None")
        if not isinstance(self.requires_student_confirmation, bool):
            raise ValueError("requires_student_confirmation must be a boolean")

        if self.response_type == "question":
            if self.question is None or self.hint_level is not None or self.skill_patch is not None:
                raise ValueError("question responses require only the question field")
        elif self.response_type == "hint":
            if self.question is not None or self.hint_level is None or self.hint_level > 3:
                raise ValueError("hint responses require hint_level 0..3 and no question")
            if self.skill_patch is not None:
                raise ValueError("hint responses cannot contain a skill patch")
        elif self.response_type == "skill_patch":
            if self.role != "teaching_agent":
                raise ValueError("only teaching_agent can propose a skill patch")
            if self.question is not None or self.hint_level != 4 or self.skill_patch is None:
                raise ValueError("skill_patch responses require level 4 and one patch")
            if not self.requires_student_confirmation:
                raise ValueError("skill patches require explicit student confirmation")
        else:
            if (
                self.question is not None
                or self.hint_level is not None
                or self.skill_patch is not None
            ):
                raise ValueError(
                    "message and growth_summary responses cannot carry structured hints"
                )
        if self.response_type != "skill_patch" and self.requires_student_confirmation:
            raise ValueError("only a skill patch can require student confirmation")
        if self.response_type == "growth_summary" and self.role != "book_agent":
            raise ValueError("only book_agent can produce a growth summary")


@dataclass(frozen=True, slots=True)
class ToolCallRecord:
    execution_id: str
    model_call_id: str
    name: str
    arguments: FrozenObject
    result_summary: FrozenObject

    def __post_init__(self) -> None:
        _require_identifier(self.execution_id, "execution_id")
        _require_identifier(self.model_call_id, "model_call_id")
        if not isinstance(self.name, str) or not _LOWER_KEY.fullmatch(self.name):
            raise ValueError("name is not a valid tool name")
        object.__setattr__(self, "arguments", freeze_object(self.arguments, "arguments"))
        object.__setattr__(
            self,
            "result_summary",
            freeze_object(self.result_summary, "result_summary"),
        )


@dataclass(frozen=True, slots=True)
class AgentDecision:
    draft: DecisionDraft
    message_key: str
    source: Literal["provider", "provider_fallback"]
    degraded: bool
    fallback_reason: str | None
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    tool_calls: tuple[ToolCallRecord, ...]
    evidence_refs: tuple[EvidenceRef, ...]
    completed_at: datetime
    runtime_warnings: tuple[str, ...] = ()
    teaching_directive: TeachingDirective | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.draft, DecisionDraft):
            raise TypeError("draft must be a DecisionDraft")
        if not isinstance(self.message_key, str) or not _LOWER_KEY.fullmatch(self.message_key):
            raise ValueError("message_key is not a valid telemetry key")
        if not isinstance(self.degraded, bool):
            raise ValueError("degraded must be a boolean")
        if self.degraded:
            if self.source != "provider_fallback":
                raise ValueError("degraded decisions require provider_fallback source")
            if not isinstance(self.fallback_reason, str) or not _UPPER_CODE.fullmatch(
                self.fallback_reason
            ):
                raise ValueError("degraded decisions require a machine-readable fallback reason")
        elif self.source != "provider" or self.fallback_reason is not None:
            raise ValueError(
                "non-degraded decisions require provider source and no fallback reason"
            )
        _require_text(self.provider, "provider", 1, 128)
        _require_text(self.model, "model", 1, 128)
        _require_integer(self.input_tokens, "input_tokens", minimum=0)
        _require_integer(self.output_tokens, "output_tokens", minimum=0)
        tool_calls = tuple(self.tool_calls)
        if any(not isinstance(item, ToolCallRecord) for item in tool_calls):
            raise TypeError("tool_calls must contain ToolCallRecord values")
        execution_ids = {item.execution_id for item in tool_calls}
        if len(execution_ids) != len(tool_calls):
            raise ValueError("tool_calls must contain unique execution_id values")
        object.__setattr__(self, "tool_calls", tool_calls)
        object.__setattr__(
            self, "evidence_refs", _freeze_evidence(self.evidence_refs, "evidence_refs")
        )
        _require_datetime(self.completed_at, "completed_at")
        warnings = tuple(self.runtime_warnings)
        if len(warnings) > 16 or len(warnings) != len(set(warnings)):
            raise ValueError("runtime_warnings must contain at most 16 unique codes")
        for warning in warnings:
            if not isinstance(warning, str) or not _UPPER_CODE.fullmatch(warning):
                raise ValueError("runtime_warnings contains an invalid machine code")
        object.__setattr__(self, "runtime_warnings", warnings)

        from .pedagogy_policy import TeachingDirective

        directive = self.teaching_directive
        if self.role == "xiaohutao":
            if directive is not None:
                raise ValueError("xiaohutao decisions must not carry a TeachingDirective")
        elif not isinstance(directive, TeachingDirective):
            raise TypeError("directive-bearing decisions require one TeachingDirective")
        else:
            if self.response_type not in directive.allowed_response_types:
                raise ValueError("decision response_type exceeds the TeachingDirective")
            patch = self.draft.skill_patch
            if directive.patch_eligible:
                if (
                    self.response_type != "skill_patch"
                    or patch is None
                    or self.source != "provider"
                    or self.degraded
                ):
                    raise ValueError(
                        "eligible Skill Patch requires one non-degraded provider proposal"
                    )
            elif patch is not None or self.response_type == "skill_patch":
                raise ValueError("Skill Patch is not eligible for this TeachingDirective")
            if self.response_type == "hint":
                if self.draft.hint_level != directive.hint_level:
                    raise ValueError("decision hint_level exceeds the TeachingDirective")
            elif self.response_type == "skill_patch":
                if self.draft.hint_level != 4:
                    raise ValueError("Skill Patch decision requires hint level 4")
            elif self.draft.hint_level is not None:
                raise ValueError("non-hint decisions cannot carry a hint_level")

    @property
    def role(self) -> RoleId:
        return self.draft.role

    @property
    def response_type(self) -> ResponseType:
        return self.draft.response_type

    @property
    def message(self) -> str:
        return self.draft.message


@dataclass(frozen=True, slots=True)
class CommittedAgentTurn:
    """Canonical durable record used to reconcile worker replays and races."""

    event: GameEvent
    actor: ActorRef
    content_ref: ContentRef
    route: RoleRoute
    decision: AgentDecision

    def __post_init__(self) -> None:
        if not isinstance(self.event, GameEvent):
            raise TypeError("event must be a GameEvent")
        if not isinstance(self.actor, ActorRef):
            raise TypeError("actor must be an ActorRef")
        if not isinstance(self.content_ref, ContentRef):
            raise TypeError("content_ref must be a ContentRef")
        if self.actor.actor_id != self.event.student_id:
            raise ValueError("stored actor must own the immutable Agent event")
        if not isinstance(self.route, RoleRoute) or not self.route.should_run:
            raise TypeError("route must be a handled RoleRoute")
        if not isinstance(self.decision, AgentDecision):
            raise TypeError("decision must be an AgentDecision")
        if self.route.event_type != self.event.event_type or self.route.role != self.decision.role:
            raise ValueError("event, stored route and decision role must be identity-closed")


@dataclass(frozen=True, slots=True)
class AgentTurnCommitReceipt:
    """Atomic create-or-replay outcome from AgentTurnCommitPort."""

    record: CommittedAgentTurn
    created: bool

    def __post_init__(self) -> None:
        if not isinstance(self.record, CommittedAgentTurn):
            raise TypeError("record must be a CommittedAgentTurn")
        if not isinstance(self.created, bool):
            raise TypeError("created must be a boolean")


@dataclass(frozen=True, slots=True)
class AgentTurnClaimReceipt:
    """Atomic claim-or-replay result acquired before any model or tool call."""

    claim_id: str | None
    claim_expires_at: datetime | None
    record: CommittedAgentTurn | None

    def __post_init__(self) -> None:
        if (self.claim_id is None) == (self.record is None):
            raise ValueError("claim receipt requires exactly one claim_id or committed record")
        if self.claim_id is not None:
            _require_identifier(self.claim_id, "claim_id")
            if self.claim_expires_at is None:
                raise ValueError("acquired claim receipt requires claim_expires_at")
            _require_datetime(self.claim_expires_at, "claim_expires_at")
        elif self.claim_expires_at is not None:
            raise ValueError("committed replay receipt cannot carry claim_expires_at")
        if self.record is not None and not isinstance(self.record, CommittedAgentTurn):
            raise TypeError("record must be a CommittedAgentTurn or None")


@dataclass(frozen=True, slots=True)
class RoleRoute:
    event_type: GameEventType
    role: RoleId | None
    reason: str

    def __post_init__(self) -> None:
        if self.event_type not in _EVENT_TYPES:
            raise ValueError("event_type is not supported")
        if self.role is not None and self.role not in _ROLES:
            raise ValueError("role is not supported")
        _require_text(self.reason, "reason", 1, 200)

    @property
    def should_run(self) -> bool:
        return self.role is not None


@dataclass(frozen=True, slots=True)
class ToolResult:
    value: FrozenObject
    summary: FrozenObject
    evidence_refs: tuple[EvidenceRef, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", freeze_object(self.value, "value"))
        object.__setattr__(self, "summary", freeze_object(self.summary, "summary"))
        object.__setattr__(
            self, "evidence_refs", _freeze_evidence(self.evidence_refs, "evidence_refs")
        )


@dataclass(frozen=True, slots=True)
class SkillInvocationRequest:
    invocation_id: str
    tenant_id: str
    session_id: str
    turn_id: str
    command_id: str
    world_id: str
    expected_world_revision: int
    skill_ref: SkillRef
    arguments: FrozenObject
    request_sha256: str

    def __post_init__(self) -> None:
        for name in ("invocation_id", "tenant_id", "session_id", "turn_id", "world_id"):
            _require_identifier(getattr(self, name), name)
        if not isinstance(self.command_id, str) or not _COMMAND_ID.fullmatch(self.command_id):
            raise ValueError("command_id does not match the canonical command format")
        _require_integer(self.expected_world_revision, "expected_world_revision", minimum=0)
        if not isinstance(self.skill_ref, SkillRef):
            raise TypeError("skill_ref must be a SkillRef")
        arguments = freeze_object(self.arguments, "arguments")
        object.__setattr__(self, "arguments", arguments)
        if not isinstance(self.request_sha256, str) or not _SHA256.fullmatch(self.request_sha256):
            raise ValueError("request_sha256 must be lowercase SHA-256")
        expected_sha256 = skill_invocation_request_sha256(
            tenant_id=self.tenant_id,
            invocation_id=self.invocation_id,
            session_id=self.session_id,
            turn_id=self.turn_id,
            command_id=self.command_id,
            world_id=self.world_id,
            expected_world_revision=self.expected_world_revision,
            skill_ref=self.skill_ref,
            arguments=arguments,
        )
        if self.request_sha256 != expected_sha256:
            raise ValueError("request_sha256 does not match the canonical invocation request")


@dataclass(frozen=True, slots=True)
class SkillInvocationResult:
    invocation_id: str
    tenant_id: str
    request_sha256: str
    arguments: FrozenObject
    run: RunResultSnapshot

    def __post_init__(self) -> None:
        _require_identifier(self.invocation_id, "invocation_id")
        _require_identifier(self.tenant_id, "tenant_id")
        if not isinstance(self.request_sha256, str) or not _SHA256.fullmatch(self.request_sha256):
            raise ValueError("request_sha256 must be lowercase SHA-256")
        arguments = freeze_object(self.arguments, "arguments")
        object.__setattr__(self, "arguments", arguments)
        if not isinstance(self.run, RunResultSnapshot):
            raise TypeError("run must be a RunResultSnapshot")
        expected_sha256 = skill_invocation_request_sha256(
            tenant_id=self.tenant_id,
            invocation_id=self.invocation_id,
            session_id=self.run.session_id,
            turn_id=self.run.turn_id,
            command_id=self.run.command_id,
            world_id=self.run.world_id,
            expected_world_revision=self.run.world_revision_before,
            skill_ref=self.run.skill_ref,
            arguments=arguments,
        )
        if self.request_sha256 != expected_sha256:
            raise ValueError("request_sha256 does not match the typed invocation receipt")


def world_commit_receipt_sha256(receipt: WorldCommitReceipt) -> str:
    if not isinstance(receipt, WorldCommitReceipt):
        raise TypeError("receipt must be a WorldCommitReceipt")
    value = {
        "evidence_kind": "WORLD_COMMIT",
        "world_id": receipt.world_id,
        "previous_revision": receipt.previous_revision,
        "world_revision": receipt.world_revision,
        "first_event_sequence": receipt.first_event_sequence,
        "last_event_sequence": receipt.last_event_sequence,
        "state_hash": receipt.state_hash,
    }
    return canonical_json_sha256(value)


def skill_invocation_request_sha256(
    *,
    tenant_id: str,
    invocation_id: str,
    session_id: str,
    turn_id: str,
    command_id: str,
    world_id: str,
    expected_world_revision: int,
    skill_ref: SkillRef,
    arguments: Mapping[str, object],
) -> str:
    value = {
        "tenant_id": tenant_id,
        "invocation_id": invocation_id,
        "session_id": session_id,
        "turn_id": turn_id,
        "command_id": command_id,
        "world_id": world_id,
        "expected_world_revision": expected_world_revision,
        "skill_ref": {
            "skill_id": skill_ref.skill_id,
            "skill_version_id": skill_ref.skill_version_id,
            "artifact_sha256": skill_ref.artifact_sha256,
            "certification_id": skill_ref.certification_id,
        },
        "arguments": thaw_value(arguments),
    }
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class AgentTraceEvent:
    name: str
    turn_id: str
    role: RoleId
    fields: FrozenObject

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _LOWER_KEY.fullmatch(self.name):
            raise ValueError("trace event name is invalid")
        _require_identifier(self.turn_id, "turn_id")
        if self.role not in _ROLES:
            raise ValueError("role is not supported")
        object.__setattr__(self, "fields", freeze_object(self.fields, "fields"))


__all__ = [
    "AgentDecision",
    "AgentTraceEvent",
    "AgentTurnClaimReceipt",
    "AgentTurnCommitReceipt",
    "BUG_FAILURE_THRESHOLD",
    "CompileResultSnapshot",
    "CommittedAgentTurn",
    "CounterexampleSnapshot",
    "DecisionDraft",
    "FrozenObject",
    "FrozenValue",
    "GameEvent",
    "GameEventType",
    "LearnerInference",
    "LearnerProfileSnapshot",
    "MessageSnapshot",
    "ResponseType",
    "RoleId",
    "RoleRoute",
    "RunResultSnapshot",
    "SessionSnapshot",
    "SkillInvocationRequest",
    "SkillInvocationResult",
    "SkillRecoveryContext",
    "SkillPatchProposal",
    "SkillSnapshot",
    "SkillVersionSummary",
    "TaskSnapshot",
    "ToolCallRecord",
    "ToolResult",
    "TurnContext",
    "WorldSummary",
    "freeze_object",
    "freeze_value",
    "thaw_value",
    "skill_invocation_request_sha256",
    "world_commit_receipt_sha256",
]
