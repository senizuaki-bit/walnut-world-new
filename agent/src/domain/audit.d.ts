import type {
  ActorRef,
  EvidenceId,
  ISODateTime,
  JsonObject,
  OperationContext,
  Sha256,
} from "./primitives.js";
import type { ContractErrorCode } from "./result.js";

export type AuditOutcome = "ALLOWED" | "DENIED" | "FAILED";

export interface AuditRecord {
  readonly schema_version: "1.0.0";
  readonly audit_id: string;
  readonly occurred_at: ISODateTime;
  readonly operation: string;
  readonly outcome: AuditOutcome;
  readonly actor: ActorRef;
  readonly request_id: OperationContext["request_id"];
  readonly correlation_id: OperationContext["correlation_id"];
  readonly trace_id: OperationContext["trace_id"];
  readonly resource_type: string;
  readonly resource_id: string;
  readonly purpose: string | null;
  readonly subject_hash: Sha256 | null;
  readonly redacted: true;
  readonly evidence_ids: readonly EvidenceId[];
  readonly error_code: ContractErrorCode | null;
  readonly details: JsonObject;
}

export interface AuditQuery {
  readonly operations: readonly string[];
  readonly outcomes: readonly AuditOutcome[];
  readonly occurred_after: ISODateTime | null;
  readonly occurred_before: ISODateTime | null;
  readonly cursor: string | null;
  readonly limit: number;
}
