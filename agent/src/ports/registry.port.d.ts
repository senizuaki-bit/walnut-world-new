import type { OperationContext, SkillId, SkillVersionId } from "../domain/primitives.js";
import type { AsyncResult, ContractError } from "../domain/result.js";
import type {
  ActiveSkill,
  CertifiedSkill,
  CertificationEvidence,
  RegistrySnapshot,
  SkillRef,
} from "../domain/skills.js";

export interface ActivateSkillInput {
  readonly skill_version_id: SkillVersionId;
  readonly artifact_sha256: string;
  readonly certification_id: string;
  readonly expected_registry_revision: number;
}

export interface RegistryPort {
  certify(
    evidence: CertificationEvidence,
    context: OperationContext,
  ): AsyncResult<CertifiedSkill, ContractError>;

  rejectCertification(
    evidence: CertificationEvidence,
    reason: ContractError,
    context: OperationContext,
  ): AsyncResult<void, ContractError>;

  getCertifiedVersion(
    ref: SkillRef,
    context: OperationContext,
  ): AsyncResult<CertifiedSkill, ContractError>;

  getActiveSkill(
    skillId: SkillId,
    context: OperationContext,
  ): AsyncResult<ActiveSkill, ContractError>;

  activate(
    input: ActivateSkillInput,
    context: OperationContext,
  ): AsyncResult<ActiveSkill, ContractError>;

  snapshot(context: OperationContext): AsyncResult<RegistrySnapshot, ContractError>;
}

/** @deprecated Prefer RegistryPort. */
export type SkillRegistryPort = RegistryPort;
