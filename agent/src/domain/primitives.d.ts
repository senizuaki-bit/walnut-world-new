/** Primitive and shared contract types. Runtime validators remain authoritative. */
export type Brand<T, Name extends string> = T & { readonly __brand: Name };

export type ISODateTime = Brand<string, "ISODateTime">;
export type Sha256 = Brand<string, "Sha256">;
export type RequestId = Brand<string, "RequestId">;
export type TraceId = Brand<string, "TraceId">;
export type CorrelationId = Brand<string, "CorrelationId">;
export type CommandId = Brand<string, "CommandId">;
export type EventId = Brand<string, "EventId">;
export type StreamId = Brand<string, "StreamId">;
export type EvidenceId = Brand<string, "EvidenceId">;
export type SkillId = Brand<string, "SkillId">;
export type SkillVersionId = Brand<string, "SkillVersionId">;
export type BuildId = Brand<string, "BuildId">;
export type RunId = Brand<string, "RunId">;
export type WorldId = Brand<string, "WorldId">;
export type LearnerId = Brand<string, "LearnerId">;

export type JsonPrimitive = string | number | boolean | null;
export type JsonValue = JsonPrimitive | JsonObject | readonly JsonValue[];
export interface JsonObject {
  /** Values are constrained by the runtime JSON Schema at each boundary. */
  readonly [key: string]: JsonValue;
}

export interface ContentRef {
  readonly unit_id: string;
  readonly version: string;
  readonly content_hash: Sha256;
}

export type ActorType =
  | "student"
  | "agent"
  | "teacher"
  | "researcher"
  | "operator"
  | "service";

export interface ActorRef {
  readonly tenant_id: string;
  readonly actor_id: string;
  readonly actor_type: ActorType;
  readonly roles: readonly string[];
}

export interface RequestContext {
  readonly schema_version: "1.0.0";
  readonly request_id: RequestId;
  readonly correlation_id: CorrelationId;
  readonly trace_id: TraceId;
  readonly requested_at: ISODateTime;
  readonly actor: ActorRef;
  readonly content_ref: ContentRef;
}

/** Context propagated across every synchronous port call. */
export interface OperationContext extends RequestContext {
  readonly command_id: CommandId;
  readonly causation_id: EventId | CommandId | null;
  readonly deadline_at?: ISODateTime;
}

export interface VersionSet {
  readonly api_version: string;
  readonly event_version: string;
  readonly policy_version: string;
  readonly world_rules_version: string;
  readonly teaching_spec_version: string;
  readonly skill_version?: string;
  readonly artifact_sha256?: Sha256;
  readonly compiler_version?: string;
  readonly sandbox_image_digest?: string;
  readonly test_suite_version?: string;
  readonly prompt_version?: string;
  readonly model_version?: string;
}

export type EvidenceType =
  | "DOMAIN_EVENT"
  | "ACTION_LOG"
  | "SANDBOX_LOG"
  | "TEST_REPORT"
  | "POLICY_DECISION"
  | "WORLD_COMMIT"
  | "LEARNER_UPDATE"
  | "AUDIT_LOG";

export interface EvidenceRef extends JsonObject {
  readonly evidence_id: EvidenceId;
  readonly evidence_type: EvidenceType;
  readonly created_at: ISODateTime;
  readonly sha256?: Sha256;
  readonly uri?: string;
}

export interface CursorPage<T> {
  readonly items: readonly T[];
  readonly next_cursor: string | null;
}
