import type {
  BuildId,
  EvidenceRef,
  ISODateTime,
  JsonObject,
  Sha256,
  SkillId,
  SkillVersionId,
} from "./primitives.js";
import type { ContractError } from "./result.js";

export type SkillBuildStatus =
  | "ACCEPTED"
  | "QUEUED"
  | "COMPILING"
  | "TESTING"
  | "CERTIFYING"
  | "CERTIFIED"
  | "REJECTED"
  | "FAILED";

export type SkillBuildPhaseName =
  | "VALIDATE_SOURCE"
  | "COMPILE"
  | "PUBLIC_TEST"
  | "HIDDEN_TEST"
  | "CERTIFY";

export interface SkillSource {
  readonly language: "cpp";
  readonly source_code: string;
  readonly entrypoint: string;
  readonly source_sha256: Sha256;
}

export interface SkillBuildRequest {
  readonly build_id: BuildId;
  readonly skill_id: SkillId;
  readonly source: SkillSource;
  readonly compiler_profile: string;
  readonly test_suite_version: string;
}

export interface BuildArtifact extends JsonObject {
  readonly artifact_sha256: Sha256;
  readonly source_sha256: Sha256;
  readonly compiler_profile: string;
  readonly compiler_version: string;
  readonly sandbox_image_digest: string;
  readonly test_suite_version: string;
  readonly artifact_uri: string;
}

export interface TestCaseResult extends JsonObject {
  readonly test_case_id: string;
  readonly visibility: "PUBLIC" | "HIDDEN";
  readonly status: "PASSED" | "FAILED" | "ERROR" | "TIMEOUT";
  readonly duration_ms: number;
  readonly diagnostic_codes: readonly string[];
  readonly evidence_refs: readonly EvidenceRef[];
}

export interface CertificationEvidence {
  readonly build_id: BuildId;
  readonly artifact: BuildArtifact;
  readonly tests: readonly TestCaseResult[];
  readonly all_required_tests_passed: boolean;
  readonly evidence_refs: readonly EvidenceRef[];
}

export interface CertifiedSkill {
  readonly certification_id: string;
  readonly skill_id: SkillId;
  readonly skill_version_id: SkillVersionId;
  readonly semantic_version: string;
  readonly artifact: BuildArtifact;
  readonly capabilities: readonly string[];
  readonly certified_at: ISODateTime;
  readonly revoked_at: ISODateTime | null;
  readonly metadata: JsonObject;
}

/** Execution must pin this exact version; aliases such as `latest` are forbidden. */
export interface SkillRef {
  readonly skill_id: SkillId;
  readonly skill_version_id: SkillVersionId;
  readonly artifact_sha256: Sha256;
  readonly certification_id: string;
}

export interface ActiveSkill {
  readonly skill: CertifiedSkill;
  readonly registry_revision: number;
  readonly activated_at: ISODateTime;
}

export interface RegistrySnapshot {
  readonly revision: number;
  readonly skills: readonly ActiveSkill[];
}

export interface SkillError extends ContractError {
  readonly category: "SKILL" | "CONCURRENCY" | "DEPENDENCY" | "INVARIANT";
}
