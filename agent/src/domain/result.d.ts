import type { EvidenceId, JsonObject } from "./primitives.js";

export type ErrorCategory =
  | "VALIDATION"
  | "AUTHENTICATION"
  | "AUTHORIZATION"
  | "POLICY"
  | "CONCURRENCY"
  | "SKILL"
  | "SANDBOX"
  | "WORLD_RULE"
  | "DEPENDENCY"
  | "INVARIANT"
  | "RATE_LIMIT"
  | "INTERNAL";

export type ContractErrorCode =
  | "INVALID_REQUEST"
  | "SCHEMA_VERSION_UNSUPPORTED"
  | "CONTENT_VERSION_MISMATCH"
  | "AUTHENTICATION_REQUIRED"
  | "AUTHORIZATION_DENIED"
  | "POLICY_DENIED"
  | "NOT_FOUND"
  | "PAYLOAD_TOO_LARGE"
  | "IDEMPOTENCY_KEY_REUSED"
  | "WORLD_REVISION_CONFLICT"
  | "EVENT_SEQUENCE_GAP"
  | "SKILL_NOT_CERTIFIED"
  | "SKILL_VERSION_MISMATCH"
  | "ACTIVE_SKILL_ARTIFACT_MISMATCH"
  | "SANDBOX_COMPILE_ERROR"
  | "SANDBOX_RUNTIME_ERROR"
  | "SANDBOX_RESOURCE_LIMIT"
  | "WORLD_RULE_REJECTED"
  | "DEPENDENCY_UNAVAILABLE"
  | "FEISHU_SIGNATURE_INVALID"
  | "FEISHU_REPLAY_DETECTED"
  | "FEISHU_SYNC_FAILED"
  | "RATE_LIMITED"
  | "UNKNOWN_COMMIT_STATE"
  | "INVARIANT_VIOLATION"
  | "INTERNAL_ERROR";

export interface ContractError<Code extends ContractErrorCode = ContractErrorCode>
  extends JsonObject {
  readonly code: Code;
  readonly category: ErrorCategory;
  readonly retryable: boolean;
  readonly user_message_key: string;
  readonly stage: string;
  readonly message?: string;
  readonly details?: JsonObject;
  readonly evidence_ids?: readonly EvidenceId[];
}

/** Ports never use undefined or a rejected promise to represent an expected outcome. */
export type Result<Value, Error extends ContractError = ContractError> =
  | { readonly ok: true; readonly value: Value }
  | { readonly ok: false; readonly error: Error };

export type AsyncResult<Value, Error extends ContractError = ContractError> = Promise<
  Result<Value, Error>
>;
