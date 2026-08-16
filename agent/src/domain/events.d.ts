import type {
  BuildId,
  ActorRef,
  CommandId,
  ContentRef,
  CorrelationId,
  EventId,
  EvidenceRef,
  ISODateTime,
  JsonObject,
  LearnerId,
  RunId,
  Sha256,
  SkillId,
  SkillVersionId,
  StreamId,
  TraceId,
  WorldId,
} from "./primitives.js";
import type {
  CommandStatus,
  CommandStatusSuccessor,
  CommandType,
  MutableCommandStatus,
} from "./commands.js";
import type { ContractError } from "./result.js";
import type { BuildArtifact, TestCaseResult } from "./skills.js";
import type { ActionIntent } from "./world.js";
import type { AgentTurnFeedback } from "./feedback.js";

/**
 * Persisted event shape used by domain streams.
 *
 * Domain event types are intentionally extensible. RuntimeEvent below is the
 * closed, schema-versioned subset that may be published on the integration bus.
 */
export interface DomainEvent<
  Type extends string = string,
  Payload extends JsonObject = JsonObject,
> {
  readonly event_id: EventId;
  readonly event_type: Type;
  readonly event_version: number;
  readonly schema_version: "1.0.0" | "2.0.0";
  readonly stream_id: StreamId;
  /** Monotonically increasing and gap-free within stream_id. */
  readonly sequence: number;
  readonly occurred_at: ISODateTime;
  readonly producer: string;
  readonly trace_id: TraceId;
  readonly command_id: CommandId;
  readonly correlation_id: CorrelationId;
  readonly causation_id: EventId | CommandId | null;
  readonly content_ref: ContentRef;
  readonly payload: Payload;
}

/** Version 2 integration envelope, introduced additively for learner inference facts. */
export interface DomainEventV2<
  Type extends string = string,
  Payload extends JsonObject = JsonObject,
> {
  readonly event_id: EventId;
  readonly event_type: Type;
  readonly event_version: number;
  readonly schema_version: "2.0.0";
  readonly stream_id: StreamId;
  /** Monotonically increasing and gap-free within stream_id. */
  readonly sequence: number;
  readonly occurred_at: ISODateTime;
  readonly producer: string;
  readonly trace_id: TraceId;
  readonly command_id: CommandId;
  readonly correlation_id: CorrelationId;
  readonly causation_id: EventId | CommandId | null;
  readonly content_ref: ContentRef;
  readonly payload: Payload;
}

export type EventEnvelopeV2<
  Type extends "learner.inference.recorded" = "learner.inference.recorded",
  Payload extends JsonObject = JsonObject,
> = DomainEventV2<Type, Payload>;

/** AsyncAPI's closed runtime envelope, built on the generic domain-event shape. */
export type EventEnvelope<
  Type extends Exclude<RuntimeEventType, "learner.inference.recorded"> = Exclude<
    RuntimeEventType,
    "learner.inference.recorded"
  >,
  Payload extends JsonObject = JsonObject,
> = DomainEvent<Type, Payload> & { readonly schema_version: "1.0.0" };

export interface CommandAcceptedPayload extends JsonObject {
  readonly command_type: CommandType;
  readonly status: "ACCEPTED";
  readonly accepted_at: ISODateTime;
}

export type CommandStageChangedPayload = JsonObject & {
  readonly [Status in MutableCommandStatus]: {
    readonly from_status: Status;
    readonly to_status: Exclude<CommandStatusSuccessor<Status>, Status>;
    readonly command_revision: number;
    readonly attempt: number;
  };
}[MutableCommandStatus];

export interface CommandTerminalPayload extends JsonObject {
  readonly status: "APPLIED" | "REJECTED" | "FAILED" | "UNKNOWN" | "CANCELLED";
  readonly terminal_at: ISODateTime;
  readonly result_ref: string | null;
  readonly error: ContractError | null;
}

/** Downlink feedback for an accepted agent turn. */
export type AgentTurnFeedbackReadyPayload = AgentTurnFeedback;

export interface SkillBuildRequestedPayload extends JsonObject {
  readonly build_id: BuildId;
  readonly skill_id: SkillId;
  readonly source_sha256: string;
  readonly compiler_profile: string;
  readonly test_suite_version: string;
}

export interface SkillBuildStartedPayload extends JsonObject {
  readonly build_id: BuildId;
  readonly worker_id: string;
  readonly attempt: number;
  readonly started_at: ISODateTime;
}

export interface SkillBuildCompletedPayload extends JsonObject {
  readonly build_id: BuildId;
  readonly artifact: BuildArtifact;
  readonly tests: readonly TestCaseResult[];
  readonly completed_at: ISODateTime;
}

export interface SkillBuildFailedPayload extends JsonObject {
  readonly build_id: BuildId;
  readonly failed_at: ISODateTime;
  readonly error: ContractError;
}

export interface SkillCertificationGrantedPayload extends JsonObject {
  readonly build_id: BuildId;
  readonly certification_id: string;
  readonly skill_id: SkillId;
  readonly skill_version_id: SkillVersionId;
  readonly artifact_sha256: string;
  readonly capabilities: readonly string[];
  readonly certified_at: ISODateTime;
}

export interface SkillCertificationRejectedPayload extends JsonObject {
  readonly build_id: BuildId;
  readonly skill_id: SkillId;
  readonly rejected_at: ISODateTime;
  readonly error: ContractError;
  readonly evidence_refs: readonly EvidenceRef[];
}

export interface SkillActivationScope extends JsonObject {
  readonly world_id: WorldId;
  readonly agent_profile_id: string;
}

export interface SkillActivationAppliedPayload extends JsonObject {
  readonly skill_id: SkillId;
  readonly skill_version_id: SkillVersionId;
  readonly certification_id: string;
  readonly artifact_sha256: string;
  readonly activation_scope: SkillActivationScope;
  readonly previous_registry_revision: number;
  readonly registry_revision: number;
  readonly activated_at: ISODateTime;
}

export interface SkillActivationRejectedPayload extends JsonObject {
  readonly skill_version_id: SkillVersionId;
  readonly activation_scope: SkillActivationScope;
  readonly expected_registry_revision: number;
  readonly current_registry_revision: number;
  readonly rejected_at: ISODateTime;
  readonly error: ContractError;
}

export interface SandboxRunStartedPayload extends JsonObject {
  readonly run_id: RunId;
  readonly skill_version_id: SkillVersionId;
  readonly world_id: WorldId;
  readonly expected_world_revision: number;
  readonly worker_id: string;
  readonly started_at: ISODateTime;
}

export interface SandboxRunCompletedPayload extends JsonObject {
  readonly run_id: RunId;
  readonly exit_code: 0;
  readonly action_intents: readonly ActionIntent[];
  readonly finished_at: ISODateTime;
  readonly evidence_refs: readonly EvidenceRef[];
}

export interface SandboxRunFailedPayload extends JsonObject {
  readonly run_id: RunId;
  readonly failed_at: ISODateTime;
  readonly error: ContractError;
  readonly evidence_refs: readonly EvidenceRef[];
}

export interface WorldCommittedPayload extends JsonObject {
  readonly commit_id: string;
  readonly run_id: RunId;
  readonly world_id: WorldId;
  readonly previous_world_revision: number;
  readonly world_revision: number;
  readonly state_hash: string;
  readonly applied_intent_ids: readonly string[];
  readonly committed_at: ISODateTime;
  readonly evidence_refs: readonly EvidenceRef[];
}

export interface WorldRejectedPayload extends JsonObject {
  readonly run_id: RunId;
  readonly world_id: WorldId;
  readonly expected_world_revision: number;
  readonly current_world_revision: number;
  readonly rejected_intent_ids: readonly string[];
  readonly rejected_at: ISODateTime;
  readonly error: ContractError;
}

export interface LearnerEvidenceRecordedPayload extends JsonObject {
  readonly learner_id: LearnerId;
  readonly evidence_refs: readonly EvidenceRef[];
  readonly competency_ids: readonly string[];
  readonly recorded_at: ISODateTime;
}

export type LearnerInferenceRole = "teaching_agent" | "bug_agent" | "book_agent";

export interface LearnerInferenceEvidenceRef extends EvidenceRef {
  readonly sha256: Sha256;
}

export interface LearnerInferenceActorRef extends ActorRef, JsonObject {}

export interface LearnerInferenceRecordedPayload extends JsonObject {
  readonly actor: LearnerInferenceActorRef;
  readonly learner_id: LearnerId;
  readonly session_id: string;
  readonly turn_id: string;
  readonly command_id: CommandId;
  readonly run_id: RunId | null;
  readonly source_event_id: EventId;
  readonly source_event_sha256: Sha256;
  readonly turn_commit_sha256: Sha256;
  readonly task_id: string;
  readonly teaching_spec_version: string;
  readonly role: LearnerInferenceRole;
  readonly concept: string;
  readonly score_delta: number;
  readonly confidence: number;
  readonly reason: string;
  readonly evidence_refs: readonly LearnerInferenceEvidenceRef[];
  readonly inferred_at: ISODateTime;
  readonly inference_sha256: Sha256;
}

export interface LearnerModelUpdatedPayload extends JsonObject {
  readonly learner_id: LearnerId;
  readonly previous_revision: number;
  readonly learner_revision: number;
  readonly projected_through_sequence: number;
  readonly changed_competency_ids: readonly string[];
  readonly updated_at: ISODateTime;
  readonly evidence_refs: readonly EvidenceRef[];
}

export interface LearnerProjectionFailedPayload extends JsonObject {
  readonly learner_id: LearnerId;
  readonly source_event_id: EventId;
  readonly failed_at: ISODateTime;
  readonly error: ContractError;
}

export interface FeishuSyncRequestedPayload extends JsonObject {
  readonly sync_id: string;
  readonly sync_kind: string;
  readonly target_ref: string;
  readonly attempt: number;
  readonly requested_at: ISODateTime;
}

export interface FeishuSyncSucceededPayload extends JsonObject {
  readonly sync_id: string;
  readonly remote_object_id: string;
  readonly attempt: number;
  readonly succeeded_at: ISODateTime;
}

export interface FeishuSyncFailedPayload extends JsonObject {
  readonly sync_id: string;
  readonly attempt: number;
  readonly next_attempt_at: ISODateTime | null;
  readonly failed_at: ISODateTime;
  readonly error: ContractError;
}

export interface FeishuSyncDeadLetteredPayload extends JsonObject {
  readonly sync_id: string;
  readonly attempts: number;
  readonly dead_lettered_at: ISODateTime;
  readonly error: ContractError;
}

export interface RuntimeEventPayloadMap {
  readonly "command.accepted": CommandAcceptedPayload;
  readonly "command.stage_changed": CommandStageChangedPayload;
  readonly "command.terminal": CommandTerminalPayload;
  readonly "agent.turn.feedback_ready": AgentTurnFeedbackReadyPayload;
  readonly "skill.build.requested": SkillBuildRequestedPayload;
  readonly "skill.build.started": SkillBuildStartedPayload;
  readonly "skill.build.completed": SkillBuildCompletedPayload;
  readonly "skill.build.failed": SkillBuildFailedPayload;
  readonly "skill.certification.granted": SkillCertificationGrantedPayload;
  readonly "skill.certification.rejected": SkillCertificationRejectedPayload;
  readonly "skill.activation.applied": SkillActivationAppliedPayload;
  readonly "skill.activation.rejected": SkillActivationRejectedPayload;
  readonly "sandbox.run.started": SandboxRunStartedPayload;
  readonly "sandbox.run.completed": SandboxRunCompletedPayload;
  readonly "sandbox.run.failed": SandboxRunFailedPayload;
  readonly "world.committed": WorldCommittedPayload;
  readonly "world.rejected": WorldRejectedPayload;
  readonly "learner.evidence.recorded": LearnerEvidenceRecordedPayload;
  readonly "learner.inference.recorded": LearnerInferenceRecordedPayload;
  readonly "learner.model.updated": LearnerModelUpdatedPayload;
  readonly "learner.projection.failed": LearnerProjectionFailedPayload;
  readonly "feishu.sync.requested": FeishuSyncRequestedPayload;
  readonly "feishu.sync.succeeded": FeishuSyncSucceededPayload;
  readonly "feishu.sync.failed": FeishuSyncFailedPayload;
  readonly "feishu.sync.dead_lettered": FeishuSyncDeadLetteredPayload;
}

export type RuntimeEventType = keyof RuntimeEventPayloadMap;

export type RuntimeEventEnvelope<Type extends RuntimeEventType> =
  Type extends "learner.inference.recorded"
    ? EventEnvelopeV2<Type, RuntimeEventPayloadMap[Type]>
    : EventEnvelope<
        Exclude<Type, "learner.inference.recorded">,
        RuntimeEventPayloadMap[Type]
      >;

export type RuntimeEvent = {
  readonly [Type in RuntimeEventType]: RuntimeEventEnvelope<Type>;
}[RuntimeEventType];

export type RuntimeEventOf<Type extends RuntimeEventType> = RuntimeEventEnvelope<Type>;

/**
 * Open domain event before persistence. The append call supplies stream_id;
 * the store assigns event_id, sequence and occurred_at atomically.
 */
export type UncommittedEvent<
  Type extends string = string,
  Payload extends JsonObject = JsonObject,
> = Omit<
  DomainEvent<Type, Payload>,
  "event_id" | "stream_id" | "sequence" | "occurred_at"
>;

export interface EventAppendReceipt {
  readonly stream_id: StreamId;
  readonly previous_sequence: number;
  readonly next_sequence: number;
  readonly events: readonly DomainEvent[];
}
