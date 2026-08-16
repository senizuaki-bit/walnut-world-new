import type {
  BuildId,
  EvidenceRef,
  ISODateTime,
  JsonObject,
  RunId,
  WorldId,
} from "./primitives.js";
import type { ContractError } from "./result.js";
import type {
  CertificationEvidence,
  SkillBuildRequest,
  SkillRef,
} from "./skills.js";
import type { ActionIntent, WorldSnapshot } from "./world.js";

export interface SandboxLimits {
  readonly cpu_ms: number;
  readonly wall_ms: number;
  readonly memory_bytes: number;
  readonly max_intents: number;
  readonly max_output_bytes: number;
  readonly max_processes: number;
  readonly network_access: false;
}

export interface SkillSourceFile {
  readonly path: string;
  readonly content: string;
  readonly content_sha256: string;
}

export interface SkillSourceBundle {
  readonly language: "CPP20";
  readonly entrypoint: string;
  readonly files: readonly SkillSourceFile[];
}

export interface CompileAndTestRequest {
  readonly build_id: BuildId;
  readonly skill_id: string;
  readonly source_bundle: SkillSourceBundle;
  readonly compiler_profile: string;
  readonly test_suite_version: string;
  readonly limits: SandboxLimits;
}

export interface SandboxRunRequest {
  readonly run_id: RunId;
  readonly skill_ref: SkillRef;
  readonly world_id: WorldId;
  readonly world_snapshot: WorldSnapshot;
  readonly input: JsonObject;
  readonly deterministic_seed: string;
  readonly limits: SandboxLimits;
}

/** @deprecated Prefer the cross-language SandboxRunRequest name. */
export type SkillRunRequest = SandboxRunRequest;

export interface SandboxRunResult {
  readonly run_id: RunId;
  readonly status: "SUCCEEDED";
  readonly started_at: ISODateTime;
  readonly finished_at: ISODateTime;
  readonly exit_code: 0;
  readonly action_intents: readonly ActionIntent[];
  readonly stdout_ref: EvidenceRef | null;
  readonly stderr_ref: EvidenceRef | null;
  readonly evidence_refs: readonly EvidenceRef[];
  readonly usage: {
    readonly cpu_ms: number;
    readonly wall_ms: number;
    readonly peak_memory_bytes: number;
  };
}

export interface SandboxFailure extends ContractError {
  readonly category: "SANDBOX" | "VALIDATION" | "DEPENDENCY" | "INTERNAL";
}

export type CompileAndTestResult = CertificationEvidence;
