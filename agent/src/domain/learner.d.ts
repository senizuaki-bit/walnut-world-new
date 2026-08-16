import type {
  CommandId,
  EvidenceRef,
  ISODateTime,
  JsonObject,
  LearnerId,
} from "./primitives.js";
import type { ContractError } from "./result.js";

export interface LearnerEvidence {
  readonly learner_id: LearnerId;
  readonly command_id: CommandId;
  readonly competency_id: string;
  readonly observation_type: string;
  readonly observed_at: ISODateTime;
  readonly value: JsonObject;
  readonly source_refs: readonly EvidenceRef[];
}

export interface LearnerModelSnapshot {
  readonly learner_id: LearnerId;
  readonly revision: number;
  readonly model_version: string;
  readonly projected_through_sequence: number;
  readonly competencies: JsonObject;
  readonly updated_at: ISODateTime;
  readonly evidence_refs: readonly EvidenceRef[];
}

export interface LearnerUpdate {
  readonly learner_id: LearnerId;
  readonly previous_revision: number;
  readonly revision: number;
  readonly model_version: string;
  readonly changed_competency_ids: readonly string[];
  readonly evidence_refs: readonly EvidenceRef[];
  readonly updated_at: ISODateTime;
}

export interface LearnerError extends ContractError {
  readonly category: "VALIDATION" | "CONCURRENCY" | "DEPENDENCY" | "INVARIANT";
}
